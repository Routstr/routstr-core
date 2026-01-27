import string
from dataclasses import replace
from typing import Dict, List, Set

from ..core.logging import get_logger
from ..core.settings import settings
from .base_ranker import BaseRanker
from .types import SearchResult

logger = get_logger(__name__)

# --- Library Check ---
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
        super().__init__(provider_name="bm25", max_chunks_per_source=max_chunks_per_source)

        # Number of selected Chunks per Website during _rank_local
        # This acts as an upperlimit as _rank_global removes the most irrelevant chunks
        self.local_k = max_chunks_per_source

        # Number of overall selected Chunks during _rank_global
        # TODO: Maybe move this to a setting? 
        self.global_k = 20

    async def check_availability(self) -> bool:
        """Returns True if rank_bm25 is installed and available."""
        return RANK_BM25_AVAILABLE


    #TODO:  Add/update website relevancy scores based on the average score of the remaining chunks 
    async def rank(self, search_result: SearchResult, query: str) -> SearchResult:
        """Orchestrates the 2-step ranking process"""
        if not RANK_BM25_AVAILABLE or not search_result.webpages:
            return search_result

        logger.info(
            f"Ranking results for query: '{query}' (Local K: {self.local_k}, Global K: {self.global_k})"
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

        self._print_report(query, stats)

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

    def _print_report(self, query: str, stats: Dict[str, Dict[str, int]]) -> None:
        """Prints a funnel report showing chunk retention."""
        print("\n" + "=" * 95)
        print(f"RANKING REPORT | Query: '{query}'")
        print(f"{'Source URL':<55} | {'Start':<6} | {'L-Keep':<7} | {'FINAL'}")
        print("-" * 95)

        total_start = total_l_keep = total_final = 0

        for url, data in stats.items():
            initial, l_keep, final = data["initial"], data["after_local"], data["final"]
            total_start += initial
            total_l_keep += l_keep
            total_final += final

            display_url = (url[:52] + "...") if len(url) > 55 else url
            print(f"{display_url:<55} | {initial:<6} | {l_keep:<7} | {final}")

        print("-" * 95)
        print(f"{'TOTALS':<55} | {total_start:<6} | {total_l_keep:<7} | {total_final}")
        print("=" * 95 + "\n")
