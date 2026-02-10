"""Recursive semantic text chunker implementation.

This module provides a semantic chunker that splits text based on natural
boundaries (paragraphs, lines, sentences) recursively. This approach respects
the logical flow of the document better than fixed-size chunking.
"""

import re
from typing import List

from ..core.logging import get_logger
from .base_chunker import BaseChunker

logger = get_logger(__name__)


class RecursiveChunker(BaseChunker):
    """Recursive semantic text chunker.

    Splits text by hierarchy:
    1. Paragraphs (double newlines)
    2. Single newlines
    3. Sentences (. ! ? ...)
    4. Words
    """

    chunker_name = "recursive"

    def __init__(self, chunk_size: int, chunk_overlap_perc: float = 0.05) -> None:
        """Initialize the recursive semantic chunker.

        Args:
            chunk_size: Maximum size of each chunk in characters.
        """

        # Number of characters to overlap between chunks.
        # Adds 5% overlap in total, half on each side, in addition to chunk_size
        chunk_overlap = int(chunk_size * chunk_overlap_perc)
        super().__init__(chunk_size, chunk_overlap)
        
  
        logger.info(
            f"Initialized {self.chunker_name} chunker with size={chunk_size}, overlap={self.chunk_overlap}"
        )

    async def chunk_text(self, text: str) -> List[str]:
        """Chunk text by recursively splitting on semantic boundaries.

        Note:
            The final chunks will be slightly larger than chunk_size because
            overlap is added as contextual padding on both sides of each chunk.

        Args:
            text: The text to chunk

        Returns:
            List of text chunks as strings
        """
        if not text or not text.strip():
            logger.debug("Empty text provided, returning empty chunk list")
            return []

        # Clean up multiple spaces/tabs to normalize text
        text = re.sub(r"[ \t]+", " ", text).strip()

        text_length = len(text)

        # If text is shorter than chunk_size, return it as a single chunk
        if text_length <= self.chunk_size:
            logger.debug(
                f"Text length ({text_length}) <= chunk_size, returning single chunk"
            )
            return [text]

        # Define the hierarchy of separators
        separators = ["\n\n", "\n", "SENTENCE_END", " "]

        # Perform recursive splitting and reconstruction
        raw_chunks = self._split_recursive(text, separators)

        # Apply overlap if requested
        if self.chunk_overlap > 0 and len(raw_chunks) > 1:
            raw_chunks = self._apply_overlap(raw_chunks)

        logger.debug(
            f"Chunked text into {len(raw_chunks)} chunks using {self.chunker_name} strategy"
        )

        return raw_chunks

    def _split_recursive(self, text: str, separators: List[str]) -> List[str]:
        """Recursively splits text and recombines pieces to fill chunk_size."""

        if len(text) <= self.chunk_size:
            return [text]

        # force hard cut
        if not separators:
            return [
                text[i : i + self.chunk_size]
                for i in range(0, len(text), self.chunk_size)
            ]

        current_sep = separators[0]

        if current_sep == "SENTENCE_END":
            splits = re.split(r"(?<=[.!?])\s+", text)
        elif current_sep == "\n\n":
            splits = re.split(r"\n\s*\n", text)
        elif current_sep == "\n":
            splits = text.split("\n")
        else:
            splits = text.split(" ")

        splits = [s for s in splits if s.strip()]

        # try the next separator
        if len(splits) <= 1:
            return self._split_recursive(text, separators[1:])

        final_chunks: list[str] = []
        buffer: list[str] = []
        buffer_len: int = 0

        for piece in splits:
            if len(piece) > self.chunk_size:
                if buffer:
                    final_chunks.append(self._join_pieces(buffer, current_sep))
                    buffer, buffer_len = [], 0

                final_chunks.extend(self._split_recursive(piece, separators[1:]))
                continue

            potential_len = buffer_len + len(piece) + (1 if buffer else 0)

            if potential_len <= self.chunk_size:
                buffer.append(piece)
                buffer_len = potential_len
            else:
                if buffer:
                    final_chunks.append(self._join_pieces(buffer, current_sep))

                buffer = [piece]
                buffer_len = len(piece)

        if buffer:
            final_chunks.append(self._join_pieces(buffer, current_sep))

        return final_chunks

    def _join_pieces(self, pieces: list[str], separator_type: str) -> str:
        """Helper to join pieces back together with the correct character."""
        if separator_type == "\n\n":
            return "\n\n".join(pieces)
        if separator_type == "\n":
            return "\n".join(pieces)
        return " ".join(pieces)

    def _apply_overlap(self, chunks: List[str]) -> List[str]:
        merged_chunks = []
        num_chunks = len(chunks)
        
        # Calculate half-overlap to keep chunk size growth under control
        half_overlap = self.chunk_overlap // 2

        # If overlap is too small to split across both sides, return as-is
        if half_overlap <= 0:
           return chunks   
    
        for i in range(num_chunks):
            current = chunks[i]
            
            # Add tail of previous chunk to the START
            prefix = ""
            if i > 0:
                prefix = chunks[i-1][-half_overlap:]
                
            # Add head of next chunk to the END
            suffix = ""
            if i < num_chunks - 1:
                suffix = chunks[i+1][:half_overlap]
                
            merged_chunks.append(prefix + current + suffix)
            
        return merged_chunks