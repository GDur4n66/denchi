"""Exercise the rename against real, isolated LNbits SQLite databases."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from lnbits.db import Database
from lnbits.settings import settings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "denchi", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert spec and spec.loader
denchi = importlib.util.module_from_spec(spec)
sys.modules["denchi"] = denchi
spec.loader.exec_module(denchi)
from denchi import migrations, receipt  # noqa: E402


async def initialize(conn):
    await migrations.m001_initial(conn)
    await migrations.m002_create_profiles(conn)
    await migrations.m003_create_cards(conn)
    await migrations.m004_add_withdraw_lookup_hash(conn)


def test_fresh_install_does_not_create_legacy_db(tmp_path, monkeypatch):
    asyncio.run(fresh_install(tmp_path, monkeypatch))


async def fresh_install(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lnbits_data_folder", str(tmp_path))
    target = Database("ext_denchi")
    try:
        async with target.connect() as conn:
            await initialize(conn)
            await migrations.m005_import_giftcard(conn)
            assert await conn.fetchall("SELECT * FROM denchi.cards") == []
        assert not (tmp_path / "ext_giftcard.sqlite3").exists()
    finally:
        await target.engine.dispose()


def test_legacy_copy_preserves_identity_and_is_retryable(tmp_path, monkeypatch):
    asyncio.run(legacy_copy(tmp_path, monkeypatch))


async def legacy_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lnbits_data_folder", str(tmp_path))
    source = Database("ext_giftcard")
    target = Database("ext_denchi")

    class LegacySchema:
        type = "SQLITE"
        big_int = "INT"
        timestamp_now = "CURRENT_TIMESTAMP"

        async def execute(self, sql):
            await source.execute(
                sql.replace("denchi.", "giftcard.").replace("cards", "giftcards")
            )

    try:
        await initialize(LegacySchema())
        await source.execute(
            "INSERT INTO giftcard.profiles VALUES "
            "('p1', 'owner', 'Event', 'gc', 1000, 2, 'weeks')"
        )
        await source.execute(
            "INSERT INTO giftcard.giftcards "
            "(id, wallet_id, withdraw_id, nfc_written, created_at, "
            "withdraw_lookup_hash) VALUES "
            "('c1', 'wallet1', 'withdraw1', true, '2026-09-20 12:00:00', 'hash1')"
        )
        old_cards = [
            dict(r) for r in await source.fetchall("SELECT * FROM giftcard.giftcards")
        ]
        old_profiles = [
            dict(r) for r in await source.fetchall("SELECT * FROM giftcard.profiles")
        ]
        async with target.connect() as conn:
            await initialize(conn)
            await migrations.m005_import_giftcard(conn)
            await migrations.m005_import_giftcard(conn)
            assert [
                dict(r) for r in await conn.fetchall("SELECT * FROM denchi.cards")
            ] == old_cards
            assert [
                dict(r) for r in await conn.fetchall("SELECT * FROM denchi.profiles")
            ] == old_profiles
        assert [
            dict(r) for r in await source.fetchall("SELECT * FROM giftcard.giftcards")
        ] == old_cards
    finally:
        await source.engine.dispose()
        await target.engine.dispose()


def test_routes_and_receipt_environment_compatibility(monkeypatch):
    paths = {route.path for route in denchi.denchi_ext.routes}
    assert "/denchi/" in paths
    assert "/denchi/lookup" in paths
    assert "/denchi/api/v1/cards/issue" in paths
    assert not any(path.startswith("/giftcard") for path in paths)
    monkeypatch.delenv("DENCHI_RECEIPT_PRINTER", raising=False)
    monkeypatch.setenv("GIFTCARD_RECEIPT_PRINTER", "tcp://old-printer:9100")
    assert receipt.ReceiptSettings(_env_file=None).denchi_receipt_printer == (
        "tcp://old-printer:9100"
    )
    monkeypatch.setenv("DENCHI_RECEIPT_PRINTER", "tcp://new-printer:9100")
    assert receipt.ReceiptSettings(_env_file=None).denchi_receipt_printer == (
        "tcp://new-printer:9100"
    )


def test_import_refuses_active_legacy_manager(monkeypatch):
    monkeypatch.setattr(
        type(settings),
        "is_installed_extension_id",
        lambda self, code: code == "giftcard",
    )
    monkeypatch.setattr(settings, "lnbits_deactivated_extensions", [])
    with pytest.raises(RuntimeError, match="Disable Giftcard"):
        asyncio.run(migrations.m005_import_giftcard(None))
