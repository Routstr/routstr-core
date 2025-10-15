import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import select

from ..payment.models import Model, get_model_by_id, list_models
from ..payment.models import sync_models_with_api
from ..wallet import (
    fetch_all_balances,
    get_proofs_per_mint_and_unit,
    get_wallet,
    send_token,
    slow_filter_spend_proofs,
)
from .db import ApiKey, ModelRow, create_session
from .logging import get_logger
from .settings import SettingsService, settings

logger = get_logger(__name__)

admin_router = APIRouter(prefix="/admin", include_in_schema=False)


def require_admin_api(request: Request) -> None:
    admin_cookie = request.cookies.get("admin_password")
    if not admin_cookie or admin_cookie != settings.admin_password:
        raise HTTPException(status_code=403, detail="Unauthorized")


def is_admin_authenticated(request: Request) -> bool:
    admin_cookie = request.cookies.get("admin_password")
    return bool(admin_cookie and admin_cookie == settings.admin_password)


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


@admin_router.patch("/api/settings", dependencies=[Depends(require_admin_api)])
async def update_settings(request: Request, update: SettingsUpdate) -> dict:
    async with create_session() as session:
        new_settings = await SettingsService.update(update.__root__, session)
    data = new_settings.dict()
    if "upstream_api_key" in data:
        data["upstream_api_key"] = "[REDACTED]" if data["upstream_api_key"] else ""
    if "admin_password" in data:
        data["admin_password"] = "[REDACTED]" if data["admin_password"] else ""
    if "nsec" in data:
        data["nsec"] = "[REDACTED]" if data["nsec"] else ""
    return data


class SetupRequest(BaseModel):
    password: str


