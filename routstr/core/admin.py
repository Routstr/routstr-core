import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import select

from ..payment.models import _row_to_model, list_models
from ..proxy import refresh_model_maps, reinitialize_upstreams
from ..search import search_logs
from ..wallet import (
    fetch_all_balances,
    get_proofs_per_mint_and_unit,
    get_wallet,
    send_token,
    slow_filter_spend_proofs,
)
from .db import ApiKey, ModelRow, UpstreamProviderRow, create_session
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


def is_admin_authenticated(request: Request) -> bool:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        expiry = admin_sessions.get(token)
        if expiry and expiry > int(datetime.now(timezone.utc).timestamp()):
            return True

    return False


@admin_router.get(
    "/partials/balances",
    dependencies=[Depends(require_admin_api)],
    response_class=HTMLResponse,
)
async def partial_balances(request: Request) -> str:
    (
        balance_details,
        total_wallet_balance_sats,
        total_user_balance_sats,
        owner_balance,
    ) = await fetch_all_balances()
    # Provide JSON for client usage
    # Embed a script tag to update balanceDetails and the UI markup
    rows = "".join(
        [
            f"""<div class="currency-row {"error-row" if detail.get("error") else ""}">
                <div class="mint-name">{detail["mint_url"].replace("https://", "").replace("http://", "")} • {detail["unit"].upper()}</div>
                <div class="balance-num">{detail["wallet_balance"] if not detail.get("error") else "error"}</div>
                <div class="balance-num">{detail["user_balance"] if not detail.get("error") else "-"}</div>
                <div class="balance-num {"owner-positive" if detail["owner_balance"] > 0 else ""}">{detail["owner_balance"] if not detail.get("error") else "-"}</div>
            </div>"""
            for detail in balance_details
            if detail.get("wallet_balance", 0) > 0 or detail.get("error")
        ]
    )
    return f"""
        <h2>Cashu Wallet Balance</h2>
        <div class="balance-item">
            <span class="balance-label">Your Balance (Total)</span>
            <span class="balance-value balance-primary">{owner_balance} sats</span>
        </div>
        <div class="balance-item">
            <span class="balance-label">Total Wallet</span>
            <span class="balance-value">{total_wallet_balance_sats} sats</span>
        </div>
        <div class="balance-item">
            <span class="balance-label">User Balance</span>
            <span class="balance-value">{total_user_balance_sats} sats</span>
        </div>
        <p style="margin-top: 1rem; font-size: 0.9rem; color: #718096;">Your balance = Total wallet - User balance</p>
        <div class="currency-grid">
            <div class="currency-row currency-header">
                <div>Mint / Unit</div>
                <div class="balance-num">Wallet</div>
                <div class="balance-num">Users</div>
                <div class="balance-num">Owner</div>
            </div>
            {rows}
        </div>
        <script>balanceDetails = {json.dumps(balance_details)};</script>
    """


@admin_router.get(
    "/partials/apikeys",
    dependencies=[Depends(require_admin_api)],
    response_class=HTMLResponse,
)
async def partial_apikeys(request: Request) -> str:
    async with create_session() as session:
        result = await session.exec(select(ApiKey))
        api_keys = result.all()

    def fmt_time(ts: int | None) -> str:
        if ts is None:
            return ""
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return f"{ts} ({dt.strftime('%Y-%m-%d %H:%M:%S')} UTC)"

    rows = "".join(
        [
            f"<tr><td>{key.hashed_key}</td><td>{key.balance}</td><td>{key.total_spent}</td><td>{key.total_requests}</td><td>{key.refund_address}</td><td>{fmt_time(key.key_expiry_time)}</td></tr>"
            for key in api_keys
        ]
    )
    return f"""
        <h2>Temporary Balances</h2>
        <table>
            <tr>
                <th>Hashed Key</th>
                <th>Balance (mSats)</th>
                <th>Total Spent (mSats)</th>
                <th>Total Requests</th>
                <th>Refund Address</th>
                <th>Refund Time</th>
            </tr>
            {rows}
        </table>
    """


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
        }
        for key in api_keys
    ]


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


def login_form() -> str:
    return """<!DOCTYPE html>
    <html>
        <head>
            <style>
                * { margin: 0; padding: 0; box-sizing: border-box; }
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background: #f5f7fa; }
                .login-card { background: white; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); width: 320px; }
                h2 { margin-bottom: 1.5rem; color: #1a202c; text-align: center; }
                input[type="password"] { width: 100%; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px; font-size: 16px; transition: border 0.2s; }
                input[type="password"]:focus { outline: none; border-color: #4299e1; }
                button { width: 100%; padding: 12px; margin-top: 1rem; background: #4299e1; color: white; border: none; border-radius: 6px; font-size: 16px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
                button:hover { background: #3182ce; transform: translateY(-1px); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            </style>
            <script>
                function handleSubmit(e) {
                    e.preventDefault();
                    const password = document.getElementById('password').value;
                    document.cookie = `admin_password=${password}; path=/; max-age=86400`;
                    window.location.reload();
                }
            </script>
        </head>
        <body>
            <div class="login-card">
                <h2>🔐 Admin Login</h2>
                <form onsubmit="handleSubmit(event)">
                    <input type="password" id="password" placeholder="Admin Password" required autofocus>
                    <button type="submit">Login</button>
                </form>
            </div>
        </body>
    </html>
    """


def setup_form() -> str:
    return """<!DOCTYPE html>
    <html>
        <head>
            <style>
                * { margin: 0; padding: 0; box-sizing: border-box; }
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background: #f5f7fa; }
                .setup-card { background: white; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); width: 360px; }
                h2 { margin-bottom: 1.25rem; color: #1a202c; text-align: center; }
                p { color: #4a5568; font-size: 0.95rem; margin-bottom: 1rem; text-align: center; }
                input[type="password"] { width: 100%; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px; font-size: 16px; transition: border 0.2s; }
                input[type="password"]:focus { outline: none; border-color: #4299e1; }
                button { width: 100%; padding: 12px; margin-top: 1rem; background: #4299e1; color: white; border: none; border-radius: 6px; font-size: 16px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
                button:hover { background: #3182ce; transform: translateY(-1px); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
                .error { color: #e53e3e; margin-top: 10px; text-align: center; }
            </style>
            <script>
                async function handleSetupSubmit(e) {
                    e.preventDefault();
                    const pw = document.getElementById('password').value;
                    const pw2 = document.getElementById('password2').value;
                    const err = document.getElementById('error');
                    err.textContent = '';
                    if (pw.length < 8) { err.textContent = 'Password must be at least 8 characters'; return; }
                    if (pw !== pw2) { err.textContent = 'Passwords do not match'; return; }
                    try {
                        const resp = await fetch('/admin/api/setup', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'same-origin',
                            body: JSON.stringify({ password: pw })
                        });
                        if (!resp.ok) {
                            let msg = 'Failed to set password';
                            try { const j = await resp.json(); if (j && j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch(_) {}
                            throw new Error(msg);
                        }
                        document.cookie = `admin_password=${pw}; path=/; max-age=86400; samesite=lax`;
                        window.location.replace('/admin');
                    } catch (e) {
                        err.textContent = e.message || String(e);
                    }
                }
            </script>
        </head>
        <body>
            <div class="setup-card">
                <h2>🔧 Initial Admin Setup</h2>
                <p>Create a secure password for your admin dashboard.</p>
                <form onsubmit="handleSetupSubmit(event)">
                    <input type="password" id="password" placeholder="New Password" required autofocus>
                    <input type="password" id="password2" placeholder="Confirm Password" required style="margin-top:10px;">
                    <button type="submit">Set Password</button>
                    <div id="error" class="error"></div>
                </form>
            </div>
        </body>
    </html>
    """


def info(content: str) -> str:
    return f"""<!DOCTYPE html>
    <html>
        <head>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background: #f5f7fa; }}
                .info-card {{ background: white; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); max-width: 500px; text-align: center; }}
                .info-card p {{ color: #4a5568; font-size: 1.1rem; }}
            </style>
        </head>
        <body>
            <div class="info-card">
                <p>{content}</p>
            </div>
        </body>
    </html>
    """


def admin_auth() -> str:
    admin_pw = settings.admin_password
    if admin_pw == "":
        return setup_form()
    else:
        return login_form()


