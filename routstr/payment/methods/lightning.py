from ...core.db import ApiKey, AsyncSession
from ...core.logging import get_logger
from ..payment_methods import PaymentMethod, PaymentResult

logger = get_logger(__name__)


class BitcoinLightningPaymentMethod(PaymentMethod):
    @property
    def name(self) -> str:
        return "lightning"

    @property
    def display_name(self) -> str:
        return "Bitcoin Lightning Network"

    async def validate_payment_data(self, payment_data: str) -> bool:
        if not payment_data:
            return False

        payment_data = payment_data.strip()

        if payment_data.startswith("lnbc") or payment_data.startswith("lntb") or payment_data.startswith("lnbcrt"):
            return True

        if payment_data.startswith("lightning:") or payment_data.startswith("LIGHTNING:"):
            invoice = payment_data.split(":", 1)[1]
            return invoice.startswith("lnbc") or invoice.startswith("lntb") or invoice.startswith("lnbcrt")

        return False

    async def process_payment(
        self, payment_data: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        if not await self.validate_payment_data(payment_data):
            raise ValueError("Invalid Lightning invoice format")

        payment_data = payment_data.strip()
        if payment_data.startswith("lightning:") or payment_data.startswith("LIGHTNING:"):
            payment_data = payment_data.split(":", 1)[1]

        logger.info(
            "Processing Lightning payment",
            extra={"invoice_preview": payment_data[:50]},
        )

        # TODO: Full implementation requires:
        # 1. Lightning node integration (e.g., LND, CLN, LDK, or similar)
        # 2. Invoice verification and payment checking
        # 3. Amount extraction from invoice (decode BOLT11 invoice)
        # 4. Payment status polling or webhook handling
        # 5. Atomic balance updates after payment confirmation
        # 6. Error handling for expired/invalid invoices
        # 7. Support for both mainnet and testnet
        #
        # Example libraries to consider:
        # - pyln-client (for Core Lightning)
        # - lndgrpc (for LND)
        # - bolt11 (for invoice decoding)
        #
        # Example flow:
        # 1. Decode invoice to get amount and expiry
        # 2. Check if invoice is already paid (query node)
        # 3. If not paid, wait for payment confirmation (poll or webhook)
        # 4. Once confirmed, credit balance atomically
        # 5. Store invoice hash in metadata to prevent double-spending

        raise NotImplementedError(
            "Lightning payment processing not yet fully implemented. "
            "Requires Lightning node integration and invoice verification."
        )

    async def refund_payment(
        self,
        amount_msats: int,
        currency: str,
        refund_address: str | None,
        metadata: dict[str, str | None],
        session: AsyncSession,
    ) -> dict[str, str]:
        if not refund_address:
            raise ValueError("Refund address required for Lightning refunds")

        logger.info(
            "Processing Lightning refund",
            extra={"amount_msats": amount_msats, "refund_address": refund_address},
        )

        # TODO: Full implementation requires:
        # 1. Lightning node integration
        # 2. Create outgoing invoice or use keysend
        # 3. Send payment to refund_address
        # 4. Handle payment failures and retries
        # 5. Track payment status and confirmations
        #
        # Example flow:
        # 1. Validate refund_address (Lightning address or node pubkey)
        # 2. Create invoice or use keysend if supported
        # 3. Send payment via Lightning node
        # 4. Wait for payment confirmation
        # 5. Return payment hash/preimage

        raise NotImplementedError(
            "Lightning refund processing not yet fully implemented. "
            "Requires Lightning node integration and payment sending capabilities."
        )

    def extract_metadata(self, payment_data: str) -> dict[str, str | None]:
        payment_data = payment_data.strip()
        if payment_data.startswith("lightning:") or payment_data.startswith("LIGHTNING:"):
            payment_data = payment_data.split(":", 1)[1]

        # TODO: Decode BOLT11 invoice to extract:
        # - Amount
        # - Expiry timestamp
        # - Payment hash
        # - Description
        # - Network (mainnet/testnet)
        #
        # Can use bolt11 library:
        # from bolt11 import decode
        # invoice = decode(payment_data)
        # return {
        #     "amount_msats": invoice.amount_msat,
        #     "expiry": invoice.expiry,
        #     "payment_hash": invoice.payment_hash,
        #     "description": invoice.description,
        #     "network": invoice.network,
        # }

        return {
            "invoice": payment_data,
            "currency": "sat",
        }
