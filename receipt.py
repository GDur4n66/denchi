"""Optional, event-specific receipt printing for the public lookup terminal."""

import asyncio
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from time import monotonic
from urllib.parse import urlsplit

from lnbits.settings import Settings
from loguru import logger
from pydantic import BaseSettings, Field

from .models import PublicLookupResponse

LINE_WIDTH = 48
AUTO_COOLDOWN_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 2.0
SEND_TIMEOUT_SECONDS = 5.0
INIT = b"\x1b\x40"
ALIGN_LEFT = b"\x1b\x61\x00"
ALIGN_CENTER = b"\x1b\x61\x01"
BOLD_ON = b"\x1b\x45\x01"
BOLD_OFF = b"\x1b\x45\x00"
FEED_6 = b"\x1b\x64\x06"
FULL_CUT = b"\x1d\x56\x00"

_printer_lock = asyncio.Lock()
_last_auto_print: dict[str, float] = {}


class ReceiptSettings(BaseSettings):
    denchi_receipt_printer: str | None = Field(
        None, env=["DENCHI_RECEIPT_PRINTER", "GIFTCARD_RECEIPT_PRINTER"]
    )
    denchi_receipt_auto_print: str = Field(
        "", env=["DENCHI_RECEIPT_AUTO_PRINT", "GIFTCARD_RECEIPT_AUTO_PRINT"]
    )
    denchi_receipt_terminal_token: str | None = Field(
        None, env=["DENCHI_RECEIPT_TERMINAL_TOKEN", "GIFTCARD_RECEIPT_TERMINAL_TOKEN"]
    )

    class Config(Settings.Config):
        pass


@dataclass(frozen=True)
class ReceiptConfig:
    host: str | None
    port: int | None
    auto_print: bool
    terminal_token: str | None

    @property
    def enabled(self) -> bool:
        return (
            self.host is not None
            and self.port is not None
            and self.terminal_token is not None
        )


def _parse_printer(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    if len(value) > 512:
        raise ValueError("printer URL is too long")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("invalid printer URL")
    try:
        if not value.startswith("tcp://"):
            raise ValueError("invalid printer URL")
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "tcp"
            or not parsed.hostname
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("invalid printer URL")
    except ValueError as exc:
        raise ValueError("invalid printer URL") from exc
    return parsed.hostname, port


def _get_config() -> ReceiptConfig:
    settings = ReceiptSettings()
    printer_value = settings.denchi_receipt_printer
    try:
        printer = _parse_printer(printer_value)
    except ValueError:
        logger.warning("Card receipt printer configuration is invalid.")
        printer = None

    token = settings.denchi_receipt_terminal_token
    if token is not None and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        logger.warning("Card receipt terminal token configuration is invalid.")
        token = None
    if not token:
        token = None

    auto_print = settings.denchi_receipt_auto_print.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if printer is None:
        return ReceiptConfig(None, None, False, token)
    return ReceiptConfig(
        printer[0], printer[1], auto_print and token is not None, token
    )


@lru_cache(maxsize=1)
def receipt_config() -> ReceiptConfig:
    """Read extension-local environment configuration once per process."""
    return _get_config()


def valid_terminal_token(candidate: str) -> bool:
    config = receipt_config()
    if not config.enabled or len(candidate) > 256:
        return False
    return hmac.compare_digest(candidate, config.terminal_token or "")


def _line(value: str) -> bytes:
    if not value.isascii() or len(value) > LINE_WIDTH or "\n" in value or "\r" in value:
        raise ValueError("invalid receipt line")
    return value.encode("ascii") + b"\n"


def _amount_line(label: str, amount: str) -> bytes:
    if len(label) + len(amount) > LINE_WIDTH:
        raise ValueError("receipt line is too long")
    return _line(label + " " * (LINE_WIDTH - len(label) - len(amount)) + amount)


def render_receipt(result: PublicLookupResponse) -> bytes:
    """Render bounded ASCII ESC/POS bytes from authoritative lookup data."""
    separator = "-" * LINE_WIDTH
    parts = [
        INIT,
        ALIGN_CENTER,
        BOLD_ON,
        _line(separator),
        _line("DENCHI CARD BALANCE".center(LINE_WIDTH)),
        BOLD_OFF,
        _line(separator),
        ALIGN_LEFT,
        _line(""),
        _line("Balance"),
        _amount_line("", f"{result.balance:,} sats"),
        _line(""),
        _line("Usage History"),
        _line(""),
    ]
    for entry in result.history[:20]:
        timestamp = entry.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
        parts.append(_amount_line(timestamp, f"{entry.amount:,} sats"))
    if not result.history:
        parts.append(_line("No usage yet."))
    parts.extend(
        [
            _line(""),
            _line(separator),
            _line(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")),
            _line(separator),
            FEED_6,
            FULL_CUT,
        ]
    )
    return b"".join(parts)


async def _send_bytes(host: str, port: int, data: bytes) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT_SECONDS
    )
    del reader
    try:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout=SEND_TIMEOUT_SECONDS)
    finally:
        writer.close()
        await writer.wait_closed()


async def print_receipt(
    result: PublicLookupResponse, lookup_hash: str, mode: str
) -> bool:
    """Print once; return False only for a suppressed duplicate auto-print."""
    config = receipt_config()
    if not config.enabled or config.host is None or config.port is None:
        raise RuntimeError("receipt printing is unavailable")
    if mode not in ("auto", "manual"):
        raise ValueError("invalid print mode")
    if mode == "auto" and not config.auto_print:
        raise RuntimeError("automatic receipt printing is unavailable")
    data = render_receipt(result)
    async with _printer_lock:
        now = monotonic()
        if mode == "auto":
            if (
                now - _last_auto_print.get(lookup_hash, float("-inf"))
                < AUTO_COOLDOWN_SECONDS
            ):
                return False
            # Reserve before sending: an ambiguous send must not auto-retry.
            _last_auto_print[lookup_hash] = now
            for key, printed_at in list(_last_auto_print.items()):
                if now - printed_at >= AUTO_COOLDOWN_SECONDS:
                    del _last_auto_print[key]
        await asyncio.wait_for(
            _send_bytes(config.host, config.port, data),
            timeout=SEND_TIMEOUT_SECONDS,
        )
    return True