async def dashboard(request: Request) -> str:
    return (
        f"""<!DOCTYPE html>
    <html>
        <head>
        <style>{DASHBOARD_CSS}</style>
            <script src="https://unpkg.com/htmx.org@1.9.12"></script>
        """
        + """<!--html-->
            <script>
                let balanceDetails = [];

                async function openWithdrawModal() {
                    const modal = document.getElementById('withdraw-modal');
                    try {
                        if (!balanceDetails.length) {
                            const resp = await fetch('/admin/api/balances', { credentials: 'same-origin' });
                            if (!resp.ok) throw new Error('HTTP ' + resp.status);
                            balanceDetails = await resp.json();
                        }
                        const select = document.getElementById('mint-unit-select');
                        select.innerHTML = '';
                        balanceDetails
                            .filter(d => !d.error && d.wallet_balance > 0)
                            .forEach(d => {
                                const opt = document.createElement('option');
                                opt.value = `${d.mint_url}|${d.unit}`;
                                opt.textContent = `${d.mint_url.replace("https://", "").replace("http://", "")} • ${d.unit.toUpperCase()} (${d.owner_balance})`;
                                select.appendChild(opt);
                            });
                        updateWithdrawForm();
                    } catch (e) {
                        alert('Failed to load balances: ' + e.message);
                    }
                    modal.style.display = 'block';
                }

                function closeWithdrawModal() {
                    const modal = document.getElementById('withdraw-modal');
                    modal.style.display = 'none';
                }

                function updateWithdrawForm() {
                    const select = document.getElementById('mint-unit-select');
                    const selectedValue = select.value;
                    if (!selectedValue) return;

                    const [mint, unit] = selectedValue.split('|');
                    const detail = balanceDetails.find(d => d.mint_url === mint && d.unit === unit);

                    if (detail) {
                        const amountInput = document.getElementById('withdraw-amount');
                        const maxSpan = document.getElementById('max-amount');
                        const recommendedSpan = document.getElementById('recommended-amount');

                        amountInput.max = detail.wallet_balance;
                        amountInput.value = detail.owner_balance > 0 ? detail.owner_balance : 0;
                        maxSpan.textContent = `${detail.wallet_balance} ${unit}`;
                        recommendedSpan.textContent = `${detail.owner_balance} ${unit}`;

                        checkAmount();
                    }
                }

                function checkAmount() {
                    const select = document.getElementById('mint-unit-select');
                    const selectedValue = select.value;
                    if (!selectedValue) return;

                    const [mint, unit] = selectedValue.split('|');
                    const detail = balanceDetails.find(d => d.mint_url === mint && d.unit === unit);

                    if (detail) {
                        const amount = parseInt(document.getElementById('withdraw-amount').value) || 0;
                        const warning = document.getElementById('withdraw-warning');

                        if (amount > detail.owner_balance && amount <= detail.wallet_balance) {
                            warning.style.display = 'block';
                        } else {
                            warning.style.display = 'none';
                        }
                    }
                }

                async function performWithdraw() {
                    const amount = parseInt(document.getElementById('withdraw-amount').value);
                    const select = document.getElementById('mint-unit-select');
                    const selectedValue = select.value;
                    const button = document.getElementById('confirm-withdraw-btn');
                    const tokenResult = document.getElementById('token-result');

                    if (!selectedValue) {
                        alert('Please select a mint and unit');
                        return;
                    }

                    const [mint, unit] = selectedValue.split('|');
                    const detail = balanceDetails.find(d => d.mint_url === mint && d.unit === unit);

                    if (!amount || amount <= 0) {
                        alert('Please enter a valid amount');
                        return;
                    }

                    if (amount > detail.wallet_balance) {
                        alert('Amount exceeds wallet balance');
                        return;
                    }

                    button.disabled = true;
                    button.textContent = 'Withdrawing...';

                    try {
                        const response = await fetch('/admin/withdraw', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                            },
                            credentials: 'same-origin',
                            body: JSON.stringify({
                                amount: amount,
                                mint_url: mint,
                                unit: unit
                            })
                        });

                        if (response.ok) {
                            const data = await response.json();
                            document.getElementById('token-text').textContent = data.token;
                            tokenResult.style.display = 'block';
                            closeWithdrawModal();
                        } else {
                            const errorData = await response.json();
                            alert('Failed to withdraw balance: ' + (errorData.detail || 'Unknown error'));
                        }
                    } catch (error) {
                        alert('Error: ' + error.message);
                    } finally {
                        button.disabled = false;
                        button.textContent = 'Withdraw';
                    }
                }

                function copyToken() {
                    const tokenText = document.getElementById('token-text');
                    navigator.clipboard.writeText(tokenText.textContent).then(() => {
                        const copyBtn = document.getElementById('copy-btn');
                        const originalText = copyBtn.textContent;
                        copyBtn.textContent = 'Copied!';
                        setTimeout(() => {
                            copyBtn.textContent = originalText;
                        }, 2000);
                    }).catch(err => {
                        alert('Failed to copy token');
                    });
                }

                function refreshPage() {
                    window.location.reload();
                }

                function openInvestigateModal() {
                    const modal = document.getElementById('investigate-modal');
                    modal.style.display = 'block';
                }

                function closeInvestigateModal() {
                    const modal = document.getElementById('investigate-modal');
                    modal.style.display = 'none';
                }

                function investigateLogs() {
                    const requestId = document.getElementById('request-id').value.trim();
                    if (!requestId) {
                        alert('Please enter a Request ID');
                        return;
                    }
                    window.location.href = `/admin/logs/${requestId}`;
                }

                async function openSettingsModal() {
                    const modal = document.getElementById('settings-modal');
                    const textarea = document.getElementById('settings-json');
                    const errorBox = document.getElementById('settings-error');
                    errorBox.style.display = 'none';
                    errorBox.textContent = '';
                    try {
                        const resp = await fetch('/admin/api/settings', { credentials: 'same-origin' });
                        if (!resp.ok) {
                            throw new Error('HTTP ' + resp.status);
                        }
                        const data = await resp.json();
                        textarea.value = JSON.stringify(data, null, 2);
                    } catch (e) {
                        errorBox.style.display = 'block';
                        errorBox.textContent = 'Failed to load settings: ' + e.message;
                        textarea.value = '{}';
                    }
                    modal.style.display = 'block';
                }

                function closeSettingsModal() {
                    const modal = document.getElementById('settings-modal');
                    modal.style.display = 'none';
                }

                async function saveSettings() {
                    const textarea = document.getElementById('settings-json');
                    const errorBox = document.getElementById('settings-error');
                    errorBox.style.display = 'none';
                    errorBox.style.color = '#e53e3e';
                    let payload;
                    try {
                        payload = JSON.parse(textarea.value);
                    } catch (e) {
                        errorBox.style.display = 'block';
                        errorBox.textContent = 'Invalid JSON: ' + e.message;
                        return;
                    }

                    ['upstream_api_key', 'admin_password', 'nsec'].forEach(k => {
                        if (payload && payload[k] === '[REDACTED]') { delete payload[k]; }
                    });

                    try {
                        const resp = await fetch('/admin/api/settings', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'same-origin',
                            body: JSON.stringify(payload)
                        });
                        if (resp.ok) {
                            const data = await resp.json();
                            textarea.value = JSON.stringify(data, null, 2);
                            errorBox.style.display = 'block';
                            errorBox.style.color = '#22c55e';
                            errorBox.textContent = 'Saved successfully';
                            setTimeout(() => { errorBox.style.display = 'none'; }, 2000);
                        } else {
                            let errText = 'Failed to save settings';
                            try {
                                const err = await resp.json();
                                if (err && err.detail) {
                                    errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
                                }
                            } catch (_ignored) {}
                            errorBox.style.display = 'block';
                            errorBox.style.color = '#e53e3e';
                            errorBox.textContent = errText;
                        }
                    } catch (e) {
                        errorBox.style.display = 'block';
                        errorBox.style.color = '#e53e3e';
                        errorBox.textContent = 'Request failed: ' + e.message;
                    }
                }

                window.onclick = function(event) {
                    const withdrawModal = document.getElementById('withdraw-modal');
                    const investigateModal = document.getElementById('investigate-modal');
                    const settingsModal = document.getElementById('settings-modal');
                    if (event.target == withdrawModal) {
                        closeWithdrawModal();
                    } else if (event.target == investigateModal) {
                        closeInvestigateModal();
                    } else if (event.target == settingsModal) {
                        closeSettingsModal();
                    }
                }
            </script>
            </head>
            """
        + """<!--html-->
        <body>
            <h1>Admin Dashboard</h1>

            <div class="balance-card" id="balances-card"
                 hx-get="/admin/partials/balances"
                 hx-trigger="load"
                 hx-swap="innerHTML">
                <div style="color:#718096;">Loading balances…</div>
            </div>

            <button id="withdraw-btn" onclick="openWithdrawModal()">
                💸 Withdraw Balance
            </button>
            <button class="refresh-btn" onclick="refreshPage()">
                🔄 Refresh
            </button>
            <button class="investigate-btn" onclick="openInvestigateModal()">
                🔍 Investigate Logs
            </button>
            <button onclick="window.location.href='/admin/upstream-providers'">
                🔌 Upstream Providers
            </button>
            <button onclick="openSettingsModal()">
                ⚙️ Settings
            </button>

            <div id="withdraw-modal" class="modal">
                <div class="modal-content">
                    <span class="close" onclick="closeWithdrawModal()">&times;</span>
                    <h3>Withdraw Balance</h3>
                    <p>Select mint and currency:</p>
                    <select id="mint-unit-select" onchange="updateWithdrawForm()"></select>
                    <p>Enter amount to withdraw:</p>
                    <input type="number" id="withdraw-amount" min="1" placeholder="Amount" oninput="checkAmount()">
                    <p>Maximum: <span id="max-amount">-</span></p>
                    <p>Your recommended balance: <span id="recommended-amount">-</span></p>
                    <div id="withdraw-warning" class="warning" style="display: none;">
                        ⚠️ Warning: Withdrawing more than your balance will use user funds!
                    </div>
                    <button id="confirm-withdraw-btn" onclick="performWithdraw()">💸 Withdraw</button>
                    <button onclick="closeWithdrawModal()" style="background-color: #718096;">Cancel</button>
                </div>
            </div>

            <div id="settings-modal" class="modal">
                <div class="modal-content">
                    <span class="close" onclick="closeSettingsModal()">&times;</span>
                    <h3>Edit Settings (JSON)</h3>
                    <p style="font-size: 0.9rem; color: #718096; margin-bottom: 8px;">Values shown as "[REDACTED]" will remain unchanged if left as-is.</p>
                    <textarea id="settings-json" placeholder="{{}}" style="width: 100%; min-height: 280px; font-family: 'Monaco', monospace; font-size: 13px; background: #f8fafc; color: #2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>
                    <div id="settings-error" style="display: none; margin-top: 8px; font-size: 0.95rem; color: #e53e3e;"></div>
                    <div style="margin-top: 12px; display: flex; gap: 10px;">
                        <button onclick="saveSettings()">💾 Save</button>
                        <button onclick="closeSettingsModal()" style="background-color: #718096;">Cancel</button>
                    </div>
                </div>
            </div>

            <div id="investigate-modal" class="modal">
                <div class="modal-content">
                    <span class="close" onclick="closeInvestigateModal()">&times;</span>
                    <h3>Investigate Logs</h3>
                    <p>Enter Request ID to investigate:</p>
                    <input type="text" id="request-id" placeholder="e.g., 123e4567-e89b-12d3-a456-426614174000" style="width: 100%; padding: 8px; margin: 10px 0; border: 1px solid #ddd; border-radius: 4px;">
                    <button onclick="investigateLogs()">🔍 Investigate</button>
                    <button onclick="closeInvestigateModal()" style="background-color: #718096;">Cancel</button>
                </div>
            </div>

            <div id="token-result">
                <strong>Withdrawal Token:</strong>
                <div id="token-text"></div>
                <button id="copy-btn" class="copy-btn" onclick="copyToken()">Copy Token</button>
                <p><em>Save this token! It represents your withdrawn balance.</em></p>
            </div>

            <div id="apikeys-table"
                 hx-get="/admin/partials/apikeys"
                 hx-trigger="load"
                 hx-swap="outerHTML">
                <h2>Temporary Balances</h2>
                <div style="color:#718096;">Loading API keys…</div>
            </div>
        </body>
    </html>
    """
    )


@admin_router.get("/", response_class=HTMLResponse)
async def admin(request: Request) -> RedirectResponse:
    return RedirectResponse("/")


@admin_router.get("/logs/{request_id}", response_class=HTMLResponse)
async def view_logs(request: Request, request_id: str) -> str:
    if not is_admin_authenticated(request):
        return admin_auth()

    logger.info(f"Investigating logs for request_id: {request_id}")

    # Search for log entries with this request_id
    log_entries = []
    logs_dir = Path("logs")

    if logs_dir.exists():
        # Get all log files sorted by modification time (most recent first)
        log_files = sorted(
            logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True
        )

        for log_file in log_files[:7]:  # Check last 7 days of logs
            try:
                with open(log_file, "r") as f:
                    for line in f:
                        if request_id in line:
                            try:
                                # Parse JSON log entry
                                log_data = json.loads(line.strip())
                                log_entries.append(log_data)
                            except json.JSONDecodeError:
                                # If not JSON, include raw line
                                log_entries.append({"raw": line.strip()})
            except Exception as e:
                logger.error(f"Error reading log file {log_file}: {e}")

    # Sort entries by timestamp if available (newest first)
    log_entries.sort(key=lambda x: x.get("asctime", ""), reverse=True)

    # Format log entries for display
    formatted_logs = []
    for entry in log_entries:
        if "raw" in entry:
            formatted_logs.append(f'<div class="log-entry">{entry["raw"]}</div>')
        else:
            # Format JSON log entry
            timestamp = entry.get("asctime", "Unknown time")
            level = entry.get("levelname", "INFO")
            message = entry.get("message", "")
            pathname = entry.get("pathname", "")
            lineno = entry.get("lineno", "")

            # Extract additional fields
            extra_fields = {
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
                    "version",
                    "request_id",
                ]
            }

            level_class = level.lower()
            formatted_entry = f"""
                <div class="log-entry log-{level_class}">
                    <div class="log-header">
                        <span class="log-timestamp">{timestamp}</span>
                        <span class="log-level">[{level}]</span>
                        <span class="log-location">{pathname}:{lineno}</span>
                    </div>
                    <div class="log-message">{message}</div>
            """

            if extra_fields:
                formatted_entry += '<div class="log-extra">'
                for key, value in extra_fields.items():
                    formatted_entry += f'<div class="log-field"><strong>{key}:</strong> {json.dumps(value) if isinstance(value, (dict, list)) else value}</div>'
                formatted_entry += "</div>"

            formatted_entry += "</div>"
            formatted_logs.append(formatted_entry)

    return (
        f"""<!DOCTYPE html>
    <html>
        <head>
            <style>
        {LOGS_CSS}
        </style>
            </head>
            </head>
        """
        + f"""<!--html-->
        <body>
            <a href="/admin" class="back-btn">← Back to Dashboard</a>
            <h1>Log Investigation</h1>
            <div class="request-id-display">
                <strong>Request ID:</strong> {request_id}
            </div>
            <div class="log-container">
                {"".join(formatted_logs) if formatted_logs else '<div class="no-logs">No log entries found for this Request ID</div>'}
            </div>
            <p style="color: #666; margin-top: 20px;">
                Found {len(log_entries)} log entries • Searched last 7 days of logs
            </p>
        </body>
    </html>
    """
    )


