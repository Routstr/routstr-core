import string
from dataclasses import replace
from typing import Dict, List, Set

from ..core.logging import get_logger
from .base_ranker import BaseRanker
from .types import SearchResult

logger = get_logger(__name__)

RANK_BM25_AVAILABLE = False
try:
    from rank_bm25 import BM25Okapi

    RANK_BM25_AVAILABLE = True
except ImportError:
    logger.warning("rank_bm25 not installed. BM25 ranking will be unavailable.")
    BM25Okapi = None


class BM25Ranker(BaseRanker):
    """
    BM25-based ranker that performs local-to-global pruning.
    """

    def __init__(self, max_chunks_per_source: int = 5) -> None:
        super().__init__(
            provider_name="bm25", max_chunks_per_source=max_chunks_per_source
        )

        # Number of selected Chunks per Website during _rank_local
        # This acts as an upperlimit as _rank_global removes the most irrelevant chunks
        self.local_k = max_chunks_per_source

        # Number of overall selected Chunks during _rank_global
        # TODO: Maybe move this to a setting?
        self.global_k = 20

    async def check_availability(self) -> bool:
        """Returns True if rank_bm25 is installed and available."""
        return RANK_BM25_AVAILABLE

    async def rank(self, search_result: SearchResult, query: str) -> SearchResult:
        """
        Orchestrates the 2-step ranking process:
        1. Local pruning: Keep top-self.local_k chunks per webpage.
        2. Global pruning: Keep top-self.global_k chunks across all webpages.
        """
        if not RANK_BM25_AVAILABLE or not search_result.webpages:
            return search_result

        logger.info(
            f"Ranking results for query: '{query}'",
            extra={"query": query, "local_k": self.local_k, "global_k": self.global_k},
        )

        # Statistics Tracking
        stats: Dict[str, Dict[str, int]] = {}
        for p in search_result.webpages:
            stats[p.url] = {"initial": len(p.chunks or [])}

        # Local Pruning
        local_result = self._rank_local(search_result, query, top_k=self.local_k)
        for p in local_result.webpages:
            stats[p.url]["after_local"] = len(p.chunks or [])

        # Global Pruning
        final_result = self._rank_global(local_result, query, total_k=self.global_k)
        for p in final_result.webpages:
            stats[p.url]["final"] = len(p.chunks or [])

        self._log_report(query, stats)

        return final_result

    def _rank_local(
        self, search_result: SearchResult, query: str, top_k: int
    ) -> SearchResult:
        """Sorts and prunes chunks within each individual page."""
        updated_pages = []
        for page in search_result.webpages:
            if not page.chunks:
                updated_pages.append(page)
                continue

            sorted_chunks = self._sort_chunks(query, page.chunks)
            updated_pages.append(replace(page, chunks=sorted_chunks[:top_k]))

        return replace(search_result, webpages=updated_pages)

    def _rank_global(
        self, search_result: SearchResult, query: str, total_k: int
    ) -> SearchResult:
        """Pools all chunks from all pages and picks the top global winners."""
        all_candidates = []
        for page in search_result.webpages:
            if page.chunks:
                all_candidates.extend(page.chunks)

        if not all_candidates:
            return search_result

        global_winners = self._sort_chunks(query, all_candidates)[:total_k]
        winners_set: Set[str] = set(global_winners)

        final_pages = []
        for page in search_result.webpages:
            if not page.chunks:
                final_pages.append(page)
                continue

            surviving_chunks = [c for c in page.chunks if c in winners_set]
            final_pages.append(replace(page, chunks=surviving_chunks))

        return replace(search_result, webpages=final_pages)

    def _sort_chunks(self, query: str, chunks: List[str]) -> List[str]:
        """
        Uses the BM25 (Best Matching 25) algorithm to calculate relevance.
        """
        if not chunks:
            return []

        def tokenize(text: str) -> List[str]:
            # Lowercase and remove punctuation
            return (
                text.lower()
                .translate(str.maketrans("", "", string.punctuation))
                .split()
            )

        tokenized_corpus = [tokenize(c) for c in chunks]
        tokenized_query = tokenize(query)

        bm25 = BM25Okapi(tokenized_corpus)
        return bm25.get_top_n(
            tokenized_query, chunks, n=len(chunks)
        )  # n=len(chunks) returns every chunk, but sorted by score

    def _log_report(self, query: str, stats: Dict[str, Dict[str, int]]) -> None:
        """Logs a funnel report showing chunk retention."""
        report_lines = [
            f"RANKING REPORT | Query: '{query}'",
            f"{'Source URL':<40} | {'Start':<5} | {'L-Keep':<6} | {'FINAL'}",
            "-" * 70,
        ]

        total_start = total_l_keep = total_final = 0

        for url, data in stats.items():
            initial, l_keep, final = data["initial"], data["after_local"], data["final"]
            total_start += initial
            total_l_keep += l_keep
            total_final += final

            # Sanitize URL for display
            display_url = url.replace("https://", "").replace("http://", "")
            if len(display_url) > 37:
                display_url = display_url[:37] + "..."

            report_lines.append(
                f"{display_url:<40} | {initial:<5} | {l_keep:<6} | {final}"
            )

        report_lines.append("-" * 70)
        report_lines.append(
            f"{'TOTALS':<40} | {total_start:<5} | {total_l_keep:<6} | {total_final}"
        )

        logger.info("BM25 Ranking completed", extra={"ranking_stats": stats})
        logger.debug("\n".join(report_lines))
