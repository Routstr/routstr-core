import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import select

from ..payment.models import _row_to_model, list_models
from ..proxy import refresh_model_maps, reinitialize_upstreams
from ..wallet import (
    fetch_all_balances,
    get_proofs_per_mint_and_unit,
    get_wallet,
    send_token,
    slow_filter_spend_proofs,
)
from .db import ApiKey, ModelRow, UpstreamProviderRow, create_session
from .log_manager import log_manager
from .logging import get_logger
from .settings import SettingsService, settings

logger = get_logger(__name__)

admin_router = APIRouter(prefix="/admin", include_in_schema=False)

admin_sessions: dict[str, int] = {}
ADMIN_SESSION_DURATION = 3600


def require_admin_api(request: Request) -> None:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        expiry = admin_sessions.get(token)
        if expiry and expiry > int(datetime.now(timezone.utc).timestamp()):
            return

    raise HTTPException(status_code=403, detail="Unauthorized")


@admin_router.get("/api/temporary-balances", dependencies=[Depends(require_admin_api)])
async def get_temporary_balances_api(request: Request) -> list[dict[str, object]]:
    async with create_session() as session:
        result = await session.exec(select(ApiKey))
        api_keys = result.all()

    return [
        {
            "hashed_key": key.hashed_key,
            "balance": key.balance,
            "total_spent": key.total_spent,
            "total_requests": key.total_requests,
            "refund_address": key.refund_address,
            "key_expiry_time": key.key_expiry_time,
            "parent_key_hash": key.parent_key_hash,
            "balance_limit": key.balance_limit,
            "balance_limit_reset": key.balance_limit_reset,
            "validity_date": key.validity_date,
        }
        for key in api_keys
    ]


class ApiKeyUpdate(BaseModel):
    balance_limit: int | None = None
    balance_limit_reset: str | None = None
    validity_date: int | None = None


@admin_router.patch(
    "/api/apikeys/{hashed_key}", dependencies=[Depends(require_admin_api)]
)
async def update_apikey(
    request: Request, hashed_key: str, update: ApiKeyUpdate
) -> dict:
    async with create_session() as session:
        key = await session.get(ApiKey, hashed_key)
        if not key:
            raise HTTPException(status_code=404, detail="API key not found")

        if update.balance_limit is not None:
            key.balance_limit = update.balance_limit
        if update.balance_limit_reset is not None:
            key.balance_limit_reset = update.balance_limit_reset
        if update.validity_date is not None:
            key.validity_date = update.validity_date

        session.add(key)
        await session.commit()
        await session.refresh(key)

    return {
        "hashed_key": key.hashed_key,
        "balance_limit": key.balance_limit,
        "balance_limit_reset": key.balance_limit_reset,
        "validity_date": key.validity_date,
    }


@admin_router.get("/api/balances", dependencies=[Depends(require_admin_api)])
async def get_balances_api(request: Request) -> list[dict[str, object]]:
    balance_details, _tw, _tu, _ow = await fetch_all_balances()
    return [dict(d) for d in balance_details]


@admin_router.get("/api/settings", dependencies=[Depends(require_admin_api)])
async def get_settings(request: Request) -> dict:
    data = settings.dict()
    if "upstream_api_key" in data:
        data["upstream_api_key"] = "[REDACTED]" if data["upstream_api_key"] else ""
    if "admin_password" in data:
        data["admin_password"] = "[REDACTED]" if data["admin_password"] else ""
    if "nsec" in data:
        data["nsec"] = "[REDACTED]" if data["nsec"] else ""
    return data


class SettingsUpdate(BaseModel):
    __root__: dict[str, object]


class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str


@admin_router.patch("/api/settings", dependencies=[Depends(require_admin_api)])
async def update_settings(request: Request, update: SettingsUpdate) -> dict:
    # Remove sensitive fields from general settings update
    settings_data = update.__root__.copy()
    sensitive_fields = ["admin_password", "upstream_api_key", "nsec"]
    for field in sensitive_fields:
        if field in settings_data:
            del settings_data[field]

    async with create_session() as session:
        new_settings = await SettingsService.update(settings_data, session)
    data = new_settings.dict()
    if "upstream_api_key" in data:
        data["upstream_api_key"] = "[REDACTED]" if data["upstream_api_key"] else ""
    if "admin_password" in data:
        data["admin_password"] = "[REDACTED]" if data["admin_password"] else ""
    if "nsec" in data:
        data["nsec"] = "[REDACTED]" if data["nsec"] else ""
    return data


