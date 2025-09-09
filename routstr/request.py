import hashlib

from cashu.core.base import Token
from fastapi import Request

from .auth import validate_bearer_key
from .core.db import ApiKey, AsyncSession
from .wallet import deserialize_token_from_string

# TODO change ValueError to HTTPExceptions


class RoutstrRequest:
    path: str | None
    model_id: str
    request_body: dict
    request_headers: dict
    cashu_token: Token | None
    balance_id: str | None
    upstream_filter: dict | None
    _balance: ApiKey | None
    _request: Request
    reserved_balance: int | None

    def __init__(
        self,
        path: str,
        model_id: str,
        request_body: dict,
        request_headers: dict,
        cashu_token: Token | None,
        balance_id: str,
        upstream_filter: dict | None,
        _request: Request,
    ) -> None:
        self.path = path
        self.model_id = model_id
        self.request_body = request_body
        self.request_headers = request_headers
        self.cashu_token = cashu_token
        self.balance_id = balance_id
        self.upstream_filter = upstream_filter
        self._balance = None
        self._request = _request

    @classmethod
    def from_request(
        cls, request_body: dict, headers: dict, path: str, _request: Request
    ) -> "RoutstrRequest":
        token_hash: str | None = None
        cashu_token: Token | None = None
        raw_cashu_token: str | None = None

        if "x-cashu" in headers:
            raw_cashu_token = headers["x-cashu"]

        if (auth := headers.get("authorization", "")) and auth.startswith("Bearer "):
            bearer_token: str = auth[7:]

            if bearer_token.startswith("cashu"):
                raw_cashu_token = bearer_token
            elif bearer_token.startswith("sk-"):
                token_hash = bearer_token.removeprefix("sk-")

        if raw_cashu_token:
            token_hash = hashlib.sha256(raw_cashu_token.encode()).hexdigest()
            cashu_token = deserialize_token_from_string(raw_cashu_token)

        if not token_hash:
            raise ValueError("No token hash found")

        return cls(
            path=path,
            model_id=request_body.get("model", "auto"),
            request_body=request_body,
            request_headers=headers,
            cashu_token=cashu_token,
            balance_id=token_hash,
            upstream_filter=None,
            _request=_request,
        )

    def validate(self) -> None:
        if not self.path:
            raise ValueError("Path is required")
        if not self.balance_id:
            raise ValueError("Balance ID is required")

    async def get_or_create_balance(self, db_session: AsyncSession) -> ApiKey:
        if self._balance:
            return self._balance

        if not self.balance_id:
            raise ValueError("No balance ID found")

        if key := await db_session.get(ApiKey, self.balance_id):
            return key

        if not self.cashu_token:
            raise ValueError("No cashu token found")

        key = await validate_bearer_key(
            self.cashu_token, db_session, self.cashu_token.refund_address
        )
        self._balance = key

        # key = ApiKey.from_cashu_token(self.cashu_token)
        # db_session.add(key)
        # await db_session.commit()
        return key

    async def reserve_balance(
        self, db_session: AsyncSession, amount_msats: int
    ) -> None:
        if not (balance := await db_session.get(ApiKey, self.balance_id)):
            raise ValueError("No balance found")

        if balance.balance < amount_msats:
            raise ValueError("Insufficient balance")

        self.reserved_balance = amount_msats
        balance.reserved_balance += amount_msats
        await db_session.commit()
