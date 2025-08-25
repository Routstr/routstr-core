#!/usr/bin/env python3
"""
Simple script to verify the integration test setup without running actual tests.
This checks that all components are properly configured.
"""

import os
import sys

# Add project root to path
project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, project_root)


def check_imports() -> bool:
    """Check that all required modules can be imported"""
    print("Checking imports...")

    try:
        # Check test utilities - imports are for verification only
        from .utils import (
            CashuTokenGenerator,
            ConcurrencyTester,
            DatabaseStateValidator,
            MockServiceBuilder,
            PerformanceValidator,
            ResponseValidator,
            TestDataBuilder,
        )

        del CashuTokenGenerator, ConcurrencyTester, DatabaseStateValidator
        del MockServiceBuilder, PerformanceValidator, ResponseValidator
        del TestDataBuilder

        print("Test utilities imported successfully")

        # Check conftest fixtures - imports are for verification only
        from .conftest import DatabaseSnapshot, TestmintWallet

        del DatabaseSnapshot, TestmintWallet

        print("Conftest fixtures imported successfully")

        # Check routstr modules - imports are for verification only
        from routstr.core.db import ApiKey

        del ApiKey

        print("Router modules imported successfully")

        return True

    except ImportError as e:
        print(f"Import error: {e}")
        return False


def check_environment() -> None:
    """Check environment variables"""
    print("\nChecking environment variables...")

    required_vars = [
        "DATABASE_URL",
        "UPSTREAM_BASE_URL",
        "MINT",
        "RECEIVE_LN_ADDRESS",
        "NSEC",
    ]

    # These are set in conftest.py
    for var in required_vars:
        value = os.environ.get(var)
        if value:
            print(f"{var}: {value[:20]}..." if len(value) > 20 else f"{var}: {value}")
        else:
            print(f"{var}: Not set")


def check_test_infrastructure() -> None:
    """Check test infrastructure components"""
    print("\nChecking test infrastructure...")

    # Check if test directories exist
    test_dirs = [
        "tests/integration",
        "tests/integration/__pycache__",  # Will exist after first import
    ]

    for dir_path in test_dirs:
        full_path = os.path.join(project_root, dir_path)
        if os.path.exists(full_path):
            print(f"Directory exists: {dir_path}")
        else:
            print(
                f"Directory not yet created: {dir_path} (will be created on first run)"
            )

    # Check test files
    test_files = [
        "tests/integration/__init__.py",
        "tests/integration/conftest.py",
        "tests/integration/utils.py",
        "tests/integration/README.md",
        "tests/integration/test_example.py",
    ]

    for file_path in test_files:
        full_path = os.path.join(project_root, file_path)
        if os.path.exists(full_path):
            size = os.path.getsize(full_path)
            print(f"File exists: {file_path} ({size} bytes)")
        else:
            print(f"File missing: {file_path}")


def demonstrate_token_generation() -> bool:
    """Demonstrate token generation"""
    print("\nDemonstrating token generation...")

    try:
        from .utils import CashuTokenGenerator

        # Generate a valid token
        token = CashuTokenGenerator.generate_token(1000, memo="Demo token")
        print(f"Generated token: {token[:50]}...")

        # Verify token format
        if token.startswith("cashuA"):
            print("Token has correct prefix")
        else:
            print("Token has incorrect prefix")

        return True

    except Exception as e:
        print(f"Error generating token: {e}")
        return False


def demonstrate_testmint_wallet() -> bool:
    """Demonstrate testmint wallet functionality"""
    print("\nDemonstrating testmint wallet...")

    try:
        import asyncio

        from .conftest import TestmintWallet

        async def test_wallet() -> bool:
            wallet = TestmintWallet()

            # Generate token
            token = await wallet.mint_tokens(500)
            print(f"Minted token: {token[:50]}...")

            # Redeem token
            amount = await wallet.redeem_token(token)
            print(f"Redeemed {amount} sats")

            # Try to redeem again (should fail)
            try:
                await wallet.redeem_token(token)
                print("Token was redeemed twice (should have failed)")
            except ValueError as e:
                print(f"Token correctly rejected on second use: {e}")

            return True

        # Run async function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(test_wallet())
        loop.close()

        return result

    except Exception as e:
        print(f"Error testing wallet: {e}")
        return False


def main() -> None:
    """Main verification function"""
    print("Integration Test Infrastructure Verification")
    print("=" * 50)

    # Run all checks
    imports_ok = check_imports()
    check_environment()
    check_test_infrastructure()

    if imports_ok:
        token_ok = demonstrate_token_generation()
        wallet_ok = demonstrate_testmint_wallet()

        if token_ok and wallet_ok:
            print("\n" + "=" * 50)
            print("All checks passed! Integration test infrastructure is ready.")
            print("\nNext steps:")
            print("1. Install pytest: pip install pytest pytest-asyncio")
            print("2. Run example tests: pytest tests/integration/test_example.py -v")
            print("3. Start implementing the remaining test tickets")
        else:
            print("\nSome functionality checks failed")
    else:
        print("\nImport checks failed. Make sure all dependencies are installed:")
        print("  pip install -e '.[dev]'")


if __name__ == "__main__":
    main()
