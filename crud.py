from lnbits.core.db import db as core_db
from lnbits.db import SQLITE, Page
from lnbits.helpers import urlsafe_short_hash

from .models import Card, CardListItem, CreateProfile, Profile


async def create_profile(data: CreateProfile, user_id: str) -> Profile:
    from . import db

    profile = Profile(id=urlsafe_short_hash()[:22], user_id=user_id, **data.dict())
    await db.insert("denchi.profiles", profile)
    return profile


async def get_profiles(user_id: str) -> list[Profile]:
    from . import db

    return await db.fetchall(
        """
        SELECT * FROM denchi.profiles
        WHERE user_id = :user_id
        ORDER BY name, id
        """,
        {"user_id": user_id},
        Profile,
    )


async def get_profile(profile_id: str, user_id: str) -> Profile | None:
    from . import db

    return await db.fetchone(
        """
        SELECT * FROM denchi.profiles
        WHERE id = :id AND user_id = :user_id
        """,
        {"id": profile_id, "user_id": user_id},
        Profile,
    )


async def delete_profile(profile_id: str, user_id: str) -> None:
    from . import db

    await db.execute(
        """
        DELETE FROM denchi.profiles
        WHERE id = :id AND user_id = :user_id
        """,
        {"id": profile_id, "user_id": user_id},
    )