@admin_router.get("/api/logs", dependencies=[Depends(require_admin_api)])
async def get_logs_api(
    request: Request,
    date: str | None = None,
    level: str | None = None,
    request_id: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """
    Get filtered log entries.

    Args:
        date: Filter by specific date (YYYY-MM-DD)
        level: Filter by log level
        request_id: Filter by request ID
        search: Search text in message and name fields (case-insensitive)
        limit: Maximum number of entries to return

    Returns:
        Dict containing logs and filter metadata
    """
    logs_dir = Path("logs")

    # Use the search module for log filtering
    log_entries = search_logs(
        logs_dir=logs_dir,
        date=date,
        level=level,
        request_id=request_id,
        search_text=search,
        limit=limit,
    )

    return {
        "logs": log_entries,
        "total": len(log_entries),
        "date": date,
        "level": level,
        "request_id": request_id,
        "search": search,
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


DASHBOARD_MODELS_JS: str = """<!--html-->
    <script>
        let modelsList = [];
        let currentQuery = '';
        let editModelCreated = 0;

        async function fetchModels() {
            const tableBody = document.getElementById('models-tbody');
            tableBody.innerHTML = '<tr><td colspan="1" style="color:#718096;">Loading…</td></tr>';
            try {
                const resp = await fetch('/admin/api/models', { credentials: 'same-origin' });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const data = await resp.json();
                modelsList = data;
                renderModelsTable();
            } catch (e) {
                tableBody.innerHTML = '<tr><td colspan="1" style="color:#e53e3e;">Failed to load models: ' + e.message + '</td></tr>';
            }
        }

        function renderModelsTable() {
            const tableBody = document.getElementById('models-tbody');
            if (!Array.isArray(modelsList) || !modelsList.length) {
                tableBody.innerHTML = '<tr><td colspan="1" style="color:#718096;">No models found</td></tr>';
                return;
            }
            const q = (currentQuery || '').trim().toLowerCase();
            const items = q ? modelsList.filter(m => {
                const id = (m.id || '').toLowerCase();
                return id.includes(q);
            }) : modelsList;
            if (!items.length) {
                tableBody.innerHTML = '<tr><td colspan="1" style="color:#718096;">No models match your search</td></tr>';
                return;
            }
            const rows = items.map(m => `
                <tr>
                    <td>
                        <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
                            <span style="font-family:monospace; word-break: break-all;">${m.id}</span>
                            <span>
                                <button onclick=\"openModelEditor('${m.id}')\">Edit</button>
                                <button onclick=\"deleteModel(event, '${m.id}')\" style=\"background:#e53e3e;\">Delete</button>
                            </span>
                        </div>
                    </td>
                </tr>
            `).join('');
            tableBody.innerHTML = rows;
        }

        function handleSearch(query) {
            currentQuery = String(query || '');
            renderModelsTable();
        }

        async function deleteModel(ev, modelId) {
            if (!confirm('Are you sure you want to delete model: ' + modelId + '?')) return;
            const btn = ev && ev.currentTarget ? ev.currentTarget : null;
            if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
            const errorBox = document.getElementById('models-error');
            if (errorBox) { errorBox.style.display = 'none'; errorBox.textContent = ''; }
            try {
                const resp = await fetch('/admin/api/models/' + encodeURIComponent(modelId), {
                    method: 'DELETE',
                    credentials: 'same-origin'
                });
                if (!resp.ok) {
                    let errText = 'Failed to delete model';
                    try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                    throw new Error(errText);
                }
                await fetchModels();
            } catch (e) {
                if (errorBox) { errorBox.style.display = 'block'; errorBox.textContent = e.message; }
                else { alert(e.message); }
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
            }
        }

        async function deleteAllModels() {
            if (!confirm('Are you absolutely sure you want to delete ALL models?')) return;
            const errorBox = document.getElementById('models-error');
            if (errorBox) { errorBox.style.display = 'none'; errorBox.textContent = ''; }
            try {
                const resp = await fetch('/admin/api/models', { method: 'DELETE', credentials: 'same-origin' });
                if (!resp.ok) {
                    let errText = 'Failed to delete all models';
                    try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                    throw new Error(errText);
                }
                await fetchModels();
            } catch (e) {
                if (errorBox) { errorBox.style.display = 'block'; errorBox.textContent = e.message; }
                else { alert(e.message); }
            }
        }

        async function openModelEditor(modelId) {
            const modal = document.getElementById('model-edit-modal');
            const errorBox = document.getElementById('model-error');
            errorBox.style.display = 'none';
            errorBox.textContent = '';
            document.getElementById('model-id').value = modelId;
            try {
                const resp = await fetch('/admin/api/models/' + encodeURIComponent(modelId), { credentials: 'same-origin' });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const m = await resp.json();
                document.getElementById('model-name').value = m.name || '';
                document.getElementById('model-description').value = m.description || '';
                editModelCreated = m.created || Math.floor(Date.now()/1000);
                document.getElementById('model-context').value = m.context_length || 0;
                document.getElementById('model-architecture').value = JSON.stringify(m.architecture || {
                    modality: '',
                    input_modalities: [],
                    output_modalities: [],
                    tokenizer: '',
                    instruct_type: null
                }, null, 2);
                const pricingObj = m.pricing || {
                    prompt: 0.0,
                    completion: 0.0,
                    request: 0.0,
                    image: 0.0,
                    web_search: 0.0,
                    internal_reasoning: 0.0
                };
                delete pricingObj.max_prompt_cost;
                delete pricingObj.max_completion_cost;
                delete pricingObj.max_cost;
                document.getElementById('model-pricing').value = JSON.stringify(pricingObj, null, 2);
                document.getElementById('model-per-request-limits').value = m.per_request_limits ? JSON.stringify(m.per_request_limits, null, 2) : '';
                document.getElementById('model-top-provider').value = m.top_provider ? JSON.stringify(m.top_provider, null, 2) : '';
            } catch (e) {
                errorBox.style.display = 'block';
                errorBox.textContent = 'Failed to load model: ' + e.message;
            }
            modal.style.display = 'block';
        }

        function closeModelEditor() {
            const modal = document.getElementById('model-edit-modal');
            modal.style.display = 'none';
        }

        async function saveModel() {
            const modelId = document.getElementById('model-id').value;
            const errorBox = document.getElementById('model-error');
            errorBox.style.display = 'none';
            errorBox.style.color = '#e53e3e';
            let payload = {};
            try {
                const name = document.getElementById('model-name').value;
                const description = document.getElementById('model-description').value;
                const contextLength = parseInt(document.getElementById('model-context').value) || 0;
                const architecture = JSON.parse(document.getElementById('model-architecture').value || '{}');
                const pricing = JSON.parse(document.getElementById('model-pricing').value || '{}');
                const perReqLimitsStr = document.getElementById('model-per-request-limits').value.trim();
                const topProviderStr = document.getElementById('model-top-provider').value.trim();

                payload = {
                    id: modelId,
                    name: name,
                    description: description,
                    created: editModelCreated,
                    context_length: contextLength,
                    architecture: architecture,
                    pricing: pricing,
                    per_request_limits: perReqLimitsStr === '' ? null : JSON.parse(perReqLimitsStr),
                    top_provider: topProviderStr === '' ? null : JSON.parse(topProviderStr)
                };
            } catch (e) {
                errorBox.style.display = 'block';
                errorBox.textContent = 'Invalid input: ' + e.message;
                return;
            }

            const saveBtn = document.getElementById('model-save-btn');
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving…';
            try {
                const resp = await fetch('/admin/api/models/' + encodeURIComponent(modelId), {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify(payload)
                });
                if (!resp.ok) {
                    let errText = 'Failed to save model';
                    try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                    throw new Error(errText);
                }
                await resp.json();
                closeModelEditor();
                fetchModels();
            } catch (e) {
                errorBox.style.display = 'block';
                errorBox.textContent = e.message;
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save';
            }
        }

        async function deleteAllModels() {
            if (!confirm('Are you absolutely sure you want to delete ALL models?')) return;
            const errorBox = document.getElementById('models-error');
            if (errorBox) { errorBox.style.display = 'none'; errorBox.textContent = ''; }
            try {
                const resp = await fetch('/admin/api/models', { method: 'DELETE', credentials: 'same-origin' });
                if (!resp.ok) {
                    let errText = 'Failed to delete all models';
                    try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                    throw new Error(errText);
                }
                await fetchModels();
            } catch (e) {
                if (errorBox) { errorBox.style.display = 'block'; errorBox.textContent = e.message; }
                else { alert(e.message); }
            }
        }

        window.addEventListener('DOMContentLoaded', fetchModels);

        window.onclick = function(event) {
            const modal = document.getElementById('model-edit-modal');
            if (event.target == modal) {
                closeModelEditor();
            }
            const createModal = document.getElementById('model-create-modal');
            if (event.target == createModal) {
                closeCreateModel();
            }
            const batchModal = document.getElementById('model-batch-modal');
            if (event.target == batchModal) {
                closeBatchModal();
            }
        }

        function openCreateModel() {
            const modal = document.getElementById('model-create-modal');
            const err = document.getElementById('model-create-error');
            err.style.display = 'none';
            err.textContent = '';
            const defaults = {
                architecture: {
                    modality: 'text',
                    input_modalities: ['text'],
                    output_modalities: ['text'],
                    tokenizer: '',
                    instruct_type: null
                },
                pricing: {
                    prompt: 0.0,
                    completion: 0.0,
                    request: 0.0,
                    image: 0.0,
                    web_search: 0.0,
                    internal_reasoning: 0.0
                }
            };
            document.getElementById('create-id').value = '';
            document.getElementById('create-name').value = '';
            document.getElementById('create-description').value = '';
            document.getElementById('create-context').value = 0;
            document.getElementById('create-architecture').value = JSON.stringify(defaults.architecture, null, 2);
            document.getElementById('create-pricing').value = JSON.stringify(defaults.pricing, null, 2);
            document.getElementById('create-per-request-limits').value = '';
            document.getElementById('create-top-provider').value = '';
            modal.style.display = 'block';
        }

        function closeCreateModel() {
            const modal = document.getElementById('model-create-modal');
            modal.style.display = 'none';
        }

        async function createModel() {
            const err = document.getElementById('model-create-error');
            err.style.display = 'none';
            err.textContent = '';
            const btn = document.getElementById('model-create-btn');
            btn.disabled = true;
            btn.textContent = 'Creating…';
            try {
                const id = document.getElementById('create-id').value.trim();
                const name = document.getElementById('create-name').value.trim();
                const description = document.getElementById('create-description').value.trim();
                const contextStr = document.getElementById('create-context').value.trim();
                const architectureStr = document.getElementById('create-architecture').value.trim();
                const pricingStr = document.getElementById('create-pricing').value.trim();
                const perReqLimitsStr = document.getElementById('create-per-request-limits').value.trim();
                const topProviderStr = document.getElementById('create-top-provider').value.trim();

                if (!id) throw new Error('ID is required');
                if (!name) throw new Error('Name is required');
                if (!description) throw new Error('Description is required');
                const created = Math.floor(Date.now()/1000);
                if (!contextStr) throw new Error('Context length is required');
                const context_length = parseInt(contextStr);
                if (!architectureStr) throw new Error('Architecture JSON is required');
                const architecture = JSON.parse(architectureStr);
                if (!pricingStr) throw new Error('Pricing JSON is required');
                const pricing = JSON.parse(pricingStr);

                const payload = {
                    id: id,
                    name: name,
                    description: description,
                    created: created,
                    context_length: context_length,
                    architecture: architecture,
                    pricing: pricing,
                    per_request_limits: perReqLimitsStr ? JSON.parse(perReqLimitsStr) : null,
                    top_provider: topProviderStr ? JSON.parse(topProviderStr) : null
                };

                const resp = await fetch('/admin/api/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify(payload)
                });
                if (!resp.ok) {
                    let errText = 'Failed to create model';
                    try { const e = await resp.json(); if (e && e.detail) errText = typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail); } catch(_) {}
                    throw new Error(errText);
                }
                closeCreateModel();
                await fetchModels();
            } catch (e) {
                err.style.display = 'block';
                err.textContent = e.message || String(e);
            } finally {
                btn.disabled = false;
                btn.textContent = '➕ Create';
            }
        }

        function openBatchModal() {
            const modal = document.getElementById('model-batch-modal');
            if (!modal) { alert('Batch modal not found'); return; }
            const err = document.getElementById('batch-error');
            if (err) { err.style.display = 'none'; err.textContent = ''; }
            const textarea = document.getElementById('batch-json');
            if (textarea && !textarea.value.trim()) {
                const sample = {
                    models: [
                        {
                            id: 'provider/model-id',
                            name: 'Model Name',
                            description: 'Description',
                            created: Math.floor(Date.now()/1000),
                            context_length: 0,
                            architecture: { modality: 'text', input_modalities: ['text'], output_modalities: ['text'], tokenizer: '', instruct_type: null },
                            pricing: { prompt: 0.0, completion: 0.0, request: 0.0, image: 0.0, web_search: 0.0, internal_reasoning: 0.0 },
                            per_request_limits: null,
                            top_provider: null
                        }
                    ]
                };
                textarea.value = JSON.stringify(sample, null, 2);
            }
            modal.style.display = 'block';
        }

        function closeBatchModal() {
            const modal = document.getElementById('model-batch-modal');
            if (modal) modal.style.display = 'none';
        }

        async function performBatchAdd() {
            const textarea = document.getElementById('batch-json');
            const err = document.getElementById('batch-error');
            const btn = document.getElementById('batch-submit-btn');
            if (err) { err.style.display = 'none'; err.textContent = ''; }
            if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
            try {
                if (!textarea) throw new Error('Input not found');
                const data = JSON.parse(textarea.value);
                if (!data || !Array.isArray(data.models) || data.models.length === 0) {
                    throw new Error('Payload must include a non-empty "models" array');
                }
                const resp = await fetch('/admin/api/models/batch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify(data)
                });
                if (!resp.ok) {
                    let errText = 'Failed to add models';
                    try { const e = await resp.json(); if (e && e.detail) errText = typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail); } catch(_) {}
                    throw new Error(errText);
                }
                closeBatchModal();
                await fetchModels();
            } catch (e) {
                if (err) { err.style.display = 'block'; err.textContent = e.message || String(e); }
                else { alert(e.message || String(e)); }
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '➕ Add Models'; }
            }
        }
    </script>
"""


def models_page() -> str:
    return (
        f"""<!DOCTYPE html>
    <html>
        <head>
        <style>{DASHBOARD_CSS}</style>
        {DASHBOARD_MODELS_JS}
        </head>
        """
        + """<!--html-->
        <body>
            <a href="/admin" class="back-btn">← Back to Dashboard</a>
            <h1>Models</h1>

            <div class="balance-card">
                <h2>Models Table</h2>
                <div style="display:flex; gap:10px; align-items:center; margin: 8px 0 12px;">
                    <input type="text" id="models-search" placeholder="Search by id" oninput="handleSearch(this.value)" style="flex:1; padding: 10px; border: 2px solid #e2e8f0; border-radius: 6px;">
                    <button onclick="openCreateModel()">➕ Create Model</button>
                </div>
                <div id="models-error" style="display:none; margin: 8px 0; color:#e53e3e;"></div>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                        </tr>
                    </thead>
                    <tbody id="models-tbody">
                        <tr><td colspan="1" style="color:#718096;">Loading…</td></tr>
                    </tbody>
                </table>
                <div style="margin-top: 12px; display:flex; justify-content:flex-end;">
                    <button onclick="deleteAllModels()" style="background:#e53e3e;">🗑️ Delete All</button>
                    <button onclick="openBatchModal()" style="background:#4a5568;">📥 Batch Add</button>
                </div>
            </div>

            <div id="model-edit-modal" class="modal">
                <div class="modal-content" style="max-width: 720px;">
                    <span class="close" onclick="closeModelEditor()">&times;</span>
                    <h3>Edit Model: <span id="model-id" style="font-family:monospace;"></span></h3>

                    <div id="model-error" style="display:none; margin: 10px 0; color:#e53e3e;"></div>

                    <label>ID</label>
                    <input type="text" id="model-id" placeholder="model-id" disabled>

                    <label>Name</label>
                    <input type="text" id="model-name" placeholder="Name">

                    <label>Description</label>
                    <input type="text" id="model-description" placeholder="Description">

                    <label>Context Length</label>
                    <input type="number" id="model-context" min="0" placeholder="Context length">

                    <h4 style="margin-top:10px;">Architecture (JSON)</h4>
                    <textarea id="model-architecture" style="width:100%; min-height: 160px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <h4 style="margin-top:10px;">Pricing (JSON)</h4>
                    <textarea id="model-pricing" style="width:100%; min-height: 160px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <h4 style="margin-top:10px;">Per Request Limits (JSON, optional) — leave blank to clear</h4>
                    <textarea id="model-per-request-limits" style="width:100%; min-height: 120px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <h4 style="margin-top:10px;">Top Provider (JSON, optional) — leave blank to clear</h4>
                    <textarea id="model-top-provider" style="width:100%; min-height: 120px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <div style="margin-top: 12px; display: flex; gap: 10px;">
                        <button id="model-save-btn" onclick="saveModel()">💾 Save</button>
                        <button onclick="closeModelEditor()" style="background-color: #718096;">Cancel</button>
                    </div>
                </div>
            </div>

            <div id="model-create-modal" class="modal">
                <div class="modal-content" style="max-width: 720px;">
                    <span class="close" onclick="closeCreateModel()">&times;</span>
                    <h3>Create Model</h3>

                    <div id="model-create-error" style="display:none; margin: 10px 0; color:#e53e3e;"></div>

                    <label>ID</label>
                    <input type="text" id="create-id" placeholder="model-id">

                    <label>Name</label>
                    <input type="text" id="create-name" placeholder="Name">

                    <label>Description</label>
                    <input type="text" id="create-description" placeholder="Description">

                    <label>Context Length</label>
                    <input type="number" id="create-context" min="0" placeholder="Context length">

                    <h4 style="margin-top:10px;">Architecture (JSON)</h4>
                    <textarea id="create-architecture" style="width:100%; min-height: 160px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <h4 style="margin-top:10px;">Pricing (JSON)</h4>
                    <textarea id="create-pricing" style="width:100%; min-height: 160px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <h4 style="margin-top:10px;">Per Request Limits (JSON, optional)</h4>
                    <textarea id="create-per-request-limits" style="width:100%; min-height: 120px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <h4 style="margin-top:10px;">Top Provider (JSON, optional)</h4>
                    <textarea id="create-top-provider" style="width:100%; min-height: 120px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                    <div style="margin-top: 12px; display: flex; gap: 10px;">
                        <button id="model-create-btn" onclick="createModel()">➕ Create</button>
                        <button onclick="closeCreateModel()" style="background-color: #718096;">Cancel</button>
                    </div>
                </div>
            </div>

            <div id="model-batch-modal" class="modal">
                <div class="modal-content" style="max-width: 840px;">
                    <span class="close" onclick="closeBatchModal()">&times;</span>
                    <h3>Batch Add Models</h3>
                    <p style="font-size: 0.9rem; color: #718096; margin: 6px 0 10px;">Paste JSON in the format like models.example.json</p>
                    <div id="batch-error" style="display:none; margin: 10px 0; color:#e53e3e;"></div>
                    <textarea id="batch-json" style="width:100%; min-height: 320px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>
                    <div style="margin-top: 12px; display:flex; gap:10px;">
                        <button id="batch-submit-btn" onclick="performBatchAdd()">➕ Add Models</button>
                        <button onclick="closeBatchModal()" style="background-color:#718096;">Cancel</button>
                    </div>
                </div>
            </div>
        </body>
    </html>
    """
    )


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
    enabled: bool = True


class ModelUpdate(BaseModel):
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
    enabled: bool = True


@admin_router.get("/models", response_class=HTMLResponse)
async def admin_models(request: Request) -> str:
    if is_admin_authenticated(request):
        return models_page()
    return admin_auth()


UPSTREAM_PROVIDERS_JS: str = """<!--html-->
<script>
    let providersList = [];
    let selectedProviderId = null;
    let providerModels = { db_models: [], remote_models: [], provider: {} };
    let openrouterPresets = [];

    async function fetchProviders() {
        const tableBody = document.getElementById('providers-tbody');
        tableBody.innerHTML = '<tr><td colspan="4" style="color:#718096;">Loading…</td></tr>';
        try {
            const resp = await fetch('/admin/api/upstream-providers', { credentials: 'same-origin' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            providersList = await resp.json();
            renderProvidersTable();
        } catch (e) {
            tableBody.innerHTML = '<tr><td colspan="4" style="color:#e53e3e;">Failed to load providers: ' + e.message + '</td></tr>';
        }
    }

    function renderProvidersTable() {
        const tableBody = document.getElementById('providers-tbody');
        if (!Array.isArray(providersList) || !providersList.length) {
            tableBody.innerHTML = '<tr><td colspan="4" style="color:#718096;">No providers found</td></tr>';
            return;
        }
        const rows = providersList.map(p => `
            <tr>
                <td>${p.provider_type}</td>
                <td style="word-break: break-all;">${p.base_url}</td>
                <td><span style="padding:2px 8px; border-radius:4px; background:${p.enabled ? '#22c55e' : '#ef4444'}; color:white; font-size:12px;">${p.enabled ? 'Enabled' : 'Disabled'}</span></td>
                <td>
                    <button onclick="viewProviderModels(${p.id})">📋 Models</button>
                    <button onclick="openProviderEditor(${p.id})">Edit</button>
                    <button onclick="deleteProvider(event, ${p.id})" style="background:#e53e3e;">Delete</button>
                </td>
            </tr>
        `).join('');
        tableBody.innerHTML = rows;
    }

    async function viewProviderModels(providerId) {
        selectedProviderId = providerId;
        const modelsSection = document.getElementById('models-section');
        const modelsLoading = document.getElementById('models-loading');
        const modelsContent = document.getElementById('models-content');

        modelsSection.style.display = 'block';
        modelsLoading.style.display = 'block';
        modelsContent.style.display = 'none';

        try {
            const resp = await fetch(`/admin/api/upstream-providers/${providerId}/models`, { credentials: 'same-origin' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json();
            providerModels = data;

            document.getElementById('provider-name-display').textContent = data.provider.base_url;
            renderProviderModels();

            modelsLoading.style.display = 'none';
            modelsContent.style.display = 'block';
        } catch (e) {
            modelsLoading.innerHTML = '<p style="color:#e53e3e;">Failed to load models: ' + e.message + '</p>';
        }
    }

    function renderProviderModels() {
        const dbModelsBody = document.getElementById('db-models-tbody');
        const remoteModelsBody = document.getElementById('remote-models-tbody');
        const remoteModelsSection = document.getElementById('remote-models-section');
        const customProviderActions = document.getElementById('custom-provider-actions');
        const isCustomProvider = providerModels.provider && providerModels.provider.provider_type === 'custom';

        if (providerModels.db_models && providerModels.db_models.length > 0) {
            dbModelsBody.innerHTML = '';
            providerModels.db_models.forEach(m => {
                const row = document.createElement('tr');
                const enabledBadge = m.enabled
                    ? '<span style="padding:2px 8px; border-radius:4px; background:#22c55e; color:white; font-size:12px;">Enabled</span>'
                    : '<span style="padding:2px 8px; border-radius:4px; background:#ef4444; color:white; font-size:12px;">Disabled</span>';
                row.innerHTML = `
                    <td style="font-family:monospace; word-break: break-all;">${m.id}</td>
                    <td>${m.name} ${enabledBadge}</td>
                    <td>
                        <button class="toggle-btn">${m.enabled ? '🚫 Disable' : '✅ Enable'}</button>
                        <button class="edit-btn" ${!m.enabled ? 'disabled' : ''}>Edit</button>
                        <button class="delete-btn" style="background:#e53e3e;">Delete</button>
                    </td>
                `;
                const toggleBtn = row.querySelector('.toggle-btn');
                const editBtn = row.querySelector('.edit-btn');
                const deleteBtn = row.querySelector('.delete-btn');
                toggleBtn.onclick = () => toggleModelEnabled(m.id, !m.enabled);
                if (m.enabled) {
                    editBtn.onclick = () => editModelOverride(m.id);
                }
                deleteBtn.onclick = () => deleteModelOverride(m.id);
                dbModelsBody.appendChild(row);
            });
        } else {
            dbModelsBody.innerHTML = '<tr><td colspan="3" style="color:#718096;">No models defined</td></tr>';
        }

        if (isCustomProvider) {
            remoteModelsSection.style.display = 'none';
            customProviderActions.style.display = 'block';
        } else {
            remoteModelsSection.style.display = 'block';
            customProviderActions.style.display = 'none';

            if (providerModels.remote_models && providerModels.remote_models.length > 0) {
                remoteModelsBody.innerHTML = '';
                providerModels.remote_models.forEach(m => {
                    const isInDb = providerModels.db_models.some(db => db.id === m.id);
                    const row = document.createElement('tr');
                    row.innerHTML = `
                        <td style="font-family:monospace; word-break: break-all;">${m.id}</td>
                        <td>${m.name}</td>
                        <td>
                            <button class="override-btn" ${isInDb ? 'disabled' : ''}>+ Override</button>
                            <button class="disable-btn" style="background:#ef4444;" ${isInDb ? 'disabled' : ''}>🚫 Disable</button>
                        </td>
                    `;
                    const overrideBtn = row.querySelector('.override-btn');
                    const disableBtn = row.querySelector('.disable-btn');
                    if (!isInDb) {
                        overrideBtn.onclick = () => createModelOverride(m, true);
                        disableBtn.onclick = () => createModelOverride(m, false);
                    }
                    remoteModelsBody.appendChild(row);
                });
            } else {
                remoteModelsBody.innerHTML = '<tr><td colspan="3" style="color:#718096;">No remote models available</td></tr>';
            }
        }
    }

    function closeModelsSection() {
        document.getElementById('models-section').style.display = 'none';
        selectedProviderId = null;
    }

    const PROVIDER_CONFIGS = {
        'openai': { baseUrl: 'https://api.openai.com/v1', showApiVersion: false },
        'openrouter': { baseUrl: 'https://openrouter.ai/api/v1', showApiVersion: false },
        'anthropic': { baseUrl: 'https://api.anthropic.com/v1', showApiVersion: false },
        'azure': { baseUrl: '', showApiVersion: true },
        'custom': { baseUrl: '', showApiVersion: false }
    };

    function getProviderFeePlaceholder(providerType) {
        return providerType === 'openrouter' ? 'Default: 1.06 (6%)' : 'Default: 1.01 (1%)';
    }

    function updateProviderFields() {
        const providerType = document.getElementById('provider-type').value;
        const config = PROVIDER_CONFIGS[providerType] || { baseUrl: '', showApiVersion: false };
        const baseUrlField = document.getElementById('provider-base-url');
        const apiVersionRow = document.getElementById('api-version-row');
        const providerId = document.getElementById('provider-id').value;
        const feeField = document.getElementById('provider-fee');

        if (config.baseUrl && !providerId) {
            baseUrlField.value = config.baseUrl;
            baseUrlField.readOnly = true;
            baseUrlField.style.backgroundColor = '#f0f0f0';
        } else if (!providerId) {
            baseUrlField.readOnly = false;
            baseUrlField.style.backgroundColor = '';
        }

        apiVersionRow.style.display = config.showApiVersion ? 'block' : 'none';

        if (!feeField.value) {
            feeField.placeholder = getProviderFeePlaceholder(providerType);
        }
    }

    async function openProviderEditor(providerId) {
        const modal = document.getElementById('provider-edit-modal');
        const errorBox = document.getElementById('provider-error');
        errorBox.style.display = 'none';

        if (providerId) {
            document.getElementById('modal-title').textContent = 'Edit Upstream Provider';
            document.getElementById('provider-save-btn').textContent = 'Save';
            document.getElementById('provider-id').value = providerId;

            try {
                const resp = await fetch(`/admin/api/upstream-providers/${providerId}`, { credentials: 'same-origin' });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const p = await resp.json();

                document.getElementById('provider-type').value = p.provider_type;
                document.getElementById('provider-base-url').value = p.base_url;
                document.getElementById('provider-base-url').readOnly = false;
                document.getElementById('provider-base-url').style.backgroundColor = '';
                document.getElementById('provider-api-key').value = '';
                document.getElementById('provider-api-key').placeholder = '[Keep existing]';
                document.getElementById('provider-api-version').value = p.api_version || '';
                document.getElementById('provider-enabled').checked = p.enabled;
                document.getElementById('provider-fee').value = p.provider_fee || '';
                updateProviderFields();
            } catch (e) {
                errorBox.style.display = 'block';
                errorBox.textContent = 'Failed to load provider: ' + e.message;
            }
        } else {
            document.getElementById('modal-title').textContent = 'Create Upstream Provider';
            document.getElementById('provider-save-btn').textContent = 'Create';
            document.getElementById('provider-id').value = '';
            document.getElementById('provider-type').value = 'openrouter';
            document.getElementById('provider-api-key').value = '';
            document.getElementById('provider-api-key').placeholder = 'API Key';
            document.getElementById('provider-api-version').value = '';
            document.getElementById('provider-enabled').checked = true;
            document.getElementById('provider-fee').value = '';
            document.getElementById('provider-fee').placeholder = getProviderFeePlaceholder('openrouter');
            updateProviderFields();
        }

        modal.style.display = 'block';
    }

    function closeProviderEditor() {
        document.getElementById('provider-edit-modal').style.display = 'none';
    }

    async function saveProvider() {
        const providerId = document.getElementById('provider-id').value;
        const errorBox = document.getElementById('provider-error');
        errorBox.style.display = 'none';

        const providerType = document.getElementById('provider-type').value;
        const feeValue = document.getElementById('provider-fee').value;
        const defaultFee = providerType === 'openrouter' ? 1.06 : 1.01;

        const payload = {
            provider_type: providerType,
            base_url: document.getElementById('provider-base-url').value,
            api_version: document.getElementById('provider-api-version').value || null,
            enabled: document.getElementById('provider-enabled').checked,
            provider_fee: feeValue ? parseFloat(feeValue) : defaultFee,
        };

        const apiKey = document.getElementById('provider-api-key').value;
        if (apiKey) {
            payload.api_key = apiKey;
        }

        const saveBtn = document.getElementById('provider-save-btn');
        const originalText = saveBtn.textContent;
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving…';

        try {
            let resp;
            if (providerId) {
                resp = await fetch(`/admin/api/upstream-providers/${providerId}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify(payload)
                });
            } else {
                if (!apiKey) {
                    throw new Error('API Key is required for new providers');
                }
                resp = await fetch('/admin/api/upstream-providers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify(payload)
                });
            }

            if (!resp.ok) {
                let errText = 'Failed to save provider';
                try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                throw new Error(errText);
            }

            closeProviderEditor();
            await fetchProviders();
        } catch (e) {
            errorBox.style.display = 'block';
            errorBox.textContent = e.message;
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = originalText;
        }
    }

    async function deleteProvider(ev, providerId) {
        if (!confirm('Are you sure you want to delete this provider?')) return;

        const btn = ev && ev.currentTarget ? ev.currentTarget : null;
        if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }

        try {
            const resp = await fetch(`/admin/api/upstream-providers/${providerId}`, {
                method: 'DELETE',
                credentials: 'same-origin'
            });
            if (!resp.ok) {
                let errText = 'Failed to delete provider';
                try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                throw new Error(errText);
            }
            await fetchProviders();
        } catch (e) {
            alert(e.message);
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
        }
    }

    async function openCustomModelCreator() {
        const modal = document.getElementById('model-override-modal');
        const errorBox = document.getElementById('model-override-error');
        errorBox.style.display = 'none';

        document.getElementById('override-model-id').value = '';
        document.getElementById('override-model-id').disabled = false;
        document.getElementById('override-model-name').value = '';
        document.getElementById('override-description').value = '';
        document.getElementById('override-context').value = 8192;

        const architecture = {
            modality: 'text',
            input_modalities: ['text'],
            output_modalities: ['text'],
            tokenizer: '',
            instruct_type: null
        };
        document.getElementById('override-architecture').value = JSON.stringify(architecture, null, 2);

        const pricing = {
            prompt: 0.0,
            completion: 0.0,
            request: 0.0,
            image: 0.0,
            web_search: 0.0,
            internal_reasoning: 0.0
        };
        document.getElementById('override-pricing').value = JSON.stringify(pricing, null, 2);

        document.getElementById('override-enabled').value = 'true';
        document.getElementById('override-mode').value = 'create';
        document.getElementById('override-created').value = Math.floor(Date.now() / 1000);
        document.getElementById('override-upstream-provider-id').value = selectedProviderId;
        document.getElementById('modal-title').textContent = 'Create Custom Model';
        document.getElementById('override-save-btn').textContent = 'Create Model';
        document.getElementById('override-model-name').disabled = false;

        modal.style.display = 'block';
    }

    async function openPresetSelector() {
        const modal = document.getElementById('preset-selector-modal');
        const errorBox = document.getElementById('preset-error');
        const searchInput = document.getElementById('preset-search');
        errorBox.style.display = 'none';
        searchInput.value = '';

        if (!openrouterPresets.length) {
            const loadingDiv = document.getElementById('preset-loading');
            const presetsDiv = document.getElementById('presets-list');
            loadingDiv.style.display = 'block';
            presetsDiv.style.display = 'none';

            try {
                const resp = await fetch('/admin/api/openrouter-presets', { credentials: 'same-origin' });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                openrouterPresets = await resp.json();
                renderPresets('');
                loadingDiv.style.display = 'none';
                presetsDiv.style.display = 'block';
            } catch (e) {
                errorBox.style.display = 'block';
                errorBox.textContent = 'Failed to load presets: ' + e.message;
                loadingDiv.style.display = 'none';
            }
        } else {
            renderPresets('');
        }

        modal.style.display = 'block';
    }

    function renderPresets(query) {
        const presetsBody = document.getElementById('presets-tbody');
        const q = (query || '').trim().toLowerCase();
        const filtered = q ? openrouterPresets.filter(m => {
            const id = (m.id || '').toLowerCase();
            const name = (m.name || '').toLowerCase();
            return id.includes(q) || name.includes(q);
        }) : openrouterPresets;

        if (!filtered.length) {
            presetsBody.innerHTML = '<tr><td colspan="3" style="color:#718096;">No models match your search</td></tr>';
            return;
        }

        presetsBody.innerHTML = '';
        filtered.slice(0, 100).forEach(m => {
            const row = document.createElement('tr');
            row.innerHTML = `
                <td style="font-family:monospace; word-break: break-all; font-size: 0.85rem;">${m.id}</td>
                <td>${m.name}</td>
                <td><button class="use-preset-btn">Use Preset</button></td>
            `;
            const btn = row.querySelector('.use-preset-btn');
            btn.onclick = () => usePreset(m);
            presetsBody.appendChild(row);
        });
    }

    function searchPresets(query) {
        renderPresets(query);
    }

    function closePresetSelector() {
        document.getElementById('preset-selector-modal').style.display = 'none';
    }

    async function usePreset(modelData) {
        closePresetSelector();
        await createModelOverride(modelData, true, true);
    }

    async function createModelOverride(modelData, enabled, isCustomModel = false) {
        if (enabled) {
            const modal = document.getElementById('model-override-modal');
            const errorBox = document.getElementById('model-override-error');
            errorBox.style.display = 'none';

            document.getElementById('override-model-id').value = modelData.id || '';
            document.getElementById('override-model-id').disabled = !isCustomModel;
            document.getElementById('override-model-name').value = modelData.name || '';
            document.getElementById('override-description').value = modelData.description || '';
            document.getElementById('override-context').value = modelData.context_length || 8192;

            const architecture = modelData.architecture || {
                modality: 'text',
                input_modalities: ['text'],
                output_modalities: ['text'],
                tokenizer: '',
                instruct_type: null
            };
            document.getElementById('override-architecture').value = JSON.stringify(architecture, null, 2);

            const pricing = modelData.pricing || {
                prompt: 0.0,
                completion: 0.0,
                request: 0.0,
                image: 0.0,
                web_search: 0.0,
                internal_reasoning: 0.0
            };
            delete pricing.max_prompt_cost;
            delete pricing.max_completion_cost;
            delete pricing.max_cost;
            document.getElementById('override-pricing').value = JSON.stringify(pricing, null, 2);

            document.getElementById('override-enabled').value = 'true';
            document.getElementById('override-mode').value = 'create';
            document.getElementById('override-created').value = Math.floor(Date.now() / 1000);
            document.getElementById('override-upstream-provider-id').value = selectedProviderId;
            const isCustomProvider = providerModels.provider && providerModels.provider.provider_type === 'custom';
            document.getElementById('modal-title').textContent = isCustomProvider ? 'Create Model from Preset' : 'Create Model Override';
            document.getElementById('override-save-btn').textContent = isCustomProvider ? 'Create Model' : 'Create Override';
            document.getElementById('override-model-name').disabled = false;

            modal.style.display = 'block';
        } else {
            await quickDisableModel(modelData);
        }
    }

    async function quickDisableModel(modelData) {
        try {
            const payload = {
                id: modelData.id,
                name: modelData.name,
                description: modelData.description || '',
                created: modelData.created || Math.floor(Date.now() / 1000),
                context_length: modelData.context_length || 0,
                architecture: modelData.architecture || {},
                pricing: modelData.pricing || {},
                upstream_provider_id: selectedProviderId,
                enabled: false
            };

            const resp = await fetch(`/admin/api/upstream-providers/${selectedProviderId}/models`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(payload)
            });

            if (!resp.ok) {
                let errText = 'Failed to disable model';
                try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                throw new Error(errText);
            }

            await viewProviderModels(selectedProviderId);
        } catch (e) {
            alert(e.message);
        }
    }

    async function toggleModelEnabled(modelId, newEnabledState) {
        try {
            const resp = await fetch(`/admin/api/upstream-providers/${selectedProviderId}/models/${encodeURIComponent(modelId)}`, {
                credentials: 'same-origin'
            });
            if (!resp.ok) throw new Error('Failed to fetch model');
            const model = await resp.json();

            model.enabled = newEnabledState;

            const updateResp = await fetch(`/admin/api/upstream-providers/${selectedProviderId}/models/${encodeURIComponent(modelId)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(model)
            });

            if (!updateResp.ok) throw new Error('Failed to update model');
            await viewProviderModels(selectedProviderId);
        } catch (e) {
            alert(e.message);
        }
    }

    function closeModelOverrideModal() {
        document.getElementById('model-override-modal').style.display = 'none';
    }

    async function saveModelOverride() {
        const errorBox = document.getElementById('model-override-error');
        errorBox.style.display = 'none';

        const btn = document.getElementById('override-save-btn');
        const mode = document.getElementById('override-mode').value;
        const isEdit = mode === 'edit';

        btn.disabled = true;
        btn.textContent = isEdit ? 'Saving…' : 'Creating…';

        try {
            const modelId = document.getElementById('override-model-id').value;
            const upstreamProviderId = parseInt(document.getElementById('override-upstream-provider-id').value);
            const payload = {
                id: modelId,
                name: document.getElementById('override-model-name').value,
                description: document.getElementById('override-description').value,
                created: parseInt(document.getElementById('override-created').value),
                context_length: parseInt(document.getElementById('override-context').value),
                architecture: JSON.parse(document.getElementById('override-architecture').value),
                pricing: JSON.parse(document.getElementById('override-pricing').value),
                upstream_provider_id: upstreamProviderId,
                enabled: document.getElementById('override-enabled').value === 'true'
            };

            const resp = await fetch(
                isEdit ? `/admin/api/upstream-providers/${selectedProviderId}/models/${encodeURIComponent(modelId)}` : `/admin/api/upstream-providers/${selectedProviderId}/models`,
                {
                    method: isEdit ? 'PATCH' : 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify(payload)
                }
            );

            if (!resp.ok) {
                let errText = isEdit ? 'Failed to update override' : 'Failed to create override';
                try { const err = await resp.json(); if (err && err.detail) errText = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail); } catch (_) {}
                throw new Error(errText);
            }

            closeModelOverrideModal();
            await viewProviderModels(selectedProviderId);
        } catch (e) {
            errorBox.style.display = 'block';
            errorBox.textContent = e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = isEdit ? 'Save Changes' : 'Create Override';
        }
    }

    async function editModelOverride(modelId) {
        const modal = document.getElementById('model-override-modal');
        const errorBox = document.getElementById('model-override-error');
        errorBox.style.display = 'none';

        try {
            const resp = await fetch(`/admin/api/upstream-providers/${selectedProviderId}/models/${encodeURIComponent(modelId)}`, {
                credentials: 'same-origin'
            });
            if (!resp.ok) throw new Error('Failed to fetch model');
            const modelData = await resp.json();

            document.getElementById('override-model-id').value = modelData.id;
            document.getElementById('override-model-name').value = modelData.name;
            document.getElementById('override-model-name').disabled = false;
            document.getElementById('override-description').value = modelData.description || '';
            document.getElementById('override-context').value = modelData.context_length || 8192;
            document.getElementById('override-created').value = modelData.created || Math.floor(Date.now() / 1000);

            const architecture = modelData.architecture || {
                modality: 'text',
                input_modalities: ['text'],
                output_modalities: ['text'],
                tokenizer: '',
                instruct_type: null
            };
            document.getElementById('override-architecture').value = JSON.stringify(architecture, null, 2);

            const pricing = modelData.pricing || {
                prompt: 0.0,
                completion: 0.0,
                request: 0.0,
                image: 0.0
            };
            // Remove computed fields
            delete pricing.max_prompt_cost;
            delete pricing.max_completion_cost;
            delete pricing.max_cost;
            document.getElementById('override-pricing').value = JSON.stringify(pricing, null, 2);

            document.getElementById('override-enabled').value = String(modelData.enabled !== false);
            document.getElementById('override-mode').value = 'edit';
            document.getElementById('override-upstream-provider-id').value = modelData.upstream_provider_id || selectedProviderId;
            document.getElementById('modal-title').textContent = 'Edit Model Override';
            document.getElementById('override-save-btn').textContent = 'Save Changes';

            modal.style.display = 'block';
        } catch (e) {
            alert('Failed to load model: ' + e.message);
        }
    }

    async function deleteModelOverride(modelId) {
        if (!confirm('Delete this model override?')) return;

        try {
            const resp = await fetch(`/admin/api/upstream-providers/${selectedProviderId}/models/${encodeURIComponent(modelId)}`, {
                method: 'DELETE',
                credentials: 'same-origin'
            });
            if (!resp.ok) throw new Error('Failed to delete');
            await viewProviderModels(selectedProviderId);
        } catch (e) {
            alert(e.message);
        }
    }

    window.addEventListener('DOMContentLoaded', fetchProviders);

    window.onclick = function(event) {
        const editModal = document.getElementById('provider-edit-modal');
        const overrideModal = document.getElementById('model-override-modal');
        const presetModal = document.getElementById('preset-selector-modal');
        if (event.target == editModal) closeProviderEditor();
        if (event.target == overrideModal) closeModelOverrideModal();
        if (event.target == presetModal) closePresetSelector();
    }
</script>
"""


def upstream_providers_page() -> str:
    return (
        f"""<!DOCTYPE html>
<html>
    <head>
    <style>{DASHBOARD_CSS}</style>
    {UPSTREAM_PROVIDERS_JS}
    </head>
    """
        + """<!--html-->
    <body>
        <a href="/admin" class="back-btn">← Back to Dashboard</a>
        <h1>Upstream Providers</h1>

        <div class="balance-card">
            <h2>Providers</h2>
            <div style="margin-bottom: 12px;">
                <button onclick="openProviderEditor(null)">➕ Add Provider</button>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Type</th>
                        <th>Base URL</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="providers-tbody">
                    <tr><td colspan="4" style="color:#718096;">Loading…</td></tr>
                </tbody>
            </table>
        </div>

        <div id="models-section" class="balance-card" style="display:none;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 1rem;">
                <h2>Models for <span id="provider-name-display"></span></h2>
                <button onclick="closeModelsSection()" style="background:#718096;">Close</button>
            </div>

            <div id="models-loading" style="color:#718096;">Loading models…</div>

            <div id="models-content" style="display:none;">
                <div id="custom-provider-actions" style="display:none; margin-bottom: 1rem;">
                    <button onclick="openCustomModelCreator()">➕ Add Model</button>
                    <button onclick="openPresetSelector()" style="background:#48bb78;">📋 Load from Preset</button>
                    <p style="font-size: 0.9rem; color: #718096; margin-top: 0.5rem;">
                        Custom providers don't fetch models from an API. Add models manually or use OpenRouter presets.
                    </p>
                </div>

                <h3>Models</h3>
                <table style="margin-bottom: 2rem;">
                    <thead>
                        <tr>
                            <th>Model ID</th>
                            <th>Name</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="db-models-tbody">
                        <tr><td colspan="3" style="color:#718096;">No models defined</td></tr>
                    </tbody>
                </table>

                <div id="remote-models-section">
                    <h3>Remote Models</h3>
                    <table>
                        <thead>
                            <tr>
                                <th>Model ID</th>
                                <th>Name</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody id="remote-models-tbody">
                            <tr><td colspan="3" style="color:#718096;">No remote models</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="provider-edit-modal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeProviderEditor()">&times;</span>
                <h3 id="modal-title">Edit Upstream Provider</h3>

                <div id="provider-error" style="display:none; margin: 10px 0; color:#e53e3e;"></div>

                <input type="hidden" id="provider-id">

                <label>Provider Type</label>
                <select id="provider-type" onchange="updateProviderFields()">
                    <option value="openrouter">OpenRouter</option>
                    <option value="openai">OpenAI</option>
                    <option value="anthropic">Anthropic</option>
                    <option value="azure">Azure OpenAI</option>
                    <option value="custom">Custom</option>
                </select>

                <label>Base URL</label>
                <input type="text" id="provider-base-url" placeholder="https://api.example.com/v1">

                <label>API Key</label>
                <input type="password" id="provider-api-key" placeholder="API Key">

                <div id="api-version-row" style="display:none;">
                    <label>API Version</label>
                    <input type="text" id="provider-api-version" placeholder="2024-02-15-preview">
                </div>

                <label>Provider Fee (Multiplier)</label>
                <input type="number" id="provider-fee" step="0.001" min="1.0" placeholder="Default: 1.01 (1%)">
                <small style="color:#718096; font-size:0.85rem;">Leave empty to use default (OpenRouter: 1.06, Others: 1.01)</small>

                <label style="display:flex; align-items:center; gap:8px; margin:10px 0;">
                    <input type="checkbox" id="provider-enabled" style="width:auto;">
                    <span>Enabled</span>
                </label>

                <div style="margin-top: 12px; display: flex; gap: 10px;">
                    <button id="provider-save-btn" onclick="saveProvider()">Save</button>
                    <button onclick="closeProviderEditor()" style="background-color: #718096;">Cancel</button>
                </div>
            </div>
        </div>

        <div id="model-override-modal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeModelOverrideModal()">&times;</span>
                <h3 id="modal-title">Create Model Override</h3>

                <div id="model-override-error" style="display:none; margin: 10px 0; color:#e53e3e;"></div>

                <label>Model ID</label>
                <input type="text" id="override-model-id" disabled>

                <label>Name</label>
                <input type="text" id="override-model-name">

                <label>Description</label>
                <input type="text" id="override-description">

                <label>Context Length</label>
                <input type="number" id="override-context" min="0">

                <label>Architecture (JSON)</label>
                <textarea id="override-architecture" style="width:100%; min-height: 120px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                <label>Pricing (JSON)</label>
                <textarea id="override-pricing" style="width:100%; min-height: 120px; font-family: 'Monaco', monospace; font-size: 13px; background:#f8fafc; color:#2d3748; padding: 12px; border: 2px solid #e2e8f0; border-radius: 6px;"></textarea>

                <input type="hidden" id="override-enabled" value="true">
                <input type="hidden" id="override-mode" value="create">
                <input type="hidden" id="override-created" value="">
                <input type="hidden" id="override-upstream-provider-id" value="">

                <div style="margin-top: 12px; display: flex; gap: 10px;">
                    <button id="override-save-btn" onclick="saveModelOverride()">Create Override</button>
                    <button onclick="closeModelOverrideModal()" style="background-color: #718096;">Cancel</button>
                </div>
            </div>
        </div>

        <div id="preset-selector-modal" class="modal">
            <div class="modal-content" style="max-width: 900px;">
                <span class="close" onclick="closePresetSelector()">&times;</span>
                <h3>Load Model from OpenRouter Preset</h3>

                <div id="preset-error" style="display:none; margin: 10px 0; color:#e53e3e;"></div>

                <div id="preset-loading" style="color:#718096; padding: 20px; text-align: center;">
                    Loading OpenRouter models...
                </div>

                <div id="presets-list" style="display:none;">
                    <input type="text" id="preset-search" placeholder="Search models by ID or name..."
                           oninput="searchPresets(this.value)"
                           style="width:100%; padding:10px; margin-bottom:12px; border:2px solid #e2e8f0; border-radius:6px;">

                    <div style="max-height: 60vh; overflow-y: auto;">
                        <table style="margin: 0;">
                            <thead style="position: sticky; top: 0; z-index: 10;">
                                <tr>
                                    <th style="width: 35%;">Model ID</th>
                                    <th style="width: 45%;">Name</th>
                                    <th style="width: 20%;">Actions</th>
                                </tr>
                            </thead>
                            <tbody id="presets-tbody">
                                <tr><td colspan="3" style="color:#718096;">Loading...</td></tr>
                            </tbody>
                        </table>
                    </div>

                    <p style="font-size: 0.85rem; color: #718096; margin-top: 10px;">
                        Showing up to 100 models. Use search to find specific models.
                    </p>
                </div>

                <div style="margin-top: 12px;">
                    <button onclick="closePresetSelector()" style="background-color: #718096;">Cancel</button>
                </div>
            </div>
        </div>
    </body>
</html>
    """
    )


def logs_page() -> str:
    return """
    <!DOCTYPE html>
    <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Logs - Admin Dashboard</title>
            <script>
                window.location.href = '/logs';
            </script>
        </head>
        <body>
            <p>Redirecting to logs page...</p>
        </body>
    </html>
    """


@admin_router.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request) -> str:
    if is_admin_authenticated(request):
        return logs_page()
    return admin_auth()


@admin_router.get("/upstream-providers", response_class=HTMLResponse)
async def admin_upstream_providers(request: Request) -> str:
    if is_admin_authenticated(request):
        return upstream_providers_page()
    return admin_auth()


@admin_router.post(
    "/api/upstream-providers/{provider_id}/models",
    dependencies=[Depends(require_admin_api)],
)
async def create_provider_model(
    provider_id: int, payload: ModelCreate
) -> dict[str, object]:
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        exists = await session.get(ModelRow, (payload.id, provider_id))
        if exists:
            raise HTTPException(
                status_code=409,
                detail="Model with this ID already exists for this provider",
            )

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
            row, apply_provider_fee=True, provider_fee=provider.provider_fee
        ).dict()  # type: ignore


