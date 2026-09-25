"""Add sats to one account's Card wallets via LNbits v1.6.2 admin API.

This is an operator-run, additive operation. A second run credits wallets again.
No request that changes balances is sent in --dry-run mode.
Requires Python 3.10+ only. No third-party packages required.
"""

import argparse
import getpass
import json
import os
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


@dataclass(frozen=True)
class WalletToTopUp:
    id: str
    name: str
    balance_msat: int | None


class ScriptError(Exception):
    """An operator-facing error that must not contain credentials."""


def redact_secret(value: object, token: str) -> str:
    message = str(value)
    return message.replace(token, "[redacted]") if token else message


def base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ScriptError("Invalid LNbits base URL.") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ScriptError("LNbits URL must be an HTTP(S) origin without a path.")
    if parsed.scheme == "http" and parsed.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ScriptError("Use HTTPS when sending a SuperUser token to a remote host.")
    return value.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_url(origin: str, path: str) -> str:
    return origin + path


def request_json(
    origin: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: object | None = None,
    timeout: int = 10,
) -> object:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(api_url(origin, path), data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        raise ScriptError(f"{method} {path} failed: HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ScriptError(f"{method} {path} failed: network error.") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ScriptError(f"{method} {path} returned invalid JSON.") from exc


def verify_superuser(origin: str, token: str) -> None:
    # Admin settings exposes this flag without fetching or creating any wallets.
    payload = request_json(origin, "GET", "/admin/api/v1/settings", token=token)
    if not isinstance(payload, dict) or payload.get("is_super_user") is not True:
        raise ScriptError("SuperUser privileges are required.")


def login(origin: str) -> str:
    try:
        username = input("SuperUser username: ")
        password = getpass.getpass("SuperUser password: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise ScriptError("SuperUser login was cancelled.") from exc
    try:
        payload = request_json(
            origin,
            "POST",
            "/api/v1/auth",
            payload={"username": username, "password": password},
        )
    finally:
        del password
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("access_token"), str)
        or not payload["access_token"]
    ):
        raise ScriptError("SuperUser login returned no access token.")
    return payload["access_token"]


def get_account(origin: str, token: str, user_id: str) -> dict:
    # Unlike GET /user/{id}, this account-list endpoint never creates a wallet.
    query = urlencode({"id": user_id, "limit": 1})
    payload = request_json(origin, "GET", f"/users/api/v1/user?{query}", token=token)
    if (
        not isinstance(payload, dict)
        or payload.get("total") != 1
        or not isinstance(payload.get("data"), list)
    ):
        raise ScriptError("LNbits returned an invalid account-list response.")
    accounts = payload["data"]
    if len(accounts) != 1 or not isinstance(accounts[0], dict):
        raise ScriptError("Target account was not found.")
    account = accounts[0]
    if account.get("id") != user_id:
        raise ScriptError("Target account ID did not match the requested ID.")
    return account


def get_matching_wallets(
    origin: str, token: str, user_id: str, wallet_prefix: str
) -> list[WalletToTopUp]:
    payload = request_json(
        origin, "GET", f"/users/api/v1/user/{user_id}/wallet", token=token
    )
    if not isinstance(payload, list):
        raise ScriptError("LNbits returned an invalid wallet-list response.")
    matched: list[WalletToTopUp] = []
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ScriptError("LNbits returned an invalid wallet entry.")
        wallet_id, name = item.get("id"), item.get("name")
        if not isinstance(wallet_id, str) or not isinstance(name, str):
            raise ScriptError("LNbits returned an invalid wallet entry.")
        if item.get("user") != user_id:
            raise ScriptError("LNbits returned a wallet owned by another account.")
        if wallet_id in seen_ids:
            raise ScriptError("LNbits returned a duplicate wallet ID.")
        seen_ids.add(wallet_id)
        if item.get("deleted") is not False or item.get("wallet_type") != "lightning":
            continue
        if not name.startswith(wallet_prefix + "-"):
            continue
        balance_msat = item.get("balance_msat")
        if not isinstance(balance_msat, int) or isinstance(balance_msat, bool):
            balance_msat = None
        matched.append(WalletToTopUp(wallet_id, name, balance_msat))
    matched.sort(key=lambda wallet: (wallet.name, wallet.id))
    return matched


def print_summary(
    account: dict,
    user_id: str,
    wallet_prefix: str,
    amount: int,
    wallets: list[WalletToTopUp],
) -> None:
    display = account.get("username") or account.get("email") or "no display name"
    print(f"Account: {user_id} ({display})")
    print(f"Wallet name prefix: {wallet_prefix}-")
    print(f"Matched wallets: {len(wallets)}")
    print(f"Amount per wallet: {amount:,} sats")
    print(f"Total top-up: {amount * len(wallets):,} sats")
    print()
    for wallet in wallets:
        balance = (
            f"{wallet.balance_msat // 1000:,} sats"
            if wallet.balance_msat is not None
            else "unavailable"
        )
        print(f"{wallet.name}  {wallet.id}  current: {balance}")


def top_up_wallet(origin: str, token: str, wallet: WalletToTopUp, amount: int) -> None:
    # This is the same additive SuperUser endpoint and sats body used by Admin UI.
    payload = request_json(
        origin,
        "PUT",
        "/users/api/v1/balance",
        token=token,
        payload={"id": wallet.id, "amount": amount},
    )
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ScriptError("LNbits did not confirm the top-up.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="LNbits base URL")
    parser.add_argument("--user", required=True, help="exact LNbits account ID")
    parser.add_argument("--wallet-prefix", required=True, help="Card name base")
    parser.add_argument(
        "--amount", required=True, type=int, help="sats to add to each wallet"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preview only; send no top-ups"
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip interactive confirmation"
    )
    args = parser.parse_args(argv)
    try:
        args.user = UUID(args.user).hex
    except ValueError:
        parser.error("--user must be an LNbits account UUID")
    if not args.wallet_prefix or not args.wallet_prefix.strip():
        parser.error("--wallet-prefix must not be empty")
    if args.wallet_prefix != args.wallet_prefix.strip():
        parser.error("--wallet-prefix must not have surrounding whitespace")
    if args.amount <= 0:
        parser.error("--amount must be a positive integer in sats")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.getenv("LNBITS_SUPERUSER_ACCESS_TOKEN", "").strip()
    try:
        origin = base_url(args.url)
        if not token:
            token = login(origin)
        verify_superuser(origin, token)
        account = get_account(origin, token, args.user)
        wallets = get_matching_wallets(origin, token, args.user, args.wallet_prefix)
        print_summary(account, args.user, args.wallet_prefix, args.amount, wallets)
        if not wallets:
            print("No matching active Card wallets; no top-ups sent.")
            return 1
        if args.dry_run:
            print("DRY RUN: no balances were changed.")
            return 0

        print()
        print("WARNING: This operation is additive.")
        print("Running it again will top up the same wallets again.")
        if not args.yes:
            print(
                f"Top up {len(wallets)} wallets by {args.amount:,} sats each? "
                f"Total credit: {args.amount * len(wallets):,} sats"
            )
            try:
                confirmation = input("Type YES to continue: ")
            except EOFError:
                confirmation = ""
            if confirmation != "YES":
                print("Cancelled; no top-ups sent.")
                return 1

        succeeded = 0
        failed: list[tuple[WalletToTopUp, str]] = []
        for wallet in wallets:
            try:
                top_up_wallet(origin, token, wallet, args.amount)
                succeeded += 1
                print(f"OK: {wallet.name}  {wallet.id}")
            except ScriptError as exc:
                failed.append((wallet, redact_secret(exc, token)))
                print(f"FAILED: {wallet.name}  {wallet.id}")
        print(f"Successful: {succeeded}")
        print(f"Failed: {len(failed)}")
        for wallet, error in failed:
            print(f"  {wallet.name}  {wallet.id}: {error}")
        return 1 if failed else 0
    except ScriptError as exc:
        print(
            f"Error: {redact_secret(exc, token)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
