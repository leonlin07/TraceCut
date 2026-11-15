"""Cost observability for the TraceCut demo.

Aggregates the per-call token usage recorded by ``tracecut.providers.llm``
(via ``set_usage_sink``) into a per-model cost breakdown using the SiliconFlow
(CN) price table. TTS (CosyVoice2) is billed per character, not per token, and
is reported separately by the caller when a character count is available.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ¥ / 百万 token。2026-11 参考值，实际以官网实时价格为准。
_PRICE_IN = {
    "Qwen3.5-35B-A3B": 0.42,
    "Qwen3-VL": 0.53,
    "Qwen3-8B": 0.0,
    "Qwen3.5-4B": 0.0,
    "Xing4.0": 0.0,
}
_PRICE_OUT = {
    "Qwen3.5-35B-A3B": 3.4,
    "Qwen3-VL": 2.1,
    "Qwen3-8B": 0.0,
    "Qwen3.5-4B": 0.0,
    "Xing4.0": 0.0,
}

# CosyVoice2：约 ¥0.5 / 千字
_TTS_RMB_PER_CHAR = 0.5 / 1000


def _match(model: str, table: dict[str, float]) -> float:
    for key, price in table.items():
        if key in model:
            return price
    return 0.0


def _model_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_rmb = _match(model, _PRICE_IN) * prompt_tokens / 1_000_000
    out_rmb = _match(model, _PRICE_OUT) * completion_tokens / 1_000_000
    return in_rmb + out_rmb


@dataclass
class CostSummary:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    llm_cost_rmb: float = 0.0
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    tts_chars: int = 0
    tts_cost_rmb: float = 0.0

    @property
    def total_cost_rmb(self) -> float:
        return self.llm_cost_rmb + self.tts_cost_rmb

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_cost_rmb": round(self.llm_cost_rmb, 4),
            "tts_chars": self.tts_chars,
            "tts_cost_rmb": round(self.tts_cost_rmb, 4),
            "total_cost_rmb": round(self.total_cost_rmb, 4),
            "by_model": self.by_model,
        }


def summarize(usage_records: list[dict], *, tts_chars: int = 0) -> CostSummary:
    """Aggregate usage records into a CostSummary."""
    summary = CostSummary()
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_rmb": 0.0}
    )
    for rec in usage_records:
        model = str(rec.get("model", "unknown"))
        prompt = int(rec.get("prompt_tokens", 0) or 0)
        completion = int(rec.get("completion_tokens", 0) or 0)
        summary.calls += 1
        summary.prompt_tokens += prompt
        summary.completion_tokens += completion
        cost = _model_cost(model, prompt, completion)
        summary.llm_cost_rmb += cost
        bucket = by_model[model]
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt
        bucket["completion_tokens"] += completion
        bucket["cost_rmb"] = round(bucket["cost_rmb"] + cost, 4)
    summary.by_model = {k: v for k, v in by_model.items()}
    summary.tts_chars = tts_chars
    summary.tts_cost_rmb = tts_chars * _TTS_RMB_PER_CHAR
    return summary
