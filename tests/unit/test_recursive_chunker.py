import pytest
from routstr.websearch.recursive_chunker import RecursiveChunker

@pytest.mark.asyncio
async def test_recursive_chunker_basic_fit() -> None:
    """Verify that text smaller than chunk_size is returned as a single chunk."""
    chunk_size = 100
    chunker = RecursiveChunker(chunk_size=chunk_size, chunk_overlap=0)
    text = "This is a short string."
    
    chunks = await chunker.chunk_text(text)
    
    assert len(chunks) == 1
    assert chunks[0] == text

@pytest.mark.asyncio
async def test_recursive_chunker_paragraph_splitting() -> None:
    """Verify that the chunker respects paragraph boundaries (\n\n)"""
    # Set chunk size to fit one paragraph but not two
    chunker = RecursiveChunker(chunk_size=15, chunk_overlap=0)
    text = "123456789\n\n987654321"
                           #| <- 15th character  
    chunks = await chunker.chunk_text(text)
    
    assert len(chunks) == 2
    assert chunks[0] == "123456789"
    assert chunks[1] == "987654321"

@pytest.mark.asyncio
async def test_recursive_chunker_truncate_text() -> None:
    """Verify that the chunker respects paragraph boundaries (\n\n)"""
    # Set chunk size to fit one paragraph but not two
    chunker = RecursiveChunker(chunk_size=4, chunk_overlap=0)
    text = "123456789ABCDEFGHIJKL"
    chunks = await chunker.chunk_text(text)
    print(chunks)
    assert len(chunks) == 6
    assert chunks[0] == "1234"
    assert chunks[1] == "5678"
    assert chunks[2] == "9ABC"
    assert chunks[3] == "DEFG"
    assert chunks[4] == "HIJK"
    assert chunks[5] == "L"

@pytest.mark.asyncio
async def test_recursive_chunker_overlap_logic() -> None:
    """Verify that chunk_overlap correctly prepends the tail of the previous chunk."""
    chunk_size = 15
    overlap = 5
    chunker = RecursiveChunker(chunk_size=chunk_size, chunk_overlap=overlap)
    # Text will be split into [0:15] and [15:25]
    text = "123456789ABCDEFGHIJKLMNOP"
    
    chunks = await chunker.chunk_text(text)
    print(chunks)
    assert len(chunks) == 2
    
    assert chunks[0] == "123456789ABCDEF"

    overlap_segment = chunks[0][-overlap:]

    assert chunks[1] == overlap_segment + "GHIJKLMNOP"


@pytest.mark.asyncio
async def test_recursive_chunker_whitespace_normalization() -> None:
    """Verify that internal tabs and multiple spaces are collapsed."""
    chunker = RecursiveChunker(chunk_size=100)
    text = "Too  \t  many    spaces\tand\ttabs.\t\t\t"
    
    chunks = await chunker.chunk_text(text)
    print(chunks)
    assert chunks[0] == "Too many spaces and tabs."

@pytest.mark.asyncio
async def test_recursive_chunker_empty_input() -> None:
    """Verify graceful handling of empty or whitespace-only strings."""
    chunker = RecursiveChunker(chunk_size=100)
    
    assert await chunker.chunk_text("") == []
    assert await chunker.chunk_text("   ") == []

@pytest.mark.asyncio
async def test_recursive_chunker_sentence_boundary_fallback() -> None:
    """Verify that if a paragraph is too long, it falls back to sentence splitting."""
    # Chunk size is small, but paragraph is long. 
    # It should split at the period.
    chunker = RecursiveChunker(chunk_size=30, chunk_overlap=0)
    text = "This is sentence one. This is sentence two."
                                        # | <- 30th character
    chunks = await chunker.chunk_text(text)
    
    assert len(chunks) == 2
    
    assert chunks[0] == "This is sentence one."
    assert chunks[1] == "This is sentence two."
