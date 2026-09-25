from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from lnbits.core.models import SimpleStatus
from lnbits.core.models.users import AccountId
from lnbits.db import Page
from lnbits.decorators import check_account_id_exists
from loguru import logger

from .crud import (
    create_profile,
    delete_profile,
    get_cards,
    get_denchi,
    get_owned_denchi,
    get_profile,
    get_profiles,
    mark_denchi_nfc_written,
)
from .models import (
    BulkDisableResult,
    Card,
    CardEnabled,
    CardListItem,
    CreateProfile,
    IssueCard,
    NfcWriteData,
    PrintReceiptRequest,
    PrintReceiptResponse,
    ProfileResponse,
    PublicLookupRequest,
    PublicLookupResponse,
    SetCardEnabled,
)
from .receipt import print_receipt, receipt_config, valid_terminal_token
from .services import (
    DeleteCardError,
    IssueError,
    SetCardEnabledError,
    delete_existing_denchi,
    disable_all_cards,
    enrich_denchi_page,
    get_nfc_write_data,
    get_public_lookup,
    get_public_lookup_with_hash,
    issue_denchi,
    set_denchi_enabled,
)

denchi_ext_api = APIRouter(prefix="/api/v1")


@denchi_ext_api.post(
    "/public/lookup",
    response_model=PublicLookupResponse,
    responses={HTTPStatus.NOT_FOUND: {"description": "Card not found."}},
)
async def api_public_lookup(
    data: PublicLookupRequest, request: Request
) -> PublicLookupResponse:
    result = await get_public_lookup(request, data.lnurlw_uri)
    if not result:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Card not found.",
        )
    return result


@denchi_ext_api.post("/public/print-receipt", response_model=PrintReceiptResponse)
async def api_print_receipt(
    data: PrintReceiptRequest, request: Request
) -> PrintReceiptResponse:
    if not valid_terminal_token(data.printer_token):
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Not found.")
    if data.mode == "auto" and not receipt_config().auto_print:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Not found.")
    lookup = await get_public_lookup_with_hash(request, data.lnurlw_uri)
    if not lookup:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Card not found.")
    result, lookup_hash = lookup
    try:
        printed = await print_receipt(result, lookup_hash, data.mode)
    except Exception as exc:
        logger.warning(f"Card receipt print failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Receipt printer is unavailable.",
        ) from exc
    return PrintReceiptResponse(success=True, printed=printed)


@denchi_ext_api.get("/cards", response_model=Page[CardListItem])
async def api_list_cards(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    nfc_written: bool | None = Query(None),
    wallet_name: str | None = Query(None),
    account_id: AccountId = Depends(check_account_id_exists),
) -> Page[CardListItem]:
    page, admin_keys = await get_cards(
        account_id.id,
        limit,
        offset,
        nfc_written,
        wallet_name,
    )
    return await enrich_denchi_page(request, page, admin_keys)


@denchi_ext_api.post(
    "/cards/disable-all",
    response_model=BulkDisableResult,
)
async def api_disable_all_cards(
    request: Request,
    account_id: AccountId = Depends(check_account_id_exists),
) -> BulkDisableResult:
    try:
        return await disable_all_cards(request, account_id.id)
    except IssueError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@denchi_ext_api.put("/cards/{denchi_id}/enabled", response_model=CardEnabled)
async def api_set_denchi_enabled(
    denchi_id: str,
    data: SetCardEnabled,
    request: Request,
    account_id: AccountId = Depends(check_account_id_exists),
) -> CardEnabled:
    denchi = await get_owned_denchi(denchi_id, account_id.id)
    if not denchi:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Card not found.")

    try:
        return await set_denchi_enabled(request, account_id.id, denchi, data.enabled)
    except (SetCardEnabledError, IssueError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@denchi_ext_api.delete("/cards/{denchi_id}")
async def api_delete_denchi(
    denchi_id: str,
    request: Request,
    account_id: AccountId = Depends(check_account_id_exists),
) -> SimpleStatus:
    denchi = await get_denchi(denchi_id)
    if not denchi:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Card not found.")

    try:
        await delete_existing_denchi(request, account_id.id, denchi)
    except (DeleteCardError, IssueError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return SimpleStatus(success=True, message="Card deleted.")


@denchi_ext_api.post(
    "/cards/issue",
    status_code=HTTPStatus.CREATED,
    response_model=Card,
)
async def api_issue_denchi(
    request: Request,
    data: IssueCard,
    account_id: AccountId = Depends(check_account_id_exists),
) -> Card:
    profile = await get_profile(data.profile_id, account_id.id)
    if not profile:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Profile not found."
        )

    try:
        return await issue_denchi(request, account_id.id, profile)
    except IssueError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@denchi_ext_api.get("/cards/{denchi_id}/nfc", response_model=NfcWriteData)
async def api_get_denchi_nfc_data(
    denchi_id: str,
    request: Request,
    account_id: AccountId = Depends(check_account_id_exists),
) -> NfcWriteData:
    denchi = await get_owned_denchi(denchi_id, account_id.id)
    if not denchi:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Card not found.")
    if denchi.nfc_written:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail="Card NFC has already been written.",
        )

    try:
        return await get_nfc_write_data(request, account_id.id, denchi)
    except IssueError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@denchi_ext_api.post("/cards/{denchi_id}/nfc-written", response_model=Card)
async def api_mark_denchi_nfc_written(
    denchi_id: str,
    account_id: AccountId = Depends(check_account_id_exists),
) -> Card:
    denchi = await get_owned_denchi(denchi_id, account_id.id)
    if not denchi:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Card not found.")
    return await mark_denchi_nfc_written(denchi.id)


@denchi_ext_api.get("/profiles", response_model=list[ProfileResponse])
async def api_list_profiles(
    account_id: AccountId = Depends(check_account_id_exists),
) -> list[ProfileResponse]:
    profiles = await get_profiles(account_id.id)
    return [
        ProfileResponse(**profile.dict(exclude={"user_id"})) for profile in profiles
    ]


@denchi_ext_api.post(
    "/profiles", status_code=HTTPStatus.CREATED, response_model=ProfileResponse
)
async def api_create_profile(
    data: CreateProfile,
    account_id: AccountId = Depends(check_account_id_exists),
) -> ProfileResponse:
    profile = await create_profile(data, account_id.id)
    return ProfileResponse(**profile.dict(exclude={"user_id"}))


@denchi_ext_api.delete("/profiles/{profile_id}")
async def api_delete_profile(
    profile_id: str,
    account_id: AccountId = Depends(check_account_id_exists),
) -> SimpleStatus:
    profile = await get_profile(profile_id, account_id.id)
    if not profile:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Profile not found."
        )

    await delete_profile(profile_id, account_id.id)
    return SimpleStatus(success=True, message="Profile deleted.")
