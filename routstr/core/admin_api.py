import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlmodel import select

from ..wallet import (
    TRUSTED_MINTS,
    fetch_all_balances,
    get_proofs_per_mint_and_unit,
    get_wallet,
    send_token,
    slow_filter_spend_proofs,
)
from .db import ApiKey, create_session
from .logging import get_logger

logger = get_logger(__name__)

# JWT Configuration
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 1

# Security
security = HTTPBearer()

# Router
admin_api_router = APIRouter(prefix="/v1/admin", tags=["admin"])


# Request/Response Models
class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    token_type: str = "Bearer"


class WithdrawRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Amount to withdraw")
    mint_url: str | None = None
    unit: str = "sat"


class WithdrawResponse(BaseModel):
    token: str
    amount: int
    unit: str
    mint_url: str
    created_at: datetime
    transaction_id: str


class ErrorResponse(BaseModel):
    error: dict[str, Any]


class WalletBalanceSummary(BaseModel):
    owner_balance_sats: int
    total_wallet_balance_sats: int
    total_user_balance_sats: int
    last_updated: datetime


class WalletBalanceDetail(BaseModel):
    mint_url: str
    unit: str
    wallet_balance: int
    user_balance: int
    owner_balance: int
    status: str
    error: str | None = None


class WalletBalanceResponse(BaseModel):
    summary: WalletBalanceSummary
    details: list[WalletBalanceDetail]


class ApiKeyInfo(BaseModel):
    hashed_key: str
    balance_msats: int
    total_spent_msats: int
    total_requests: int
    refund_address: str | None
    created_at: datetime | None
    expires_at: datetime | None
    status: str


class ApiKeysResponse(BaseModel):
    api_keys: list[ApiKeyInfo]
    pagination: dict[str, int]


class LogEntry(BaseModel):
    timestamp: datetime
    level: str
    request_id: str | None
    message: str
    context: dict[str, Any]
    source: dict[str, Any]


class LogsResponse(BaseModel):
    logs: list[LogEntry]
    pagination: dict[str, int]


class SystemStatus(BaseModel):
    status: str
    version: str
    uptime_seconds: int
    last_health_check: datetime
    mints: list[dict[str, Any]]
    database: dict[str, Any]
    admin_password_set: bool


class NodeConfig(BaseModel):
    node_info: dict[str, str]
    cashu_mints: list[str]
    receive_ln_address: str
    pricing: dict[str, Any]
    features: dict[str, Any]
    upstream: dict[str, Any]


