from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core.db import ApiKey, AsyncSession
from ..core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PaymentCredentials:
    """Credentials for a payment method (e.g., token, invoice, address)."""

    raw_credential: str
    payment_type: str
    metadata: dict[str, str | int] | None = None


@dataclass
class PaymentResult:
    """Result of a payment operation."""

    amount_msats: int
    currency: str
    payment_method: str
    transaction_id: str | None = None
    metadata: dict[str, str | int] | None = None


class AbstractPaymentMethod(ABC):
    """
    Abstract base class for payment methods used to fund temporary balances.
    
    Each payment method must implement:
    - Detection of valid payment credentials
    - Redemption/receipt of payment
    - Refund operations (full or partial)
    """

    @abstractmethod
    def can_handle(self, credential: str) -> bool:
        """
        Determine if this payment method can handle the given credential.
        
        Args:
            credential: Raw payment credential string
            
        Returns:
            True if this method can process the credential
        """
        pass

    @abstractmethod
    async def parse_credential(self, credential: str) -> PaymentCredentials:
        """
        Parse and validate the payment credential.
        
        Args:
            credential: Raw payment credential string
            
        Returns:
            PaymentCredentials with parsed metadata
            
        Raises:
            ValueError: If credential is invalid
        """
        pass

    @abstractmethod
    async def receive_payment(
        self, credential: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        """
        Receive and process a payment, crediting the ApiKey balance.
        
        Args:
            credential: Payment credential (token, invoice, etc.)
            key: ApiKey to credit
            session: Database session
            
        Returns:
            PaymentResult with amount and metadata
            
        Raises:
            ValueError: If payment cannot be processed
        """
        pass

    @abstractmethod
    async def refund_payment(
        self, key: ApiKey, amount_msats: int | None = None
    ) -> dict[str, str]:
        """
        Refund the balance to the original payment source.
        
        Args:
            key: ApiKey containing refund information
            amount_msats: Amount to refund (None = full balance)
            
        Returns:
            Dictionary with refund details (token, txid, address, etc.)
            
        Raises:
            ValueError: If refund cannot be processed
        """
        pass

    @abstractmethod
    def get_refund_metadata(self, key: ApiKey) -> dict[str, str]:
        """
        Extract refund-related metadata from ApiKey for this payment method.
        
        Args:
            key: ApiKey to extract metadata from
            
        Returns:
            Dictionary with refund configuration (mint_url, currency, etc.)
        """
        pass


class CashuPaymentMethod(AbstractPaymentMethod):
    """
    Cashu eCash payment method implementation.
    Currently the only fully implemented payment method.
    """

    def can_handle(self, credential: str) -> bool:
        return credential.startswith("cashu")

    async def parse_credential(self, credential: str) -> PaymentCredentials:
        from ..wallet import deserialize_token_from_string

        try:
            token_obj = deserialize_token_from_string(credential)
            return PaymentCredentials(
                raw_credential=credential,
                payment_type="cashu",
                metadata={
                    "mint_url": token_obj.mint,
                    "unit": token_obj.unit,
                    "amount": token_obj.amount,
                },
            )
        except Exception as e:
            raise ValueError(f"Invalid Cashu token: {e}")

    async def receive_payment(
        self, credential: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        from ..wallet import credit_balance

        logger.info(
            "Processing Cashu payment",
            extra={"key_hash": key.hashed_key[:8] + "...", "token_preview": credential[:50]},
        )

        msats = await credit_balance(credential, key, session)
        parsed = await self.parse_credential(credential)

        return PaymentResult(
            amount_msats=msats,
            currency=str(parsed.metadata.get("unit", "sat")) if parsed.metadata else "sat",
            payment_method="cashu",
            metadata=parsed.metadata,
        )

    async def refund_payment(
        self, key: ApiKey, amount_msats: int | None = None
    ) -> dict[str, str]:
        from ..core.settings import settings as global_settings
        from ..wallet import send_to_lnurl, send_token

        amount_to_refund = amount_msats if amount_msats is not None else key.balance

        if key.refund_currency == "sat":
            amount = amount_to_refund // 1000
        else:
            amount = amount_to_refund

        if amount_to_refund > 0 and amount <= 0:
            raise ValueError("Balance too small to refund")
        elif amount <= 0:
            raise ValueError("No balance to refund")

        if key.refund_address:
            await send_to_lnurl(
                amount,
                key.refund_currency or "sat",
                key.refund_mint_url or global_settings.primary_mint,
                key.refund_address,
            )
            result = {"recipient": key.refund_address, "method": "lnurl"}
        else:
            token = await send_token(amount, key.refund_currency or "sat", key.refund_mint_url)
            result = {"token": token, "method": "cashu"}

        if key.refund_currency == "sat":
            result["sats"] = str(amount_to_refund // 1000)
        else:
            result["msats"] = str(amount_to_refund)

        return result

    def get_refund_metadata(self, key: ApiKey) -> dict[str, str]:
        return {
            "refund_currency": key.refund_currency or "sat",
            "refund_mint_url": key.refund_mint_url or "",
            "refund_address": key.refund_address or "",
        }


class LightningPaymentMethod(AbstractPaymentMethod):
    """
    Bitcoin Lightning Network payment method (PSEUDO-IMPLEMENTATION).
    
    To fully implement:
    1. Add lightning network library dependency (e.g., lnbits, lnd-grpc, or c-lightning)
    2. Configure lightning node connection (host, port, macaroon/credentials)
    3. Store lightning invoice details in ApiKey model (add fields: payment_hash, preimage)
    4. Implement invoice generation and payment verification
    5. For refunds, implement lightning payment sending logic
    6. Add lightning-specific error handling (routing failures, insufficient capacity)
    7. Consider hold invoices (HODL invoices) for better UX during request processing
    8. Add webhook/polling mechanism to detect invoice payment
    """

    def can_handle(self, credential: str) -> bool:
        return credential.lower().startswith("ln") or credential.lower().startswith("lnbc")

    async def parse_credential(self, credential: str) -> PaymentCredentials:
        logger.info("Parsing Lightning invoice (PSEUDO)", extra={"invoice_preview": credential[:20]})
        
        # TODO: Use lightning library to decode invoice
        # from lightning_lib import decode_invoice
        # invoice_data = decode_invoice(credential)
        
        return PaymentCredentials(
            raw_credential=credential,
            payment_type="lightning",
            metadata={
                "invoice": credential,
                "amount_msats": 0,  # TODO: Extract from decoded invoice
                "payment_hash": "",  # TODO: Extract payment hash
            },
        )

    async def receive_payment(
        self, credential: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        logger.warning(
            "Lightning payment attempted but not fully implemented",
            extra={"key_hash": key.hashed_key[:8] + "..."},
        )
        
        # TODO: Full implementation steps:
        # 1. Connect to lightning node
        #    node_client = LightningNode(host=settings.lightning_host, macaroon=settings.lightning_macaroon)
        # 
        # 2. Verify invoice was paid
        #    invoice_data = await node_client.lookup_invoice(payment_hash)
        #    if not invoice_data.settled:
        #        raise ValueError("Invoice not yet paid")
        # 
        # 3. Extract amount from invoice
        #    amount_msats = invoice_data.amount_msat
        # 
        # 4. Credit the ApiKey balance (similar to Cashu)
        #    from sqlmodel import col, update
        #    stmt = (
        #        update(ApiKey)
        #        .where(col(ApiKey.hashed_key) == key.hashed_key)
        #        .values(balance=ApiKey.balance + amount_msats)
        #    )
        #    await session.exec(stmt)
        #    await session.commit()
        #    await session.refresh(key)
        # 
        # 5. Return payment result
        
        raise NotImplementedError(
            "Lightning payment method not yet implemented. "
            "See comments in code for implementation steps."
        )

    async def refund_payment(
        self, key: ApiKey, amount_msats: int | None = None
    ) -> dict[str, str]:
        logger.warning("Lightning refund attempted but not fully implemented")
        
        # TODO: Full implementation steps:
        # 1. Get refund destination (lightning address or invoice)
        #    refund_destination = key.refund_address  # Should be lightning address or pubkey
        # 
        # 2. Determine amount to refund
        #    amount_to_refund_sats = (amount_msats or key.balance) // 1000
        # 
        # 3. If refund_address is a lightning address (user@domain.com):
        #    a. Fetch LNURL data from lightning address
        #       lnurl_data = await fetch_lnurl_from_address(key.refund_address)
        #    b. Request invoice from LNURL service
        #       invoice = await request_lnurl_invoice(lnurl_data, amount_to_refund_sats)
        # 
        # 4. If refund_address is a direct invoice, use it
        #    invoice = key.refund_address
        # 
        # 5. Pay the invoice using lightning node
        #    node_client = LightningNode(host=settings.lightning_host, macaroon=settings.lightning_macaroon)
        #    payment_result = await node_client.pay_invoice(invoice)
        # 
        # 6. Return payment details
        #    return {
        #        "payment_hash": payment_result.payment_hash,
        #        "preimage": payment_result.preimage,
        #        "amount_sats": str(amount_to_refund_sats),
        #        "destination": key.refund_address,
        #        "method": "lightning"
        #    }
        
        raise NotImplementedError(
            "Lightning refund not yet implemented. "
            "See comments in code for implementation steps."
        )

    def get_refund_metadata(self, key: ApiKey) -> dict[str, str]:
        return {
            "refund_address": key.refund_address or "",
            "payment_method": "lightning",
        }


class USDTetherPaymentMethod(AbstractPaymentMethod):
    """
    USDT (Tether) payment method for stablecoin payments (PSEUDO-IMPLEMENTATION).
    
    To fully implement:
    1. Choose blockchain network (Ethereum, Tron, Liquid, Lightning, etc.)
    2. Add web3/blockchain library dependency:
       - For Ethereum: web3.py or eth-brownie
       - For Tron: tronpy
       - For Liquid: elements-rpc
       - For Lightning: taproot-assets or RGB
    3. Configure blockchain node connection or API service (Infura, Alchemy, etc.)
    4. Store transaction metadata in ApiKey (add fields: chain, tx_hash, contract_address)
    5. Implement transaction verification:
       - Monitor specific wallet address for incoming USDT
       - Verify transaction confirmations (wait for N blocks)
       - Check transfer amount and recipient
    6. Handle conversion: USDT amount → msats (using exchange rate API)
    7. For refunds:
       - Store user's USDT address
       - Implement token transfer (gas fee consideration)
       - Handle gas estimation and fee payment
    8. Add webhook/event monitoring for blockchain events
    9. Consider multi-chain support (different USDT contracts per chain)
    10. Implement proper error handling for:
        - Insufficient gas
        - Transaction failures
        - Blockchain network congestion
        - Exchange rate fluctuations
    """

    def can_handle(self, credential: str) -> bool:
        # USDT credentials could be:
        # - Transaction hash (0x... for Ethereum/ERC20)
        # - Tron transaction (T... address format)
        # - Payment proof or signed message
        return (
            credential.startswith("0x") and len(credential) == 66  # Ethereum tx hash
        ) or (
            credential.startswith("usdt:") or credential.startswith("tether:")
        )

    async def parse_credential(self, credential: str) -> PaymentCredentials:
        logger.info("Parsing USDT credential (PSEUDO)", extra={"credential_preview": credential[:20]})
        
        # TODO: Implement actual parsing based on chain
        # For Ethereum/ERC20:
        # from web3 import Web3
        # w3 = Web3(Web3.HTTPProvider(settings.ethereum_rpc_url))
        # tx = w3.eth.get_transaction(credential)
        # tx_receipt = w3.eth.get_transaction_receipt(credential)
        # 
        # # Verify it's a USDT transfer to our address
        # usdt_contract_address = "0xdAC17F958D2ee523a2206206994597C13D831ec7"  # Ethereum mainnet
        # if tx['to'].lower() != usdt_contract_address.lower():
        #     raise ValueError("Not a USDT transaction")
        # 
        # # Decode transfer event to get amount and recipient
        # # (requires ABI parsing)
        # amount_usdt = decode_transfer_amount(tx_receipt)
        # recipient = decode_transfer_recipient(tx_receipt)
        # 
        # if recipient.lower() != settings.usdt_receiving_address.lower():
        #     raise ValueError("USDT not sent to our address")
        
        return PaymentCredentials(
            raw_credential=credential,
            payment_type="usdt",
            metadata={
                "tx_hash": credential if credential.startswith("0x") else "",
                "chain": "ethereum",  # TODO: Detect chain from credential format
                "amount_usdt": "0.00",  # TODO: Extract from transaction
                "confirmations": 0,  # TODO: Get confirmation count
            },
        )

    async def receive_payment(
        self, credential: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        logger.warning(
            "USDT payment attempted but not fully implemented",
            extra={"key_hash": key.hashed_key[:8] + "..."},
        )
        
        # TODO: Full implementation steps:
        # 1. Parse and verify the transaction
        #    payment_creds = await self.parse_credential(credential)
        # 
        # 2. Wait for sufficient confirmations (if needed)
        #    required_confirmations = settings.usdt_required_confirmations  # e.g., 3 for ETH
        #    current_confirmations = payment_creds.metadata["confirmations"]
        #    if current_confirmations < required_confirmations:
        #        raise ValueError(f"Insufficient confirmations: {current_confirmations}/{required_confirmations}")
        # 
        # 3. Get USDT amount from transaction
        #    amount_usdt = float(payment_creds.metadata["amount_usdt"])
        # 
        # 4. Convert USDT to msats using exchange rate
        #    from ..payment.price import get_usdt_to_sats_rate
        #    usdt_to_sats = await get_usdt_to_sats_rate()  # e.g., 1 USDT = 3000 sats
        #    amount_sats = int(amount_usdt * usdt_to_sats)
        #    amount_msats = amount_sats * 1000
        # 
        # 5. Credit the balance
        #    from sqlmodel import col, update
        #    stmt = (
        #        update(ApiKey)
        #        .where(col(ApiKey.hashed_key) == key.hashed_key)
        #        .values(balance=ApiKey.balance + amount_msats)
        #    )
        #    await session.exec(stmt)
        #    await session.commit()
        #    await session.refresh(key)
        # 
        # 6. Store transaction metadata for refund purposes
        #    # Might need to add fields to ApiKey: usdt_chain, usdt_tx_hash, usdt_sender_address
        # 
        # 7. Return result
        #    return PaymentResult(
        #        amount_msats=amount_msats,
        #        currency="usdt",
        #        payment_method="usdt",
        #        transaction_id=payment_creds.metadata["tx_hash"],
        #        metadata=payment_creds.metadata,
        #    )
        
        raise NotImplementedError(
            "USDT payment method not yet implemented. "
            "Requires blockchain integration, transaction verification, and exchange rate conversion. "
            "See comments in code for full implementation steps."
        )

    async def refund_payment(
        self, key: ApiKey, amount_msats: int | None = None
    ) -> dict[str, str]:
        logger.warning("USDT refund attempted but not fully implemented")
        
        # TODO: Full implementation steps:
        # 1. Verify we have a refund address
        #    if not key.refund_address:
        #        raise ValueError("No USDT refund address configured")
        # 
        # 2. Validate refund address format (depends on chain)
        #    # For Ethereum: should be 0x... address
        #    # For Tron: should be T... address
        # 
        # 3. Calculate amount to refund
        #    amount_to_refund_msats = amount_msats or key.balance
        #    amount_sats = amount_to_refund_msats // 1000
        # 
        # 4. Convert sats to USDT using exchange rate
        #    from ..payment.price import get_sats_to_usdt_rate
        #    sats_to_usdt = await get_sats_to_usdt_rate()
        #    amount_usdt = amount_sats * sats_to_usdt
        # 
        # 5. Connect to blockchain and estimate gas
        #    from web3 import Web3
        #    w3 = Web3(Web3.HTTPProvider(settings.ethereum_rpc_url))
        #    usdt_contract = w3.eth.contract(
        #        address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        #        abi=USDT_ABI
        #    )
        #    gas_estimate = usdt_contract.functions.transfer(
        #        key.refund_address,
        #        int(amount_usdt * 10**6)  # USDT has 6 decimals
        #    ).estimate_gas({'from': settings.usdt_hot_wallet_address})
        # 
        # 6. Build and sign transaction
        #    nonce = w3.eth.get_transaction_count(settings.usdt_hot_wallet_address)
        #    tx = usdt_contract.functions.transfer(
        #        key.refund_address,
        #        int(amount_usdt * 10**6)
        #    ).build_transaction({
        #        'from': settings.usdt_hot_wallet_address,
        #        'gas': gas_estimate,
        #        'gasPrice': w3.eth.gas_price,
        #        'nonce': nonce,
        #    })
        #    signed_tx = w3.eth.account.sign_transaction(tx, private_key=settings.usdt_private_key)
        # 
        # 7. Broadcast transaction
        #    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        # 
        # 8. Wait for confirmation (optional, for better UX)
        #    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        # 
        # 9. Return transaction details
        #    return {
        #        "tx_hash": tx_hash.hex(),
        #        "recipient": key.refund_address,
        #        "amount_usdt": str(amount_usdt),
        #        "chain": "ethereum",  # or "tron", "liquid", etc.
        #        "method": "usdt",
        #        "block_number": receipt.blockNumber,
        #    }
        
        raise NotImplementedError(
            "USDT refund not yet implemented. "
            "Requires blockchain wallet integration, gas fee handling, and transaction broadcasting. "
            "See comments in code for full implementation steps."
        )

    def get_refund_metadata(self, key: ApiKey) -> dict[str, str]:
        # TODO: Add fields to ApiKey model for USDT-specific metadata
        # - usdt_chain: str (ethereum, tron, liquid, etc.)
        # - usdt_receiving_address: str (for deposits)
        # - usdt_tx_hash: str (original deposit transaction)
        
        return {
            "refund_address": key.refund_address or "",
            "payment_method": "usdt",
            "chain": "ethereum",  # TODO: Store actual chain in ApiKey
        }


class OnChainBitcoinPaymentMethod(AbstractPaymentMethod):
    """
    On-chain Bitcoin payment method (PSEUDO-IMPLEMENTATION).
    
    To fully implement:
    1. Add Bitcoin library dependency (bitcoin-python, bitcoinlib, or electrum)
    2. Configure Bitcoin node/Electrum server connection
    3. Implement HD wallet for address generation (BIP32/BIP44)
    4. Store deposit address per ApiKey (add field: btc_deposit_address)
    5. Monitor blockchain for incoming transactions to deposit addresses
    6. Wait for confirmations (typically 1-6 depending on amount)
    7. Handle fee estimation for refunds (using mempool.space API or fee estimation)
    8. For refunds, build and broadcast Bitcoin transactions
    9. Consider UTXO management and coin selection algorithms
    10. Add support for different address types (P2PKH, P2SH, P2WPKH, P2WSH, Taproot)
    11. Implement proper error handling for:
        - Insufficient confirmations
        - Transaction malleability
        - Replace-by-fee (RBF) scenarios
        - Network congestion
    """

    def can_handle(self, credential: str) -> bool:
        # Bitcoin transaction IDs are 64 hex characters
        # Or could be a signed payment proof
        return (
            len(credential) == 64 and all(c in "0123456789abcdefABCDEF" for c in credential)
        ) or credential.startswith("bitcoin:") or credential.startswith("btc:")

    async def parse_credential(self, credential: str) -> PaymentCredentials:
        logger.info("Parsing Bitcoin transaction (PSEUDO)", extra={"tx_preview": credential[:20]})
        
        # TODO: Use Bitcoin library to verify transaction
        # from bitcoin import SelectParams, deserialize
        # SelectParams('mainnet')  # or 'testnet'
        # 
        # # If credential is a txid, fetch transaction from node/explorer
        # if len(credential) == 64:
        #     from bitcoinrpc.authproxy import AuthServiceProxy
        #     rpc = AuthServiceProxy(settings.bitcoin_rpc_url)
        #     tx_hex = rpc.getrawtransaction(credential)
        #     tx = deserialize(tx_hex)
        #     
        #     # Find output that pays to our address
        #     our_address = settings.btc_receiving_address
        #     amount_btc = 0
        #     for output in tx['vout']:
        #         if output['scriptPubKey']['addresses'][0] == our_address:
        #             amount_btc = output['value']
        #             break
        # 
        # # Get confirmation count
        # block_info = rpc.getblock(tx['blockhash'])
        # confirmations = rpc.getblockcount() - block_info['height'] + 1
        
        return PaymentCredentials(
            raw_credential=credential,
            payment_type="bitcoin",
            metadata={
                "txid": credential if len(credential) == 64 else "",
                "amount_btc": "0.00000000",  # TODO: Extract from transaction
                "confirmations": 0,  # TODO: Get actual confirmation count
            },
        )

    async def receive_payment(
        self, credential: str, key: ApiKey, session: AsyncSession
    ) -> PaymentResult:
        logger.warning(
            "On-chain Bitcoin payment attempted but not fully implemented",
            extra={"key_hash": key.hashed_key[:8] + "..."},
        )
        
        # TODO: Full implementation steps:
        # 1. Parse and verify transaction
        #    payment_creds = await self.parse_credential(credential)
        #    txid = payment_creds.metadata["txid"]
        # 
        # 2. Verify sufficient confirmations
        #    required_confirmations = settings.btc_required_confirmations  # e.g., 3
        #    confirmations = int(payment_creds.metadata["confirmations"])
        #    if confirmations < required_confirmations:
        #        raise ValueError(f"Insufficient confirmations: {confirmations}/{required_confirmations}")
        # 
        # 3. Extract amount in BTC and convert to msats
        #    amount_btc = float(payment_creds.metadata["amount_btc"])
        #    amount_sats = int(amount_btc * 100_000_000)
        #    amount_msats = amount_sats * 1000
        # 
        # 4. Verify transaction hasn't been processed before (check if txid already used)
        #    # Might need to add a transactions table to track processed txids
        #    # from sqlmodel import select
        #    # existing = await session.exec(
        #    #     select(ProcessedTransaction).where(ProcessedTransaction.txid == txid)
        #    # )
        #    # if existing.first():
        #    #     raise ValueError("Transaction already processed")
        # 
        # 5. Credit the balance
        #    from sqlmodel import col, update
        #    stmt = (
        #        update(ApiKey)
        #        .where(col(ApiKey.hashed_key) == key.hashed_key)
        #        .values(balance=ApiKey.balance + amount_msats)
        #    )
        #    await session.exec(stmt)
        #    await session.commit()
        # 
        # 6. Record transaction as processed
        #    # session.add(ProcessedTransaction(txid=txid, key_hash=key.hashed_key))
        #    # await session.commit()
        # 
        # 7. Refresh key and return result
        #    await session.refresh(key)
        #    return PaymentResult(
        #        amount_msats=amount_msats,
        #        currency="btc",
        #        payment_method="bitcoin",
        #        transaction_id=txid,
        #        metadata=payment_creds.metadata,
        #    )
        
        raise NotImplementedError(
            "On-chain Bitcoin payment not yet implemented. "
            "Requires Bitcoin node integration, confirmation monitoring, and UTXO tracking. "
            "See comments in code for full implementation steps."
        )

    async def refund_payment(
        self, key: ApiKey, amount_msats: int | None = None
    ) -> dict[str, str]:
        logger.warning("Bitcoin refund attempted but not fully implemented")
        
        # TODO: Full implementation steps:
        # 1. Verify refund address
        #    if not key.refund_address:
        #        raise ValueError("No Bitcoin refund address configured")
        # 
        # 2. Validate Bitcoin address format
        #    from bitcoin import address_is_valid
        #    if not address_is_valid(key.refund_address):
        #        raise ValueError("Invalid Bitcoin address")
        # 
        # 3. Calculate amount to send
        #    amount_to_refund_msats = amount_msats or key.balance
        #    amount_sats = amount_to_refund_msats // 1000
        #    amount_btc = amount_sats / 100_000_000
        # 
        # 4. Estimate transaction fee
        #    from bitcoinrpc.authproxy import AuthServiceProxy
        #    rpc = AuthServiceProxy(settings.bitcoin_rpc_url)
        #    fee_rate = rpc.estimatesmartfee(6)['feerate']  # BTC/KB for 6-block confirmation
        #    
        #    # Estimate tx size (depends on input/output count and types)
        #    estimated_tx_size = 250  # bytes (1 input, 2 outputs)
        #    fee_btc = (fee_rate / 1024) * estimated_tx_size
        #    fee_sats = int(fee_btc * 100_000_000)
        # 
        # 5. Select UTXOs to spend
        #    # Use coin selection algorithm (e.g., Branch and Bound)
        #    utxos = rpc.listunspent(minconf=1)
        #    selected_utxos = select_coins_for_payment(utxos, amount_sats + fee_sats)
        # 
        # 6. Build transaction
        #    inputs = [{"txid": utxo["txid"], "vout": utxo["vout"]} for utxo in selected_utxos]
        #    outputs = {
        #        key.refund_address: amount_btc,
        #        settings.btc_change_address: (sum(u["amount"] for u in selected_utxos) - amount_btc - fee_btc)
        #    }
        #    raw_tx = rpc.createrawtransaction(inputs, outputs)
        # 
        # 7. Sign transaction
        #    signed_tx = rpc.signrawtransactionwithwallet(raw_tx)
        # 
        # 8. Broadcast transaction
        #    txid = rpc.sendrawtransaction(signed_tx['hex'])
        # 
        # 9. Return transaction details
        #    return {
        #        "txid": txid,
        #        "recipient": key.refund_address,
        #        "amount_btc": str(amount_btc),
        #        "amount_sats": str(amount_sats),
        #        "fee_sats": str(fee_sats),
        #        "method": "bitcoin",
        #    }
        
        raise NotImplementedError(
            "Bitcoin refund not yet implemented. "
            "Requires wallet integration, UTXO management, fee estimation, and transaction signing. "
            "See comments in code for full implementation steps."
        )

    def get_refund_metadata(self, key: ApiKey) -> dict[str, str]:
        # TODO: Add fields to ApiKey model for Bitcoin-specific metadata
        # - btc_deposit_address: str (generated for this key)
        # - btc_deposit_txid: str (original deposit transaction)
        # - btc_address_index: int (for HD wallet derivation)
        
        return {
            "refund_address": key.refund_address or "",
            "payment_method": "bitcoin",
        }


# Registry of available payment methods
_PAYMENT_METHODS: list[AbstractPaymentMethod] = [
    CashuPaymentMethod(),
    LightningPaymentMethod(),
    USDTetherPaymentMethod(),
    OnChainBitcoinPaymentMethod(),
]


def get_payment_method(credential: str) -> AbstractPaymentMethod:
    """
    Get the appropriate payment method handler for a credential.
    
    Args:
        credential: Payment credential string
        
    Returns:
        Payment method instance that can handle the credential
        
    Raises:
        ValueError: If no payment method can handle the credential
    """
    for method in _PAYMENT_METHODS:
        if method.can_handle(credential):
            logger.debug(
                "Matched payment method",
                extra={
                    "method": method.__class__.__name__,
                    "credential_preview": credential[:20] + "...",
                },
            )
            return method
    
    raise ValueError(
        f"No payment method available for credential type. "
        f"Credential preview: {credential[:20]}... "
        f"Available methods: {', '.join(m.__class__.__name__ for m in _PAYMENT_METHODS)}"
    )


def register_payment_method(method: AbstractPaymentMethod) -> None:
    """
    Register a custom payment method.
    
    Args:
        method: Payment method instance to register
    """
    _PAYMENT_METHODS.append(method)
    logger.info(
        "Registered payment method",
        extra={"method_class": method.__class__.__name__},
    )


def list_payment_methods() -> list[str]:
    """
    Get list of registered payment method names.
    
    Returns:
        List of payment method class names
    """
    return [method.__class__.__name__ for method in _PAYMENT_METHODS]
