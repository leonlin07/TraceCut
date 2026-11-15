"""Reverse-demo: verify TraceCut's skeleton can do rumor veracity judgment.

This does NOT touch production code and bypasses rendering/TTS entirely. It
reuses three existing components:
  1. EvidenceCollector (sources/web.py) — fetch public sources for a rumor claim
  2. LLMProvider.complete_json — claim extraction + verdict in one prompt each
  3. The same SiliconFlow/Venus gateway already used for planning/review

Pipeline: rumor claim -> evidence collection -> atomic claim extraction ->
per-claim verdict (SUPPORT / REFUTE / UNVERIFIABLE) with evidence attribution.

Goal: confirm the claim-evidence-reviewer backbone is a viable rumor-judgment
core, independent of video generation quality.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tracecut" / "src"))

from tracecut.config import Settings
from tracecut.providers.llm import make_provider
from tracecut.sources.search import make_search_provider
from tracecut.sources.web import EvidenceCollector


CLAIM_EXTRACT_SYSTEM = (
    "You are a fact-checking assistant. Break the input into atomic factual claims "
    "that can be individually verified against public sources. Return ONLY JSON."
)

VERDICT_SYSTEM = (
    "You are a strict fact-checker. For each claim, judge whether the supplied "
    "evidence SUPPORTS it, REFUTES it, or leaves it UNVERIFIABLE. Cite the "
    "evidence_id(s) you used. Never guess. Return ONLY JSON."
)


def extract_claims(provider, rumor: str) -> list[dict]:
    prompt = {
        "task": "Break this rumor into atomic verifiable claims.",
        "rumor": rumor,
        "instruction": (
            'Return JSON: {"claims": [{"claim_id": "c_01", "text": "atomic claim in Chinese"}, ...]}. '
            "Each claim must be a single verifiable statement. 2-6 claims."
        ),
    }
    raw = provider.complete_json(
        system=CLAIM_EXTRACT_SYSTEM,
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=1200,
    )
    return raw.get("claims", [])[:6]


def rewrite_queries(provider, claims: list[dict]) -> list[str]:
    """Turn each sentence-form claim into a short Wikipedia-friendly search query.

    Wikipedia search is full-text and fails on long Chinese sentences
    ("2024年诺贝尔物理学奖授予了John Hopfield" → 0 hits). A short entity query
    ("诺贝尔物理学奖" / "John Hopfield") matches. Uses the existing SiliconFlow
    LLM — no new API, no cost beyond the call already being made.
    """
    if not claims:
        return []
    prompt = {
        "task": "Rewrite each claim into a SHORT search query (Wikipedia-style).",
        "claims": [{"claim_id": c["claim_id"], "text": c["text"]} for c in claims],
        "instruction": (
            'Return JSON: {"queries": [{"claim_id": "c_01", "query": "2-6 word search query"}, ...]}. '
            "Query should be the key entity/subject, in the claim's language, no full sentences. "
            "One query per claim, same order."
        ),
    }
    raw = provider.complete_json(
        system="You rewrite factual claims into concise search queries. Return ONLY JSON.",
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=800,
    )
    by_id = {q.get("claim_id"): str(q.get("query", "")) for q in raw.get("queries", [])}
    return [by_id.get(c["claim_id"], c["text"]) for c in claims]



def judge_claims(provider, claims: list[dict], evidence) -> list[dict]:
    ev_payload = [
        {"evidence_id": e.evidence_id, "title": e.title, "url": e.url, "excerpt": e.excerpt[:1500]}
        for e in evidence
    ]
    prompt = {
        "task": "Judge each claim against the evidence.",
        "claims": claims,
        "evidence": ev_payload,
        "instruction": (
            'Return JSON: {"verdicts": [{"claim_id": "c_01", '
            '"verdict": "SUPPORT|REFUTE|UNVERIFIABLE", '
            '"evidence_ids": ["ev_x", ...], "reason": "concise Chinese reason"}]}. '
            "verdict must be exactly one of SUPPORT/REFUTE/UNVERIFIABLE. "
            "If UNVERIFIABLE, evidence_ids may be empty."
        ),
    }
    raw = provider.complete_json(
        system=VERDICT_SYSTEM,
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=2000,
    )
    return raw.get("verdicts", [])


def run_case(rumor: str, source_urls: list[str], label: str) -> None:
    settings = Settings.from_env()
    print(f"\n{'='*70}\nCASE: {label}\nRUMOR: {rumor}\n{'='*70}")

    search = make_search_provider(api_key=settings.brave_api_key, base_url=settings.brave_base_url)
    provider = make_provider(settings, "siliconflow")
    try:
        # 1. Extract atomic claims first, so we can search per claim.
        claims = extract_claims(provider, rumor)
        print(f"\n[1] Atomic claims: {len(claims)}")
        for c in claims:
            print(f"    - {c['claim_id']}: {c['text']}")

        # 1b. Rewrite each claim into a short search query (free LLM call).
        queries = rewrite_queries(provider, claims)
        print(f"[1b] Search queries: {queries}")

        # 2. Active per-claim evidence retrieval.
        collector = EvidenceCollector(timeout_s=30, search=search)
        try:
            evidence = collector.collect_by_claims(
                claims=[c["text"] for c in claims],
                claim_queries=queries,
                topic=rumor,
                urls=source_urls,
                max_docs=8,
                per_claim_results=2,
            )
        finally:
            collector.close()
        print(f"\n[2] Evidence collected: {len(evidence)} docs  (search={search.name})")
        for e in evidence[:5]:
            print(f"    - {e.evidence_id} | {e.source_type} | {e.title[:50]}")

        # 3. Judge.
        verdicts = judge_claims(provider, claims, evidence)
        print("\n[3] Verdicts:")
        for v in verdicts:
            print(f"    - {v.get('claim_id')}: {v.get('verdict')} | ev={v.get('evidence_ids',[])}")
            print(f"        reason: {str(v.get('reason',''))[:80]}")
    finally:
        provider.close()


if __name__ == "__main__":
    # Case 1: a TRUE claim (should mostly SUPPORT)
    run_case(
        rumor="2024 年诺贝尔物理学奖颁发给了 John Hopfield 和 Geoffrey Hinton，表彰他们在人工神经网络和机器学习方面的基础性发现。",
        source_urls=["https://en.wikipedia.org/wiki/Nobel_Prize_in_Physics",
                     "https://en.wikipedia.org/wiki/Geoffrey_Hinton"],
        label="TRUE — 2024 Nobel Physics",
    )
    # Case 2: a FALSE rumor (should REFUTE)
    run_case(
        rumor="埃菲尔铁塔在 2024 年巴黎奥运会期间被完全拆除并搬到里昂重建。",
        source_urls=["https://en.wikipedia.org/wiki/Eiffel_Tower"],
        label="FALSE — Eiffel Tower dismantled",
    )
    # Case 3: an out-of-context / unverifiable claim
    run_case(
        rumor="某研究显示喝咖啡能让人寿命延长 50 年。",
        source_urls=["https://en.wikipedia.org/wiki/Coffee"],
        label="UNVERIFIABLE — coffee extends life 50y",
    )
