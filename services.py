import asyncio
import hashlib
import re
import secrets
import string
from http import HTTPStatus
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Request
from lnbits.core.crud import (
    create_wallet,
    delete_wallet,
    force_delete_wallet,
    get_payments,
    get_wallet,
)
from lnbits.core.models import PaymentFilters
from lnbits.db import Filter, Filters, Page
from lnbits.decorators import check_user_extension_access
from lnbits.helpers import urlsafe_short_hash
from lnbits.settings import settings
from loguru import logger

from .crud import (
    create_denchi,
    delete_denchi,
    delete_denchi_after_failed_issue,
    get_denchi,
    get_denchi_by_lookup_hash,
    get_owned_cards_with_admin_keys,
)
from .models import (
    BulkDisableResult,
    Card,
    CardEnabled,
    CardListItem,
    NfcWriteData,
    Profile,
    PublicLookupResponse,
    PublicUsageEntry,
)

WITHDRAW_EXTENSION_ID = "withdraw"
WITHDRAW_CREATE_PATH = "/withdraw/api/v1/links"
WALLET_SUFFIX_ALPHABET = string.ascii_uppercase + string.digits
EXPIRATION_SECONDS = {
    "days": 86400,
    "weeks": 604800,
    "months": 2592000,
}
WITHDRAW_ENRICHMENT_CONCURRENCY = 10
WITHDRAW_LNURL_PATH = re.compile(
    r"^/withdraw/api/v1/lnurl/(?P<unique_hash>[A-Za-z0-9_-]{22})$"
)
PUBLIC_HISTORY_LIMIT = 20
PUBLIC_HISTORY_BATCH_SIZE = 100


class IssueError(Exception):
    def __init__(
        self,
        detail: str,
        status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class DeleteCardError(Exception):
    def __init__(
        self,
        detail: str,
        status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class SetCardEnabledError(Exception):
    def __init__(
        self,
        detail: str,
        status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def ensure_withdraw_available(account_id: str) -> None:
    installed = settings.is_installed_extension_id(WITHDRAW_EXTENSION_ID)
    active = WITHDRAW_EXTENSION_ID not in settings.lnbits_deactivated_extensions
    if installed and active:
        access = await check_user_extension_access(account_id, WITHDRAW_EXTENSION_ID)
        active = access.success

    if not installed or not active:
        raise IssueError(
            "Withdraw extension is required.",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )


def wallet_name(profile: Profile) -> str:
    suffix = "".join(secrets.choice(WALLET_SUFFIX_ALPHABET) for _ in range(6))
    return f"{profile.wallet_name_base}-{suffix}"


def expiry_seconds(profile: Profile) -> int:
    if profile.expiration_value is None or profile.expiration_unit is None:
        return 0
    return profile.expiration_value * EXPIRATION_SECONDS[profile.expiration_unit]


def withdraw_request(profile: Profile, generated_wallet_name: str) -> dict:
    return {
        "title": generated_wallet_name,
        "min_withdrawable": 1,
        "max_withdrawable": profile.max_payment_per_use,
        "uses": 250,
        "wait_time": 1,
        "is_unique": False,
        "webhook_url": None,
        "webhook_headers": None,
        "webhook_body": None,
        "custom_url": None,
        "enabled": True,
        "currency": None,
        "expiry_seconds": expiry_seconds(profile),
    }


def response_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])
    except ValueError:
        pass
    return f"HTTP {response.status_code}"


def extract_withdraw_unique_hash(
    value: str,
    allowed_schemes: tuple[str, ...],
    expected_host: str | None = None,
    expected_port: int | None = None,
) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid Withdraw LNURL URL.") from exc

    match = WITHDRAW_LNURL_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not match
    ):
        raise ValueError("Invalid Withdraw LNURL URL.")
    if expected_host and parsed.hostname.lower() != expected_host.lower():
        raise ValueError("Invalid Withdraw LNURL URL.")
    if port is not None and port != expected_port:
        raise ValueError("Invalid Withdraw LNURL URL.")
    return match.group("unique_hash")


def withdraw_lookup_hash(unique_hash: str) -> str:
    return hashlib.sha256(unique_hash.encode()).hexdigest()


def lookup_hash_from_lnurl_url(lnurl_url: object) -> str:
    if not isinstance(lnurl_url, str):
        raise ValueError("Withdraw response did not include an LNURL URL.")
    unique_hash = extract_withdraw_unique_hash(lnurl_url, ("http", "https"))
    return withdraw_lookup_hash(unique_hash)


