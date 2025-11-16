from typing import TypedDict

from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ApiKey
from ..core.settings import settings
from ..wallet import credit_balance as wallet_credit_balance, send_token, send_to_lnurl
from .methods import (
    PaymentMethod,
    PaymentMethodMetadata,
    PaymentResult,
    RefundResult,
)


class CashuTokenPaymentMethod(PaymentMethod):
    """Payment method for Cashu eCash tokens."""

    @property
    def method_type(self) -> str:
        return "cashu"

    async def validate_payment_data(
        self, payment_data: str, session: AsyncSession
    ) -> PaymentMethodMetadata:
        if not payment_data or len(payment_data) < 10 or "cashu" not in payment_data:
            raise ValueError("Invalid Cashu token format")

        try:
            from ..wallet import deserialize_token_from_string

            token_obj = deserialize_token_from_string(payment_data)
            return PaymentMethodMetadata(
                mint_url=token_obj.mint,
                currency=token_obj.unit,
            )
        except Exception as e:
            raise ValueError(f"Invalid Cashu token: {e}") from e

    async def credit_balance(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        from ..wallet import deserialize_token_from_string
        
        token_obj = deserialize_token_from_string(payment_data)
        amount_msats = await wallet_credit_balance(payment_data, key, session)
        metadata = PaymentMethodMetadata(
            mint_url=token_obj.mint,
            currency=token_obj.unit,
        )
        return PaymentResult(amount_msats=amount_msats, metadata=metadata)

    async def refund_balance(
        self, key: ApiKey, amount_msats: int, session: AsyncSession
    ) -> RefundResult:
        if key.refund_address:
            from ..core.settings import settings as global_settings

            await send_to_lnurl(
                amount_msats // 1000 if key.refund_currency == "sat" else amount_msats,
                key.refund_currency or "sat",
                key.refund_mint_url or global_settings.primary_mint,
                key.refund_address,
            )
            return RefundResult(
                success=True,
                refund_identifier=key.refund_address,
                metadata=PaymentMethodMetadata(
                    mint_url=key.refund_mint_url,
                    currency=key.refund_currency,
                ),
            )
        else:
            refund_currency = key.refund_currency or "sat"
            remaining_balance = (
                amount_msats // 1000 if refund_currency == "sat" else amount_msats
            )
            token = await send_token(
                remaining_balance, refund_currency, key.refund_mint_url
            )
            return RefundResult(
                success=True,
                refund_identifier=token,
                metadata=PaymentMethodMetadata(
                    mint_url=key.refund_mint_url,
                    currency=refund_currency,
                ),
            )

    def can_handle(self, payment_data: str) -> bool:
        return (
            payment_data is not None
            and len(payment_data) >= 10
            and payment_data.startswith("cashu")
        )


class BitcoinLightningPaymentMethod(PaymentMethod):
    """Payment method for Bitcoin Lightning Network invoices.
    
    To fully implement this payment method, you would need to:
    1. Integrate with a Lightning node (e.g., LND, CLN, or LDK)
    2. Implement invoice validation and payment verification
    3. Set up webhook handling for payment confirmations
    4. Implement proper invoice generation for refunds
    5. Handle payment routing and channel management
    6. Add support for keysend payments
    7. Implement proper error handling for payment failures
    8. Add rate limiting and anti-spam measures
    9. Store invoice preimages for verification
    10. Implement proper database schema for tracking Lightning payments
    """

    @property
    def method_type(self) -> str:
        return "lightning"

    async def validate_payment_data(
        self, payment_data: str, session: AsyncSession
    ) -> PaymentMethodMetadata:
        if not payment_data or not payment_data.startswith("ln"):
            raise ValueError("Invalid Lightning invoice format")

        try:
            from .lnurl import parse_lightning_invoice_amount

            amount_msats = parse_lightning_invoice_amount(payment_data, currency="msat")
            return PaymentMethodMetadata(
                invoice=payment_data,
                currency="msat",
            )
        except Exception as e:
            raise ValueError(f"Invalid Lightning invoice: {e}") from e

    async def credit_balance(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        # TODO: Full implementation requires:
        # 1. Verify invoice is paid by checking with Lightning node
        # 2. Extract payment amount from invoice
        # 3. Verify payment preimage matches invoice
        # 4. Check for double-spending
        # 5. Update database with payment confirmation
        
        # Pseudo-implementation:
        from .lnurl import parse_lightning_invoice_amount

        amount_msats = parse_lightning_invoice_amount(payment_data, currency="msat")
        
        # In real implementation, you would:
        # - Connect to Lightning node: lnd_client = LndClient(...)
        # - Verify payment: await lnd_client.verify_payment(payment_data)
        # - Get payment details: payment = await lnd_client.get_payment(payment_hash)
        # - Check if already processed: await check_payment_already_processed(payment_hash)
        
        metadata = PaymentMethodMetadata(
            invoice=payment_data,
            currency="msat",
        )
        return PaymentResult(amount_msats=amount_msats, metadata=metadata)

    async def refund_balance(
        self, key: ApiKey, amount_msats: int, session: AsyncSession
    ) -> RefundResult:
        # TODO: Full implementation requires:
        # 1. Generate Lightning invoice for refund amount
        # 2. Store invoice in database with refund mapping
        # 3. Pay invoice using Lightning node
        # 4. Handle payment failures and retries
        # 5. Update refund status in database
        
        # Pseudo-implementation:
        if not key.refund_address:
            raise ValueError("No refund address configured for Lightning refund")

        # In real implementation, you would:
        # - Generate invoice: invoice = await lnd_client.add_invoice(amount_msats, memo="Refund")
        # - Pay invoice: await lnd_client.send_payment(key.refund_address, amount_msats)
        # - Store refund record: await store_refund_record(key.hashed_key, invoice.payment_hash)
        
        refund_invoice = f"lnbc{amount_msats // 1000}u..."  # Pseudo invoice
        return RefundResult(
            success=True,
            refund_identifier=refund_invoice,
            metadata=PaymentMethodMetadata(
                invoice=refund_invoice,
                currency="msat",
            ),
        )

    def can_handle(self, payment_data: str) -> bool:
        return payment_data is not None and payment_data.startswith("ln")


class USDTTetherPaymentMethod(PaymentMethod):
    """Payment method for USDT (Tether) on various blockchains.
    
    To fully implement this payment method, you would need to:
    1. Choose blockchain(s) to support (Ethereum, Tron, Polygon, etc.)
    2. Set up blockchain node connections (Infura, Alchemy, or self-hosted)
    3. Implement USDT contract interaction (ERC-20 or TRC-20)
    4. Set up wallet for receiving payments
    5. Implement transaction monitoring and confirmation tracking
    6. Add support for multiple networks (mainnet, testnet)
    7. Implement proper gas fee estimation and handling
    8. Add transaction verification and double-spend prevention
    9. Implement refund mechanism (send USDT back to user address)
    10. Add proper error handling for network issues and failed transactions
    11. Implement rate limiting and anti-spam measures
    12. Store transaction hashes and block confirmations in database
    13. Add support for different USDT versions (USDT, USDT.e, etc.)
    """

    @property
    def method_type(self) -> str:
        return "usdt"

    async def validate_payment_data(
        self, payment_data: str, session: AsyncSession
    ) -> PaymentMethodMetadata:
        # TODO: Full implementation requires:
        # 1. Validate transaction hash format for chosen blockchain
        # 2. Verify transaction exists on blockchain
        # 3. Check transaction is confirmed (sufficient block confirmations)
        # 4. Verify transaction is to our wallet address
        # 5. Verify transaction amount matches expected amount
        # 6. Check transaction hasn't been used before (double-spend prevention)
        
        # Pseudo-implementation:
        if not payment_data or len(payment_data) < 40:
            raise ValueError("Invalid transaction hash format")

        # In real implementation, you would:
        # - Validate format: if not re.match(r'^0x[a-fA-F0-9]{64}$', payment_data): raise ValueError(...)
        # - Check transaction: tx = await web3.eth.get_transaction(payment_data)
        # - Verify recipient: assert tx['to'].lower() == our_wallet_address.lower()
        # - Check confirmations: confirmations = await get_confirmations(tx['blockNumber'])
        # - Verify amount: assert tx['value'] == expected_amount
        
        return PaymentMethodMetadata(
            transaction_hash=payment_data,
            network="ethereum",  # or "tron", "polygon", etc.
            contract_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT on Ethereum
        )

    async def credit_balance(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        # TODO: Full implementation requires:
        # 1. Fetch transaction details from blockchain
        # 2. Convert USDT amount to millisatoshis (using exchange rate)
        # 3. Verify transaction is confirmed and not already processed
        # 4. Store transaction record in database
        # 5. Update balance atomically
        
        # Pseudo-implementation:
        # In real implementation, you would:
        # - Get transaction: tx = await web3.eth.get_transaction_receipt(payment_data)
        # - Parse USDT transfer event: usdt_amount = parse_usdt_transfer_event(tx['logs'])
        # - Get exchange rate: rate = await get_usdt_to_btc_rate()
        # - Convert to msats: amount_msats = int(usdt_amount * rate * 100_000_000_000)
        # - Check if processed: if await is_transaction_processed(payment_data): raise ValueError("Already processed")
        # - Store transaction: await store_transaction_record(key.hashed_key, payment_data, usdt_amount)
        
        amount_msats = 1000000  # Pseudo amount
        metadata = PaymentMethodMetadata(
            transaction_hash=payment_data,
            network="ethereum",
            contract_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        )
        return PaymentResult(amount_msats=amount_msats, metadata=metadata)

    async def refund_balance(
        self, key: ApiKey, amount_msats: int, session: AsyncSession
    ) -> RefundResult:
        # TODO: Full implementation requires:
        # 1. Convert millisatoshis to USDT using exchange rate
        # 2. Get user's refund address (stored in key metadata)
        # 3. Estimate gas fees for transaction
        # 4. Send USDT transaction to user's address
        # 5. Wait for transaction confirmation
        # 6. Store refund transaction hash in database
        # 7. Handle transaction failures and retries
        
        # Pseudo-implementation:
        if not key.refund_address:
            raise ValueError("No refund address configured for USDT refund")

        # In real implementation, you would:
        # - Get exchange rate: rate = await get_btc_to_usdt_rate()
        # - Convert to USDT: usdt_amount = (amount_msats / 100_000_000_000) * rate
        # - Build transaction: tx = usdt_contract.functions.transfer(key.refund_address, usdt_amount)
        # - Estimate gas: gas = await tx.estimate_gas({'from': our_wallet_address})
        # - Send transaction: tx_hash = await tx.transact({'from': our_wallet_address, 'gas': gas})
        # - Wait for confirmation: await wait_for_confirmation(tx_hash)
        # - Store refund: await store_refund_record(key.hashed_key, tx_hash, usdt_amount)
        
        refund_tx_hash = "0x" + "0" * 64  # Pseudo transaction hash
        return RefundResult(
            success=True,
            refund_identifier=refund_tx_hash,
            metadata=PaymentMethodMetadata(
                transaction_hash=refund_tx_hash,
                network="ethereum",
                contract_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            ),
        )

    def can_handle(self, payment_data: str) -> bool:
        # In real implementation, you might check for specific prefixes or formats
        # For now, this is a catch-all that should be called after other methods
        return False  # Disabled by default, enable when fully implemented


def get_payment_method(payment_data: str) -> PaymentMethod | None:
    """Get the appropriate payment method for the given payment data.
    
    Args:
        payment_data: Payment data string to identify
        
    Returns:
        PaymentMethod instance or None if no method can handle it
    """
    methods: list[PaymentMethod] = [
        CashuTokenPaymentMethod(),
        BitcoinLightningPaymentMethod(),
        USDTTetherPaymentMethod(),
    ]

    for method in methods:
        if method.can_handle(payment_data):
            return method

    return None
