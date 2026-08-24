import multiprocessing
import os
from pathlib import Path

import pytest

from routstr import node_coordination


def _assert_read_json_returns_none(path: Path) -> None:
    assert node_coordination.read_json(path) is None


def test_read_json_rejects_oversized_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"{" + b'"x":"' + b"a" * node_coordination._MAX_STATE_BYTES)

    assert node_coordination.read_json(path) is None


def test_read_json_rejects_deeply_nested_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[" * 1100 + "0" + "]" * 1100)

    assert node_coordination.read_json(path) is None


def test_read_json_rejects_oversized_integer(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"value":' + "9" * 4000 + "}")

    assert node_coordination.read_json(path) is None


def test_read_json_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"value":1}')
    link = tmp_path / "state.json"
    os.symlink(target, link)

    assert node_coordination.read_json(link) is None


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="FIFO non-blocking reads are unavailable",
)
def test_read_json_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    os.mkfifo(path)
    process = multiprocessing.Process(
        target=_assert_read_json_returns_none, args=(path,)
    )
    process.start()
    process.join(timeout=2)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("read_json blocked while opening a FIFO")

    assert process.exitcode == 0
