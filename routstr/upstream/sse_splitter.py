"""Incremental SSE event splitting that stays linear in stream size."""


class SSEEventSplitter:
    """Split upstream bytes into SSE events delimited by a blank line.

    CRLF is normalized to LF. Each call only scans newly received bytes, so a
    large event arriving over many network chunks (e.g. a Responses API
    ``response.completed`` carrying the full output) costs O(n) rather than
    rescanning the buffered prefix on every chunk.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        # A trailing CR may be the first half of a CRLF split across chunks.
        self._pending_cr = False

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add ``chunk`` and return the events it completed, without delimiters."""
        if self._pending_cr:
            chunk = b"\r" + chunk
        self._pending_cr = chunk.endswith(b"\r")
        if self._pending_cr:
            chunk = chunk[:-1]

        # The delimiter may straddle the old tail and the new chunk.
        scan_from = max(len(self._buffer) - 1, 0)
        self._buffer += chunk.replace(b"\r\n", b"\n")

        events: list[bytes] = []
        start = 0
        while (end := self._buffer.find(b"\n\n", scan_from)) != -1:
            events.append(bytes(self._buffer[start:end]))
            start = scan_from = end + 2
        if start:
            del self._buffer[:start]
        return events

    def flush(self) -> bytes:
        """Return any trailing bytes that never saw a closing blank line."""
        tail = bytes(self._buffer) + (b"\r" if self._pending_cr else b"")
        self._buffer.clear()
        self._pending_cr = False
        return tail
