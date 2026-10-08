"""Endpoint capabilities and unit-labelled rates, independent of routing/billing."""

import math

from pydantic.v1 import BaseModel, Field, validator


class ModalityPricingLine(BaseModel):
    billable: str
    unit: str
    cost_usd: float
    variant: str | None = None

    @validator("cost_usd", pre=True)
    def _usable_rate(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("cost_usd must be a numeric rate")
        try:
            rate = float(value)
        except (ValueError, OverflowError):
            raise ValueError("cost_usd must be finite and non-negative")
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("cost_usd must be finite and non-negative")
        return rate

    @validator("billable", "unit")
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pricing dimension must not be empty")
        return value


class ModalityEndpoint(BaseModel):
    provider_slug: str
    provider_tag: str | None = None
    supported_parameters: dict[str, dict] = Field(default_factory=dict)
    allowed_passthrough_parameters: list[str] = Field(default_factory=list)
    supports_streaming: bool = False
    pricing: list[ModalityPricingLine]


class ApiCapability(BaseModel):
    fetched_at: int = 0
    supported_parameters: dict[str, dict] = Field(default_factory=dict)
    supports_streaming: bool = False
    endpoints: list[ModalityEndpoint] = Field(default_factory=list)
