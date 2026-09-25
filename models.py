from datetime import datetime
from typing import Literal

from pydantic import BaseModel, StrictBool, StrictStr, conint, validator

ExpirationUnit = Literal["days", "weeks", "months"]


class CreateProfile(BaseModel):
    name: str
    wallet_name_base: str
    max_payment_per_use: conint(strict=True, gt=0)  # type: ignore[valid-type]
    expiration_value: conint(strict=True, gt=0) | None = None  # type: ignore[valid-type]
    expiration_unit: ExpirationUnit | None = None

    @validator("name", "wallet_name_base")
    def validate_non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @validator("expiration_unit", always=True)
    def validate_expiration_fields(
        cls, value: ExpirationUnit | None, values: dict
    ) -> ExpirationUnit | None:
        expiration_value = values.get("expiration_value")
        if (expiration_value is None) != (value is None):
            raise ValueError(
                "expiration_value and expiration_unit must be provided together"
            )
        return value


class Profile(CreateProfile):
    id: str
    user_id: str


class ProfileResponse(CreateProfile):
    id: str


class Card(BaseModel):
    id: str
    wallet_id: str
    withdraw_id: str
    nfc_written: bool
    created_at: datetime


class CardListItem(Card):
    wallet_name: str | None = None
    balance: int | None = None
    withdraw_url: str | None = None
    enabled: bool | None = None


class SetCardEnabled(BaseModel):
    enabled: StrictBool


class CardEnabled(BaseModel):
    enabled: bool


class IssueCard(BaseModel):
    profile_id: str

    @validator("profile_id")
    def validate_profile_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class NfcWriteData(BaseModel):
    denchi_id: str
    lnurlw_uri: str


class BulkDisableResult(BaseModel):
    updated: int
    skipped: int
    failed: int


class PublicLookupRequest(BaseModel):
    lnurlw_uri: StrictStr


class PrintReceiptRequest(PublicLookupRequest):
    printer_token: StrictStr
    mode: Literal["manual", "auto"]

    class Config:
        extra = "forbid"


class PrintReceiptResponse(BaseModel):
    success: bool
    printed: bool


class PublicUsageEntry(BaseModel):
    timestamp: datetime
    amount: int


class PublicLookupResponse(BaseModel):
    balance: int
    history: list[PublicUsageEntry]