@admin_router.patch(
    "/api/upstream-providers/{provider_id}/models/{model_id:path}",
    dependencies=[Depends(require_admin_api)],
)
async def update_provider_model(
    provider_id: int, model_id: str, payload: ModelUpdate
) -> dict[str, object]:
    if payload.id != model_id:
        raise HTTPException(status_code=400, detail="Path id does not match payload id")

    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        row = await session.get(ModelRow, (model_id, provider_id))
        if not row:
            raise HTTPException(
                status_code=404, detail="Model not found for this provider"
            )

        row.name = payload.name
        row.description = payload.description
        row.created = int(payload.created)
        row.context_length = int(payload.context_length)
        row.architecture = json.dumps(payload.architecture)
        row.pricing = json.dumps(payload.pricing)
        row.sats_pricing = None
        row.per_request_limits = (
            json.dumps(payload.per_request_limits)
            if payload.per_request_limits is not None
            else None
        )
        row.top_provider = (
            json.dumps(payload.top_provider) if payload.top_provider else None
        )
        was_disabled = not row.enabled
        row.enabled = payload.enabled

        session.add(row)
        await session.commit()
        await session.refresh(row)

    if was_disabled and payload.enabled:
        from ..payment.models import _cleanup_enabled_models_once

        try:
            await _cleanup_enabled_models_once()
        except Exception as e:
            logger.warning(
                f"Failed to run model cleanup after enabling: {e}",
                extra={"model_id": model_id, "error": str(e)},
            )

    await refresh_model_maps()
    return _row_to_model(
        row, apply_provider_fee=True, provider_fee=provider.provider_fee
    ).dict()  # type: ignore