@admin_router.patch("/api/password", dependencies=[Depends(require_admin_api)])
async def update_password(request: Request, password_update: PasswordUpdate) -> dict:
    current_password = settings.admin_password

    if not current_password:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    if password_update.current_password != current_password:
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Validate new password
    new_password = password_update.new_password.strip()
    if len(new_password) < 6:
        raise HTTPException(
            status_code=400, detail="New password must be at least 6 characters"
        )

    # Update password
    async with create_session() as session:
        await SettingsService.update({"admin_password": new_password}, session)

    return {"ok": True, "message": "Password updated successfully"}


class SetupRequest(BaseModel):
    password: str


@admin_router.post("/api/setup")
async def initial_setup(request: Request, payload: SetupRequest) -> dict[str, object]:
    if settings.admin_password:
        raise HTTPException(status_code=409, detail="Admin password already set")
    pw = (payload.password or "").strip()
    if len(pw) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )
    async with create_session() as session:
        await SettingsService.update({"admin_password": pw}, session)
    return {"ok": True}


class AdminLoginRequest(BaseModel):
    password: str


@admin_router.post("/api/login")
async def admin_login(
    request: Request, payload: AdminLoginRequest
) -> dict[str, object]:
    admin_pw = settings.admin_password

    if not admin_pw:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    if payload.password != admin_pw:
        raise HTTPException(status_code=401, detail="Invalid password")

    token = secrets.token_urlsafe(32)
    expiry_timestamp = (
        int(datetime.now(timezone.utc).timestamp()) + ADMIN_SESSION_DURATION
    )
    admin_sessions[token] = expiry_timestamp

    expired_tokens = [
        t
        for t, exp in admin_sessions.items()
        if exp <= int(datetime.now(timezone.utc).timestamp())
    ]
    for t in expired_tokens:
        del admin_sessions[t]

    return {"ok": True, "token": token, "expires_in": ADMIN_SESSION_DURATION}


@admin_router.post("/api/logout", dependencies=[Depends(require_admin_api)])
async def admin_logout(request: Request) -> dict[str, object]:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        if token in admin_sessions:
            del admin_sessions[token]

    return {"ok": True}


class WithdrawRequest(BaseModel):
    amount: int
    mint_url: str | None = None
    unit: str = "sat"


@admin_router.post("/withdraw", dependencies=[Depends(require_admin_api)])
async def withdraw(
    request: Request, withdraw_request: WithdrawRequest
) -> dict[str, str]:
    # Get wallet and check balance
    from .settings import settings as global_settings

    wallet = await get_wallet(
        withdraw_request.mint_url or global_settings.primary_mint, withdraw_request.unit
    )
    proofs = get_proofs_per_mint_and_unit(
        wallet,
        withdraw_request.mint_url or global_settings.primary_mint,
        withdraw_request.unit,
        not_reserved=True,
    )
    proofs = await slow_filter_spend_proofs(proofs, wallet)
    current_balance = sum(proof.amount for proof in proofs)

    if withdraw_request.amount <= 0:
        raise HTTPException(
            status_code=400, detail="Withdrawal amount must be positive"
        )

    if withdraw_request.amount > current_balance:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    token = await send_token(
        withdraw_request.amount, withdraw_request.unit, withdraw_request.mint_url
    )
    return {"token": token}


class ModelCreate(BaseModel):
    id: str
    name: str
    description: str
    created: int
    context_length: int
    architecture: dict[str, object]
    pricing: dict[str, object]
    per_request_limits: dict[str, object] | None = None
    top_provider: dict[str, object] | None = None
    upstream_provider_id: int | None = None
    canonical_slug: str | None = None
    alias_ids: list[str] | None = None
    enabled: bool = True


