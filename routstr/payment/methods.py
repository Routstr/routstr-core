"""
Abstract payment method system for temporary balance management.

This module provides a flexible architecture for supporting multiple payment methods
(Cashu, Lightning, USDT, etc.) for managing temporary balances in the system.
"""

from abc import ABC, abstractmethod
from typing import TypedDict

from cashu.core.base import Proof, Token
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet

from ..core import db, get_logger
from ..core.settings import settings

logger = get_logger(__name__)


class PaymentTokenInfo(TypedDict):
    """Information extracted from a payment token"""
    amount: int
    unit: str
    mint_url: str


class PaymentMethod(ABC):
    """
    Abstract base class for payment methods.
    
    All payment methods must implement these core operations:
    - receive: Accept payment and return amount/details
    - send: Send payment and return token/receipt
    - validate: Validate a payment token format
    """
    
    @property
    @abstractmethod
    def method_name(self) -> str:
        """Unique identifier for this payment method"""
        pass
    
    @abstractmethod
    async def receive(self, token: str) -> PaymentTokenInfo:
        """
        Receive and process a payment token.
        
        Args:
            token: Payment token string (format varies by payment method)
            
        Returns:
            PaymentTokenInfo with amount (in msats), unit, and source URL/ID
            
        Raises:
            ValueError: If token is invalid or already spent
            Exception: For other processing errors
        """
        pass
    
    @abstractmethod
    async def send(
        self, amount: int, unit: str, destination: str | None = None
    ) -> tuple[int, str]:
        """
        Send payment and generate a token/receipt.
        
        Args:
            amount: Amount to send (in the specified unit)
            unit: Currency unit (e.g., 'sat', 'msat', 'usd')
            destination: Optional destination address/URL
            
        Returns:
            Tuple of (actual_amount_sent, token_or_receipt)
        """
        pass
    
    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        """
        Validate token format without processing it.
        
        Args:
            token: Payment token string
            
        Returns:
            True if token format is valid, False otherwise
        """
        pass
    
    @abstractmethod
    async def get_balance(self, unit: str) -> int:
        """
        Get current balance held by this payment method.
        
        Args:
            unit: Currency unit to check
            
        Returns:
            Balance amount in the specified unit
        """
        pass