@admin_router.put(
    "/api/upstream-providers/{provider_id}/models/{model_id:path}",
    dependencies=[Depends(require_admin_api)],
)
async def update_provider_model_put(
    provider_id: int, model_id: str, payload: ModelUpdate
) -> dict[str, object]:
    return await update_provider_model(provider_id, model_id, payload)


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
                UpstreamProviderRow.base_url == payload.base_url
            )
        )
        if result.first():
            raise HTTPException(
                status_code=409, detail="Provider with this base URL already exists"
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
            session=session, upstream_id=provider_id, include_disabled=True
        )

        upstream_models = []
        upstream_instance = _instantiate_provider(provider)
        if upstream_instance:
            try:
                raw_models = await upstream_instance.fetch_models()
                upstream_models = [
                    upstream_instance._apply_provider_fee_to_model(m)
                    for m in raw_models
                ]
            except Exception as e:
                logger.error(
                    f"Failed to fetch models from {provider.provider_type}: {e}"
                )

        db_model_ids = {model.id for model in db_models}
        filtered_remote_models = [
            m for m in upstream_models if m.name not in db_model_ids
        ]

        return {
            "provider": {
                "id": provider.id,
                "provider_type": provider.provider_type,
                "base_url": provider.base_url,
            },
            "db_models": [m.dict() for m in db_models],
            "remote_models": [m.dict() for m in filtered_remote_models],
        }