@admin_router.post(
    "/api/upstream-providers/{provider_id}/models",
    dependencies=[Depends(require_admin_api)],
)
async def upsert_provider_model(
    provider_id: int, payload: ModelCreate
) -> dict[str, object]:
    print(payload)
    logger.info(
        f"UPSERT_PROVIDER_MODEL called: provider_id={provider_id}, model_id={payload.id}"
    )
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        # Try to get existing model
        existing_row = await session.get(ModelRow, (payload.id, provider_id))

        if existing_row:
            # Update existing model
            logger.info(f"Updating existing model: {payload.id}")
            existing_row.name = payload.name
            existing_row.description = payload.description
            existing_row.created = int(payload.created)
            existing_row.context_length = int(payload.context_length)
            existing_row.architecture = json.dumps(payload.architecture)
            existing_row.pricing = json.dumps(payload.pricing)
            existing_row.sats_pricing = None
            existing_row.per_request_limits = (
                json.dumps(payload.per_request_limits)
                if payload.per_request_limits is not None
                else None
            )
            existing_row.top_provider = (
                json.dumps(payload.top_provider) if payload.top_provider else None
            )
            existing_row.canonical_slug = payload.canonical_slug
            existing_row.alias_ids = (
                json.dumps(payload.alias_ids) if payload.alias_ids else None
            )
            existing_row.enabled = payload.enabled

            session.add(existing_row)
            await session.commit()
            await session.refresh(existing_row)
            row = existing_row

        else:
            # Create new model
            logger.info(f"Creating new model: {payload.id}")
            row = ModelRow(
                id=payload.id,
                name=payload.name,
                description=payload.description,
                created=int(payload.created),
                context_length=int(payload.context_length),
                architecture=json.dumps(payload.architecture),
                pricing=json.dumps(payload.pricing),
                sats_pricing=None,
                per_request_limits=(
                    json.dumps(payload.per_request_limits)
                    if payload.per_request_limits is not None
                    else None
                ),
                top_provider=(
                    json.dumps(payload.top_provider) if payload.top_provider else None
                ),
                canonical_slug=payload.canonical_slug,
                alias_ids=(
                    json.dumps(payload.alias_ids) if payload.alias_ids else None
                ),
                upstream_provider_id=provider_id,
                enabled=payload.enabled,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)

    await refresh_model_maps()
    return _row_to_model(
        row, apply_provider_fee=True, provider_fee=provider.provider_fee
    ).dict()  # type: ignore


@admin_router.patch(
    "/api/upstream-providers/{provider_id}/models/{model_id:path}",
    dependencies=[Depends(require_admin_api)],
)
async def update_provider_model_legacy(
    provider_id: int, model_id: str, payload: ModelCreate
) -> dict[str, object]:
    """Legacy PATCH endpoint - redirects to upsert POST endpoint for backward compatibility."""
    logger.info(
        f"LEGACY_PATCH_UPDATE called: provider_id={provider_id}, model_id={model_id}"
    )
    return await upsert_provider_model(provider_id, payload)


@admin_router.get(
    "/api/upstream-providers/{provider_id}/models/{model_id:path}",
    dependencies=[Depends(require_admin_api)],
)
async def get_provider_model(provider_id: int, model_id: str) -> dict[str, object]:
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        row = await session.get(ModelRow, (model_id, provider_id))
        if not row:
            raise HTTPException(
                status_code=404, detail="Model not found for this provider"
            )
        return _row_to_model(
            row, apply_provider_fee=False, provider_fee=provider.provider_fee
        ).dict()  # type: ignore


@admin_router.delete(
    "/api/upstream-providers/{provider_id}/models/{model_id:path}",
    dependencies=[Depends(require_admin_api)],
)
async def delete_provider_model(provider_id: int, model_id: str) -> dict[str, object]:
    async with create_session() as session:
        row = await session.get(ModelRow, (model_id, provider_id))
        if not row:
            raise HTTPException(
                status_code=404, detail="Model not found for this provider"
            )
        await session.delete(row)
        await session.commit()
    await refresh_model_maps()
    return {"ok": True, "deleted_id": model_id}


@admin_router.delete(
    "/api/upstream-providers/{provider_id}/models",
    dependencies=[Depends(require_admin_api)],
)
async def delete_all_provider_models(provider_id: int) -> dict[str, object]:
    async with create_session() as session:
        result = await session.exec(
            select(ModelRow).where(ModelRow.upstream_provider_id == provider_id)
        )  # type: ignore
        rows = result.all()
        for row in rows:
            await session.delete(row)  # type: ignore
        await session.commit()
    await refresh_model_maps()
    return {"ok": True, "deleted": len(rows)}


class BatchOverrideRequest(BaseModel):
    models: list[ModelCreate]


