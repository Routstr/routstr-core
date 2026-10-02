import random

import pytest

from routstr.upstream.sse_splitter import SSEEventSplitter


def _reference_split(chunks: list[bytes]) -> tuple[list[bytes], bytes]:
    """The original rescanning implementation the splitter replaces."""
    events: list[bytes] = []
    buffer = b""
    for chunk in chunks:
        buffer = (buffer + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            raw_event, buffer = buffer.split(b"\n\n", 1)
            events.append(raw_event)
    return events, buffer


def _split(chunks: list[bytes]) -> tuple[list[bytes], bytes]:
    splitter = SSEEventSplitter()
    events = [event for chunk in chunks for event in splitter.feed(chunk)]
    return events, splitter.flush()


STREAMS = [
    b'data: {"a":1}\n\ndata: {"b":2}\n\ndata: [DONE]\n\n',
    b'data: {"a":1}\r\n\r\ndata: {"b":2}\r\n\r\ndata: [DONE]\r\n\r\n',
    b': OPENROUTER PROCESSING\n\ndata: {"a":1}\n\n: keepalive\n\ndata: [DONE]\n\n',
    b'event: response.created\ndata: {"type":"x"}\n\nevent: done\ndata: {"t":1}\n\n',
    b'data: {"part":\ndata: "two"}\n\n\n\ndata: {"trailing":true}',
    b'data: {"a":1}\r\n\r\ndata: {"tail":1}\r',
    b"\n\n\n\n",
    b"",
]


@pytest.mark.parametrize("stream", STREAMS)
def test_matches_reference_at_every_two_way_split(stream: bytes) -> None:
    for cut in range(len(stream) + 1):
        chunks = [stream[:cut], stream[cut:]]
        assert _split(chunks) == _reference_split(chunks)


@pytest.mark.parametrize("stream", STREAMS)
def test_matches_reference_on_random_chunkings(stream: bytes) -> None:
    rng = random.Random(0)
    for _ in range(200):
        cuts = sorted(rng.sample(range(len(stream) + 1), min(len(stream), 6)))
        bounds = [0, *cuts, len(stream)]
        chunks = [stream[a:b] for a, b in zip(bounds, bounds[1:])]
        assert _split(chunks) == _reference_split(chunks)


def test_byte_at_a_time_crlf_stream() -> None:
    stream = b'data: {"a":1}\r\n\r\ndata: {"b":2}\r\n\r\n'
    events, tail = _split([bytes([b]) for b in stream])
    assert events == [b'data: {"a":1}', b'data: {"b":2}']
    assert tail == b""


def test_flush_returns_held_back_carriage_return() -> None:
    splitter = SSEEventSplitter()
    assert splitter.feed(b"data: x\r") == []
    assert splitter.flush() == b"data: x\r"
    assert splitter.flush() == b""


def test_large_event_over_many_chunks() -> None:
    payload = b"data: " + b"x" * 200_000 + b"\n\n"
    chunks = [payload[i : i + 64] for i in range(0, len(payload), 64)]
    events, tail = _split(chunks)
    assert events == [payload[:-2]]
    assert tail == b""
