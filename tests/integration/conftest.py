import asyncio
import json
import os
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import select

from router.core.logging import get_logger

logger = get_logger(__name__)

# Configure test environment based on whether we're using local services or not
use_local_services = os.environ.get("USE_LOCAL_SERVICES", "0") == "1"

if use_local_services:
    # Docker mode: Use Docker services for more realistic testing
    logger.info("🐳 Using Docker services for integration tests")
    test_env = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "UPSTREAM_BASE_URL": "http://localhost:3000",  # Mock OpenAI service
        "UPSTREAM_API_KEY": "test-upstream-key",
        "CASHU_MINTS": "http://mint:3338",  # Docker service name for router validation
        "MINT": "http://mint:3338",
        "MINT_URL": "http://mint:3338",
        "NOSTR_RELAY_URL": "ws://localhost:8088",
        "RECEIVE_LN_ADDRESS": "test@routstr.com",
        "REFUND_PROCESSING_INTERVAL": "3600",
        "NSEC": "nsec1testkey1234567890abcdef",
        "COST_PER_REQUEST": "10",
        "MODEL_BASED_PRICING": "true",
        "MINIMUM_PAYOUT": "1000",
        "PAYOUT_INTERVAL": "86400",
        "NAME": "TestRoutstrNode",
        "DESCRIPTION": "Test Node for Integration Tests",
        "NPUB": "npub1test",
        "HTTP_URL": "http://localhost:8000",
        "ONION_URL": "http://test.onion",
        "CORS_ORIGINS": "*",
    }
else:
    # Mock mode: Use in-memory mocks for fast testing
    logger.info("🎭 Using mocked services for integration tests")
    test_env = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "UPSTREAM_BASE_URL": "https://api.openai.com/v1",
        "UPSTREAM_API_KEY": "test-upstream-key",
        "CASHU_MINTS": "http://localhost:3338",
        "RECEIVE_LN_ADDRESS": "test@routstr.com",
        "REFUND_PROCESSING_INTERVAL": "3600",
        "NSEC": "nsec1testkey1234567890abcdef",
        "COST_PER_REQUEST": "10",
        "MODEL_BASED_PRICING": "true",
        "MINIMUM_PAYOUT": "1000",
        "PAYOUT_INTERVAL": "86400",
    }

# Set test environment variables before importing the app
os.environ.update(test_env)

from router.core.db import ApiKey, get_session  # noqa: E402
from router.core.main import app, lifespan  # noqa: E402


@pytest.fixture(scope="session")
def test_mode() -> str:
    """Returns current test mode for clarity"""
    if os.environ.get("USE_LOCAL_SERVICES") == "1":
        print("\n🐳 Running with Docker services (realistic mode)")
        return "docker"
    else:
        print("\n🎭 Running with mocked services (fast mode)")
        return "mock"


