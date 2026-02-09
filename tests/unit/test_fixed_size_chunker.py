import pytest

from routstr.websearch.fixed_size_chunker import FixedSizeChunker


@pytest.mark.asyncio
async def test_fixed_chunker_basic_split() -> None:
    """Verify that text is split into exactly the specified chunk size."""
    chunk_size = 10
    chunker = FixedSizeChunker(chunk_size=chunk_size, chunk_overlap=0)
    text = "1234567890ABCDEFGHIJ"  # 20 chars

    chunks = await chunker.chunk_text(text)

    assert len(chunks) == 2
    assert chunks[0] == "1234567890"
    assert chunks[1] == "ABCDEFGHIJ"
