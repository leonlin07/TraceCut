from __future__ import annotations

import hashlib
import html
import ipaddress
import os
import re
import socket
import time
from urllib.parse import quote, urljoin, urlparse, urlunsplit

import httpx

from tracecut.schemas import EvidenceDoc


# Wikipedia and other public APIs 403 on bare/non-identifiable User-Agents.
# Include a contact address so the default works out of the box; override via env.
_USER_AGENT = "TraceCut/0.1 (research prototype; mailto:tracecut-dev@example.com)"
_MAX_DOCUMENT_BYTES = 2_000_000
_MAX_REDIRECTS = 4
_MAX_FETCH_ATTEMPTS = 3


def _evidence_id(url: str) -> str:
    return "ev_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _strip_html(raw: str) -> str:
    """Extract readable article text, dropping nav/sidebar/footer boilerplate.

    Strategy: strip scripts/styles, then prefer a main content container
    (<article>, <main>, #content, .mw-parser-output for Wikipedia). Within it,
    keep only <p> paragraph text (the actual prose). If no container is found,
    fall back to all <p> tags; if those are empty, fall back to the old
    whole-page strip. This is what stops the excerpt from being just the
    navigation/menu — the failure mode that left every rumor case UNVERIFIABLE.
    """
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.IGNORECASE)
    # Drop obvious boilerplate blocks before extracting.
    raw = re.sub(r"<nav[\s\S]*?</nav>", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<footer[\s\S]*?</footer>", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<(header|aside)[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)

    # arXiv: the abstract lives in a citation meta tag / blockquote, not inside
    # <article>. Without this, the generic paragraph fallback grabbed the
    # arXivLabs footer boilerplate ("arXivLabs is a framework...") instead of the
    # actual paper abstract — the failure mode that made the plan drift off-topic.
    m = re.search(
        r'<meta[^>]+name=["\']citation_abstract["\'][^>]+content=["\']([\s\S]*?)["\']',
        raw, flags=re.IGNORECASE,
    )
    if m:
        abstract = _clean_text(m.group(1))
        if len(abstract) >= 120:
            return abstract
    m = re.search(
        r'<blockquote[^>]*class=["\'][^"\']*abstract[^"\']*["\'][^>]*>([\s\S]*?)</blockquote>',
        raw, flags=re.IGNORECASE,
    )
    if m:
        abstract = _clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
        if len(abstract) >= 120:
            return abstract

    def _paragraphs(html_chunk: str) -> str:
        paras = re.findall(r"<p[\s>][\s\S]*?</p>", html_chunk, flags=re.IGNORECASE)
        texts = [re.sub(r"<[^>]+>", " ", p) for p in paras]
        joined = _clean_text(" ".join(texts))
        return joined

    # Try known main-content containers in priority order.
    for pattern in (
        r"<article[\s\S]*?</article>",
        r"<main[\s\S]*?</main>",
        r'<div[^>]*id=["\']?content["\']?[\s\S]*?</div>',
        r'<div[^>]*class=["\']?mw-parser-output["\']?[\s\S]*?</div>',
        r'<div[^>]*id=["\']?mw-content-text["\']?[\s\S]*?</div>',
    ):
        m = re.search(pattern, raw, flags=re.IGNORECASE)
        if m:
            body = _paragraphs(m.group(0))
            if len(body) >= 120:
                return body

    # Fallback: all paragraphs anywhere on the page.
    body = _paragraphs(raw)
    if len(body) >= 120:
        return body

    # Last resort: whole-page text strip.
    return _clean_text(re.sub(r"<[^>]+>", " ", raw))



def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        records = socket.getaddrinfo(
            parsed.hostname, parsed.port or default_port, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return False
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False
    return bool(records)


class EvidenceCollector:
    def __init__(self, timeout_s: float = 30.0, search=None):
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            headers={"User-Agent": os.getenv("TRACECUT_USER_AGENT", _USER_AGENT)},
            trust_env=False,
        )
        # Optional SearchProvider (e.g. Brave) for per-claim active retrieval.
        # When None, collect_by_claims falls back to Wikipedia search.
        self.search = search

    def close(self) -> None:
        self.client.close()
        if self.search is not None:
            self.search.close()

    def collect(self, topic: str, urls: list[str], max_docs: int = 6) -> list[EvidenceDoc]:
        docs: list[EvidenceDoc] = []
        for url in urls:
            if len(docs) >= max_docs:
                break
            doc = self.fetch_url(url)
            if doc:
                docs.append(doc)
        if len(docs) < max_docs:
            for doc in self.wikipedia(topic, max_docs=max_docs - len(docs)):
                if doc.url not in {existing.url for existing in docs}:
                    docs.append(doc)
        return docs[:max_docs]

    def collect_by_claims(
        self,
        claims: list[str],
        topic: str,
        urls: list[str],
        max_docs: int = 8,
        per_claim_results: int = 2,
        claim_queries: list[str] | None = None,
    ) -> list[EvidenceDoc]:
        """Active per-claim evidence retrieval.

        Seed with user-supplied URLs, then for each atomic claim run a web search
        and fetch the top result pages. This is the path that turns UNVERIFIABLE
        (no relevant source among user URLs) into SUPPORT/REFUTE — e.g. the Nobel
        case where the answer was one search away. Deduplicates by final URL.

        ``claim_queries`` optionally provides a short, search-friendly query per
        claim (e.g. LLM-rewritten). Wikipedia search matches full-text and does
        poorly on long sentence-form claims, so a rewritten query ("诺贝尔物理学奖"
        instead of "2024年诺贝尔物理学奖授予了John Hopfield") dramatically
        improves zero-config recall. Falls back to the raw claim if not provided.
        """
        docs: list[EvidenceDoc] = []
        seen_urls: set[str] = set()

        def _add(doc: EvidenceDoc | None) -> None:
            if doc and doc.url not in seen_urls and len(docs) < max_docs:
                seen_urls.add(doc.url)
                docs.append(doc)

        # 1. Seed with user URLs.
        for url in urls:
            if len(docs) >= max_docs:
                break
            _add(self.fetch_url(url))

        # 2. Active search per claim.
        for i, claim in enumerate(claims):
            if len(docs) >= max_docs:
                break
            query = claim_queries[i] if claim_queries and i < len(claim_queries) else claim
            # Try the rewritten query first; if it yields nothing, fall back to
            # the raw claim so a bad rewrite can't starve retrieval.
            results = self._search(query)
            if not results and query != claim:
                results = self._search(claim)
            for r in results[:per_claim_results]:
                if len(docs) >= max_docs:
                    break
                _add(self.fetch_url(r.url))

        # 3. Backfill with Wikipedia on the topic if still short (en + zh).
        if len(docs) < max_docs and self.search is None:
            for lang in ("zh", "en"):
                if len(docs) >= max_docs:
                    break
                for doc in self.wikipedia(topic, max_docs=max_docs - len(docs), lang=lang):
                    _add(doc)
        return docs[:max_docs]

    def _search(self, query: str, count: int = 5):
        """Run a search via the configured provider, or fall back to Wikipedia.

        A real provider (e.g. Brave) is used when configured. When no provider is
        configured (NoSearch), or the provider returns nothing, fall back to
        querying BOTH English and Chinese Wikipedia so a Chinese-language claim
        (e.g. "2024年诺贝尔物理学奖授予了John Hopfield") can still surface the
        right article — en.wikipedia alone returns nothing for Chinese queries.
        Each result's full article is fetched downstream by collect_by_claims via
        fetch_url, so the judge sees real prose, not just search snippets.
        """
        from tracecut.sources.search import NoSearch, SearchResult

        if self.search is not None and not isinstance(self.search, NoSearch):
            try:
                results = self.search.search(query, count=count)
                if results:
                    return results
            except Exception:
                pass
        out: list = []
        seen_titles: set[str] = set()
        for lang in ("en", "zh"):
            for doc in self.wikipedia(query, max_docs=count, lang=lang):
                key = (lang, doc.title)
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                out.append(SearchResult(title=doc.title, url=doc.url, snippet=doc.excerpt[:200]))
                if len(out) >= count:
                    return out
        return out

    def fetch_url(self, url: str) -> EvidenceDoc | None:
        # Public sources (e.g. Wikipedia) intermittently return 403 to bot-like
        # User-Agents even when the UA is contact-bearing. Retry transient HTTP
        # status errors with a short backoff rather than silently dropping the
        # document — a single flaky 403 would otherwise empty the evidence set and
        # abort the whole pipeline.
        # On some networks the plain-HTTP port (80) is reset for foreign hosts
        # (arXiv/Wikipedia) while HTTPS (443) works, so a user-pasted http:// link
        # would fail before any redirect is seen. Prefer upgrading http:// sources
        # to https:// and fall back to the original only if that fails.
        candidates = [url]
        parsed = urlparse(url)
        if parsed.scheme == "http":
            https_url = urlunsplit(
                ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
            )
            candidates = [https_url, url]
        for candidate in candidates:
            for attempt in range(_MAX_FETCH_ATTEMPTS):
                doc, transient = self._fetch_url_once(candidate)
                if doc is not None:
                    return doc
                if not transient:
                    break  # permanent rejection; try the next candidate
                time.sleep(0.8 * (attempt + 1))
        return None

    def _fetch_url_once(self, url: str) -> tuple[EvidenceDoc | None, bool]:
        """Fetch once. Returns (doc, transient_error). A transient error means a
        retry is worthwhile (HTTP status error or network error); a None with
        transient=False means a permanent rejection (SSRF, non-text body, etc.)."""
        current_url = url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                if not _is_public_http_url(current_url):
                    return None, False
                with self.client.stream("GET", current_url) as streamed:
                    if streamed.status_code in {301, 302, 303, 307, 308}:
                        location = streamed.headers.get("location")
                        if not location:
                            return None, False
                        current_url = urljoin(current_url, location)
                        continue
                    streamed.raise_for_status()
                    content_type = streamed.headers.get("content-type", "")
                    if "text" not in content_type and "json" not in content_type:
                        return None, False
                    # Read the decoded body directly (iter_bytes already
                    # gunzips). We must NOT re-wrap it in an httpx.Response with
                    # the original Content-Encoding header — that would make
                    # .text try to gunzip already-decoded bytes and raise
                    # DecodingError on every gzip'd page (e.g. Wikipedia).
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in streamed.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_DOCUMENT_BYTES:
                            return None, False
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    encoding = streamed.charset_encoding or "utf-8"
                    try:
                        text = body.decode(encoding, errors="replace")
                    except (LookupError, TypeError):
                        text = body.decode("utf-8", errors="replace")
                    title_match = re.search(
                        r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S
                    )
                    parsed = urlparse(current_url)
                    title = _strip_html(title_match.group(1)) if title_match else parsed.netloc
                    stripped = _strip_html(text)[:6000]
                    if len(stripped) < 80:
                        return None, False
                    return (
                        EvidenceDoc(
                            evidence_id=_evidence_id(current_url),
                            title=title[:300],
                            url=current_url,
                            excerpt=stripped,
                            source_type="user_url",
                        ),
                        False,
                    )
        except httpx.HTTPStatusError:
            return None, True
        except httpx.HTTPError:
            # Network/timeout/decoding error — transient.
            return None, True

    def wikipedia(self, topic: str, max_docs: int = 3, lang: str = "en") -> list[EvidenceDoc]:
        search_url = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "format": "json",
            "utf8": "1",
            "srlimit": str(max_docs),
        }
        try:
            response = self.client.get(search_url, params=params)
            response.raise_for_status()
            hits = response.json().get("query", {}).get("search", [])
        except (httpx.HTTPError, ValueError):
            return []
        docs = []
        for hit in hits:
            title = str(hit.get("title", "")).strip()
            snippet = _strip_html(str(hit.get("snippet", "")))
            if not title or not snippet:
                continue
            url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
            docs.append(
                EvidenceDoc(
                    evidence_id=_evidence_id(url),
                    title=title,
                    url=url,
                    excerpt=snippet,
                    source_type="wikipedia",
                )
            )
        return docs
