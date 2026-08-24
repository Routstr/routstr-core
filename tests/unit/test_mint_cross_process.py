import asyncio
import fcntl
import json
import multiprocessing
import os
import time
from pathlib import Path

from routstr import node_coordination
from routstr.mint import MintRateGuard


def _configure(root: str) -> None:
    node_coordination.NODE_STATE_DIR = Path(root)
    MintRateGuard._guards.clear()


def _read_counter(path: Path) -> dict[str, int]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"active": 0, "peak": 0, "calls": 0}


def _cooldown_reader(root: str, queue: multiprocessing.Queue) -> None:  # type: ignore[type-arg]
    _configure(root)
    queue.put(MintRateGuard("https://mint.test", 1).cooldown_remaining())


def _probe_worker(
    root: str, counter_path: str, release_path: str, queue: multiprocessing.Queue
) -> None:  # type: ignore[type-arg]
    _configure(root)

    async def run() -> None:
        async def operation() -> str:
            path = Path(counter_path)
            lock_path = path.with_suffix(".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                value = _read_counter(path)
                value["calls"] += 1
                path.write_text(json.dumps(value))
                first = value["calls"] == 1
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            if first:
                while not Path(release_path).exists():
                    await asyncio.sleep(0.01)
            return "ok"

        result = await MintRateGuard("https://mint.test", 2).run(operation)
        queue.put(result)

    try:
        asyncio.run(run())
    except BaseException as error:
        queue.put(f"error:{error!r}")


def _slot_worker(root: str, counter_path: str, queue: multiprocessing.Queue) -> None:  # type: ignore[type-arg]
    _configure(root)

    async def run() -> None:
        path = Path(counter_path)

        async def operation() -> None:
            lock_path = path.with_suffix(".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                value = _read_counter(path)
                active = value["active"] + 1
                value.update(active=active, peak=max(value["peak"], active))
                path.write_text(json.dumps(value))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            await asyncio.sleep(0.2)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                value = _read_counter(path)
                value["active"] -= 1
                path.write_text(json.dumps(value))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        await MintRateGuard("https://mint.test", 2).run(operation)
        queue.put("ok")

    try:
        asyncio.run(run())
    except BaseException as error:
        queue.put(f"error:{error!r}")


def _join(processes: list[multiprocessing.Process]) -> None:
    try:
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)


def test_cooldown_is_visible_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _configure(str(root))
    MintRateGuard("https://mint.test", 1).apply_cooldown(10, reason="rate_limited")
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_cooldown_reader, args=(str(root), queue))
    process.start()
    _join([process])

    assert queue.get(timeout=1) > 0


def test_one_recovery_probe_runs_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    counter = tmp_path / "probe-counter.json"
    release = tmp_path / "release"
    counter.write_text(json.dumps({"active": 0, "peak": 0, "calls": 0}))
    _configure(str(root))
    MintRateGuard("https://mint.test", 2).apply_cooldown(0, reason="rate_limited")
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_probe_worker,
            args=(str(root), str(counter), str(release), queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()

    try:
        deadline = time.time() + 5
        while _read_counter(counter)["calls"] < 1 and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert _read_counter(counter)["calls"] == 1
    finally:
        release.touch()
        _join(processes)

    assert sorted(queue.get(timeout=1) for _ in processes) == ["ok", "ok"]
    assert _read_counter(counter)["calls"] == 2


def test_node_concurrency_slots_bound_all_processes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    counter = tmp_path / "slot-counter.json"
    counter.write_text(json.dumps({"active": 0, "peak": 0, "calls": 0}))
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [
        context.Process(target=_slot_worker, args=(str(root), str(counter), queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    _join(processes)

    assert [queue.get(timeout=1) for _ in processes] == ["ok"] * 4
    assert _read_counter(counter)["peak"] == 2