@admin_router.post(
    "/api/upstream-providers/{provider_id}/batch-override",
    dependencies=[Depends(require_admin_api)],
)
async def batch_override_provider_models(
    provider_id: int, payload: BatchOverrideRequest
) -> dict[str, object]:
    """Batch override models for a specific provider."""
    logger.info(
        f"BATCH_OVERRIDE called: provider_id={provider_id}, count={len(payload.models)}"
    )

    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        overridden_count = 0

        for model_data in payload.models:
            # Try to get existing model regardless of whether it's enabled or not
            existing_row = await session.get(ModelRow, (model_data.id, provider_id))

            if existing_row:
                # Update existing
                existing_row.name = model_data.name
                existing_row.description = model_data.description
                existing_row.created = int(model_data.created)
                existing_row.context_length = int(model_data.context_length)
                existing_row.architecture = json.dumps(model_data.architecture)
                existing_row.pricing = json.dumps(model_data.pricing)
                existing_row.sats_pricing = None
                existing_row.per_request_limits = (
                    json.dumps(model_data.per_request_limits)
                    if model_data.per_request_limits is not None
                    else None
                )
                existing_row.top_provider = (
                    json.dumps(model_data.top_provider)
                    if model_data.top_provider
                    else None
                )
                existing_row.canonical_slug = model_data.canonical_slug
                existing_row.alias_ids = (
                    json.dumps(model_data.alias_ids) if model_data.alias_ids else None
                )
                existing_row.enabled = model_data.enabled
                session.add(existing_row)
            else:
                # Create new
                row = ModelRow(
                    id=model_data.id,
                    name=model_data.name,
                    description=model_data.description,
                    created=int(model_data.created),
                    context_length=int(model_data.context_length),
                    architecture=json.dumps(model_data.architecture),
                    pricing=json.dumps(model_data.pricing),
                    sats_pricing=None,
                    per_request_limits=(
                        json.dumps(model_data.per_request_limits)
                        if model_data.per_request_limits is not None
                        else None
                    ),
                    top_provider=(
                        json.dumps(model_data.top_provider)
                        if model_data.top_provider
                        else None
                    ),
                    canonical_slug=model_data.canonical_slug,
                    alias_ids=(
                        json.dumps(model_data.alias_ids)
                        if model_data.alias_ids
                        else None
                    ),
                    upstream_provider_id=provider_id,
                    enabled=model_data.enabled,
                )
                session.add(row)

            overridden_count += 1

        await session.commit()

    await refresh_model_maps()
    return {
        "ok": True,
        "count": overridden_count,
        "message": f"Successfully batch overridden {overridden_count} models",
    }


class UpstreamProviderCreate(BaseModel):
    provider_type: str
    base_url: str
    api_key: str
    api_version: str | None = None
    enabled: bool = True
    provider_fee: float = 1.01


class UpstreamProviderUpdate(BaseModel):
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_version: str | None = None
    enabled: bool | None = None
    provider_fee: float | None = None


@admin_router.get("/api/upstream-providers", dependencies=[Depends(require_admin_api)])
async def get_upstream_providers() -> list[dict[str, object]]:
    async with create_session() as session:
        result = await session.exec(select(UpstreamProviderRow))
        providers = result.all()
        return [
            {
                "id": p.id,
                "provider_type": p.provider_type,
                "base_url": p.base_url,
                "api_key": "[REDACTED]" if p.api_key else "",
                "api_version": p.api_version,
                "enabled": p.enabled,
                "provider_fee": p.provider_fee,
            }
            for p in providers
        ]


@admin_router.post("/api/upstream-providers", dependencies=[Depends(require_admin_api)])
async def create_upstream_provider(
    payload: UpstreamProviderCreate,
) -> dict[str, object]:
    async with create_session() as session:
        result = await session.exec(
            select(UpstreamProviderRow).where(
                UpstreamProviderRow.base_url == payload.base_url,
                UpstreamProviderRow.api_key == payload.api_key,
            )
        )
        if result.first():
            raise HTTPException(
                status_code=409,
                detail="Provider with this base URL and API key already exists",
            )

        provider = UpstreamProviderRow(
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            api_key=payload.api_key,
            api_version=payload.api_version,
            enabled=payload.enabled,
            provider_fee=payload.provider_fee,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)

    await reinitialize_upstreams()
    await refresh_model_maps()
    return {
        "id": provider.id,
        "provider_type": provider.provider_type,
        "base_url": provider.base_url,
        "api_key": "[REDACTED]",
        "api_version": provider.api_version,
        "enabled": provider.enabled,
        "provider_fee": provider.provider_fee,
    }


