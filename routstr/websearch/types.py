from dataclasses import dataclass, field
from enum import StrEnum
from typing import Dict, List, Optional


class RAGProvider(StrEnum):
    TAVILY = "tavily"
    EXA = "exa"
    CUSTOM = "custom"
    DISABLED = "disabled"


class WebSearchProvider(StrEnum):
    SERPER = "serper"


class WebScrapeProvider(StrEnum):
    HTTP = "http"


class ChunkProvider(StrEnum):
    FIXED = "fixed"
    RECURSIVE = "recursive"


class RankProvider(StrEnum):
    BM25 = "bm25"


@dataclass(frozen=True)
class WebPage:
    """Content retrieved from a single URL.

    Represents processed web content including metadata, full text, and
    relevance-ranked chunks ready for AI context injection.
    """

    url: str
    title: Optional[str] = None
    summary: Optional[str] = None
    publication_date: Optional[str] = None
    relevance_score: Optional[float] = None
    content: Optional[str] = None  # Complete webpage content
    chunks: Optional[List[str]] = None  # List of chunks.


@dataclass(frozen=True)
class SearchResult:
    """Complete RAG search result containing processed web content and metadata.

    Contains the list of processed WebPage objects along with search
    metadata like timing, result count, and optional AI-generated summaries.
    """

    query: str
    webpages: List[WebPage]
    summary: Optional[str] = None
    timestamp: Optional[str] = None
    time_ms: Optional[Dict[str, int]] = field(default_factory=dict)


@dataclass
class WebSearchContext:
    """Context information about a web search/RAG operation.

    Used to pass RAG-related metadata through the request processing pipeline.
    """

    executed: bool = False
    sources: Dict[str, str] = field(default_factory=dict)