async def get_cards(
    account_id: str,
    limit: int,
    offset: int,
    nfc_written: bool | None = None,
    wallet_name: str | None = None,
) -> tuple[Page[CardListItem], dict[str, str]]:
    from . import db as extension_db

    where = [
        'wallets."user" = :account_id',
    ]
    values: dict = {"account_id": account_id}

    if nfc_written is not None:
        where.append("cards.nfc_written = :nfc_written")
        values["nfc_written"] = nfc_written

    if wallet_name:
        where.append("LOWER(wallets.name) LIKE :wallet_name")
        values["wallet_name"] = f"%{wallet_name.strip().lower()}%"

    where_sql = " AND ".join(where)
    async with core_db.connect() as conn:
        extension_attached = False
        try:
            if conn.type == SQLITE:
                await conn.execute(
                    "ATTACH DATABASE :path AS denchi",
                    {"path": extension_db.path},
                )
                extension_attached = True

            count_result = await conn.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM denchi.cards AS cards
                JOIN wallets ON wallets.id = cards.wallet_id
                WHERE {where_sql}
                """,
                values,
            )
            count_row = count_result.mappings().first()
            count_result.close()
            total = int(count_row["total"]) if count_row else 0

            cards_result = await conn.execute(
                f"""
                SELECT
                    cards.*,
                    wallets.name AS wallet_name,
                    wallets.adminkey AS wallet_admin_key,
                    COALESCE((
                        SELECT balance FROM balances
                        WHERE wallet_id = wallets.id
                    ), 0) AS balance_msat
                FROM denchi.cards AS cards
                JOIN wallets ON wallets.id = cards.wallet_id
                WHERE {where_sql}
                ORDER BY cards.created_at DESC, cards.id
                LIMIT :limit OFFSET :offset
                """,
                {**values, "limit": limit, "offset": offset},
            )
            card_rows = cards_result.mappings().all()
            cards_result.close()
            admin_keys: dict[str, str] = {}
            cards: list[CardListItem] = []
            for row in card_rows:
                row_data = dict(row)
                admin_keys[row_data["id"]] = row_data.pop("wallet_admin_key")
                balance_msat = int(row_data.pop("balance_msat"))
                cards.append(CardListItem(balance=balance_msat // 1000, **row_data))
            return Page(data=cards, total=total), admin_keys
        finally:
            if extension_attached:
                await conn.execute("DETACH DATABASE denchi")


async def create_denchi(
    denchi_id: str,
    wallet_id: str,
    withdraw_id: str,
    withdraw_lookup_hash: str,
) -> Card:
    from . import db

    await db.execute(
        """
        INSERT INTO denchi.cards (
            id, wallet_id, withdraw_id, withdraw_lookup_hash, nfc_written
        ) VALUES (
            :id, :wallet_id, :withdraw_id, :withdraw_lookup_hash, false
        )
        """,
        {
            "id": denchi_id,
            "wallet_id": wallet_id,
            "withdraw_id": withdraw_id,
            "withdraw_lookup_hash": withdraw_lookup_hash,
        },
    )
    denchi = await db.fetchone(
        "SELECT * FROM denchi.cards WHERE id = :id",
        {"id": denchi_id},
        Card,
    )
    if not denchi:
        raise RuntimeError("Created Card record could not be loaded.")
    return denchi


async def get_denchi_by_lookup_hash(withdraw_lookup_hash: str) -> Card | None:
    from . import db

    return await db.fetchone(
        """
        SELECT id, wallet_id, withdraw_id, nfc_written, created_at
        FROM denchi.cards
        WHERE withdraw_lookup_hash = :withdraw_lookup_hash
        """,
        {"withdraw_lookup_hash": withdraw_lookup_hash},
        Card,
    )


async def get_owned_cards_with_admin_keys(
    account_id: str,
) -> list[tuple[Card, str]]:
    from . import db as extension_db

    async with core_db.connect() as conn:
        extension_attached = False
        try:
            if conn.type == SQLITE:
                await conn.execute(
                    "ATTACH DATABASE :path AS denchi",
                    {"path": extension_db.path},
                )
                extension_attached = True

            result = await conn.execute(
                """
                SELECT cards.*, wallets.adminkey AS wallet_admin_key
                FROM denchi.cards AS cards
                JOIN wallets ON wallets.id = cards.wallet_id
                WHERE wallets."user" = :account_id
                    AND wallets.deleted = false
                ORDER BY cards.id
                """,
                {"account_id": account_id},
            )
            rows = result.mappings().all()
            result.close()
            cards: list[tuple[Card, str]] = []
            for row in rows:
                row_data = dict(row)
                admin_key = row_data.pop("wallet_admin_key")
                cards.append((Card(**row_data), admin_key))
            return cards
        finally:
            if extension_attached:
                await conn.execute("DETACH DATABASE denchi")


async def get_denchi(denchi_id: str) -> Card | None:
    from . import db

    return await db.fetchone(
        "SELECT * FROM denchi.cards WHERE id = :id",
        {"id": denchi_id},
        Card,
    )


async def delete_denchi(denchi_id: str) -> None:
    from . import db

    await db.execute(
        "DELETE FROM denchi.cards WHERE id = :id",
        {"id": denchi_id},
    )


async def get_owned_denchi(denchi_id: str, account_id: str) -> Card | None:
    from . import db as extension_db

    async with core_db.connect() as conn:
        extension_attached = False
        try:
            if conn.type == SQLITE:
                await conn.execute(
                    "ATTACH DATABASE :path AS denchi",
                    {"path": extension_db.path},
                )
                extension_attached = True

            result = await conn.execute(
                """
                SELECT cards.*
                FROM denchi.cards AS cards
                WHERE cards.id = :denchi_id
                    AND EXISTS (
                        SELECT 1 FROM wallets
                        WHERE wallets.id = cards.wallet_id
                            AND wallets."user" = :account_id
                            AND wallets.deleted = false
                    )
                """,
                {"denchi_id": denchi_id, "account_id": account_id},
            )
            row = result.mappings().first()
            result.close()
            return Card(**row) if row else None
        finally:
            if extension_attached:
                await conn.execute("DETACH DATABASE denchi")


async def mark_denchi_nfc_written(denchi_id: str) -> Card:
    from . import db

    await db.execute(
        """
        UPDATE denchi.cards
        SET nfc_written = true
        WHERE id = :id AND nfc_written = false
        """,
        {"id": denchi_id},
    )
    denchi = await db.fetchone(
        "SELECT * FROM denchi.cards WHERE id = :id",
        {"id": denchi_id},
        Card,
    )
    if not denchi:
        raise RuntimeError("Card record could not be loaded.")
    return denchi


async def delete_denchi_after_failed_issue(denchi_id: str) -> None:
    from . import db

    await db.execute(
        "DELETE FROM denchi.cards WHERE id = :id",
        {"id": denchi_id},
    )