@admin_router.get(
    "/api/upstream-providers/{provider_id}", dependencies=[Depends(require_admin_api)]
)
async def get_upstream_provider(provider_id: int) -> dict[str, object]:
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        return {
            "id": provider.id,
            "provider_type": provider.provider_type,
            "base_url": provider.base_url,
            "api_key": "[REDACTED]" if provider.api_key else "",
            "api_version": provider.api_version,
            "enabled": provider.enabled,
            "provider_fee": provider.provider_fee,
        }


@admin_router.patch(
    "/api/upstream-providers/{provider_id}", dependencies=[Depends(require_admin_api)]
)
async def update_upstream_provider(
    provider_id: int, payload: UpstreamProviderUpdate
) -> dict[str, object]:
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        if payload.provider_type is not None:
            provider.provider_type = payload.provider_type
        if payload.base_url is not None:
            provider.base_url = payload.base_url
        if payload.api_key is not None:
            provider.api_key = payload.api_key
        if payload.api_version is not None:
            provider.api_version = payload.api_version
        if payload.enabled is not None:
            provider.enabled = payload.enabled
        if payload.provider_fee is not None:
            provider.provider_fee = payload.provider_fee

        session.add(provider)
        await session.commit()
        await session.refresh(provider)

    await reinitialize_upstreams()
    await refresh_model_maps()
    return {
        "id": provider.id,
        "provider_type": provider.provider_type,
        "base_url": provider.base_url,
        "api_key": "[REDACTED]",
        "api_version": provider.api_version,
        "enabled": provider.enabled,
        "provider_fee": provider.provider_fee,
    }


@admin_router.delete(
    "/api/upstream-providers/{provider_id}", dependencies=[Depends(require_admin_api)]
)
async def delete_upstream_provider(provider_id: int) -> dict[str, object]:
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        await session.delete(provider)
        await session.commit()
    await reinitialize_upstreams()
    await refresh_model_maps()
    return {"ok": True, "deleted_id": provider_id}


@admin_router.get("/api/provider-types", dependencies=[Depends(require_admin_api)])
async def get_provider_types() -> list[dict[str, object]]:
    """Get metadata about available provider types including default URLs and whether they're fixed."""
    from ..upstream import upstream_provider_classes

    return [cls.get_provider_metadata() for cls in upstream_provider_classes]


@admin_router.get(
    "/api/upstream-providers/{provider_id}/models",
    dependencies=[Depends(require_admin_api)],
)
async def get_provider_models(provider_id: int) -> dict[str, object]:
    from ..upstream.helpers import _instantiate_provider

    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        db_models = await list_models(
            session=session,
            upstream_id=provider_id,
            include_disabled=True,
            apply_fees=False,
        )

        upstream_models = []
        upstream_instance = _instantiate_provider(provider)
        if upstream_instance:
            try:
                raw_models = await upstream_instance.fetch_models()
                upstream_models = raw_models
            except Exception as e:
                logger.error(
                    f"Failed to fetch models from {provider.provider_type}: {e}"
                )

        db_model_ids = {model.id for model in db_models}
        filtered_remote_models = [m for m in upstream_models if m.id not in db_model_ids]

        return {
            "provider": {
                "id": provider.id,
                "provider_type": provider.provider_type,
                "base_url": provider.base_url,
            },
            "db_models": [m.dict() for m in db_models],
            "remote_models": [m.dict() for m in filtered_remote_models],
        }


class CreateAccountRequest(BaseModel):
    provider_type: str


@admin_router.post(
    "/api/upstream-providers/create-account",
    dependencies=[Depends(require_admin_api)],
)
async def create_provider_account_by_type(
    payload: CreateAccountRequest,
) -> dict[str, object]:
    """Create a new account with a provider by provider type (before provider exists in DB)."""
    from ..upstream import upstream_provider_classes

    provider_class = next(
        (
            cls
            for cls in upstream_provider_classes
            if cls.provider_type == payload.provider_type
        ),
        None,
    )
    if not provider_class:
        raise HTTPException(status_code=404, detail="Provider type not found")

    try:
        account_data = await provider_class.create_account_static()

        return {
            "ok": True,
            "account_data": account_data,
            "message": "Account created successfully",
        }
    except NotImplementedError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Provider does not support account creation: {str(e)}",
        )
    except Exception as e:
        logger.error(
            f"Failed to create account for provider type {payload.provider_type}: {e}"
        )
        raise HTTPException(status_code=500, detail=str(e))