class CashuPaymentMethod(PaymentMethod):
    """
    Cashu eCash payment method implementation.
    
    Supports Bitcoin Lightning payments through Cashu mints using eCash tokens.
    This is the current production implementation.
    """
    
    def __init__(self) -> None:
        self._wallets: dict[str, Wallet] = {}
    
    @property
    def method_name(self) -> str:
        return "cashu"
    
    async def receive(self, token: str) -> PaymentTokenInfo:
        """
        Receive a Cashu token and redeem it.
        
        Process:
        1. Deserialize and validate the token
        2. Verify DLEQ proofs
        3. Split proofs to store in wallet
        4. If from untrusted mint, swap to primary mint
        """
        logger.info(
            "CashuPaymentMethod: Receiving token",
            extra={"token_preview": token[:50]},
        )
        
        token_obj = deserialize_token_from_string(token)
        
        if len(token_obj.keysets) > 1:
            raise ValueError("Multiple keysets per token currently not supported")
        
        wallet = await self._get_wallet(token_obj.mint, token_obj.unit, load=False)
        wallet.keyset_id = token_obj.keysets[0]
        
        # If token is from untrusted mint, swap to primary mint
        if token_obj.mint not in settings.cashu_mints:
            return await self._swap_to_primary_mint(token_obj, wallet)
        
        # Verify and store proofs
        wallet.verify_proofs_dleq(token_obj.proofs)
        await wallet.split(proofs=token_obj.proofs, amount=0, include_fees=True)
        
        return PaymentTokenInfo(
            amount=token_obj.amount,
            unit=token_obj.unit,
            mint_url=token_obj.mint,
        )
    
    async def send(
        self, amount: int, unit: str, destination: str | None = None
    ) -> tuple[int, str]:
        """
        Send Cashu token.
        
        Process:
        1. Get wallet for mint/unit
        2. Select proofs to send
        3. Serialize proofs into token
        """
        mint_url = destination or settings.primary_mint
        wallet = await self._get_wallet(mint_url, unit)
        
        proofs = self._get_proofs_per_mint_and_unit(wallet, mint_url, unit)
        
        send_proofs, _ = await wallet.select_to_send(
            proofs, amount, set_reserved=True, include_fees=False
        )
        
        token = await wallet.serialize_proofs(
            send_proofs, include_dleq=False, legacy=False, memo=None
        )
        
        return amount, token
    
    async def validate_token(self, token: str) -> bool:
        """Validate Cashu token format"""
        token = token.strip()
        if len(token) < 10 or "cashu" not in token.lower():
            return False
        
        try:
            deserialize_token_from_string(token)
            return True
        except Exception:
            return False
    
    async def get_balance(self, unit: str) -> int:
        """Get balance from primary Cashu mint wallet"""
        wallet = await self._get_wallet(settings.primary_mint, unit)
        return wallet.available_balance.amount
    
    async def _get_wallet(
        self, mint_url: str, unit: str = "sat", load: bool = True
    ) -> Wallet:
        """Get or create a wallet for the specified mint and unit"""
        wallet_id = f"{mint_url}_{unit}"
        if wallet_id not in self._wallets:
            self._wallets[wallet_id] = await Wallet.with_db(
                mint_url, db=".wallet", unit=unit
            )
        
        if load:
            await self._wallets[wallet_id].load_mint()
            await self._wallets[wallet_id].load_proofs(reload=True)
        
        return self._wallets[wallet_id]
    
    def _get_proofs_per_mint_and_unit(
        self, wallet: Wallet, mint_url: str, unit: str, not_reserved: bool = False
    ) -> list[Proof]:
        """Filter proofs for specific mint and unit"""
        valid_keyset_ids = [
            k.id
            for k in wallet.keysets.values()
            if k.mint_url == mint_url and k.unit.name == unit
        ]
        proofs = [p for p in wallet.proofs if p.id in valid_keyset_ids]
        if not_reserved:
            proofs = [p for p in proofs if not p.reserved]
        return proofs
    
    async def _swap_to_primary_mint(
        self, token_obj: Token, token_wallet: Wallet
    ) -> PaymentTokenInfo:
        """
        Swap token from untrusted mint to primary mint via Lightning.
        
        Process:
        1. Request mint quote on primary mint
        2. Melt token from untrusted mint to pay Lightning invoice
        3. Mint new token on primary mint
        """
        import math
        
        logger.info(
            "CashuPaymentMethod: Swapping to primary mint",
            extra={
                "source_mint": token_obj.mint,
                "amount": token_obj.amount,
                "unit": token_obj.unit,
            },
        )
        
        # Calculate amount in msats
        token_amount = int(token_obj.amount)
        if token_obj.unit == "sat":
            amount_msat = token_amount * 1000
        elif token_obj.unit == "msat":
            amount_msat = token_amount
        else:
            raise ValueError(f"Invalid unit: {token_obj.unit}")
        
        # Estimate Lightning fee (1% with 2 sat minimum)
        estimated_fee_sat = math.ceil(max(amount_msat // 1000 * 0.01, 2))
        amount_msat_after_fee = amount_msat - estimated_fee_sat * 1000
        
        primary_wallet = await self._get_wallet(
            settings.primary_mint, settings.primary_mint_unit
        )
        
        # Calculate minted amount in primary mint's unit
        if settings.primary_mint_unit == "sat":
            minted_amount = int(amount_msat_after_fee // 1000)
        else:
            minted_amount = int(amount_msat_after_fee)
        
        # Request mint on primary mint
        mint_quote = await primary_wallet.request_mint(minted_amount)
        
        # Melt token to pay invoice
        melt_quote = await token_wallet.melt_quote(mint_quote.request)
        await token_wallet.melt(
            proofs=token_obj.proofs,
            invoice=mint_quote.request,
            fee_reserve_sat=melt_quote.fee_reserve,
            quote_id=melt_quote.quote,
        )
        
        # Complete minting
        await primary_wallet.mint(minted_amount, quote_id=mint_quote.quote)
        
        return PaymentTokenInfo(
            amount=minted_amount,
            unit=settings.primary_mint_unit,
            mint_url=settings.primary_mint,
        )


class LightningPaymentMethod(PaymentMethod):
    """
    Bitcoin Lightning Network payment method (pseudo-implementation).
    
    Direct Lightning Network integration without Cashu intermediary.
    Supports both BOLT11 invoices and LNURL.
    
    TO FULLY IMPLEMENT:
    1. Add Lightning Node connection library (e.g., lnd-grpc, cln-grpc, LNbits API)
    2. Configure Lightning Node credentials in settings:
       - LND: macaroon, tls_cert, grpc_endpoint
       - CLN: rune, grpc_endpoint
       - LNbits: admin_key, api_endpoint
    3. Implement invoice generation and payment verification
    4. Add webhook handlers for payment confirmations
    5. Implement proper error handling for Lightning failures
    6. Add balance tracking in Lightning node vs. database
    7. Consider implementing HTLCs for atomic swaps if needed
    """
    
    @property
    def method_name(self) -> str:
        return "lightning"
    
    async def receive(self, token: str) -> PaymentTokenInfo:
        """
        Process incoming Lightning payment.
        
        Args:
            token: BOLT11 invoice or payment hash
            
        TO IMPLEMENT:
        1. Parse BOLT11 invoice to extract payment hash and amount
        2. Check payment status via Lightning node
        3. Verify payment is settled (not just pending)
        4. Extract preimage as proof of payment
        5. Store payment hash in database to prevent double-credit
        6. Convert amount to msats for internal balance
        """
        raise NotImplementedError(
            "Lightning payment method requires Lightning node integration. "
            "Add lnd-grpc/cln-grpc library and configure node credentials."
        )
        
        # Pseudo-code implementation:
        # invoice = decode_bolt11(token)
        # payment = await lightning_node.lookup_invoice(invoice.payment_hash)
        # if payment.state != "SETTLED":
        #     raise ValueError("Payment not settled")
        # if await is_payment_already_credited(payment.payment_hash):
        #     raise ValueError("Payment already credited")
        # return PaymentTokenInfo(
        #     amount=invoice.amount_msat,
        #     unit="msat",
        #     mint_url=lightning_node.node_id,
        # )
    
    async def send(
        self, amount: int, unit: str, destination: str | None = None
    ) -> tuple[int, str]:
        """
        Send Lightning payment.
        
        Args:
            amount: Amount in sats or msats
            unit: 'sat' or 'msat'
            destination: BOLT11 invoice, Lightning address, or LNURL
            
        TO IMPLEMENT:
        1. Parse destination (BOLT11 vs LNURL vs Lightning address)
        2. For LNURL: fetch invoice from LNURL service
        3. Decode invoice to verify amount matches
        4. Send payment via Lightning node
        5. Wait for payment confirmation or timeout
        6. Return payment hash as receipt
        7. Handle routing failures and retries
        """
        raise NotImplementedError(
            "Lightning send requires Lightning node integration. "
            "Need to implement payment routing and invoice handling."
        )
        
        # Pseudo-code implementation:
        # if is_lnurl(destination):
        #     invoice = await fetch_lnurl_invoice(destination, amount)
        # elif is_lightning_address(destination):
        #     invoice = await fetch_ln_address_invoice(destination, amount)
        # else:
        #     invoice = destination
        # 
        # payment_result = await lightning_node.send_payment(
        #     invoice, timeout_seconds=60
        # )
        # if not payment_result.success:
        #     raise Exception(f"Payment failed: {payment_result.error}")
        # return amount, payment_result.payment_hash
    
    async def validate_token(self, token: str) -> bool:
        """
        Validate Lightning payment token format.
        
        TO IMPLEMENT:
        1. Check if it's a valid BOLT11 invoice (starts with ln...)
        2. Check if it's a valid Lightning address (user@domain.com)
        3. Check if it's a valid LNURL (lnurl...)
        4. Decode and verify checksum
        """
        token = token.strip().lower()
        # Basic format validation
        if token.startswith("ln"):
            # Likely BOLT11 invoice
            return len(token) > 20
        if "@" in token and "." in token:
            # Likely Lightning address
            return True
        if token.startswith("lnurl"):
            # LNURL
            return len(token) > 10
        return False
    
    async def get_balance(self, unit: str) -> int:
        """
        Get Lightning node balance.
        
        TO IMPLEMENT:
        1. Query Lightning node for channel balances
        2. Sum up local balances across all channels
        3. Convert to requested unit (sat vs msat)
        4. Consider separating on-chain vs off-chain balance
        """
        raise NotImplementedError(
            "Lightning balance check requires node integration"
        )
        # return await lightning_node.get_channel_balance()


class USDTPaymentMethod(PaymentMethod):
    """
    USDT (Tether) payment method (pseudo-implementation).
    
    Supports USDT on multiple chains: Ethereum (ERC-20), Tron (TRC-20), etc.
    
    TO FULLY IMPLEMENT:
    1. Choose blockchain(s) to support (Ethereum, Tron, Polygon, etc.)
    2. Set up blockchain node connection or use service like Infura, Alchemy
    3. Add web3 library (web3.py for Python)
    4. Configure smart contract addresses for USDT:
       - Ethereum: 0xdac17f958d2ee523a2206206994597c13d831ec7
       - Tron: TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
    5. Implement wallet management (private keys, HD wallets)
    6. Add transaction monitoring for incoming payments
    7. Implement gas fee estimation and handling
    8. Add conversion rate oracle for USDT to sats/BTC
    9. Consider atomic swaps for BTC<->USDT if needed
    10. Implement proper security for key management (HSM, KMS)
    """
    
    @property
    def method_name(self) -> str:
        return "usdt"
    
    async def receive(self, token: str) -> PaymentTokenInfo:
        """
        Process incoming USDT payment.
        
        Args:
            token: Transaction hash on blockchain
            
        TO IMPLEMENT:
        1. Parse transaction hash
        2. Query blockchain for transaction details
        3. Verify transaction is confirmed (N confirmations)
        4. Verify recipient address matches our deposit address
        5. Extract amount from transaction logs
        6. Check transaction hasn't been credited before
        7. Convert USDT amount to sats using current rate
        8. Store transaction hash to prevent double-credit
        """
        raise NotImplementedError(
            "USDT payment method requires blockchain integration. "
            "Need web3.py library and blockchain node access (Infura/Alchemy)."
        )
        
        # Pseudo-code implementation:
        # tx = await blockchain.get_transaction(token)
        # if tx.confirmations < REQUIRED_CONFIRMATIONS:
        #     raise ValueError("Transaction not confirmed yet")
        # if tx.to_address != settings.usdt_deposit_address:
        #     raise ValueError("Invalid recipient address")
        # if await is_tx_already_credited(token):
        #     raise ValueError("Transaction already credited")
        # 
        # usdt_amount = extract_usdt_amount(tx)
        # rate = await get_usdt_to_btc_rate()
        # sats_amount = int(usdt_amount * rate * 100_000_000)
        # 
        # return PaymentTokenInfo(
        #     amount=sats_amount * 1000,  # Convert to msats
        #     unit="msat",
        #     mint_url=f"{tx.chain_id}:{tx.contract_address}",
        # )
    
    async def send(
        self, amount: int, unit: str, destination: str | None = None
    ) -> tuple[int, str]:
        """
        Send USDT payment.
        
        Args:
            amount: Amount in msats (will be converted to USDT)
            unit: Currency unit
            destination: Blockchain address (0x... for ETH, T... for Tron)
            
        TO IMPLEMENT:
        1. Validate destination address format
        2. Convert amount from sats to USDT using current rate
        3. Estimate gas fees for transaction
        4. Build and sign transaction
        5. Broadcast transaction to blockchain
        6. Wait for transaction confirmation
        7. Return transaction hash as receipt
        8. Handle insufficient gas, nonce issues, etc.
        """
        raise NotImplementedError(
            "USDT send requires blockchain integration and web3 library"
        )
        
        # Pseudo-code implementation:
        # if not is_valid_address(destination):
        #     raise ValueError("Invalid blockchain address")
        # 
        # rate = await get_usdt_to_btc_rate()
        # usdt_amount = (amount / 1000 / 100_000_000) / rate
        # 
        # gas_estimate = await blockchain.estimate_gas(
        #     contract_address=USDT_CONTRACT,
        #     function="transfer",
        #     args=[destination, usdt_amount]
        # )
        # 
        # tx = await blockchain.send_transaction(
        #     to=USDT_CONTRACT,
        #     data=encode_transfer(destination, usdt_amount),
        #     gas=gas_estimate * 1.2
        # )
        # 
        # await tx.wait_for_confirmation(confirmations=3)
        # return int(usdt_amount), tx.hash
    
    async def validate_token(self, token: str) -> bool:
        """
        Validate USDT transaction hash or address.
        
        TO IMPLEMENT:
        1. Check if it's a valid Ethereum transaction hash (0x + 64 hex chars)
        2. Check if it's a valid Tron transaction hash
        3. Verify checksum if applicable
        """
        token = token.strip()
        # Ethereum transaction hash
        if token.startswith("0x") and len(token) == 66:
            try:
                int(token[2:], 16)
                return True
            except ValueError:
                return False
        # Tron transaction hash (64 hex chars)
        if len(token) == 64:
            try:
                int(token, 16)
                return True
            except ValueError:
                return False
        return False
    
    async def get_balance(self, unit: str) -> int:
        """
        Get USDT balance from wallet.
        
        TO IMPLEMENT:
        1. Query USDT contract for balance of our address
        2. Convert USDT to sats using current rate
        3. Return in requested unit
        """
        raise NotImplementedError("USDT balance check requires blockchain integration")
        # balance_usdt = await blockchain.call_contract(
        #     USDT_CONTRACT, "balanceOf", [settings.usdt_address]
        # )
        # rate = await get_usdt_to_btc_rate()
        # sats = int(balance_usdt * rate * 100_000_000)
        # return sats * 1000 if unit == "msat" else sats


class OnChainBitcoinPaymentMethod(PaymentMethod):
    """
    On-chain Bitcoin payment method (pseudo-implementation).
    
    Direct Bitcoin blockchain payments (not Lightning).
    
    TO FULLY IMPLEMENT:
    1. Set up Bitcoin node (bitcoind) or use service like Blockstream API
    2. Add bitcoin RPC library (python-bitcoinlib)
    3. Implement HD wallet for generating deposit addresses
    4. Set up address monitoring for incoming transactions
    5. Implement proper confirmation waiting (6+ confirmations)
    6. Add UTXO management for outgoing transactions
    7. Implement fee estimation (mempool analysis)
    8. Consider RBF (Replace-By-Fee) support
    9. Add batching for multiple outputs to save fees
    10. Implement proper security for private key storage
    """
    
    @property
    def method_name(self) -> str:
        return "bitcoin"
    
    async def receive(self, token: str) -> PaymentTokenInfo:
        """
        Process incoming on-chain Bitcoin payment.
        
        Args:
            token: Transaction ID (txid)
            
        TO IMPLEMENT:
        1. Fetch transaction from blockchain
        2. Verify sufficient confirmations (typically 6)
        3. Find output(s) to our address
        4. Sum amounts from relevant outputs
        5. Check for RBF/double-spend attempts
        6. Store txid to prevent double-credit
        """
        raise NotImplementedError(
            "On-chain Bitcoin requires Bitcoin node integration. "
            "Install python-bitcoinlib and configure bitcoind connection."
        )
        
        # Pseudo-code:
        # tx = await bitcoin_node.get_transaction(token)
        # if tx.confirmations < 6:
        #     raise ValueError("Insufficient confirmations")
        # 
        # our_outputs = [
        #     out for out in tx.outputs 
        #     if out.address in await get_our_addresses()
        # ]
        # total_sats = sum(out.value for out in our_outputs)
        # 
        # return PaymentTokenInfo(
        #     amount=total_sats * 1000,
        #     unit="msat",
        #     mint_url="bitcoin:mainnet",
        # )
    
    async def send(
        self, amount: int, unit: str, destination: str | None = None
    ) -> tuple[int, str]:
        """
        Send on-chain Bitcoin payment.
        
        TO IMPLEMENT:
        1. Validate Bitcoin address
        2. Select UTXOs to spend
        3. Estimate transaction fee
        4. Build transaction with change output
        5. Sign transaction
        6. Broadcast to network
        7. Return txid
        """
        raise NotImplementedError("On-chain Bitcoin send requires node integration")
        
        # Pseudo-code:
        # sats = amount // 1000 if unit == "msat" else amount
        # utxos = await bitcoin_node.list_unspent()
        # selected_utxos, change = select_utxos(utxos, sats)
        # 
        # fee = await estimate_fee(len(selected_utxos), 2)  # 2 outputs
        # 
        # tx = build_transaction(
        #     inputs=selected_utxos,
        #     outputs=[(destination, sats), (change_address, change - fee)]
        # )
        # signed_tx = await sign_transaction(tx)
        # txid = await broadcast_transaction(signed_tx)
        # return sats, txid
    
    async def validate_token(self, token: str) -> bool:
        """Validate Bitcoin transaction ID format"""
        token = token.strip()
        if len(token) == 64:
            try:
                int(token, 16)
                return True
            except ValueError:
                pass
        return False
    
    async def get_balance(self, unit: str) -> int:
        """Get on-chain Bitcoin balance from wallet"""
        raise NotImplementedError("Bitcoin balance requires node integration")
        # balance_sats = await bitcoin_node.get_balance()
        # return balance_sats * 1000 if unit == "msat" else balance_sats


class PaymentMethodFactory:
    """
    Factory for creating and managing payment method instances.
    
    Allows dynamic selection of payment methods based on token format
    or explicit method specification.
    """
    
    _instances: dict[str, PaymentMethod] = {}
    
    @classmethod
    def get_method(cls, method_name: str) -> PaymentMethod:
        """
        Get payment method instance by name.
        
        Args:
            method_name: Name of payment method ('cashu', 'lightning', 'usdt', etc.)
            
        Returns:
            PaymentMethod instance
            
        Raises:
            ValueError: If method_name is not supported
        """
        if method_name not in cls._instances:
            if method_name == "cashu":
                cls._instances[method_name] = CashuPaymentMethod()
            elif method_name == "lightning":
                cls._instances[method_name] = LightningPaymentMethod()
            elif method_name == "usdt":
                cls._instances[method_name] = USDTPaymentMethod()
            elif method_name == "bitcoin":
                cls._instances[method_name] = OnChainBitcoinPaymentMethod()
            else:
                raise ValueError(f"Unsupported payment method: {method_name}")
        
        return cls._instances[method_name]
    
    @classmethod
    def detect_method(cls, token: str) -> PaymentMethod:
        """
        Auto-detect payment method from token format.
        
        Args:
            token: Payment token string
            
        Returns:
            Appropriate PaymentMethod instance
            
        Raises:
            ValueError: If token format is not recognized
        """
        token = token.strip()
        
        # Cashu tokens start with "cashu"
        if "cashu" in token.lower()[:10]:
            return cls.get_method("cashu")
        
        # Lightning invoices start with "ln"
        if token.lower().startswith("ln"):
            return cls.get_method("lightning")
        
        # Lightning addresses contain @
        if "@" in token and "." in token:
            return cls.get_method("lightning")
        
        # Ethereum transaction hashes
        if token.startswith("0x") and len(token) == 66:
            return cls.get_method("usdt")
        
        # Bitcoin transaction hashes (64 hex chars)
        if len(token) == 64:
            try:
                int(token, 16)
                # Could be Bitcoin or USDT on Tron, default to Bitcoin
                return cls.get_method("bitcoin")
            except ValueError:
                pass
        
        raise ValueError(f"Unable to detect payment method from token format: {token[:20]}...")
    
    @classmethod
    def get_available_methods(cls) -> list[str]:
        """Get list of available payment method names"""
        return ["cashu", "lightning", "usdt", "bitcoin"]
    
    @classmethod
    def get_implemented_methods(cls) -> list[str]:
        """Get list of fully implemented payment methods"""
        # Only Cashu is fully implemented currently
        return ["cashu"]
