#!/usr/bin/env python3
"""Denchi Card balance lookup terminal for Raspberry Pi and PaSoRi."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import nfc
import requests
from dotenv import load_dotenv


DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_PRINTER_PORT = 9100
DEFAULT_HISTORY_LIMIT = 20
RECEIPT_WIDTH = 48
PAGE_SIZE = 100


class AdminSetupError(Exception):
    """Raised when admin mode cannot be initialized."""


def find_lnurlw(tag: Any) -> str:
    """Return the first lnurlw URI in the tag's NDEF records."""
    ndef = tag.ndef
    if ndef is None:
        raise ValueError("NDEFデータがありません")

    for record in ndef.records:
        uri = getattr(record, "uri", None) or getattr(record, "iri", None)
        if isinstance(uri, str) and uri.lower().startswith("lnurlw://"):
            return uri

    raise ValueError("lnurlw:// のURLレコードがありません")


def lookup_card(api_url: str, lnurlw_uri: str) -> dict[str, Any]:
    """Look up a Denchi Card and return the decoded API response."""
    response = requests.post(
        api_url,
        json={"lnurlw_uri": lnurlw_uri},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise ValueError("Denchi Cardが見つかりません")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "balance" not in data:
        raise ValueError("APIレスポンスに balance がありません")
    if not isinstance(data.get("history", []), list):
        raise ValueError("APIレスポンスの history が不正です")
    return data


def response_data(response: requests.Response, action: str) -> Any:
    """Validate an API response and decode its JSON body."""
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = f"HTTP {response.status_code}"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("detail"):
                detail = str(body["detail"])
        except ValueError:
            pass
        raise AdminSetupError(f"{action}に失敗しました: {detail}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise AdminSetupError(f"{action}のレスポンスが不正です") from exc


def create_admin_session(
    base_url: str, username: str, password: str
) -> requests.Session:
    """Log in to LNbits and return a bearer-authenticated session."""
    session = requests.Session()
    try:
        try:
            response = session.post(
                f"{base_url}/api/v1/auth",
                json={"username": username, "password": password},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise AdminSetupError(
                f"LNbitsへのログインに失敗しました: {exc}"
            ) from exc
        data = response_data(response, "LNbitsへのログイン")
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AdminSetupError(
                "LNbitsへのログインに失敗しました: access tokenがありません"
            )
        session.headers.update({"Authorization": f"Bearer {token}"})
        return session
    except Exception:
        session.close()
        raise


def fetch_paginated(
    session: requests.Session, url: str, action: str
) -> list[dict[str, Any]]:
    """Fetch all objects from an LNbits-style paginated endpoint."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = session.get(
            url,
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        page = response_data(response, action)
        data = page.get("data") if isinstance(page, dict) else None
        total = page.get("total") if isinstance(page, dict) else None
        if not isinstance(data, list) or not isinstance(total, int):
            raise AdminSetupError(f"{action}のレスポンスが不正です")
        if any(not isinstance(item, dict) for item in data):
            raise AdminSetupError(f"{action}のレスポンスが不正です")
        items.extend(data)
        if not data or len(items) >= total:
            return items
        offset += len(data)


def normalize_lnurlw(value: str, source_scheme: str) -> str:
    """Normalize an HTTP or lnurlw Withdraw URL for NFC lookup."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Withdraw URLが不正です") from exc
    if (
        parsed.scheme.lower() != source_scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Withdraw URLが不正です")
    return urlunsplit(
        ("lnurlw", parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment)
    )


def build_admin_lookup(
    session: requests.Session, base_url: str
) -> dict[str, dict[str, Any]]:
    """Build an lnurlw-to-card lookup from existing authenticated APIs."""
    wallets = fetch_paginated(
        session,
        f"{base_url}/api/v1/wallet/paginated",
        "ウォレット一覧の取得",
    )
    cards = fetch_paginated(
        session,
        f"{base_url}/denchi/api/v1/cards",
        "Denchi Card一覧の取得",
    )
    wallets_by_id = {
        wallet["id"]: wallet
        for wallet in wallets
        if isinstance(wallet.get("id"), str)
    }
    lookup: dict[str, dict[str, Any]] = {}

    for card in cards:
        card_id = card.get("id")
        wallet_id = card.get("wallet_id")
        withdraw_id = card.get("withdraw_id")
        wallet = wallets_by_id.get(wallet_id)
        if not wallet:
            print(f"警告: Card {card_id} のウォレットが見つかりません")
            continue
        admin_key = wallet.get("adminkey")
        if not isinstance(admin_key, str) or not admin_key or set(admin_key) == {"*"}:
            print(f"警告: Wallet {wallet_id} のadmin keyがありません")
            continue
        if not isinstance(withdraw_id, str) or not withdraw_id:
            print(f"警告: Card {card_id} のWithdraw IDがありません")
            continue

        response = session.get(
            f"{base_url}/withdraw/api/v1/links/{withdraw_id}",
            headers={"X-API-KEY": admin_key},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        withdraw = response_data(response, f"Card {card_id} のWithdraw取得")
        if not isinstance(withdraw, dict):
            raise AdminSetupError(
                f"Card {card_id} のWithdrawレスポンスが不正です"
            )
        if withdraw.get("id") != withdraw_id or withdraw.get("wallet") != wallet_id:
            raise AdminSetupError(f"Card {card_id} のWithdraw紐付けが不正です")
        lnurl_url = withdraw.get("lnurl_url")
        if not isinstance(lnurl_url, str):
            raise AdminSetupError(f"Card {card_id} のWithdraw URLがありません")
        try:
            source_scheme = (
                "http" if lnurl_url.lower().startswith("http://") else "https"
            )
            lnurlw_uri = normalize_lnurlw(lnurl_url, source_scheme)
        except ValueError as exc:
            raise AdminSetupError(
                f"Card {card_id} のWithdraw URLが不正です"
            ) from exc
        if lnurlw_uri in lookup:
            raise AdminSetupError(
                "複数のDenchi Cardに同じWithdraw URLがあります"
            )
        lookup[lnurlw_uri] = {
            "card": card,
            "wallet": wallet,
            "withdraw": withdraw,
            "admin_key": admin_key,
        }

    return lookup


def wallet_lightning_address(wallet: dict[str, Any], base_url: str) -> str:
    local_part = wallet.get("lightning_address")
    if not isinstance(local_part, str) or not local_part:
        return "-"
    return f"{local_part}@{urlsplit(base_url).netloc}"


def print_admin_result(
    entry: dict[str, Any], balance_sats: int, base_url: str
) -> None:
    card = entry["card"]
    wallet = entry["wallet"]
    withdraw = entry["withdraw"]
    print("\n" + "=" * 48)
    print(f"Denchi card ID: {card.get('id', '-')}")
    print(f"Wallet ID: {wallet.get('id', '-')}")
    print(f"Wallet name: {wallet.get('name', '-')}")
    print(f"Current balance: {format_number(balance_sats)} sats")
    print(f"Lightning Address: {wallet_lightning_address(wallet, base_url)}")
    print(f"Withdraw ID: {withdraw.get('id', '-')}")
    print(f"Withdraw enabled: {withdraw.get('enabled', '-')}")
    print(f"NFC written: {card.get('nfc_written', '-')}")
    print(f"Card created_at: {card.get('created_at', '-')}")
    print("=" * 48)


def format_number(value: Any) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else str(value)


def print_result(data: dict[str, Any]) -> None:
    print("\n" + "=" * 48)
    print(f"残高: {format_number(data['balance'])}")
    print("-" * 48)
    history = data.get("history", [])
    if not history:
        print("履歴: なし")
    else:
        print("履歴:")
        for item in history:
            if not isinstance(item, dict):
                print(f"  - {item}")
                continue
            timestamp = item.get("timestamp", "日時不明")
            amount = format_number(item.get("amount", "金額不明"))
            print(f"  {timestamp}  {amount}")
    print("=" * 48)


def receipt_timestamp(value: Any) -> str:
    """Format an ISO timestamp without changing its timezone."""
    if not isinstance(value, str):
        return "Unknown time"
    return value.replace("T", " ")[:16]


def build_receipt(data: dict[str, Any], history_limit: int) -> bytes:
    """Build a simple 80 mm ESC/POS receipt."""
    initialize = b"\x1b@"
    align_left = b"\x1ba\x00"
    align_center = b"\x1ba\x01"
    bold_on = b"\x1bE\x01"
    bold_off = b"\x1bE\x00"
    double_size = b"\x1d!\x11"
    normal_size = b"\x1d!\x00"

    separator = "-" * RECEIPT_WIDTH
    balance = f"{format_number(data['balance'])} sats"
    payload = bytearray(initialize)
    payload += align_left + f"{separator}\n".encode("ascii")
    payload += align_center + bold_on + b"DENCHI CARD\n" + bold_off
    payload += align_left + f"{separator}\n\nBalance\n\n".encode("ascii")
    payload += align_center + bold_on + double_size
    payload += f"{balance}\n".encode("ascii", errors="replace")
    payload += normal_size + bold_off

    if history_limit > 0:
        payload += align_left + b"\nRecent usage\n\n"
        history = data.get("history", [])[:history_limit]
        if history:
            for item in history:
                if not isinstance(item, dict):
                    continue
                timestamp = receipt_timestamp(item.get("timestamp"))
                amount = f"{format_number(item.get('amount', '?'))} sats"
                line = f"{timestamp:<16}{amount:>{RECEIPT_WIDTH - 16}}"
                payload += f"{line}\n".encode("ascii", errors="replace")
        else:
            payload += b"No recent usage\n"

    printed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    payload += align_left
    payload += f"\n{separator}\n{printed_at}\n{separator}\n\nThank you\n".encode(
        "ascii"
    )
    payload += b"\n\n\n\n\n"
    return bytes(payload)


def local_receipt_timestamp(value: Any) -> str:
    """Convert an ISO timestamp to the terminal's local time."""
    if not isinstance(value, str) or not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "-"


def build_admin_receipt(
    entry: dict[str, Any], balance_sats: int, base_url: str
) -> bytes:
    """Build an administrative receipt separately from the public receipt."""
    initialize = b"\x1b@"
    align_left = b"\x1ba\x00"
    align_center = b"\x1ba\x01"
    bold_on = b"\x1bE\x01"
    bold_off = b"\x1bE\x00"
    double_size = b"\x1d!\x11"
    normal_size = b"\x1d!\x00"

    card = entry["card"]
    wallet = entry["wallet"]
    withdraw = entry["withdraw"]
    separator = "-" * RECEIPT_WIDTH
    balance = f"{format_number(balance_sats)} sats"
    printed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    payload = bytearray(initialize)
    payload += align_left + f"{separator}\n".encode("ascii")
    payload += align_center + bold_on + b"DENCHI CARD ADMIN\n" + bold_off
    payload += align_left + f"{separator}\n\nWallet name\n".encode("ascii")
    payload += f"{wallet.get('name', '-')}\n\nBalance\n\n".encode(
        "ascii", errors="replace"
    )
    payload += align_center + bold_on + double_size
    payload += f"{balance}\n".encode("ascii", errors="replace")
    payload += normal_size + bold_off + align_left

    fields = (
        ("Lightning Address", wallet_lightning_address(wallet, base_url)),
        ("Wallet ID", wallet.get("id", "-")),
        ("Withdraw ID", withdraw.get("id", "-")),
        ("Withdraw enabled", withdraw.get("enabled", "-")),
        ("NFC written", card.get("nfc_written", "-")),
        ("Card created_at", local_receipt_timestamp(card.get("created_at"))),
        ("Print timestamp", printed_at),
    )
    for label, value in fields:
        payload += f"\n{label}\n{value}\n".encode("ascii", errors="replace")

    payload += f"\n{separator}\n".encode("ascii")
    payload += b"\n\n\n\n\n"
    return bytes(payload)


def send_receipt(host: str, port: int, receipt: bytes) -> None:
    """Send prepared ESC/POS data using the existing raw TCP connection."""
    with socket.create_connection(
        (host, port), timeout=DEFAULT_TIMEOUT_SECONDS
    ) as printer:
        printer.sendall(receipt)
        try:
            printer.sendall(b"\x1dV\x00")
        except OSError:
            # The receipt is already printed; cutting is optional.
            pass


def print_receipt(
    host: str, port: int, data: dict[str, Any], history_limit: int
) -> None:
    """Send one receipt to a raw TCP ESC/POS printer."""
    receipt = build_receipt(data, history_limit)
    send_receipt(host, port, receipt)


def print_admin_receipt(
    host: str,
    port: int,
    entry: dict[str, Any],
    balance_sats: int,
    base_url: str,
) -> None:
    """Send one administrative receipt."""
    receipt = build_admin_receipt(entry, balance_sats, base_url)
    send_receipt(host, port, receipt)


def process_tag(
    tag: Any,
    api_url: str,
    printer_host: str | None = None,
    printer_port: int = DEFAULT_PRINTER_PORT,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
) -> bool:
    """Process one tag. True keeps the connection until it is removed."""
    try:
        print("カードを読み取りました。照会中...")
        lnurlw_uri = find_lnurlw(tag)
        data = lookup_card(api_url, lnurlw_uri)
        print_result(data)
        if printer_host:
            try:
                print_receipt(printer_host, printer_port, data, history_limit)
                print("レシートを印刷しました")
            except Exception as exc:
                print(f"エラー: レシートの印刷に失敗しました: {exc}")
    except requests.Timeout:
        print("エラー: APIへの接続がタイムアウトしました")
    except requests.RequestException as exc:
        print(f"エラー: APIへの接続に失敗しました: {exc}")
    except (ValueError, TypeError) as exc:
        print(f"エラー: {exc}")
    except Exception as exc:
        # A malformed/unexpected card must not stop the terminal loop.
        print(f"エラー: カードの処理に失敗しました: {exc}")

    print("カードを離してください...")
    return True


def process_admin_tag(
    tag: Any,
    session: requests.Session,
    base_url: str,
    lookup: dict[str, dict[str, Any]],
    printer_host: str | None = None,
    printer_port: int = DEFAULT_PRINTER_PORT,
) -> bool:
    """Show and optionally print fresh administrative card information."""
    try:
        print("カードを読み取りました。管理情報を照会中...")
        lnurlw_uri = normalize_lnurlw(find_lnurlw(tag), "lnurlw")
        entry = lookup.get(lnurlw_uri)
        if not entry:
            print(
                "エラー: このカードは管理モードの一覧に見つかりません"
            )
        else:
            response = session.get(
                f"{base_url}/api/v1/wallet",
                headers={"X-API-KEY": entry["admin_key"]},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            wallet_data = response_data(response, "現在の残高取得")
            balance_msat = (
                wallet_data.get("balance")
                if isinstance(wallet_data, dict)
                else None
            )
            if not isinstance(balance_msat, int) or isinstance(balance_msat, bool):
                raise ValueError(
                    "ウォレット残高のレスポンスが不正です"
                )
            balance_sats = balance_msat // 1000
            print_admin_result(entry, balance_sats, base_url)
            if printer_host:
                try:
                    print_admin_receipt(
                        printer_host,
                        printer_port,
                        entry,
                        balance_sats,
                        base_url,
                    )
                    print("管理レシートを印刷しました")
                except Exception as exc:
                    print(f"エラー: 管理レシートの印刷に失敗しました: {exc}")
    except requests.Timeout:
        print("エラー: APIへの接続がタイムアウトしました")
    except requests.RequestException as exc:
        print(f"エラー: APIへの接続に失敗しました: {exc}")
    except (AdminSetupError, ValueError, TypeError) as exc:
        print(f"エラー: {exc}")
    except Exception as exc:
        print(f"エラー: カードの処理に失敗しました: {exc}")

    print("カードを離してください...")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Denchi Card 専用端末")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("DENCHI_API_URL"),
        help=(
            "残高照会APIの完全なURL。未指定時は環境変数 DENCHI_API_URL を使用"
        ),
    )
    parser.add_argument(
        "--printer-host",
        default=os.environ.get("DENCHI_PRINTER_HOST"),
        help="プリンターのホスト名またはIPアドレス",
    )
    parser.add_argument(
        "--printer-port",
        type=printer_port,
        default=os.environ.get("DENCHI_PRINTER_PORT", str(DEFAULT_PRINTER_PORT)),
        help=f"プリンターのTCPポート (デフォルト: {DEFAULT_PRINTER_PORT})",
    )
    parser.add_argument(
        "--history-limit",
        type=history_limit,
        default=os.environ.get("DENCHI_HISTORY_LIMIT", str(DEFAULT_HISTORY_LIMIT)),
        help=f"印刷する利用履歴の件数、0〜20 (デフォルト: {DEFAULT_HISTORY_LIMIT})",
    )
    return parser.parse_args()


def printer_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("プリンターポートは1〜65535で指定してください")
    return port


def history_limit(value: str) -> int:
    limit = int(value)
    if not 0 <= limit <= 20:
        raise argparse.ArgumentTypeError("履歴件数は0〜20で指定してください")
    return limit


def admin_base_url(value: str) -> str:
    value = value.rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise AdminSetupError("DENCHI_BASE_URLが不正です") from exc
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AdminSetupError("DENCHI_BASE_URLが不正です")
    return value


def main() -> int:
    load_dotenv()
    args = parse_args()
    mode = os.environ.get("DENCHI_MODE", "public").strip().lower()
    if mode not in ("public", "admin"):
        print(
            "エラー: DENCHI_MODEはpublicまたはadminで指定してください"
        )
        return 2

    admin_session: requests.Session | None = None
    if mode == "public" and not args.api_url:
        print(
            "エラー: --api-url または環境変数 DENCHI_API_URL に "
            "https://<host>/denchi/api/v1/public/lookup を指定してください"
        )
        return 2

    if mode == "admin":
        required = ("DENCHI_BASE_URL", "DENCHI_USERNAME", "DENCHI_PASSWORD")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            print(
                "エラー: 管理モードに必要な環境変数がありません: "
                + ", ".join(missing)
            )
            return 2
        try:
            base_url = admin_base_url(os.environ["DENCHI_BASE_URL"])
            admin_session = create_admin_session(
                base_url,
                os.environ["DENCHI_USERNAME"],
                os.environ["DENCHI_PASSWORD"],
            )
            admin_lookup = build_admin_lookup(admin_session, base_url)
            print(
                f"管理モード: {len(admin_lookup)}枚のカードを読み込みました"
            )
        except (AdminSetupError, requests.RequestException) as exc:
            print(f"エラー: 管理モードの初期化に失敗しました: {exc}")
            if admin_session:
                admin_session.close()
            return 1

        def on_connect(tag: Any) -> bool:
            return process_admin_tag(
                tag,
                admin_session,
                base_url,
                admin_lookup,
                args.printer_host,
                args.printer_port,
            )

    else:

        def on_connect(tag: Any) -> bool:
            return process_tag(
                tag,
                args.api_url,
                args.printer_host,
                args.printer_port,
                args.history_limit,
            )

    try:
        with nfc.ContactlessFrontend("usb") as frontend:
            if mode == "public":
                print("Denchi Card端末を起動しました (Ctrl+Cで終了)")
            else:
                print(
                    "Denchi Card端末をadminモードで起動しました "
                    "(Ctrl+Cで終了)"
                )
            while True:
                print("\nカードをかざしてください...")
                result = frontend.connect(rdwr={"on-connect": on_connect})
                if result is False:
                    print("\n終了しました")
                    break
    except KeyboardInterrupt:
        print("\n終了しました")
        return 0
    except OSError as exc:
        print(f"エラー: PaSoRiを開けませんでした: {exc}")
        return 1
    finally:
        if admin_session:
            admin_session.close()


if __name__ == "__main__":
    raise SystemExit(main())
