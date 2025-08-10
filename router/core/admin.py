from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import select

from ..wallet import get_balance, send_token
from .db import ApiKey, create_session
from .settings import SettingsManager

admin_router = APIRouter(prefix="/admin", include_in_schema=False)


class WithdrawRequest(BaseModel):
    amount: int


def login_form() -> str:
    return """<!DOCTYPE html>
    <html>
        <head>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                }
                form {
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                }
                input[type="password"] {
                    padding: 8px;
                }
                button {
                    padding: 8px;
                    cursor: pointer;
                }
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
            <form onsubmit="handleSubmit(event)">
                <input type="password" id="password" placeholder="Admin Password" required>
                <button type="submit">Login</button>
            </form>
        </body>
    </html>
    """


def info(content: str) -> str:
    return f"""<!DOCTYPE html>
    <html>
        <head>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                }}
            </style>
        </head>
        <body>
            <div style="text-align: center;">
                {content}
            </div>
        </body>
    </html>
    """


async def admin_auth() -> str:
    admin_password = await SettingsManager.get("ADMIN_PASSWORD", "")
    if admin_password == "":
        return info("Please set a secure ADMIN_PASSWORD= in your ENV variables.")
    else:
        return login_form()


async def dashboard(request: Request) -> str:
    # fetch cashu / api-key data from database
    async with create_session() as session:
        result = await session.exec(select(ApiKey))
        api_keys = result.all()

    api_keys_table_rows = []
    for key in api_keys:
        expiry_time_utc = (
            datetime.fromtimestamp(key.key_expiry_time, tz=timezone.utc)
            if key.key_expiry_time is not None
            else None
        )
        expiry_time_human_readable = (
            expiry_time_utc.strftime("%Y-%m-%d %H:%M:%S") if expiry_time_utc else ""
        )

        api_keys_table_rows.append(
            f"<tr><td>{key.hashed_key}</td><td>{key.balance}</td><td>{key.total_spent}</td><td>{key.total_requests}</td><td>{key.refund_address}</td><td>{'{} ({} UTC)'.format(key.key_expiry_time, expiry_time_human_readable) if key.key_expiry_time else key.key_expiry_time}</td></tr>"
        )

    # Calculate the total balance of all API keys using integer arithmetic to
    # avoid rounding issues.
    total_user_balance = sum(key.balance for key in api_keys) // 1000
    # Fetch balance from cashu
    current_balance = await get_balance("sat")
    owner_balance = current_balance - total_user_balance

    return f"""<!DOCTYPE html>
    <html>
        <head>
            <style>
                table {{
                    width: 100%;
                    border-collapse: collapse;
                }}
                th, td {{
                    border: 1px solid black;
                    padding: 8px;
                    text-align: left;
                }}
                button {{
                    padding: 8px 16px;
                    cursor: pointer;
                    background-color: #007bff;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    margin-right: 10px;
                }}
                button:hover {{
                    background-color: #0056b3;
                }}
                button:disabled {{
                    background-color: #6c757d;
                    cursor: not-allowed;
                }}
                #token-result {{
                    margin-top: 20px;
                    padding: 15px;
                    background-color: #f8f9fa;
                    border: 1px solid #dee2e6;
                    border-radius: 4px;
                    word-break: break-all;
                    display: none;
                    max-width: 100%;
                }}
                #token-text {{
                    font-family: monospace;
                    font-size: 12px;
                    background-color: #e9ecef;
                    padding: 10px;
                    border-radius: 4px;
                    margin: 10px 0;
                }}
                .copy-btn {{
                    background-color: #28a745;
                    padding: 4px 8px;
                    font-size: 12px;
                }}
                .copy-btn:hover {{
                    background-color: #1e7e34;
                }}
                .refresh-btn {{
                    background-color: #ffc107;
                    color: black;
                }}
                .refresh-btn:hover {{
                    background-color: #e0a800;
                }}
                .modal {{
                    display: none;
                    position: fixed;
                    z-index: 1;
                    left: 0;
                    top: 0;
                    width: 100%;
                    height: 100%;
                    background-color: rgba(0,0,0,0.4);
                }}
                .modal-content {{
                    background-color: #fefefe;
                    margin: 15% auto;
                    padding: 20px;
                    border: 1px solid #888;
                    width: 300px;
                    border-radius: 8px;
                    text-align: center;
                }}
                .close {{
                    color: #aaa;
                    float: right;
                    font-size: 28px;
                    font-weight: bold;
                    cursor: pointer;
                }}
                .close:hover {{
                    color: black;
                }}
                input[type="number"] {{
                    width: 100%;
                    padding: 8px;
                    margin: 10px 0;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                }}
                .warning {{
                    color: #dc3545;
                    font-weight: bold;
                    margin: 10px 0;
                }}
            </style>
            <script>
                function openWithdrawModal() {{
                    const modal = document.getElementById('withdraw-modal');
                    const amountInput = document.getElementById('withdraw-amount');
                    amountInput.value = {owner_balance};
                    modal.style.display = 'block';
                }}

                function closeWithdrawModal() {{
                    const modal = document.getElementById('withdraw-modal');
                    modal.style.display = 'none';
                }}

                function checkAmount() {{
                    const amount = parseInt(document.getElementById('withdraw-amount').value);
                    const warning = document.getElementById('withdraw-warning');
                    const ownerBalance = {owner_balance};
                    
                    if (amount > ownerBalance && amount <= {current_balance}) {{
                        warning.style.display = 'block';
                    }} else {{
                        warning.style.display = 'none';
                    }}
                }}

                async function performWithdraw() {{
                    const amount = parseInt(document.getElementById('withdraw-amount').value);
                    const button = document.getElementById('confirm-withdraw-btn');
                    const tokenResult = document.getElementById('token-result');
                    
                    if (!amount || amount <= 0) {{
                        alert('Please enter a valid amount');
                        return;
                    }}
                    
                    if (amount > {current_balance}) {{
                        alert('Amount exceeds wallet balance');
                        return;
                    }}
                    
                    button.disabled = true;
                    button.textContent = 'Withdrawing...';
                    
                    try {{
                        const response = await fetch('/admin/withdraw', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }},
                            credentials: 'same-origin',
                            body: JSON.stringify({{ amount: amount }})
                        }});
                        
                        if (response.ok) {{
                            const data = await response.json();
                            document.getElementById('token-text').textContent = data.token;
                            tokenResult.style.display = 'block';
                            closeWithdrawModal();
                        }} else {{
                            const errorData = await response.json();
                            alert('Failed to withdraw balance: ' + (errorData.detail || 'Unknown error'));
                        }}
                    }} catch (error) {{
                        alert('Error: ' + error.message);
                    }} finally {{
                        button.disabled = false;
                        button.textContent = 'Withdraw';
                    }}
                }}

                function copyToken() {{
                    const tokenText = document.getElementById('token-text');
                    navigator.clipboard.writeText(tokenText.textContent).then(() => {{
                        const copyBtn = document.getElementById('copy-btn');
                        const originalText = copyBtn.textContent;
                        copyBtn.textContent = 'Copied!';
                        setTimeout(() => {{
                            copyBtn.textContent = originalText;
                        }}, 2000);
                    }}).catch(err => {{
                        alert('Failed to copy token');
                    }});
                }}

                function refreshPage() {{
                    window.location.reload();
                }}

                window.onclick = function(event) {{
                    const modal = document.getElementById('withdraw-modal');
                    if (event.target == modal) {{
                        closeWithdrawModal();
                    }}
                }}
            </script>
        </head>
        <body>
            <h1>Admin Dashboard</h1>
            <h2>Current Cashu Balance</h2>
            <p>Your Balance: {owner_balance} sats</p>
            <p>The balance is calculated by subtracting the combined user balance from the total Cashu wallet balance.</p>
            <p>Total Cashu Balance: {current_balance} sats</p>
            <p>User Balance: {total_user_balance} sats</p>
            
            <button id="withdraw-btn" onclick="openWithdrawModal()" {"disabled" if current_balance <= 0 else ""}>
                Withdraw Balance
            </button>
            <button class="refresh-btn" onclick="refreshPage()">
                Refresh Dashboard
            </button>
            
            <div id="withdraw-modal" class="modal">
                <div class="modal-content">
                    <span class="close" onclick="closeWithdrawModal()">&times;</span>
                    <h3>Withdraw Balance</h3>
                    <p>Enter amount to withdraw (sats):</p>
                    <input type="number" id="withdraw-amount" min="1" max="{current_balance}" placeholder="Amount in sats" oninput="checkAmount()">
                    <p>Maximum: {current_balance} sats</p>
                    <p>Your recommended balance: {owner_balance} sats</p>
                    <div id="withdraw-warning" class="warning" style="display: none;">
                        ⚠️ Warning: Withdrawing more than your balance will use user funds!
                    </div>
                    <button id="confirm-withdraw-btn" onclick="performWithdraw()">Withdraw</button>
                    <button onclick="closeWithdrawModal()" style="background-color: #6c757d;">Cancel</button>
                </div>
            </div>
            
            <div id="token-result">
                <strong>Withdrawal Token:</strong>
                <div id="token-text"></div>
                <button id="copy-btn" class="copy-btn" onclick="copyToken()">Copy Token</button>
                <p><em>Save this token! It represents your withdrawn balance.</em></p>
            </div>
            
            <h2>User's API Keys</h2>
            <table>
                <tr>
                    <th>Hashed Key</th>
                    <th>Balance (mSats)</th>
                    <th>Total Spent (mSats)</th>
                    <th>Total Requests</th>
                    <th>Refund Address</th>
                    <th>Refund Time</th>
                </tr>
                {"".join(api_keys_table_rows)}
            </table>
        </body>
    </html>
    """


@admin_router.get("/", response_class=HTMLResponse)
async def admin(request: Request) -> str:
    admin_cookie = request.cookies.get("admin_password")
    admin_password = await SettingsManager.get("ADMIN_PASSWORD", "")
    if admin_cookie and admin_cookie == admin_password:
        return await dashboard(request)
    return await admin_auth()


@admin_router.post("/withdraw")
async def withdraw(
    request: Request, withdraw_request: WithdrawRequest
) -> dict[str, str]:
    admin_cookie = request.cookies.get("admin_password")
    admin_password = await SettingsManager.get("ADMIN_PASSWORD", "")
    if not admin_cookie or admin_cookie != admin_password:
        raise HTTPException(status_code=403, detail="Unauthorized")

    current_balance = await get_balance("sat")

    if withdraw_request.amount <= 0:
        raise HTTPException(
            status_code=400, detail="Withdrawal amount must be positive"
        )

    if withdraw_request.amount > current_balance:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    token = await send_token(withdraw_request.amount, "sat")
    return {"token": token}
