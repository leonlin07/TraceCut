"""Rumor judgment: decompose a rumor into atomic claims, retrieve evidence per
claim, and produce per-claim verdicts (SUPPORT / REFUTE / UNVERIFIABLE) with
evidence attribution.

This is the main line of TraceCut (see docs/ROADMAP.md). It reuses the existing
LLM provider + EvidenceCollector and adds a query-rewrite step so the zero-config
Wikipedia fallback can match long Chinese claims. Video generation is NOT involved
here — judgment is independent of render quality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from tracecut.config import Settings
from tracecut.providers.llm import LLMProvider, make_provider
from tracecut.schemas import EvidenceDoc
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

REWRITE_SYSTEM = "You rewrite factual claims into concise search queries. Return ONLY JSON."


@dataclass
class Claim:
    claim_id: str
    text: str


@dataclass
class Verdict:
    claim_id: str
    verdict: str  # SUPPORT | REFUTE | UNVERIFIABLE
    evidence_ids: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class JudgmentReport:
    rumor: str
    claims: list[Claim]
    evidence: list[EvidenceDoc]
    verdicts: list[Verdict]
    search_provider: str = "none"

    @property
    def overall(self) -> str:
        """Roll up per-claim verdicts into a single rumor-level label.

        REFUTE dominates (any refuted claim → REFUTE); else if any claim is
        SUPPORTed and none refuted → SUPPORT; else UNVERIFIABLE. This is a
        coarse summary, not a ground-truth — the per-claim verdicts are the
        real artifact.
        """
        vs = {v.verdict for v in self.verdicts}
        if "REFUTE" in vs:
            return "REFUTE"
        if "SUPPORT" in vs:
            return "SUPPORT"
        return "UNVERIFIABLE"

    def to_dict(self) -> dict:
        return {
            "rumor": self.rumor,
            "overall": self.overall,
            "search_provider": self.search_provider,
            "claims": [{"claim_id": c.claim_id, "text": c.text} for c in self.claims],
            "evidence": [
                {"evidence_id": e.evidence_id, "title": e.title, "url": e.url, "source_type": e.source_type}
                for e in self.evidence
            ],
            "verdicts": [
                {
                    "claim_id": v.claim_id,
                    "verdict": v.verdict,
                    "evidence_ids": v.evidence_ids,
                    "reason": v.reason,
                }
                for v in self.verdicts
            ],
        }


def extract_claims(provider: LLMProvider, rumor: str) -> list[Claim]:
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
    return [
        Claim(claim_id=str(c.get("claim_id", f"c_{i:02d}")), text=str(c.get("text", "")))
        for i, c in enumerate(raw.get("claims", [])[:6])
    ]


def rewrite_queries(provider: LLMProvider, claims: list[Claim]) -> list[str]:
    """Turn each sentence-form claim into a short Wikipedia-friendly search query.

    Wikipedia full-text search fails on long Chinese sentences; a short entity
    query matches. Uses the existing SiliconFlow LLM — no new API.
    """
    if not claims:
        return []
    prompt = {
        "task": "Rewrite each claim into a SHORT search query (Wikipedia-style).",
        "claims": [{"claim_id": c.claim_id, "text": c.text} for c in claims],
        "instruction": (
            'Return JSON: {"queries": [{"claim_id": "c_01", "query": "2-6 word search query"}, ...]}. '
            "Query should be the key entity/subject, in the claim's language, no full sentences. "
            "One query per claim, same order."
        ),
    }
    raw = provider.complete_json(
        system=REWRITE_SYSTEM,
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=800,
    )
    by_id = {q.get("claim_id"): str(q.get("query", "")) for q in raw.get("queries", [])}
    return [by_id.get(c.claim_id, c.text) for c in claims]


def judge_claims(provider: LLMProvider, claims: list[Claim], evidence: list[EvidenceDoc]) -> list[Verdict]:
    ev_payload = [
        {"evidence_id": e.evidence_id, "title": e.title, "url": e.url, "excerpt": e.excerpt[:1500]}
        for e in evidence
    ]
    prompt = {
        "task": "Judge each claim against the evidence.",
        "claims": [{"claim_id": c.claim_id, "text": c.text} for c in claims],
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
    out: list[Verdict] = []
    for v in raw.get("verdicts", []):
        verdict = str(v.get("verdict", "UNVERIFIABLE")).upper()
        if verdict not in {"SUPPORT", "REFUTE", "UNVERIFIABLE"}:
            verdict = "UNVERIFIABLE"
        out.append(
            Verdict(
                claim_id=str(v.get("claim_id", "")),
                verdict=verdict,
                evidence_ids=[str(x) for x in v.get("evidence_ids", [])],
                reason=str(v.get("reason", "")),
            )
        )
    return out


def judge_rumor(
    settings: Settings,
    rumor: str,
    source_urls: list[str] | None = None,
    *,
    max_docs: int = 8,
    provider_name: str = "siliconflow",
) -> JudgmentReport:
    """End-to-end rumor judgment. Returns a structured report."""
    source_urls = source_urls or []
    search = make_search_provider(api_key=settings.brave_api_key, base_url=settings.brave_base_url)
    provider = make_provider(settings, provider_name)
    try:
        claims = extract_claims(provider, rumor)
        queries = rewrite_queries(provider, claims)
        collector = EvidenceCollector(timeout_s=30, search=search)
        try:
            evidence = collector.collect_by_claims(
                claims=[c.text for c in claims],
                claim_queries=queries,
                topic=rumor,
                urls=source_urls,
                max_docs=max_docs,
                per_claim_results=2,
            )
        finally:
            collector.close()
        verdicts = judge_claims(provider, claims, evidence)
    finally:
        provider.close()
    return JudgmentReport(
        rumor=rumor,
        claims=claims,
        evidence=evidence,
        verdicts=verdicts,
        search_provider=search.name,
    )


def write_report(report: JudgmentReport, run_dir: Path) -> Path:
    """Persist the judgment report + citations to `run_dir`."""
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "judgment.json"
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "citations.json").write_text(
        json.dumps(
            [
                {"evidence_id": e.evidence_id, "title": e.title, "url": e.url, "source_type": e.source_type}
                for e in report.evidence
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return report_path