def to_lnurlw_uri(lnurl_url: str) -> str:
    try:
        parsed = urlsplit(lnurl_url)
        # Accessing port also validates malformed port values.
        _ = parsed.port
    except ValueError as exc:
        raise IssueError("Withdraw returned an invalid LNURL URL.") from exc

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise IssueError("Withdraw returned an invalid LNURL URL.")

    return urlunsplit(
        ("lnurlw", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


async def enrich_denchi_page(
    request: Request,
    page: Page[CardListItem],
    admin_keys: dict[str, str],
) -> Page[CardListItem]:
    semaphore = asyncio.Semaphore(WITHDRAW_ENRICHMENT_CONCURRENCY)
    transport = httpx.ASGITransport(app=request.app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
    ) as client:

        async def enrich(card: CardListItem) -> None:
            admin_key = admin_keys.get(card.id)
            if not admin_key:
                logger.error(f"Missing wallet credentials for Card {card.id}.")
                return
            try:
                async with semaphore:
                    response = await client.get(
                        f"{WITHDRAW_CREATE_PATH}/{card.withdraw_id}",
                        headers={"X-API-Key": admin_key},
                    )
                if response.is_error:
                    raise RuntimeError(response_error(response))
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("invalid Withdraw response")
                if (
                    data.get("id") != card.withdraw_id
                    or data.get("wallet") != card.wallet_id
                ):
                    raise RuntimeError("Withdraw link association mismatch")

                card.withdraw_url = f"/withdraw/{card.withdraw_id}"
                enabled = data.get("enabled")
                card.enabled = enabled if isinstance(enabled, bool) else None
            except Exception as exc:
                logger.warning(f"Could not enrich Card {card.id} from Withdraw: {exc}")

        await asyncio.gather(*(enrich(card) for card in page.data))

    return page


async def get_public_lookup_with_hash(
    request: Request, lnurlw_uri: str
) -> tuple[PublicLookupResponse, str] | None:
    if not lnurlw_uri or len(lnurlw_uri) > 2048:
        return None
    try:
        unique_hash = extract_withdraw_unique_hash(
            lnurlw_uri,
            ("lnurlw",),
            expected_host=request.url.hostname,
            expected_port=request.url.port,
        )
    except ValueError:
        return None

    lookup_hash = withdraw_lookup_hash(unique_hash)
    denchi = await get_denchi_by_lookup_hash(lookup_hash)
    if not denchi:
        return None

    wallet = await get_wallet(denchi.wallet_id)
    if not wallet:
        return None

    transport = httpx.ASGITransport(app=request.app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=str(request.base_url),
        ) as client:
            response = await client.get(
                f"{WITHDRAW_CREATE_PATH}/{denchi.withdraw_id}",
                headers={"X-API-Key": wallet.adminkey},
            )
        if response.is_error:
            return None
        data = response.json()
        if not isinstance(data, dict):
            return None
        if (
            data.get("id") != denchi.withdraw_id
            or data.get("wallet") != denchi.wallet_id
            or lookup_hash_from_lnurl_url(data.get("lnurl_url")) != lookup_hash
        ):
            return None
    except Exception as exc:
        logger.warning(f"Public lookup validation failed for Card {denchi.id}: {exc}")
        return None

    history: list[PublicUsageEntry] = []
    offset = 0
    try:
        while len(history) < PUBLIC_HISTORY_LIMIT:
            filters = Filters(
                filters=[
                    Filter.parse_query("tag", ["withdraw"], PaymentFilters, 0),
                    Filter.parse_query("status", ["success"], PaymentFilters, 1),
                ],
                model=PaymentFilters,
                limit=PUBLIC_HISTORY_BATCH_SIZE,
                offset=offset,
                sortby="time",
                direction="desc",
            )
            payments = await get_payments(
                wallet_id=denchi.wallet_id,
                outgoing=True,
                filters=filters,
            )
            for payment in payments:
                if payment.extra.get("withdrawal_link_id") != denchi.withdraw_id:
                    continue
                history.append(
                    PublicUsageEntry(
                        timestamp=payment.time,
                        amount=abs(payment.amount) // 1000,
                    )
                )
                if len(history) == PUBLIC_HISTORY_LIMIT:
                    break
            if len(payments) < PUBLIC_HISTORY_BATCH_SIZE:
                break
            offset += PUBLIC_HISTORY_BATCH_SIZE
    except Exception as exc:
        logger.warning(f"Public history lookup failed for Card {denchi.id}: {exc}")
        return None

    return PublicLookupResponse(balance=wallet.balance, history=history), lookup_hash


async def get_public_lookup(
    request: Request, lnurlw_uri: str
) -> PublicLookupResponse | None:
    result = await get_public_lookup_with_hash(request, lnurlw_uri)
    return result[0] if result else None


async def get_nfc_write_data(
    request: Request, account_id: str, denchi: Card
) -> NfcWriteData:
    await ensure_withdraw_available(account_id)
    wallet = await get_wallet(denchi.wallet_id)
    if not wallet or wallet.user != account_id:
        raise IssueError("Card not found.", HTTPStatus.NOT_FOUND)

    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
    ) as client:
        response = await client.get(
            f"{WITHDRAW_CREATE_PATH}/{denchi.withdraw_id}",
            headers={"X-API-Key": wallet.adminkey},
        )

    if response.is_error:
        logger.error(
            "Failed to retrieve Card Withdraw link "
            f"{denchi.withdraw_id}: {response_error(response)}"
        )
        raise IssueError(
            "Failed to retrieve Card Withdraw link.",
            HTTPStatus.BAD_GATEWAY,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise IssueError(
            "Withdraw returned an invalid response.",
            HTTPStatus.BAD_GATEWAY,
        ) from exc
    if not isinstance(data, dict):
        raise IssueError(
            "Withdraw returned an invalid response.",
            HTTPStatus.BAD_GATEWAY,
        )
    if data.get("id") != denchi.withdraw_id or data.get("wallet") != denchi.wallet_id:
        logger.error("Withdraw link association mismatch for Card " f"{denchi.id}.")
        raise IssueError(
            "Card Withdraw link association is invalid.",
            HTTPStatus.CONFLICT,
        )

    lnurl_url = data.get("lnurl_url")
    if not isinstance(lnurl_url, str):
        raise IssueError(
            "Withdraw response did not include an LNURL URL.",
            HTTPStatus.BAD_GATEWAY,
        )
    return NfcWriteData(
        denchi_id=denchi.id,
        lnurlw_uri=to_lnurlw_uri(lnurl_url),
    )


def withdraw_update_request(data: dict, enabled: bool) -> dict:
    currency = data.get("currency")
    min_withdrawable = data.get("min_withdrawable")
    max_withdrawable = data.get("max_withdrawable")
    if isinstance(currency, str) and currency.lower() != "sat":
        if not isinstance(min_withdrawable, int | float) or not isinstance(
            max_withdrawable, int | float
        ):
            raise SetCardEnabledError(
                "Withdraw returned invalid amount settings.",
                HTTPStatus.BAD_GATEWAY,
            )
        min_withdrawable = min_withdrawable / 100
        max_withdrawable = max_withdrawable / 100

    return {
        "title": data.get("title"),
        "min_withdrawable": min_withdrawable,
        "max_withdrawable": max_withdrawable,
        "uses": data.get("uses"),
        "wait_time": data.get("wait_time"),
        "is_unique": data.get("is_unique"),
        "webhook_url": data.get("webhook_url"),
        "webhook_headers": data.get("webhook_headers"),
        "webhook_body": data.get("webhook_body"),
        "custom_url": data.get("custom_url"),
        "enabled": enabled,
        "currency": currency,
        "expiry_seconds": data.get("expiry_seconds"),
    }


async def set_denchi_enabled_with_client(
    client: httpx.AsyncClient,
    denchi: Card,
    admin_key: str,
    enabled: bool,
) -> bool:
    response = await client.get(
        f"{WITHDRAW_CREATE_PATH}/{denchi.withdraw_id}",
        headers={"X-API-Key": admin_key},
    )
    if response.is_error:
        logger.error(
            f"Failed to retrieve Withdraw link {denchi.withdraw_id} before "
            f"updating Enabled: {response_error(response)}"
        )
        raise SetCardEnabledError(
            "Failed to retrieve Card Withdraw link.",
            HTTPStatus.BAD_GATEWAY,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SetCardEnabledError(
            "Withdraw returned an invalid response.",
            HTTPStatus.BAD_GATEWAY,
        ) from exc
    if not isinstance(data, dict):
        raise SetCardEnabledError(
            "Withdraw returned an invalid response.",
            HTTPStatus.BAD_GATEWAY,
        )
    if data.get("id") != denchi.withdraw_id or data.get("wallet") != denchi.wallet_id:
        logger.error(f"Withdraw link association mismatch for Card {denchi.id}.")
        raise SetCardEnabledError(
            "Card Withdraw link association is invalid.",
            HTTPStatus.CONFLICT,
        )
    current_enabled = data.get("enabled")
    if not isinstance(current_enabled, bool):
        raise SetCardEnabledError(
            "Withdraw returned an invalid Enabled state.",
            HTTPStatus.BAD_GATEWAY,
        )
    if current_enabled is enabled:
        return False

    response = await client.put(
        f"{WITHDRAW_CREATE_PATH}/{denchi.withdraw_id}",
        headers={"X-API-Key": admin_key},
        json=withdraw_update_request(data, enabled),
    )
    if response.is_error:
        logger.error(
            f"Failed to update Withdraw link {denchi.withdraw_id} Enabled: "
            f"{response_error(response)}"
        )
        raise SetCardEnabledError(
            "Failed to update Card Withdraw state.",
            HTTPStatus.BAD_GATEWAY,
        )
    try:
        updated = response.json()
    except ValueError as exc:
        raise SetCardEnabledError(
            "Withdraw returned an invalid response.",
            HTTPStatus.BAD_GATEWAY,
        ) from exc
    if (
        not isinstance(updated, dict)
        or updated.get("id") != denchi.withdraw_id
        or updated.get("wallet") != denchi.wallet_id
        or updated.get("enabled") is not enabled
    ):
        logger.error(f"Unexpected Withdraw update response for Card {denchi.id}.")
        raise SetCardEnabledError(
            "Withdraw did not confirm the requested Card state.",
            HTTPStatus.BAD_GATEWAY,
        )
    return True


async def set_denchi_enabled(
    request: Request,
    account_id: str,
    denchi: Card,
    enabled: bool,
) -> CardEnabled:
    await ensure_withdraw_available(account_id)
    wallet = await get_wallet(denchi.wallet_id)
    if not wallet or wallet.user != account_id:
        raise SetCardEnabledError("Card not found.", HTTPStatus.NOT_FOUND)

    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
    ) as client:
        await set_denchi_enabled_with_client(client, denchi, wallet.adminkey, enabled)
    return CardEnabled(enabled=enabled)


async def disable_all_cards(request: Request, account_id: str) -> BulkDisableResult:
    await ensure_withdraw_available(account_id)
    owned_cards = await get_owned_cards_with_admin_keys(account_id)
    semaphore = asyncio.Semaphore(WITHDRAW_ENRICHMENT_CONCURRENCY)
    transport = httpx.ASGITransport(app=request.app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
    ) as client:

        async def disable(denchi: Card, admin_key: str) -> str:
            try:
                async with semaphore:
                    changed = await set_denchi_enabled_with_client(
                        client, denchi, admin_key, False
                    )
                return "updated" if changed else "skipped"
            except Exception as exc:
                logger.warning(
                    f"Could not disable Card {denchi.id} during bulk action: " f"{exc}"
                )
                return "failed"

        results = await asyncio.gather(
            *(disable(denchi, admin_key) for denchi, admin_key in owned_cards)
        )

    return BulkDisableResult(
        updated=results.count("updated"),
        skipped=results.count("skipped"),
        failed=results.count("failed"),
    )


async def delete_existing_denchi(
    request: Request,
    account_id: str,
    denchi: Card,
) -> None:
    await ensure_withdraw_available(account_id)
    wallet = await get_wallet(denchi.wallet_id, deleted=None)
    if not wallet or wallet.user != account_id:
        raise DeleteCardError("Card not found.", HTTPStatus.NOT_FOUND)

    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
    ) as client:
        if wallet.deleted:
            response = await client.get(f"/withdraw/{denchi.withdraw_id}")
            if response.status_code != HTTPStatus.NOT_FOUND:
                logger.error(
                    "Cannot retry Card deletion because its wallet is deleted "
                    f"but Withdraw link {denchi.withdraw_id} is not confirmed absent."
                )
                raise DeleteCardError(
                    "Wallet is already deleted, but the Withdraw link is not "
                    "confirmed deleted. Manual recovery is required.",
                    HTTPStatus.CONFLICT,
                )
        else:
            response = await client.delete(
                f"{WITHDRAW_CREATE_PATH}/{denchi.withdraw_id}",
                headers={"X-API-Key": wallet.adminkey},
            )
            withdraw_already_deleted = (
                response.status_code == HTTPStatus.NOT_FOUND
                and response_error(response) == "Withdraw link does not exist."
            )
            if response.is_error and not withdraw_already_deleted:
                logger.error(
                    f"Failed to delete Card Withdraw link {denchi.withdraw_id}: "
                    f"{response_error(response)}"
                )
                raise DeleteCardError(
                    "Failed to delete Card Withdraw link.",
                    HTTPStatus.BAD_GATEWAY,
                )

            try:
                await delete_wallet(user_id=account_id, wallet_id=wallet.id)
            except Exception as exc:
                logger.exception(f"Failed to delete Card wallet {wallet.id}: {exc}")
                raise DeleteCardError(
                    "Failed to delete Card wallet. Retry deletion."
                ) from exc

            deleted_wallet = await get_wallet(wallet.id, deleted=None)
            if not deleted_wallet or not deleted_wallet.deleted:
                logger.error(f"Card wallet {wallet.id} was not marked deleted.")
                raise DeleteCardError(
                    "Failed to confirm Card wallet deletion. Retry deletion."
                )

    try:
        await delete_denchi(denchi.id)
        if await get_denchi(denchi.id):
            raise RuntimeError("Card row still exists after deletion.")
    except Exception as exc:
        logger.exception(f"Failed to delete Card row {denchi.id}: {exc}")
        raise DeleteCardError(
            "Wallet and Withdraw link were deleted, but the Card record could "
            "not be deleted. Retry deletion."
        ) from exc


async def rollback_wallet(wallet_id: str, failures: list[str]) -> None:
    try:
        await force_delete_wallet(wallet_id)
    except Exception as exc:
        logger.exception(f"Failed to roll back Card wallet {wallet_id}: {exc}")
        failures.append("wallet cleanup failed")


async def rollback_withdraw(
    client: httpx.AsyncClient,
    withdraw_id: str,
    admin_key: str,
    failures: list[str],
) -> None:
    try:
        response = await client.delete(
            f"{WITHDRAW_CREATE_PATH}/{withdraw_id}",
            headers={"X-API-Key": admin_key},
        )
        if response.is_error:
            raise RuntimeError(response_error(response))
    except Exception as exc:
        logger.exception(f"Failed to roll back Withdraw link {withdraw_id}: {exc}")
        failures.append("Withdraw cleanup failed")


def failure_detail(message: str, rollback_failures: list[str]) -> str:
    if not rollback_failures:
        return message
    return f"{message} Rollback failures: {', '.join(rollback_failures)}."


async def issue_denchi(request: Request, account_id: str, profile: Profile) -> Card:
    await ensure_withdraw_available(account_id)
    generated_wallet_name = wallet_name(profile)

    try:
        wallet = await create_wallet(
            user_id=account_id,
            wallet_name=generated_wallet_name,
        )
    except Exception as exc:
        logger.exception(f"Failed to create Card wallet: {exc}")
        raise IssueError("Failed to create Card wallet.") from exc

    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=str(request.base_url),
    ) as client:
        try:
            response = await client.post(
                WITHDRAW_CREATE_PATH,
                headers={"X-API-Key": wallet.adminkey},
                json=withdraw_request(profile, generated_wallet_name),
            )
            if response.is_error:
                raise RuntimeError(response_error(response))
            response_data = response.json()
            withdraw_id = response_data.get("id")
            if not isinstance(withdraw_id, str) or not withdraw_id:
                raise RuntimeError("Withdraw response did not include an ID.")
        except Exception as exc:
            logger.exception(f"Failed to create Card Withdraw link: {exc}")
            rollback_failures: list[str] = []
            await rollback_wallet(wallet.id, rollback_failures)
            raise IssueError(
                failure_detail(
                    "Failed to create Card Withdraw link.", rollback_failures
                ),
                HTTPStatus.BAD_GATEWAY,
            ) from exc

        denchi_id = urlsafe_short_hash()[:22]
        try:
            lookup_hash = lookup_hash_from_lnurl_url(response_data.get("lnurl_url"))
            return await create_denchi(
                denchi_id,
                wallet.id,
                withdraw_id,
                lookup_hash,
            )
        except Exception as exc:
            logger.exception(f"Failed to create Card record: {exc}")
            rollback_failures = []
            try:
                await delete_denchi_after_failed_issue(denchi_id)
            except Exception as cleanup_exc:
                logger.exception(
                    "Failed to remove partially created Card record "
                    f"{denchi_id}: {cleanup_exc}"
                )
                rollback_failures.append("Card record cleanup failed")
            await rollback_withdraw(
                client, withdraw_id, wallet.adminkey, rollback_failures
            )
            await rollback_wallet(wallet.id, rollback_failures)
            raise IssueError(
                failure_detail("Failed to create Card record.", rollback_failures)
            ) from exc