# JWT Helper Functions
def create_jwt_token(data: dict[str, Any]) -> tuple[str, datetime]:
    """Create a JWT token with expiration."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    data_copy = data.copy()
    data_copy.update(
        {"exp": expires_at.timestamp(), "iat": datetime.now(timezone.utc).timestamp()}
    )
    token = jwt.encode(data_copy, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, expires_at


def verify_jwt_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict[str, Any]:
    """Verify JWT token and return payload."""
    try:
        payload = jwt.decode(
            credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "TOKEN_EXPIRED", "message": "Token has expired"}},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "INVALID_TOKEN", "message": "Invalid token"}},
        )


# Authentication Endpoints
@admin_api_router.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest) -> LoginResponse:
    """Authenticate and receive a JWT token."""
    admin_password = os.environ.get("ADMIN_PASSWORD", "")

    if not admin_password:
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "CONFIG_ERROR",
                    "message": "Admin password not configured",
                }
            },
        )

    if request.password != admin_password:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {"code": "INVALID_CREDENTIALS", "message": "Invalid password"}
            },
        )

    token, expires_at = create_jwt_token({"sub": "admin", "role": "admin"})

    logger.info("Admin login successful")
    return LoginResponse(token=token, expires_at=expires_at)


@admin_api_router.post("/auth/refresh", response_model=LoginResponse)
async def refresh_token(
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> LoginResponse:
    """Refresh an existing JWT token before expiration."""
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "error": {"code": "FORBIDDEN", "message": "Insufficient permissions"}
            },
        )

    token, expires_at = create_jwt_token(
        {"sub": payload.get("sub", "admin"), "role": "admin"}
    )

    logger.info("Admin token refreshed")
    return LoginResponse(token=token, expires_at=expires_at)


# Wallet Information Endpoints
@admin_api_router.get("/wallet/balance", response_model=WalletBalanceResponse)
async def get_wallet_balance(
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> WalletBalanceResponse:
    """Get comprehensive wallet balance information."""
    try:
        (
            balance_details,
            total_wallet_sats,
            total_user_sats,
            owner_balance,
        ) = await fetch_all_balances()

        details = []
        for detail in balance_details:
            details.append(
                WalletBalanceDetail(
                    mint_url=detail["mint_url"],
                    unit=detail["unit"],
                    wallet_balance=detail.get("wallet_balance", 0),
                    user_balance=detail.get("user_balance", 0),
                    owner_balance=detail.get("owner_balance", 0),
                    status="error" if detail.get("error") else "active",
                    error=detail.get("error"),
                )
            )

        summary = WalletBalanceSummary(
            owner_balance_sats=owner_balance,
            total_wallet_balance_sats=total_wallet_sats,
            total_user_balance_sats=total_user_sats,
            last_updated=datetime.now(timezone.utc),
        )

        return WalletBalanceResponse(summary=summary, details=details)

    except Exception as e:
        logger.error(f"Error fetching wallet balance: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to fetch wallet balance",
                }
            },
        )


@admin_api_router.get("/wallet/balance/{mint_url:path}")
async def get_mint_balance(
    mint_url: str,
    unit: str | None = Query(None),
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> dict[str, Any]:
    """Get balance information for a specific mint."""
    try:
        balance_details, _, _, _ = await fetch_all_balances()

        mint_details = [d for d in balance_details if d["mint_url"] == mint_url]

        if not mint_details:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {"code": "MINT_NOT_FOUND", "message": "Mint not found"}
                },
            )

        if unit:
            mint_details = [d for d in mint_details if d["unit"] == unit]

        units = []
        for detail in mint_details:
            units.append(
                {
                    "unit": detail["unit"],
                    "wallet_balance": detail.get("wallet_balance", 0),
                    "user_balance": detail.get("user_balance", 0),
                    "owner_balance": detail.get("owner_balance", 0),
                    "last_synced": datetime.now(timezone.utc),
                }
            )

        return {"mint_url": mint_url, "units": units}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mint balance: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to fetch mint balance",
                }
            },
        )


# Withdrawal Operations
@admin_api_router.post("/wallet/withdraw", response_model=WithdrawResponse)
async def withdraw(
    request: WithdrawRequest,
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> WithdrawResponse:
    """Create a withdrawal token for the specified amount."""
    try:
        # Get wallet and check balance
        mint_url = request.mint_url or TRUSTED_MINTS[0]
        wallet = await get_wallet(mint_url, request.unit)
        proofs = await get_proofs_per_mint_and_unit(
            wallet,
            mint_url,
            request.unit,
            not_reserved=True,
        )
        proofs = await slow_filter_spend_proofs(proofs, wallet)
        current_balance = sum(proof.amount for proof in proofs)

        if request.amount > current_balance:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "INSUFFICIENT_BALANCE",
                        "message": "Insufficient wallet balance",
                        "details": {
                            "requested": request.amount,
                            "available": current_balance,
                        },
                    }
                },
            )

        token = await send_token(request.amount, request.unit, request.mint_url)
        transaction_id = f"txn_{secrets.token_hex(8)}"

        logger.info(
            f"Admin withdrawal: {request.amount} {request.unit} from {mint_url}"
        )

        return WithdrawResponse(
            token=token,
            amount=request.amount,
            unit=request.unit,
            mint_url=mint_url,
            created_at=datetime.now(timezone.utc),
            transaction_id=transaction_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing withdrawal: {e}")
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "WITHDRAWAL_FAILED", "message": str(e)}},
        )


# API Key Management
@admin_api_router.get("/keys", response_model=ApiKeysResponse)
async def get_api_keys(
    status: str = Query("all", regex="^(active|expired|all)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> ApiKeysResponse:
    """Get all API keys and their balances."""
    try:
        async with create_session() as session:
            # Build query
            query = select(ApiKey)

            # Apply status filter
            current_time = datetime.now(timezone.utc).timestamp()
            if status == "active":
                # Filter for active keys - we'll filter in Python after fetching
                pass
            elif status == "expired":
                # Filter for expired keys - we'll filter in Python after fetching
                pass

            # Get all keys first
            result = await session.exec(query)
            all_keys = result.all()

            # Filter based on status
            if status == "active":
                api_keys = [
                    key
                    for key in all_keys
                    if key.key_expiry_time is None or key.key_expiry_time > current_time
                ]
            elif status == "expired":
                api_keys = [
                    key
                    for key in all_keys
                    if key.key_expiry_time is not None
                    and key.key_expiry_time <= current_time
                ]
            else:
                api_keys = list(all_keys)

            # Get total count after filtering
            total = len(api_keys)

            # Apply pagination
            api_keys = api_keys[offset : offset + limit]

        # Format response
        api_key_list = []
        for key in api_keys:
            # Determine status
            if key.key_expiry_time and key.key_expiry_time <= current_time:
                key_status = "expired"
            else:
                key_status = "active"

            api_key_list.append(
                ApiKeyInfo(
                    hashed_key=key.hashed_key,
                    balance_msats=key.balance,
                    total_spent_msats=key.total_spent,
                    total_requests=key.total_requests,
                    refund_address=key.refund_address,
                    created_at=None,
                    expires_at=datetime.fromtimestamp(
                        key.key_expiry_time, tz=timezone.utc
                    )
                    if key.key_expiry_time
                    else None,
                    status=key_status,
                )
            )

        return ApiKeysResponse(
            api_keys=api_key_list,
            pagination={"total": total, "limit": limit, "offset": offset},
        )

    except Exception as e:
        logger.error(f"Error fetching API keys: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to fetch API keys",
                }
            },
        )


# Logging and Monitoring
@admin_api_router.get("/logs")
async def get_logs(
    request_id: str | None = Query(None),
    level: str | None = Query(None, regex="^(ERROR|WARNING|INFO|DEBUG)$"),
    from_time: datetime | None = Query(None),
    to_time: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> dict[str, Any]:
    """Search and retrieve logs."""
    try:
        log_entries = []
        logs_dir = Path("logs")

        if not logs_dir.exists():
            return {
                "logs": [],
                "pagination": {"total": 0, "limit": limit, "offset": offset},
            }

        # Get log files within time range
        log_files = sorted(
            logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True
        )

        # Filter log files by date if needed
        if from_time or to_time:
            filtered_files = []
            for log_file in log_files:
                file_time = datetime.fromtimestamp(
                    log_file.stat().st_mtime, tz=timezone.utc
                )
                if from_time and file_time < from_time:
                    continue
                if to_time and file_time > to_time:
                    continue
                filtered_files.append(log_file)
            log_files = filtered_files

        # Search through log files
        for log_file in log_files[:7]:  # Limit to last 7 days
            try:
                with open(log_file, "r") as f:
                    for line in f:
                        # Apply filters
                        if request_id and request_id not in line:
                            continue

                        try:
                            log_data = json.loads(line.strip())

                            # Filter by level
                            if level and log_data.get("levelname") != level:
                                continue

                            # Filter by time
                            log_time = datetime.fromisoformat(
                                log_data.get("asctime", "")
                            )
                            if from_time and log_time < from_time:
                                continue
                            if to_time and log_time > to_time:
                                continue

                            log_entries.append(log_data)

                        except json.JSONDecodeError:
                            # Include raw lines if they match filters
                            if not level or level in line:
                                log_entries.append(
                                    {
                                        "asctime": str(datetime.now(timezone.utc)),
                                        "levelname": "INFO",
                                        "message": line.strip(),
                                        "raw": True,
                                    }
                                )

            except Exception as e:
                logger.error(f"Error reading log file {log_file}: {e}")

        # Sort by timestamp
        log_entries.sort(key=lambda x: x.get("asctime", ""), reverse=True)

        # Apply pagination
        total = len(log_entries)
        paginated_entries = log_entries[offset : offset + limit]

        # Format entries
        formatted_logs = []
        for entry in paginated_entries:
            if entry.get("raw"):
                formatted_logs.append(
                    LogEntry(
                        timestamp=datetime.now(timezone.utc),
                        level="INFO",
                        request_id=None,
                        message=entry["message"],
                        context={},
                        source={},
                    )
                )
            else:
                formatted_logs.append(
                    LogEntry(
                        timestamp=datetime.fromisoformat(
                            entry.get("asctime", str(datetime.now(timezone.utc)))
                        ),
                        level=entry.get("levelname", "INFO"),
                        request_id=entry.get("request_id"),
                        message=entry.get("message", ""),
                        context={
                            k: v
                            for k, v in entry.items()
                            if k
                            not in [
                                "asctime",
                                "levelname",
                                "message",
                                "pathname",
                                "lineno",
                                "name",
                                "request_id",
                            ]
                        },
                        source={
                            "file": entry.get("pathname", ""),
                            "line": entry.get("lineno", 0),
                            "function": entry.get("funcName", ""),
                        },
                    )
                )

        return {
            "logs": formatted_logs,
            "pagination": {"total": total, "limit": limit, "offset": offset},
        }

    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {"code": "INTERNAL_ERROR", "message": "Failed to fetch logs"}
            },
        )


@admin_api_router.get("/logs/{request_id}")
async def get_request_logs(
    request_id: str,
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> dict[str, Any]:
    """Get all logs for a specific request ID."""
    # Use the general logs endpoint with request_id filter
    logs_response = await get_logs(
        request_id=request_id,
        limit=1000,
        offset=0,
        payload=payload,
    )

    logs = logs_response["logs"]

    # Calculate summary
    error_count = sum(1 for log in logs if log["level"] == "ERROR")
    warning_count = sum(1 for log in logs if log["level"] == "WARNING")

    # Calculate duration (approximate from first to last log)
    if logs:
        first_log = min(logs, key=lambda x: x["timestamp"])
        last_log = max(logs, key=lambda x: x["timestamp"])
        duration_ms = int(
            (last_log["timestamp"] - first_log["timestamp"]).total_seconds() * 1000
        )
    else:
        duration_ms = 0

    return {
        "request_id": request_id,
        "logs": logs,
        "summary": {
            "total_logs": len(logs),
            "error_count": error_count,
            "warning_count": warning_count,
            "duration_ms": duration_ms,
        },
    }


# System Status
@admin_api_router.get("/status", response_model=SystemStatus)
async def get_status(
    payload: dict[str, Any] = Depends(verify_jwt_token),
) -> SystemStatus:
    """Get system health and status information."""
    try:
        # Get uptime (approximate from process start)
        import time

        process_start_time = time.time() - time.process_time()
        uptime_seconds = int(time.time() - process_start_time)

        # Check mints status
        mints_status = []
        for mint_url in TRUSTED_MINTS:
            try:
                # Get balance to check connectivity
                balance_details, _, _, _ = await fetch_all_balances()
                mint_details = [
                    d
                    for d in balance_details
                    if d["mint_url"] == mint_url and not d.get("error")
                ]

                if mint_details:
                    units = list(set(d["unit"] for d in mint_details))
                    mints_status.append(
                        {
                            "url": mint_url,
                            "status": "connected",
                            "units": units,
                            "last_sync": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                else:
                    mints_status.append(
                        {
                            "url": mint_url,
                            "status": "error",
                            "units": [],
                            "last_sync": "",
                        }
                    )
            except Exception:
                mints_status.append(
                    {
                        "url": mint_url,
                        "status": "error",
                        "units": [],
                        "last_sync": "",
                    }
                )

        # Get database status
        async with create_session() as session:
            result = await session.exec(select(ApiKey))
            api_keys = result.all()
            active_keys = sum(
                1
                for key in api_keys
                if key.key_expiry_time is None
                or key.key_expiry_time > datetime.now(timezone.utc).timestamp()
            )

            # Count today's requests (simplified since we don't have created_at)
            total_requests_today = sum(key.total_requests for key in api_keys)

        return SystemStatus(
            status="healthy",
            version=__version__,
            uptime_seconds=uptime_seconds,
            last_health_check=datetime.now(timezone.utc),
            mints=mints_status,
            database={
                "status": "connected",
                "active_api_keys": active_keys,
                "total_requests_today": total_requests_today,
            },
            admin_password_set=bool(os.environ.get("ADMIN_PASSWORD")),
        )

    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to get system status",
                }
            },
        )


# Configuration
@admin_api_router.get("/config", response_model=NodeConfig)
async def get_config(payload: dict[str, Any] = Depends(verify_jwt_token)) -> NodeConfig:
    """Get current node configuration."""
    return NodeConfig(
        node_info={
            "name": os.environ.get("NAME", f"ARoutstrNode v{__version__}"),
            "description": os.environ.get("DESCRIPTION", "A Routstr Node"),
            "npub": os.environ.get("NPUB", ""),
            "http_url": os.environ.get("HTTP_URL", ""),
            "onion_url": os.environ.get("ONION_URL", ""),
        },
        cashu_mints=os.environ.get(
            "CASHU_MINTS", "https://mint.minibits.cash/Bitcoin"
        ).split(","),
        receive_ln_address=os.environ.get("RECEIVE_LN_ADDRESS", ""),
        pricing={
            "cost_per_request_msats": int(os.environ.get("COST_PER_REQUEST", "1"))
            * 1000,
            "cost_per_1k_input_tokens_msats": int(
                os.environ.get("COST_PER_1K_INPUT_TOKENS", "0")
            )
            * 1000,
            "cost_per_1k_output_tokens_msats": int(
                os.environ.get("COST_PER_1K_OUTPUT_TOKENS", "0")
            )
            * 1000,
            "model_based_pricing": os.environ.get(
                "MODEL_BASED_PRICING", "false"
            ).lower()
            == "true",
            "exchange_fee": float(os.environ.get("EXCHANGE_FEE", "1.005")),
            "upstream_provider_fee": float(
                os.environ.get("UPSTREAM_PROVIDER_FEE", "1.05")
            ),
        },
        features={
            "cors_origins": os.environ.get("CORS_ORIGINS", "*").split(","),
            "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),
            "enable_console_logging": os.environ.get(
                "ENABLE_CONSOLE_LOGGING", "true"
            ).lower()
            == "true",
        },
        upstream={
            "base_url": os.environ.get("BASE_URL", "https://openrouter.ai/api/v1"),
            "upstream_base_url": os.environ.get("UPSTREAM_BASE_URL", ""),
            "has_upstream_api_key": bool(os.environ.get("UPSTREAM_API_KEY")),
        },
    )
