import os
from datetime import datetime, timezone

from fastapi import Request
from sqlmodel import select

from .cashu import WALLET
from .db import ApiKey, create_session


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


def admin_auth() -> str:
    if os.getenv("ADMIN_PASSWORD", "") == "":
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
    current_balance = (await WALLET.fetch_wallet_state()).balance
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
            </style>
        </head>
        <body>
            <h1>Admin Dashboard</h1>
            <h2>Current Cashu Balance</h2>
            <p>Your Balance: {owner_balance} sats</p>
            <p>The balance is calculated by subtracting the combined user balance from the total Cashu wallet balance.</p>
            <p>Total Cashu Balance: {current_balance} sats</p>
            <p>User Balance: {total_user_balance} sats</p>
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


async def admin(request: Request) -> str:
    admin_cookie = request.cookies.get("admin_password")
    if admin_cookie and admin_cookie == os.getenv("ADMIN_PASSWORD"):
        return await dashboard(request)
    return admin_auth()
