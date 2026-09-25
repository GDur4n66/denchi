from pathlib import Path

from lnbits.db import SQLITE, Database
from lnbits.settings import settings


async def m001_initial(db):
    pass


async def m002_create_profiles(db):
    await db.execute(
        f"""
        CREATE TABLE denchi.profiles (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            wallet_name_base TEXT NOT NULL,
            max_payment_per_use {db.big_int} NOT NULL,
            expiration_value INTEGER,
            expiration_unit TEXT
        );
    """
    )


async def m003_create_cards(db):
    await db.execute(
        f"""
        CREATE TABLE denchi.cards (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            withdraw_id TEXT NOT NULL,
            nfc_written BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """
    )


async def m004_add_withdraw_lookup_hash(db):
    await db.execute("ALTER TABLE denchi.cards ADD COLUMN withdraw_lookup_hash TEXT")
    if db.type == "SQLITE":
        await db.execute(
            """
            CREATE UNIQUE INDEX denchi.cards_withdraw_lookup_hash_idx
            ON cards (withdraw_lookup_hash)
            """
        )
    else:
        await db.execute(
            """
            CREATE UNIQUE INDEX cards_withdraw_lookup_hash_idx
            ON denchi.cards (withdraw_lookup_hash)
            """
        )


async def m005_import_giftcard(db):
    """Copy legacy records once; wallets, Withdraw links and old DB stay intact.

    Disable Giftcard before installing Denchi Card. The two extensions must not
    manage the same wallets concurrently. LNbits records this migration as run.
    """
    if (
        settings.is_installed_extension_id("giftcard")
        and "giftcard" not in settings.lnbits_deactivated_extensions
    ):
        raise RuntimeError("Disable Giftcard before installing Denchi Card.")
    if db.type == SQLITE:
        if not (Path(settings.lnbits_data_folder) / "ext_giftcard.sqlite3").is_file():
            return
    else:
        schema = await db.fetchone(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name = 'giftcard'"
        )
        if not schema:
            return

    legacy = Database("ext_giftcard")
    try:
        async with legacy.connect() as source:
            if db.type == SQLITE:
                rows = await source.fetchall(
                    "SELECT name FROM giftcard.sqlite_master WHERE type = 'table'"
                )
            else:
                rows = await source.fetchall(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema = 'giftcard'"
                )
            tables = {row["name"] for row in rows}
            if not {"profiles", "giftcards"}.issubset(tables):
                raise RuntimeError(
                    "Legacy Giftcard database is incomplete; restore or upgrade it "
                    "before installing Denchi Card."
                )
            profiles = await source.fetchall("SELECT * FROM giftcard.profiles")
            cards = await source.fetchall("SELECT * FROM giftcard.giftcards")

        for table, rows, columns in (
            (
                "profiles",
                profiles,
                "id user_id name wallet_name_base max_payment_per_use "
                "expiration_value expiration_unit",
            ),
            (
                "cards",
                cards,
                "id wallet_id withdraw_id nfc_written created_at withdraw_lookup_hash",
            ),
        ):
            names = columns.split()
            placeholders = ", ".join(f":{name}" for name in names)
            for row in rows:
                if any(name not in row for name in names):
                    raise RuntimeError("Upgrade Giftcard to 0.1.0 before migration.")
                await db.execute(
                    f"INSERT INTO denchi.{table} ({', '.join(names)}) "
                    f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                    {name: row[name] for name in names},
                )
    finally:
        await legacy.engine.dispose()