class TestmintWallet:
    """Test wallet that simulates Cashu mint interactions for testing"""

    def __init__(
        self, mint_url: Optional[str] = None, nsec: Optional[str] = None
    ) -> None:
        # Use the configured CASHU_MINTS URL, fallback to MINT, or default
        configured_mint_url = (
            mint_url
            or os.environ.get("CASHU_MINTS", "").split(",")[0].strip()
            or os.environ.get("MINT", "http://localhost:3338")
        )

        # For local services, use localhost for connection but mint service name for token creation
        if os.environ.get("USE_LOCAL_SERVICES") == "1":
            self.connection_url = configured_mint_url.replace(
                "http://mint:", "http://localhost:"
            )
            self.mint_url = configured_mint_url  # Keep Docker service name for tokens
        else:
            self.connection_url = configured_mint_url
            self.mint_url = configured_mint_url
        # Use a valid test nsec for testing (this is a well-known test key)
        self.nsec = (
            nsec or "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
        )
        self.wallet = None
        self.tokens: List[Dict[str, Any]] = []
        self.spent_tokens: List[str] = []
        self.refund_history: List[Dict[str, Any]] = []

    async def init(self) -> None:
        """Initialize the sixty_nuts wallet"""
        # In mock mode, we don't actually create a real wallet
        # This is just a placeholder for the mock implementation
        self.wallet = None

    async def mint_tokens(self, amount: int) -> str:
        """Create a test token for the testmint"""
        logger.info(
            f"Creating test token for {amount} sats from testmint {self.mint_url}"
        )

        # For integration tests, use fallback tokens to avoid external dependencies
        return await self._create_fallback_token(amount)

    async def _create_real_token(self, amount: int) -> str:
        """Create real tokens using the testmint"""
        import tempfile

        from cashu.wallet.wallet import Wallet

        logger.info(
            f"Creating real token for {amount} sats from testmint {self.connection_url}"
        )

        try:
            # Create a temporary wallet to mint real tokens
            with tempfile.TemporaryDirectory() as temp_dir:
                wallet_db_path = os.path.join(temp_dir, "test_wallet.db")

                wallet = await Wallet.with_db(
                    self.connection_url,  # Connect via localhost
                    db=f"sqlite+aiosqlite:///{wallet_db_path}",
                    load_all_keysets=True,
                    unit="sat",
                )

                # Load mint information
                await wallet.load_mint()

                # Request a mint quote
                quote_response = await wallet.mint_quote(amount=amount, unit="sat")
                quote = quote_response.quote

                # Mint tokens (simulate payment by directly calling mint endpoint)
                mint_response = await wallet.mint(amount=amount, hash=quote)
                token = mint_response.token

                # Replace connection URL with Docker service name for router validation
                if self.connection_url != self.mint_url:
                    token = token.replace(self.connection_url, self.mint_url)

                logger.info(f"Successfully minted real token for {amount} sats")
                return token
        except Exception as e:
            logger.error(f"Failed to mint real token: {e}")
            raise

    async def _create_fallback_token(self, amount: int) -> str:
        """Fallback method to create a basic test token"""
        import base64
        import json
        import random
        import time

        unique_id = int(time.time() * 1000000) + random.randint(1000, 9999)
        token_data = {
            "token": [
                {
                    "mint": self.mint_url,
                    "proofs": [
                        {
                            "id": f"009a1f293253e41e{unique_id % 100000000:08d}",
                            "amount": amount,
                            "secret": f"test-secret-{amount}-{unique_id}",
                            "C": "02194603ffa36356f4a56b7df9371fc3192472351453ec7398b8da8117e7c3e104",
                        }
                    ],
                }
            ],
            "unit": "sat",
            "memo": f"Test token {amount} sats",
        }

        token_json = json.dumps(token_data)
        token_base64 = base64.urlsafe_b64encode(token_json.encode()).decode()
        return f"cashuA{token_base64}"

    async def redeem_token(self, token: str) -> Tuple[int, str, str]:
        """Redeem a Cashu token - compatible with wallet.recieve_token"""
        if not self.wallet:
            await self.init()

        # For testing, simulate the redemption
        import base64

        if not token.startswith("cashuA"):
            raise ValueError("Invalid token format")

        try:
            token_base64 = token[6:]  # Remove "cashuA" prefix
            # Add padding if necessary
            padding = (4 - len(token_base64) % 4) % 4
            token_base64 += "=" * padding
            token_json = base64.urlsafe_b64decode(token_base64).decode()
            token_data = json.loads(token_json)

            total_amount = 0
            mint_url = self.mint_url
            unit = token_data.get("unit", "sat")

            for mint_tokens in token_data["token"]:
                mint_url = mint_tokens.get("mint", self.mint_url)
                for proof in mint_tokens["proofs"]:
                    # Check if token was already spent
                    if proof["id"] in self.spent_tokens:
                        raise ValueError("Token already spent")

                    self.spent_tokens.append(proof["id"])
                    total_amount += proof["amount"]

            return total_amount, unit, mint_url

        except Exception as e:
            raise ValueError(f"Failed to decode token: {str(e)}")

    async def redeem_token_simple(self, token: str) -> Tuple[int, str]:
        """Redeem a Cashu token - simple version for credit_balance"""
        amount, unit, mint_url = await self.redeem_token(token)
        return amount, "test_metadata"

    async def send(self, amount: int) -> str:
        """Create a token to send (for refunds)"""
        if not self.wallet:
            await self.init()

        # For testing, create a refund token
        return await self.mint_tokens(amount)

    async def send_token(
        self, amount: int, unit: str, mint_url: Optional[str] = None
    ) -> str:
        """Send token with compatible signature for mocking router.wallet.send_token"""
        return await self.send(amount)

    async def send_to_lnurl(self, lnurl: str, amount: int) -> int:
        """Send to lightning address - simulated for testing"""
        if not self.wallet:
            await self.init()

        self.refund_history.append(
            {
                "amount": amount,
                "ln_address": lnurl,
                "timestamp": asyncio.get_event_loop().time(),
            }
        )
        return amount

    async def get_balance(self) -> int:
        """Get wallet balance"""
        if not self.wallet:
            await self.init()

        # For testing, return a simulated balance
        return 100000  # 100k sats

    async def credit_balance(
        self, cashu_token: str, key: ApiKey, session: AsyncSession
    ) -> int:
        """Credit balance to API key - test implementation"""
        try:
            logger.info(
                f"TestmintWallet.credit_balance called with token: {cashu_token[:20]}..."
            )

            # Redeem the token to get amount
            amount, _ = await self.redeem_token_simple(cashu_token)
            logger.info(f"TestmintWallet.credit_balance redeemed amount: {amount}")

            # For testing, convert to msat if needed
            amount_msat = amount * 1000  # Assume tokens are in sats
            logger.info(f"TestmintWallet.credit_balance amount in msat: {amount_msat}")

            # Credit the balance using atomic database update to prevent race conditions
            from sqlmodel import col, update

            # Use atomic update to avoid lost update problem in concurrent scenarios
            stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == key.hashed_key)
                .values(balance=ApiKey.balance + amount_msat)
            )
            await session.execute(stmt)
            await session.commit()

            # Refresh the key object to get the updated balance
            await session.refresh(key)

            logger.info(
                f"TestmintWallet.credit_balance successfully credited {amount_msat} msat"
            )

            return amount_msat
        except Exception as e:
            logger.error(f"TestmintWallet.credit_balance failed: {e}")
            import traceback

            logger.error(
                f"TestmintWallet.credit_balance full traceback: {traceback.format_exc()}"
            )
            raise ValueError(f"Failed to redeem token: {str(e)}")


