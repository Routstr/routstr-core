import hashlib

from cashu.core.base import Token

from .auth import validate_bearer_key
from .core.db import ApiKey, AsyncSession
from .wallet import deserialize_token_from_string


class RoutstrRequest:
    path: str | None
    model_id: str
    cashu_token: Token | None
    balance: ApiKey | None
    upstream_filter: dict | None

    def __init__(
        self,
        path: str | None,
        model_id: str,
        cashu_token: Token | None,
        balance_id: str | None,
        upstream_filter: dict | None,
    ) -> None:
        self.path = path
        self.model_id = model_id
        self.cashu_token = cashu_token
        self.balance_id = balance_id
        self.upstream_filter = upstream_filter

    @classmethod
    def from_request(
        cls, request_body: dict, headers: dict, path: str
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

        return cls(
            path=path,
            model_id=request_body.get("model", "auto"),
            cashu_token=cashu_token,
            balance_id=token_hash,
            upstream_filter=None,
        )

    async def get_or_create_balance(self, db_session: AsyncSession) -> ApiKey:
        if not self.balance_id:
            raise ValueError("No balance ID found")

        if key := await db_session.get(ApiKey, self.balance_id):
            return key

        if not self.cashu_token:
            raise ValueError("No cashu token found")

        key = await validate_bearer_key(
            self.cashu_token, db_session, self.cashu_token.refund_address
        )

        # key = ApiKey.from_cashu_token(self.cashu_token)
        # db_session.add(key)
        # await db_session.commit()
        return key
