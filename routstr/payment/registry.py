from .methods.cashu import CashuPaymentMethod
from .methods.lightning import BitcoinLightningPaymentMethod
from .methods.usdt import USDTPaymentMethod
from .payment_methods import PaymentMethod

_payment_methods: dict[str, PaymentMethod] = {}


def register_payment_method(method: PaymentMethod) -> None:
    _payment_methods[method.name] = method


def get_payment_method(name: str) -> PaymentMethod | None:
    return _payment_methods.get(name)


def get_all_payment_methods() -> dict[str, PaymentMethod]:
    return _payment_methods.copy()


def detect_payment_method(payment_data: str) -> PaymentMethod | None:
    for method in _payment_methods.values():
        try:
            if method.validate_payment_data(payment_data):
                return method
        except Exception:
            continue
    return None


def initialize_payment_methods() -> None:
    if not _payment_methods:
        register_payment_method(CashuPaymentMethod())
        register_payment_method(BitcoinLightningPaymentMethod())
        register_payment_method(USDTPaymentMethod())
