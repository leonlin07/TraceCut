"""Web search providers for active evidence retrieval.

A search provider turns a query string into a ranked list of public result URLs
(with title + snippet). This is what lets the rumor-judgment pipeline find
sources *per claim* instead of relying on user-supplied URLs — the gap that left
the Nobel case UNVERIFIABLE even though the answer was one search away.

The default is Brave Search (https://api.search.brave.com, free tier, one API
key). When no key is configured, the pipeline falls back to the Wikipedia search
API (already used elsewhere) so the system stays runnable with zero config.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class SearchProvider:
    name: str = "base"

    def search(self, query: str, count: int = 5) -> list[SearchResult]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class BraveSearch(SearchProvider):
    """Brave Search API. Requires BRAVE_API_KEY (X-Subscription-Token)."""

    name = "brave"

    def __init__(self, api_key: str, base_url: str = "https://api.search.brave.com", timeout_s: float = 20.0):
        if not api_key:
            raise ValueError("BraveSearch requires BRAVE_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=True,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        )

    def search(self, query: str, count: int = 5) -> list[SearchResult]:
        try:
            response = self._http.get(
                f"{self.base_url}/res/v1/web/search",
                params={"q": query, "count": min(max(count, 1), 20)},
            )
        except httpx.HTTPError:
            return []
        if response.status_code != 200:
            return []
        data = response.json()
        results_raw = (data.get("web") or {}).get("results") or []
        out: list[SearchResult] = []
        for item in results_raw:
            url = item.get("url") or ""
            if not url:
                continue
            out.append(
                SearchResult(
                    title=str(item.get("title") or "")[:300],
                    url=url,
                    snippet=str(item.get("description") or item.get("snippet") or "")[:600],
                )
            )
            if len(out) >= count:
                break
        return out

    def close(self) -> None:
        self._http.close()


class NoSearch(SearchProvider):
    """No-op provider when no search API is configured. Returns nothing so the
    caller falls back to Wikipedia / user URLs."""

    name = "none"

    def search(self, query: str, count: int = 5) -> list[SearchResult]:
        return []


def make_search_provider(api_key: str = "", base_url: str = "https://api.search.brave.com") -> SearchProvider:
    if api_key:
        return BraveSearch(api_key=api_key, base_url=base_url)
    return NoSearch()