@admin_router.get(
    "/api/openrouter-presets",
    dependencies=[Depends(require_admin_api)],
)
async def get_openrouter_presets() -> list[dict[str, object]]:
    from ..payment.models import async_fetch_openrouter_models

    models_data = await async_fetch_openrouter_models()
    return models_data


DASHBOARD_CSS: str = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7fa; color: #2c3e50; line-height: 1.6; padding: 2rem; }
h1, h2 { margin-bottom: 1rem; color: #1a202c; }
h1 { font-size: 2rem; }
h2 { font-size: 1.5rem; margin-top: 2rem; }
p { margin-bottom: 0.5rem; color: #4a5568; }
table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-top: 1rem; }
th { background: #4a5568; color: white; font-weight: 600; padding: 12px; text-align: left; }
td { padding: 12px; border-bottom: 1px solid #e2e8f0; }
tr:hover { background: #f7fafc; }
button { padding: 10px 20px; cursor: pointer; background: #4299e1; color: white; border: none; border-radius: 6px; font-weight: 600; margin-right: 10px; transition: all 0.2s; }
button:hover { background: #3182ce; transform: translateY(-1px); box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
button:disabled { background: #a0aec0; cursor: not-allowed; transform: none; }
.refresh-btn { background: #48bb78; }
.refresh-btn:hover { background: #38a169; }
.investigate-btn { background: #4299e1; }
.balance-card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 2rem; }
.balance-item { display: flex; justify-content: space-between; margin-bottom: 1rem; }
.balance-label { color: #718096; }
.balance-value { font-size: 1.5rem; font-weight: 700; color: #2d3748; }
.balance-primary { color: #48bb78; }
.currency-grid { margin-top: 1rem; font-size: 0.9rem; }
.currency-row { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 0.5rem; padding: 0.4rem 0; border-bottom: 1px solid #f0f0f0; align-items: center; }
.currency-row:last-child { border-bottom: none; }
.currency-header { font-weight: 600; color: #4a5568; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem; }
.mint-name { color: #2d3748; font-size: 0.85rem; word-break: break-all; }
.balance-num { text-align: right; font-family: monospace; }
.owner-positive { color: #22c55e; }
.error-row { color: #dc2626; font-style: italic; }
#token-result { margin-top: 20px; padding: 20px; background: #e6fffa; border: 1px solid #38b2ac; border-radius: 8px; display: none; }
#token-text { font-family: 'Monaco', monospace; font-size: 13px; background: #2d3748; color: #68d391; padding: 15px; border-radius: 6px; margin: 10px 0; word-break: break-all; }
.copy-btn { background: #38a169; padding: 6px 12px; font-size: 14px; }
.copy-btn:hover { background: #2f855a; }
.modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); }
.modal-content { background: white; margin: 5% auto; padding: 0.75rem 1rem 2.25rem; width: 90%; max-width: 720px; max-height: 85vh; overflow-y: auto; border-radius: 12px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); animation: slideIn 0.3s ease; }
@keyframes slideIn { from { transform: translateY(-20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
.close { color: #a0aec0; float: right; font-size: 28px; font-weight: bold; cursor: pointer; margin: -10px -10px 0 0; }
.close:hover { color: #2d3748; }
input[type="number"], input[type="text"], input[type="password"], select { width: 100%; padding: 10px; margin: 10px 0; border: 2px solid #e2e8f0; border-radius: 6px; font-size: 16px; transition: border 0.2s; }
input[type="number"]:focus, input[type="text"]:focus, input[type="password"]:focus, select:focus { outline: none; border-color: #4299e1; }
.warning { color: #e53e3e; font-weight: 600; margin: 10px 0; padding: 10px; background: #fff5f5; border-radius: 6px; }
"""


LOGS_CSS: str = """
body { font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }
h1 { color: #333; }
.back-btn { padding: 8px 16px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; margin-bottom: 20px; }
.back-btn:hover { background-color: #0056b3; }
.log-container { background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 20px; max-height: 80vh; overflow-y: auto; }
.log-entry { margin-bottom: 15px; padding: 10px; border: 1px solid #e0e0e0; border-radius: 4px; font-family: 'Courier New', monospace; font-size: 12px; background-color: #f9f9f9; }
.log-entry.log-error { background-color: #fee; border-color: #fcc; }
.log-entry.log-warning { background-color: #ffc; border-color: #ff9; }
.log-entry.log-debug, .log-entry.log-trace { background-color: #f0f0f0; border-color: #ccc; }
.log-header { margin-bottom: 5px; color: #666; }
.log-timestamp { color: #0066cc; }
.log-level { font-weight: bold; }
.log-message { margin: 5px 0; color: #333; }
.log-extra { margin-top: 5px; padding-top: 5px; border-top: 1px solid #e0e0e0; }
.log-field { margin: 2px 0; color: #666; word-break: break-all; }
.no-logs { text-align: center; color: #666; padding: 40px; }
.request-id-display { background-color: #e9ecef; padding: 10px; border-radius: 4px; margin-bottom: 20px; font-family: monospace; }
"""


def _parse_log_file(file_path: Path) -> list[dict]:
    """Parse JSON log file and return list of log entries."""
    entries: list[dict] = []
    try:
        with open(file_path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error(f"Error reading log file {file_path}: {e}")
    return entries


def _aggregate_metrics_by_time(
    entries: list[dict], interval_minutes: int, hours_back: int = 24
) -> dict:
    """
    Aggregate log metrics into time buckets.
    
    CRITICAL: This function parses specific log messages for usage tracking.
    The following log messages must not be modified without updating this function:
    - "received proxy request" -> counts total_requests
    - "token adjustment completed" -> counts successful_chat_completions, extracts revenue_msats from cost_data.actual_cost
    - "upstream request failed" OR "revert payment" -> counts failed_requests
    - "payment processed successfully" -> counts payment_processed
    - "revert payment" -> extracts refunds_msats from max_cost_for_model
    - ERROR level with "upstream" -> counts upstream_errors
    
    See routstr/core/logging.py for full documentation of critical log messages.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_back)

    time_buckets: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {
            "total_requests": 0,
            "successful_chat_completions": 0,
            "failed_requests": 0,
            "errors": 0,
            "warnings": 0,
            "payment_processed": 0,
            "upstream_errors": 0,
            "revenue_msats": 0,
            "refunds_msats": 0,
        }
    )

    for entry in entries:
        try:
            timestamp_str = entry.get("asctime", "")
            if not timestamp_str:
                continue

            log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            log_time = log_time.replace(tzinfo=timezone.utc)

            if log_time < cutoff:
                continue

            bucket_time = log_time.replace(
                minute=(log_time.minute // interval_minutes) * interval_minutes,
                second=0,
                microsecond=0,
            )
            bucket_key = bucket_time.isoformat()

            message = entry.get("message", "").lower()
            level = entry.get("levelname", "").upper()

            if level == "ERROR":
                time_buckets[bucket_key]["errors"] += 1
            elif level == "WARNING":
                time_buckets[bucket_key]["warnings"] += 1

            if "received proxy request" in message:
                time_buckets[bucket_key]["total_requests"] += 1

            if "token adjustment completed for non-streaming" in message:
                time_buckets[bucket_key]["successful_chat_completions"] += 1
            elif "token adjustment completed for streaming" in message:
                time_buckets[bucket_key]["successful_chat_completions"] += 1

            if "upstream request failed" in message or "revert payment" in message:
                time_buckets[bucket_key]["failed_requests"] += 1

            if "payment processed successfully" in message:
                time_buckets[bucket_key]["payment_processed"] += 1

            if "upstream" in message and level == "ERROR":
                time_buckets[bucket_key]["upstream_errors"] += 1

            if "token adjustment completed" in message:
                cost_data = entry.get("cost_data")
                if isinstance(cost_data, dict):
                    actual_cost = cost_data.get("actual_cost", 0)
                    if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                        time_buckets[bucket_key]["revenue_msats"] += actual_cost

            if "revert payment" in message:
                max_cost = entry.get("max_cost_for_model", 0)
                if isinstance(max_cost, (int, float)) and max_cost > 0:
                    time_buckets[bucket_key]["refunds_msats"] += max_cost

        except Exception:
            continue

    result = []
    for bucket_key in sorted(time_buckets.keys()):
        result.append({"timestamp": bucket_key, **time_buckets[bucket_key]})

    return {
        "metrics": result,
        "interval_minutes": interval_minutes,
        "hours_back": hours_back,
        "total_buckets": len(result),
    }


def _get_summary_stats(entries: list[dict], hours_back: int = 24) -> dict:
    """
    Calculate summary statistics from log entries.
    
    CRITICAL: This function parses specific log messages for usage tracking.
    See routstr/core/logging.py for documentation of critical log messages.
    Changes to log messages in proxy.py, auth.py, or upstream/base.py may break this function.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_back)

    stats: dict[str, int | float | set[str] | defaultdict[str, int]] = {
        "total_entries": 0,
        "total_requests": 0,
        "successful_chat_completions": 0,
        "failed_requests": 0,
        "total_errors": 0,
        "total_warnings": 0,
        "payment_processed": 0,
        "upstream_errors": 0,
        "unique_models": set(),
        "error_types": defaultdict(int),
        "revenue_msats": 0,
        "refunds_msats": 0,
        "revenue_sats": 0.0,
        "refunds_sats": 0.0,
        "net_revenue_msats": 0,
        "net_revenue_sats": 0.0,
    }

    for entry in entries:
        try:
            timestamp_str = entry.get("asctime", "")
            if not timestamp_str:
                continue

            log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            log_time = log_time.replace(tzinfo=timezone.utc)

            if log_time < cutoff:
                continue

            total_entries = stats["total_entries"]
            assert isinstance(total_entries, int)
            stats["total_entries"] = total_entries + 1

            message = entry.get("message", "").lower()
            level = entry.get("levelname", "").upper()

            if level == "ERROR":
                assert isinstance(stats["total_errors"], int)
                stats["total_errors"] += 1
                if "error_type" in entry:
                    error_type = str(entry["error_type"])
                    error_types = stats["error_types"]
                    assert isinstance(error_types, defaultdict)
                    error_types[error_type] += 1
            elif level == "WARNING":
                assert isinstance(stats["total_warnings"], int)
                stats["total_warnings"] += 1

            if "received proxy request" in message:
                assert isinstance(stats["total_requests"], int)
                stats["total_requests"] += 1

            if "token adjustment completed" in message:
                assert isinstance(stats["successful_chat_completions"], int)
                stats["successful_chat_completions"] += 1

            if "upstream request failed" in message or "revert payment" in message:
                assert isinstance(stats["failed_requests"], int)
                stats["failed_requests"] += 1

            if "payment processed successfully" in message:
                assert isinstance(stats["payment_processed"], int)
                stats["payment_processed"] += 1

            if "upstream" in message and level == "ERROR":
                assert isinstance(stats["upstream_errors"], int)
                stats["upstream_errors"] += 1

            if "model" in entry:
                model = entry["model"]
                if isinstance(model, str) and model != "unknown":
                    unique_models = stats["unique_models"]
                    assert isinstance(unique_models, set)
                    unique_models.add(model)

            if "token adjustment completed" in message:
                cost_data = entry.get("cost_data")
                if isinstance(cost_data, dict):
                    actual_cost = cost_data.get("actual_cost", 0)
                    if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                        assert isinstance(stats["revenue_msats"], (int, float))
                        stats["revenue_msats"] = float(stats["revenue_msats"]) + float(actual_cost)

            if "revert payment" in message:
                max_cost = entry.get("max_cost_for_model", 0)
                if isinstance(max_cost, (int, float)) and max_cost > 0:
                    assert isinstance(stats["refunds_msats"], (int, float))
                    stats["refunds_msats"] = float(stats["refunds_msats"]) + float(max_cost)

        except Exception:
            continue

    revenue_msats_val = stats["revenue_msats"]
    assert isinstance(revenue_msats_val, (int, float))
    revenue_msats = float(revenue_msats_val)
    
    refunds_msats_val = stats["refunds_msats"]
    assert isinstance(refunds_msats_val, (int, float))
    refunds_msats = float(refunds_msats_val)
    
    revenue_sats = revenue_msats / 1000
    refunds_sats = refunds_msats / 1000
    net_revenue_msats = revenue_msats - refunds_msats
    net_revenue_sats = net_revenue_msats / 1000

    unique_models = stats["unique_models"]
    assert isinstance(unique_models, set)
    error_types = stats["error_types"]
    assert isinstance(error_types, defaultdict)
    
    total_entries_val = stats["total_entries"]
    assert isinstance(total_entries_val, int)
    total_requests_val = stats["total_requests"]
    assert isinstance(total_requests_val, int)
    successful_completions_val = stats["successful_chat_completions"]
    assert isinstance(successful_completions_val, int)
    failed_requests_val = stats["failed_requests"]
    assert isinstance(failed_requests_val, int)
    total_errors_val = stats["total_errors"]
    assert isinstance(total_errors_val, int)
    total_warnings_val = stats["total_warnings"]
    assert isinstance(total_warnings_val, int)
    payment_processed_val = stats["payment_processed"]
    assert isinstance(payment_processed_val, int)
    upstream_errors_val = stats["upstream_errors"]
    assert isinstance(upstream_errors_val, int)
    
    return {
        "total_entries": total_entries_val,
        "total_requests": total_requests_val,
        "successful_chat_completions": successful_completions_val,
        "failed_requests": failed_requests_val,
        "total_errors": total_errors_val,
        "total_warnings": total_warnings_val,
        "payment_processed": payment_processed_val,
        "upstream_errors": upstream_errors_val,
        "unique_models_count": len(unique_models),
        "unique_models": sorted(list(unique_models)),
        "error_types": dict(error_types),
        "success_rate": (
            (successful_completions_val / total_requests_val * 100)
            if total_requests_val > 0
            else 0
        ),
        "revenue_msats": revenue_msats,
        "refunds_msats": refunds_msats,
        "revenue_sats": revenue_sats,
        "refunds_sats": refunds_sats,
        "net_revenue_msats": net_revenue_msats,
        "net_revenue_sats": net_revenue_sats,
        "avg_revenue_per_request_msats": (
            revenue_msats / successful_completions_val
            if successful_completions_val > 0
            else 0
        ),
        "refund_rate": (
            (failed_requests_val / total_requests_val * 100)
            if total_requests_val > 0
            else 0
        ),
    }


@admin_router.get("/api/usage/metrics", dependencies=[Depends(require_admin_api)])
async def get_usage_metrics(
    request: Request,
    interval: int = Query(
        default=15, ge=1, le=1440, description="Time interval in minutes"
    ),
    hours: int = Query(
        default=24, ge=1, le=168, description="Hours of history to analyze"
    ),
) -> dict:
    """Get usage metrics aggregated by time interval."""
    logs_dir = Path("logs")
    all_entries: list[dict] = []

    if not logs_dir.exists():
        return {
            "metrics": [],
            "interval_minutes": interval,
            "hours_back": hours,
            "total_buckets": 0,
        }

    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=hours)

    for log_file in sorted(logs_dir.glob("app_*.log")):
        try:
            file_date_str = log_file.stem.split("_")[1]
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

            if file_date < cutoff_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ):
                continue

            entries = _parse_log_file(log_file)
            all_entries.extend(entries)
        except Exception as e:
            logger.error(f"Error processing log file {log_file}: {e}")
            continue

    return _aggregate_metrics_by_time(all_entries, interval, hours)


@admin_router.get("/api/usage/summary", dependencies=[Depends(require_admin_api)])
async def get_usage_summary(
    request: Request,
    hours: int = Query(
        default=24, ge=1, le=168, description="Hours of history to analyze"
    ),
) -> dict:
    """Get summary statistics for the specified time period."""
    logs_dir = Path("logs")
    all_entries: list[dict] = []

    if not logs_dir.exists():
        return {
            "total_entries": 0,
            "total_requests": 0,
            "successful_chat_completions": 0,
            "failed_requests": 0,
            "total_errors": 0,
            "total_warnings": 0,
            "payment_processed": 0,
            "upstream_errors": 0,
            "unique_models_count": 0,
            "unique_models": [],
            "error_types": {},
            "success_rate": 0,
        }

    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=hours)

    for log_file in sorted(logs_dir.glob("app_*.log")):
        try:
            file_date_str = log_file.stem.split("_")[1]
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

            if file_date < cutoff_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ):
                continue

            entries = _parse_log_file(log_file)
            all_entries.extend(entries)
        except Exception as e:
            logger.error(f"Error processing log file {log_file}: {e}")
            continue

    return _get_summary_stats(all_entries, hours)


@admin_router.get("/api/usage/error-details", dependencies=[Depends(require_admin_api)])
async def get_error_details(
    request: Request,
    hours: int = Query(
        default=24, ge=1, le=168, description="Hours of history to analyze"
    ),
    limit: int = Query(
        default=100, ge=1, le=1000, description="Maximum number of errors to return"
    ),
) -> dict:
    """Get detailed error information."""
    logs_dir = Path("logs")
    errors: list[dict] = []

    if not logs_dir.exists():
        return {"errors": [], "total_count": 0}

    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=hours)

    for log_file in sorted(logs_dir.glob("app_*.log"), reverse=True):
        try:
            file_date_str = log_file.stem.split("_")[1]
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

            if file_date < cutoff_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ):
                continue

            entries = _parse_log_file(log_file)

            for entry in entries:
                if entry.get("levelname", "").upper() == "ERROR":
                    timestamp_str = entry.get("asctime", "")
                    if timestamp_str:
                        log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                        log_time = log_time.replace(tzinfo=timezone.utc)

                        if log_time >= cutoff_date:
                            errors.append(
                                {
                                    "timestamp": timestamp_str,
                                    "message": entry.get("message", ""),
                                    "error_type": entry.get("error_type", "unknown"),
                                    "pathname": entry.get("pathname", ""),
                                    "lineno": entry.get("lineno", 0),
                                    "request_id": entry.get("request_id", ""),
                                }
                            )

                if len(errors) >= limit:
                    break

        except Exception as e:
            logger.error(f"Error processing log file {log_file}: {e}")
            continue

        if len(errors) >= limit:
            break

    errors.sort(key=lambda x: x["timestamp"], reverse=True)

    return {"errors": errors[:limit], "total_count": len(errors)}


@admin_router.get(
    "/api/usage/revenue-by-model", dependencies=[Depends(require_admin_api)]
)
async def get_revenue_by_model(
    request: Request,
    hours: int = Query(
        default=24, ge=1, le=168, description="Hours of history to analyze"
    ),
    limit: int = Query(
        default=20, ge=1, le=100, description="Maximum number of models to return"
    ),
) -> dict:
    """
    Get revenue breakdown by model.
    
    CRITICAL: This function parses specific log messages for revenue tracking.
    See routstr/core/logging.py for documentation of critical log messages.
    Changes to log messages may break revenue calculations per model.
    """
    logs_dir = Path("logs")
    all_entries: list[dict] = []

    if not logs_dir.exists():
        return {"models": [], "total_revenue_sats": 0}

    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=hours)

    for log_file in sorted(logs_dir.glob("app_*.log")):
        try:
            file_date_str = log_file.stem.split("_")[1]
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

            if file_date < cutoff_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ):
                continue

            entries = _parse_log_file(log_file)
            all_entries.extend(entries)
        except Exception as e:
            logger.error(f"Error processing log file {log_file}: {e}")
            continue

    model_stats: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {
            "revenue_msats": 0,
            "refunds_msats": 0,
            "requests": 0,
            "successful": 0,
            "failed": 0,
        }
    )

    for entry in all_entries:
        try:
            timestamp_str = entry.get("asctime", "")
            if not timestamp_str:
                continue

            log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            log_time = log_time.replace(tzinfo=timezone.utc)

            if log_time < cutoff_date:
                continue

            model = entry.get("model", "unknown")
            if not isinstance(model, str):
                model = "unknown"

            message = entry.get("message", "").lower()

            if "received proxy request" in message:
                model_stats[model]["requests"] += 1

            if "token adjustment completed" in message:
                model_stats[model]["successful"] += 1
                cost_data = entry.get("cost_data")
                if isinstance(cost_data, dict):
                    actual_cost = cost_data.get("actual_cost", 0)
                    if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                        model_stats[model]["revenue_msats"] += actual_cost

            if "revert payment" in message or "upstream request failed" in message:
                model_stats[model]["failed"] += 1
                if "revert payment" in message:
                    max_cost = entry.get("max_cost_for_model", 0)
                    if isinstance(max_cost, (int, float)) and max_cost > 0:
                        model_stats[model]["refunds_msats"] += max_cost

        except Exception:
            continue

    models = []
    total_revenue = 0.0

    for model, stats in model_stats.items():
        revenue_msats_raw = stats["revenue_msats"]
        assert isinstance(revenue_msats_raw, (int, float))
        revenue_msats_val = float(revenue_msats_raw)
        
        refunds_msats_raw = stats["refunds_msats"]
        assert isinstance(refunds_msats_raw, (int, float))
        refunds_msats_val = float(refunds_msats_raw)
        
        revenue_sats = revenue_msats_val / 1000
        refunds_sats = refunds_msats_val / 1000
        net_revenue_sats = revenue_sats - refunds_sats

        total_revenue += net_revenue_sats

        requests_raw = stats["requests"]
        assert isinstance(requests_raw, int)
        requests_val = requests_raw
        
        successful_raw = stats["successful"]
        assert isinstance(successful_raw, int)
        successful_val = successful_raw
        
        failed_raw = stats["failed"]
        assert isinstance(failed_raw, int)
        failed_val = failed_raw
        
        models.append(
            {
                "model": model,
                "revenue_sats": revenue_sats,
                "refunds_sats": refunds_sats,
                "net_revenue_sats": net_revenue_sats,
                "requests": requests_val,
                "successful": successful_val,
                "failed": failed_val,
                "avg_revenue_per_request": (
                    revenue_sats / successful_val if successful_val > 0 else 0
                ),
            }
        )

    models.sort(key=lambda x: float(x["net_revenue_sats"]), reverse=True)

    return {
        "models": models[:limit],
        "total_revenue_sats": total_revenue,
        "total_models": len(models),
    }