class TopupRequest(BaseModel):
    amount: int


@admin_router.post(
    "/api/upstream-providers/{provider_id}/topup",
    dependencies=[Depends(require_admin_api)],
)
async def initiate_provider_topup(
    provider_id: int, payload: TopupRequest
) -> dict[str, object]:
    """Initiate a Lightning Network top-up for the upstream provider account."""
    from ..upstream.helpers import _instantiate_provider

    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        upstream_instance = _instantiate_provider(provider)
        if not upstream_instance:
            raise HTTPException(
                status_code=400, detail="Could not instantiate provider"
            )

        try:
            logger.info(
                f"Initiating top-up for provider {provider_id}",
                extra={"amount": payload.amount},
            )
            topup_data = await upstream_instance.initiate_topup(payload.amount)
            logger.info(
                "Top-up initiated successfully",
                extra={
                    "provider_id": provider_id,
                    "invoice_id": topup_data.invoice_id,
                    "amount": topup_data.amount,
                },
            )

            response_data = {
                "ok": True,
                "topup_data": {
                    "invoice_id": topup_data.invoice_id,
                    "payment_request": topup_data.payment_request,
                    "amount": topup_data.amount,
                    "currency": topup_data.currency,
                    "expires_at": topup_data.expires_at,
                    "checkout_url": topup_data.checkout_url,
                },
                "message": "Top-up initiated successfully",
            }
            logger.info("Returning response", extra={"response": response_data})
            return response_data
        except NotImplementedError as e:
            logger.error(f"Provider does not support top-up: {e}")
            raise HTTPException(
                status_code=400, detail=f"Provider does not support top-up: {str(e)}"
            )
        except Exception as e:
            logger.error(
                f"Failed to initiate top-up for provider {provider_id}: {e}",
                extra={"error_type": type(e).__name__, "error": str(e)},
            )
            raise HTTPException(status_code=500, detail=str(e))


@admin_router.get(
    "/api/upstream-providers/{provider_id}/topup/{invoice_id}/status",
    dependencies=[Depends(require_admin_api)],
)
async def check_topup_status(provider_id: int, invoice_id: str) -> dict[str, object]:
    """Check the status of a Lightning Network top-up invoice."""
    from ..upstream.helpers import _instantiate_provider
    from ..upstream.ppqai import PPQAIUpstreamProvider

    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        upstream_instance = _instantiate_provider(provider)
        if not upstream_instance:
            raise HTTPException(
                status_code=400, detail="Could not instantiate provider"
            )

        if not isinstance(upstream_instance, PPQAIUpstreamProvider):
            raise HTTPException(
                status_code=400,
                detail="Provider does not support top-up status checking",
            )

        try:
            paid = await upstream_instance.check_topup_status(invoice_id)
            return {"ok": True, "paid": paid}
        except Exception as e:
            logger.error(
                f"Failed to check top-up status for provider {provider_id}: {e}"
            )
            raise HTTPException(status_code=500, detail=str(e))


@admin_router.get(
    "/api/upstream-providers/{provider_id}/balance",
    dependencies=[Depends(require_admin_api)],
)
async def get_provider_balance(provider_id: int) -> dict[str, object]:
    """Get the current account balance for the upstream provider."""
    from ..upstream.helpers import _instantiate_provider

    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        upstream_instance = _instantiate_provider(provider)
        if not upstream_instance:
            raise HTTPException(
                status_code=400, detail="Could not instantiate provider"
            )

        try:
            balance_data = await upstream_instance.get_balance()
            return {"ok": True, "balance_data": balance_data}
        except NotImplementedError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Provider does not support balance checking: {str(e)}",
            )
        except Exception as e:
            logger.error(f"Failed to fetch balance for provider {provider_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@admin_router.get(
    "/api/openrouter-presets",
    dependencies=[Depends(require_admin_api)],
)
async def get_openrouter_presets() -> list[dict[str, object]]:
    from ..payment.models import async_fetch_openrouter_models

    models_data = await async_fetch_openrouter_models()
    return models_data


