from abc import ABC, abstractmethod
from typing import TypedDict

from ..core.db import ApiKey, AsyncSession


class PaymentResult(TypedDict):
    amount_msats: int
    currency: str
    payment_method: str
    metadata: dict[str, str | None]


class PaymentMethod(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        pass

    @abstractmethod
    async def validate_payment_data(self, payment_data: str) -> bool:
        pass

    @abstractmethod
    async def process_payment(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        pass

    @abstractmethod
    async def refund_payment(
        self,
        amount_msats: int,
        currency: str,
        refund_address: str | None,
        metadata: dict[str, str | None],
        session: AsyncSession,
    ) -> dict[str, str]:
        pass

    @abstractmethod
    def extract_metadata(self, payment_data: str) -> dict[str, str | None]:
        pass
