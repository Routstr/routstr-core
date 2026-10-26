from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _untracked_x_cashu_settlement(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Unit tests mock the ledger writes, so their payments are untracked.

    Tests of the settlement state itself restore the real functions against a
    real database (see test_x_cashu_settlement.py).
    """
    from routstr.payment import x_cashu_settlement

    async def no_transition(*_: object) -> bool:
        return False

    monkeypatch.setattr(x_cashu_settlement, "_transition", no_transition)
    monkeypatch.setattr(x_cashu_settlement, "_is_tracked", no_transition)
    yield