@pytest_asyncio.fixture
async def testmint_wallet() -> TestmintWallet:
    """Fixture for testmint wallet instance"""
    # Check if we should use real mint
    mint_url = os.environ.get(
        "MINT_URL", os.environ.get("MINT", "http://localhost:3338")
    )

    wallet = TestmintWallet(mint_url=mint_url)
    await wallet.init()
    return wallet


@pytest_asyncio.fixture
async def test_database_url(tmp_path: Any) -> str:
    """Create a temporary SQLite database file for integration tests"""
    db_file = tmp_path / "test_integration.db"
    return f"sqlite+aiosqlite:///{db_file}"


@pytest_asyncio.fixture
async def integration_engine(test_database_url: str) -> AsyncGenerator[Any, None]:
    """Create an async engine for integration tests"""
    engine = create_async_engine(
        test_database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

    # Initialize database schema
    # Create tables using the engine directly since init_db uses the global engine
    async with engine.begin() as conn:
        from sqlmodel import SQLModel

        await conn.run_sync(SQLModel.metadata.create_all)

    yield engine

    # Cleanup
    await engine.dispose()


@pytest_asyncio.fixture
async def integration_session(
    integration_engine: Any,
) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session for integration tests"""
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        yield session


class DatabaseSnapshot:
    """Utility to capture and compare database states"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.snapshot: Optional[Dict[str, List[Dict]]] = None

    async def capture(self) -> Dict[str, List[Dict]]:
        """Capture current database state"""
        # Get all API keys with their data
        result = await self.session.execute(select(ApiKey))
        api_keys = result.scalars().all()

        snapshot = {
            "api_keys": [
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
        }

        self.snapshot = snapshot
        return snapshot

    async def diff(
        self, new_snapshot: Optional[Dict[str, List[Dict]]] = None
    ) -> Dict[str, Any]:
        """Calculate differences between snapshots"""
        if new_snapshot is None:
            new_snapshot = await self.capture()

        if self.snapshot is None:
            raise ValueError("No initial snapshot to compare against")

        diff: Dict[str, Dict[str, List[Any]]] = {
            "api_keys": {"added": [], "removed": [], "modified": []}
        }

        # Create lookup maps
        old_keys = {k["hashed_key"]: k for k in self.snapshot["api_keys"]}
        new_keys = {k["hashed_key"]: k for k in new_snapshot["api_keys"]}

        # Find added keys
        for key_id in new_keys:
            if key_id not in old_keys:
                diff["api_keys"]["added"].append(new_keys[key_id])

        # Find removed keys
        for key_id in old_keys:
            if key_id not in new_keys:
                diff["api_keys"]["removed"].append(old_keys[key_id])

        # Find modified keys
        for key_id in old_keys:
            if key_id in new_keys:
                old = old_keys[key_id]
                new = new_keys[key_id]
                changes = {}

                for field in [
                    "balance",
                    "total_spent",
                    "total_requests",
                    "refund_address",
                    "key_expiry_time",
                ]:
                    if old[field] != new[field]:
                        changes[field] = {
                            "old": old[field],
                            "new": new[field],
                            "delta": new[field] - old[field]
                            if isinstance(new[field], (int, float))
                            else None,
                        }

                if changes:
                    diff["api_keys"]["modified"].append(
                        {"hashed_key": key_id, "changes": changes}
                    )

        return diff


@pytest_asyncio.fixture
async def db_snapshot(integration_session: AsyncSession) -> DatabaseSnapshot:
    """Database snapshot utility for tracking state changes"""
    return DatabaseSnapshot(integration_session)


@pytest_asyncio.fixture
async def integration_app(
    integration_engine: Any,
    integration_session: AsyncSession,
    testmint_wallet: TestmintWallet,
    test_database_url: str,
) -> AsyncGenerator[FastAPI, None]:
    """Create FastAPI app instance for integration tests"""

    # Override environment with test database URL
    os.environ["DATABASE_URL"] = test_database_url

    # Create a new app instance with our lifespan
    test_app = FastAPI(lifespan=lifespan)

    # Copy all routes from the main app
    test_app.router = app.router

    # Override the get_session dependency
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield integration_session

    test_app.dependency_overrides[get_session] = override_get_session

    # Check if we should use real mint
    use_real_mint = os.environ.get("USE_REAL_MINT", "false").lower() == "true"

    if use_real_mint:
        # Use real mint - no wallet patches needed
        with patch("router.core.db.engine", integration_engine):
            yield test_app
    else:
        # Use testmint with wallet patches for all integration tests
        mint_url = os.environ.get("CASHU_MINTS", "http://localhost:3338")
        with (
            patch("router.core.db.engine", integration_engine),
            patch("router.wallet._get_trusted_mints", AsyncMock(return_value=[mint_url])),
            patch("router.wallet._get_primary_mint_url", AsyncMock(return_value=mint_url)),
            patch("router.auth.credit_balance", testmint_wallet.credit_balance),
            patch("router.wallet.credit_balance", testmint_wallet.credit_balance),
            patch("router.balance.credit_balance", testmint_wallet.credit_balance),
            patch("router.wallet.send_token", testmint_wallet.send_token),
            patch("router.balance.send_token", testmint_wallet.send_token),
            patch("router.wallet.recieve_token", testmint_wallet.redeem_token),
            patch("router.wallet.get_balance", testmint_wallet.get_balance),
            patch("websockets.connect") as mock_websockets,
            patch("router.payment.price.btc_usd_ask_price", return_value=50000.0),
            patch("router.payment.price.sats_usd_ask_price", return_value=0.0005),
        ):
            # Configure the WebSocket mock for discovery service - fast failure for performance tests
            async def mock_websocket_connect(*args: Any, **kwargs: Any) -> None:
                raise ConnectionError("Mock connection failed")

            mock_websockets.side_effect = mock_websocket_connect

            yield test_app


@pytest_asyncio.fixture
async def integration_client(
    integration_app: FastAPI,
    integration_engine: Any,  # Ensure engine is created first
) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for integration tests"""
    from httpx import ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=integration_app),  # type: ignore
        base_url="http://test",
        timeout=30.0,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def authenticated_client(
    integration_client: AsyncClient,
    testmint_wallet: TestmintWallet,
    integration_session: AsyncSession,
) -> AsyncClient:
    """Create an authenticated client with a persistent API key"""
    # Generate a cashu token
    test_token = await testmint_wallet.mint_tokens(10000)  # 10k sats

    # Use the cashu token as Bearer auth to create an API key
    integration_client.headers["Authorization"] = f"Bearer {test_token}"

    # Make a request to create the API key (first use of cashu token creates the key)
    response = await integration_client.get("/v1/wallet/info")
    assert response.status_code == 200
    wallet_info = response.json()
    api_key = wallet_info["api_key"]

    # Now switch to using the persistent API key
    integration_client.headers["Authorization"] = f"Bearer {api_key}"

    # Store the API key and balance for tests that need it
    integration_client._test_api_key = api_key  # type: ignore
    integration_client._test_balance = wallet_info["balance"]  # type: ignore

    return integration_client


@pytest_asyncio.fixture
async def create_api_key() -> Callable:
    """Helper to create new API keys for testing"""

    async def _create_key(
        client: AsyncClient,
        wallet: TestmintWallet,
        amount: int = 1000,
        refund_address: Optional[str] = None,
        key_expiry_time: Optional[int] = None,
    ) -> Tuple[str, int]:
        """Create a new API key and return (api_key, balance)"""
        # Generate cashu token
        token = await wallet.mint_tokens(amount)

        # Create headers
        headers = {"Authorization": f"Bearer {token}"}
        if refund_address:
            headers["Refund-LNURL"] = refund_address
        if key_expiry_time:
            headers["Key-Expiry-Time"] = str(key_expiry_time)

        # Use the token to create API key
        response = await client.get("/v1/wallet/info", headers=headers)
        assert response.status_code == 200

        wallet_info = response.json()
        return wallet_info["api_key"], wallet_info["balance"]

    return _create_key


@pytest.fixture
def mock_upstream_server() -> Any:
    """Mock upstream API server responses"""
    responses: Dict[str, Any] = {}

    class MockResponse:
        def __init__(
            self,
            status_code: int,
            json_data: Any = None,
            text_data: Optional[str] = None,
        ) -> None:
            self.status_code = status_code
            self._json_data = json_data
            self._text_data = text_data
            self.headers = {"content-type": "application/json"}

        def json(self) -> Any:
            return self._json_data

        @property
        def text(self) -> str:
            return self._text_data or ""

        async def aiter_bytes(
            self, chunk_size: Optional[int] = None
        ) -> AsyncGenerator[bytes, None]:
            """Async iterator for streaming responses"""
            if self._text_data:
                yield self._text_data.encode()

    def add_response(method: str, path: str, response: MockResponse) -> None:
        """Add a mock response for a specific method and path"""
        responses[f"{method}:{path}"] = response

    def get_response(method: str, path: str) -> MockResponse:
        """Get mock response for a request"""
        key = f"{method}:{path}"
        if key in responses:
            return responses[key]
        # Default 404 response
        return MockResponse(404, {"error": "Not found"})

    mock_server = MagicMock()
    mock_server.add_response = add_response
    mock_server.get_response = get_response
    mock_server.responses = responses

    return mock_server


@pytest_asyncio.fixture
async def background_tasks_controller() -> AsyncGenerator[Any, None]:
    """Control background tasks during tests"""
    tasks: List[asyncio.Task] = []

    class TaskController:
        def __init__(self) -> None:
            self.paused = False
            self.cancelled = False

        async def pause(self) -> None:
            """Pause all background tasks"""
            self.paused = True

        async def resume(self) -> None:
            """Resume all background tasks"""
            self.paused = False

        async def cancel_all(self) -> None:
            """Cancel all background tasks"""
            self.cancelled = True
            for task in tasks:
                task.cancel()

    controller = TaskController()

    # Patch background task functions to respect controller
    original_update_pricing: Optional[Callable] = None
    original_periodic_payout: Optional[Callable] = None

    try:
        from router.payment.models import update_sats_pricing
        from router.wallet import periodic_payout

        async def controlled_update_pricing() -> None:
            while not controller.cancelled:
                if not controller.paused and original_update_pricing:
                    await original_update_pricing()
                await asyncio.sleep(1)

        async def controlled_periodic_payout() -> None:
            while not controller.cancelled:
                if not controller.paused and original_periodic_payout:
                    await original_periodic_payout()
                await asyncio.sleep(1)

        # Store originals and patch
        original_update_pricing = update_sats_pricing
        original_periodic_payout = periodic_payout

    except ImportError:
        pass

    yield controller

    # Cleanup
    controller.cancelled = True