@admin_router.get("/api/usage/metrics", dependencies=[Depends(require_admin_api)])
async def get_usage_metrics(
    request: Request,
    interval: int = Query(
        default=15, ge=1, le=1440, description="Time interval in minutes"
    ),
    hours: int = Query(default=24, ge=1, description="Hours of history to analyze"),
) -> dict:
    """Get usage metrics aggregated by time interval."""
    return log_manager.get_usage_metrics(interval=interval, hours=hours)


@admin_router.get("/api/usage/summary", dependencies=[Depends(require_admin_api)])
async def get_usage_summary(
    request: Request,
    hours: int = Query(default=24, ge=1, description="Hours of history to analyze"),
) -> dict:
    """Get summary statistics for the specified time period."""
    return log_manager.get_usage_summary(hours=hours)


@admin_router.get("/api/usage/error-details", dependencies=[Depends(require_admin_api)])
async def get_error_details(
    request: Request,
    hours: int = Query(default=24, ge=1, description="Hours of history to analyze"),
    limit: int = Query(
        default=100, ge=1, le=1000, description="Maximum number of errors to return"
    ),
) -> dict:
    """Get detailed error information."""
    return log_manager.get_error_details(hours=hours, limit=limit)


@admin_router.get(
    "/api/usage/revenue-by-model", dependencies=[Depends(require_admin_api)]
)
async def get_revenue_by_model(
    request: Request,
    hours: int = Query(default=24, ge=1, description="Hours of history to analyze"),
    limit: int = Query(
        default=20, ge=1, le=100, description="Maximum number of models to return"
    ),
) -> dict:
    """
    Get revenue breakdown by model.
    """
    return log_manager.get_revenue_by_model(hours=hours, limit=limit)


@admin_router.get("/api/logs", dependencies=[Depends(require_admin_api)])
async def get_logs_api(
    request: Request,
    date: str | None = None,
    level: str | None = None,
    request_id: str | None = None,
    search: str | None = None,
    status_codes: str | None = Query(None, description="Comma-separated status codes"),
    methods: str | None = Query(None, description="Comma-separated HTTP methods"),
    endpoints: str | None = Query(None, description="Comma-separated endpoints"),
    limit: int = 100,
) -> dict[str, object]:
    """
    Get filtered log entries.

    Args:
        date: Filter by specific date (YYYY-MM-DD)
        level: Filter by log level
        request_id: Filter by request ID
        search: Search text in message and name fields (case-insensitive)
        status_codes: Comma-separated list of HTTP status codes
        methods: Comma-separated list of HTTP methods
        endpoints: Comma-separated list of endpoints
        limit: Maximum number of entries to return

    Returns:
        Dict containing logs and filter metadata
    """
    status_code_list = None
    if status_codes:
        try:
            status_code_list = [int(s.strip()) for s in status_codes.split(",")]
        except ValueError:
            pass

    method_list = [m.strip() for m in methods.split(",")] if methods else None
    endpoint_list = [e.strip() for e in endpoints.split(",")] if endpoints else None

    log_entries = log_manager.search_logs(
        date=date,
        level=level,
        request_id=request_id,
        search_text=search,
        status_codes=status_code_list,
        methods=method_list,
        endpoints=endpoint_list,
        limit=limit,
    )

    return {
        "logs": log_entries,
        "total": len(log_entries),
        "date": date,
        "level": level,
        "request_id": request_id,
        "search": search,
        "status_codes": status_codes,
        "methods": methods,
        "endpoints": endpoints,
        "limit": limit,
    }


@admin_router.get("/api/logs/dates", dependencies=[Depends(require_admin_api)])
async def get_log_dates_api(request: Request) -> dict[str, object]:
    logs_dir = Path("logs")
    dates = []

    if logs_dir.exists():
        log_files = sorted(
            logs_dir.glob("app_*.log"), key=lambda x: x.stat().st_mtime, reverse=True
        )

        for log_file in log_files[:30]:
            try:
                filename = log_file.name
                date_str = filename.replace("app_", "").replace(".log", "")
                dates.append(date_str)
            except Exception:
                continue

    return {"dates": dates}