@admin_router.post("/api/setup")
async def initial_setup(request: Request, payload: SetupRequest) -> dict[str, object]:
    try:
        current = SettingsService.get()
    except Exception:
        current = settings
    if getattr(current, "admin_password", ""):
        raise HTTPException(status_code=409, detail="Admin password already set")
    pw = (payload.password or "").strip()
    if len(pw) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )
    async with create_session() as session:
        await SettingsService.update({"admin_password": pw}, session)
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
    try:
        settings = SettingsService.get()
        admin_pw = settings.admin_password
    except Exception:
        admin_pw = os.getenv("ADMIN_PASSWORD", "")
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

                function openSyncModelsModal() {
                    const modal = document.getElementById('sync-models-modal');
                    modal.style.display = 'block';
                }

                function closeSyncModelsModal() {
                    const modal = document.getElementById('sync-models-modal');
                    modal.style.display = 'none';
                }

                async function syncModels() {
                    const deleteRemoved = document.getElementById('delete-removed-models').checked;
                    const button = document.getElementById('sync-models-btn');
                    const resultDiv = document.getElementById('sync-models-result');
                    
                    button.disabled = true;
                    button.textContent = 'Syncing...';
                    resultDiv.style.display = 'none';
                    
                    try {
                        const response = await fetch('/admin/api/sync_models', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                            },
                            credentials: 'same-origin',
                            body: JSON.stringify({
                                delete_removed: deleteRemoved
                            })
                        });
                        
                        if (response.ok) {
                            const data = await response.json();
                            resultDiv.innerHTML = `<strong>✅ Success!</strong><br>${data.message}`;
                            resultDiv.style.display = 'block';
                            resultDiv.style.backgroundColor = '#d4edda';
                            resultDiv.style.borderColor = '#c3e6cb';
                            resultDiv.style.color = '#155724';
                        } else {
                            const errorData = await response.json();
                            resultDiv.innerHTML = `<strong>❌ Error:</strong><br>${errorData.detail || 'Unknown error'}`;
                            resultDiv.style.display = 'block';
                            resultDiv.style.backgroundColor = '#f8d7da';
                            resultDiv.style.borderColor = '#f5c6cb';
                            resultDiv.style.color = '#721c24';
                        }
                    } catch (error) {
                        resultDiv.innerHTML = `<strong>❌ Error:</strong><br>${error.message}`;
                        resultDiv.style.display = 'block';
                        resultDiv.style.backgroundColor = '#f8d7da';
                        resultDiv.style.borderColor = '#f5c6cb';
                        resultDiv.style.color = '#721c24';
                    } finally {
                        button.disabled = false;
                        button.textContent = 'Sync Models';
                    }
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
                    const syncModelsModal = document.getElementById('sync-models-modal');
                    if (event.target == withdrawModal) {
                        closeWithdrawModal();
                    } else if (event.target == investigateModal) {
                        closeInvestigateModal();
                    } else if (event.target == settingsModal) {
                        closeSettingsModal();
                    } else if (event.target == syncModelsModal) {
                        closeSyncModelsModal();
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
            <button onclick="window.location.href='/admin/models'">
                🧩 Edit Models
            </button>
            <button onclick="openSettingsModal()">
                ⚙️ Settings
            </button>
            <button onclick="openSyncModelsModal()">
                🔄 Sync Models
            </button>
            
            <div id="sync-models-modal" class="modal">
                <div class="modal-content">
                    <span class="close" onclick="closeSyncModelsModal()">&times;</span>
                    <h3>Sync Models from API</h3>
                    <p>This will fetch the latest models from OpenRouter and update the database.</p>
                    <div style="margin: 15px 0;">
                        <label style="display: flex; align-items: center; gap: 10px; cursor: pointer;">
                            <input type="checkbox" id="delete-removed-models" style="width: auto; margin: 0;">
                            <span>Delete models that are no longer available</span>
                        </label>
                        <p style="font-size: 0.85rem; color: #718096; margin-top: 5px; margin-left: 30px;">
                            ⚠️ Warning: This will permanently remove models that are no longer in the API.
                        </p>
                    </div>
                    <div id="sync-models-result" style="display: none; padding: 12px; border-radius: 6px; margin: 15px 0; border: 1px solid;"></div>
                    <button id="sync-models-btn" onclick="syncModels()">🔄 Sync Models</button>
                    <button onclick="closeSyncModelsModal()" style="background-color: #718096;">Cancel</button>
                </div>
            </div>
            
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
async def admin(request: Request) -> str:
    if is_admin_authenticated(request):
        return await dashboard(request)
    return admin_auth()


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

    # Sort entries by timestamp if available
    log_entries.sort(key=lambda x: x.get("asctime", ""), reverse=False)

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


@admin_router.post("/withdraw", dependencies=[Depends(require_admin_api)])
async def withdraw(
    request: Request, withdraw_request: WithdrawRequest
) -> dict[str, str]:
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

class SyncModelsRequest(BaseModel):
    delete_removed: bool = False


@admin_router.post("/api/sync_models", dependencies=[Depends(require_admin_api)])
async def sync_models(request: Request, sync_request: SyncModelsRequest) -> dict:
    try:
        src = settings.source or None
        source_filter = src if src and src.strip() else None
    except Exception:
        source_filter = None

    logger.info(
        "Manual models sync triggered",
        extra={"delete_removed": sync_request.delete_removed},
    )

    counts = await sync_models_with_api(
        source_filter=source_filter, delete_removed=sync_request.delete_removed
    )

    return {
        "success": True,
        "inserted": counts["inserted"],
        "updated": counts["updated"],
        "deleted": counts["deleted"],
        "message": f"Synced: {counts['inserted']} inserted, {counts['updated']} updated, {counts['deleted']} deleted",
    }


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


@admin_router.get("/models", response_class=HTMLResponse)
async def admin_models(request: Request) -> str:
    if is_admin_authenticated(request):
        return models_page()
    return admin_auth()


@admin_router.get("/api/models", dependencies=[Depends(require_admin_api)])
async def get_models_admin_api(request: Request) -> list[dict[str, object]]:
    items = await list_models()
    return [m.dict() for m in items]  # type: ignore


@admin_router.post("/api/models", dependencies=[Depends(require_admin_api)])
async def create_model_admin_api(payload: Model) -> dict[str, object]:
    async with create_session() as session:
        exists = await session.get(ModelRow, payload.id)
        if exists:
            raise HTTPException(
                status_code=409, detail="Model with this ID already exists"
            )
        pricing_dict = payload.pricing.dict()
        for k in ("max_prompt_cost", "max_completion_cost", "max_cost"):
            pricing_dict.pop(k, None)
        row = ModelRow(
            id=payload.id,
            name=payload.name,
            description=payload.description,
            created=int(payload.created),
            context_length=int(payload.context_length),
            architecture=json.dumps(payload.architecture.dict()),
            pricing=json.dumps(pricing_dict),
            sats_pricing=None,
            per_request_limits=(
                json.dumps(payload.per_request_limits)
                if payload.per_request_limits is not None
                else None
            ),
            top_provider=(
                json.dumps(payload.top_provider.dict())
                if payload.top_provider
                else None
            ),
        )
        session.add(row)
        await session.commit()

    created_model = await get_model_by_id(payload.id)
    return created_model.dict() if created_model else {"id": payload.id}  # type: ignore


@admin_router.post("/api/models/batch", dependencies=[Depends(require_admin_api)])
async def batch_create_models(payload: dict[str, object]) -> dict[str, int]:
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise HTTPException(
            status_code=400, detail="Payload must include non-empty 'models' array"
        )
    created = 0
    skipped = 0
    async with create_session() as session:
        for m in models:
            try:
                model = Model(**m)  # type: ignore[arg-type]
            except Exception:
                skipped += 1
                continue
            exists = await session.get(ModelRow, model.id)
            if exists:
                skipped += 1
                continue
            pricing_dict = model.pricing.dict()
            for k in ("max_prompt_cost", "max_completion_cost", "max_cost"):
                pricing_dict.pop(k, None)
            row = ModelRow(
                id=model.id,
                name=model.name,
                description=model.description,
                created=int(model.created),
                context_length=int(model.context_length),
                architecture=json.dumps(model.architecture.dict()),
                pricing=json.dumps(pricing_dict),
                sats_pricing=None,
                per_request_limits=(
                    json.dumps(model.per_request_limits)
                    if model.per_request_limits is not None
                    else None
                ),
                top_provider=(
                    json.dumps(model.top_provider.dict())
                    if model.top_provider
                    else None
                ),
            )
            session.add(row)
            created += 1
        if created:
            await session.commit()
    return {"created": created, "skipped": skipped}


@admin_router.get(
    "/api/models/{model_id:path}", dependencies=[Depends(require_admin_api)]
)
async def get_model_admin_api(model_id: str) -> dict[str, object]:
    model = await get_model_by_id(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model.dict()  # type: ignore


@admin_router.patch(
    "/api/models/{model_id:path}", dependencies=[Depends(require_admin_api)]
)
async def update_model_admin_api(model_id: str, payload: Model) -> dict[str, object]:
    if payload.id != model_id:
        raise HTTPException(status_code=400, detail="Path id does not match payload id")

    async with create_session() as session:
        row = await session.get(ModelRow, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="Model not found")

        row.name = payload.name
        row.description = payload.description
        row.created = int(payload.created)
        row.context_length = int(payload.context_length)
        row.architecture = json.dumps(payload.architecture.dict())
        pricing_dict = payload.pricing.dict()
        for k in ("max_prompt_cost", "max_completion_cost", "max_cost"):
            pricing_dict.pop(k, None)
        row.pricing = json.dumps(pricing_dict)
        row.sats_pricing = None
        row.per_request_limits = (
            json.dumps(payload.per_request_limits)
            if payload.per_request_limits is not None
            else None
        )
        row.top_provider = (
            json.dumps(payload.top_provider.dict()) if payload.top_provider else None
        )

        session.add(row)
        await session.commit()

    updated = await get_model_by_id(model_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Model not found after update")
    return updated.dict()  # type: ignore


@admin_router.delete(
    "/api/models/{model_id:path}", dependencies=[Depends(require_admin_api)]
)
async def delete_model_admin_api(model_id: str) -> dict[str, object]:
    async with create_session() as session:
        row = await session.get(ModelRow, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="Model not found")
        await session.delete(row)
        await session.commit()
    return {"ok": True, "deleted_id": model_id}


@admin_router.delete("/api/models", dependencies=[Depends(require_admin_api)])
async def delete_all_models_admin_api() -> dict[str, object]:
    async with create_session() as session:
        result = await session.exec(select(ModelRow))  # type: ignore
        rows = result.all()
        for row in rows:
            await session.delete(row)  # type: ignore
        await session.commit()
    return {"ok": True, "deleted": "all"}


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
input[type="number"], input[type="text"], select { width: 100%; padding: 10px; margin: 10px 0; border: 2px solid #e2e8f0; border-radius: 6px; font-size: 16px; transition: border 0.2s; }
input[type="number"]:focus, input[type="text"]:focus, select:focus { outline: none; border-color: #4299e1; }
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
