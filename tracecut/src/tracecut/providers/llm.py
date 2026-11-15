from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from openai import BadRequestError, OpenAI
from openai.types.chat import ChatCompletionMessageToolCall

from tracecut.config import Settings


# Optional, process-wide token-usage sink for cost observability. It is None by
# default so the hot path stays allocation-free; the cost tracker attaches a
# list via set_usage_sink() and reads per-call prompt/completion token counts.
_USAGE_SINK: list[dict] | None = None


def set_usage_sink(sink: list[dict] | None) -> None:
    """Attach (or detach) a list that receives one dict per LLM completion."""
    global _USAGE_SINK
    _USAGE_SINK = sink


def _record_usage(operation: str, model: str, usage: Any) -> None:
    """Append a usage record to the sink, if one is attached. Never raises."""
    if _USAGE_SINK is None or usage is None:
        return
    try:
        _USAGE_SINK.append(
            {
                "operation": operation,
                "model": model,
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
        )
    except Exception:  # pragma: no cover - never let accounting break a run
        pass


class ToolCallingUnsupported(Exception):
    """Raised when a provider or model does not support tool-calling."""

    pass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key: str
    model: str


class LLMProvider:
    """Minimal OpenAI-compatible client with OS proxy inheritance disabled."""

    def __init__(self, spec: ProviderSpec, timeout_s: float = 90.0):
        if not spec.api_key:
            raise ValueError(f"{spec.name} API key is not configured")
        if not spec.base_url:
            raise ValueError(f"{spec.name} base URL is not configured")
        self.spec = spec
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=True,
            trust_env=False,
        )
        self._client = OpenAI(
            api_key=spec.api_key,
            base_url=spec.base_url,
            timeout=timeout_s,
            http_client=self._http,
        )

    def close(self) -> None:
        self._http.close()

    @property
    def _is_thinking_model(self) -> bool:
        """Qwen3 thinking models accept `enable_thinking`; VL models and others
        reject it. Only set the flag for Qwen3 text thinking models."""
        m = self.spec.model.lower()
        return "qwen3" in m and "vl" not in m and "omni" not in m

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Qwen3 family "thinking" models (e.g. Qwen3.5-35B-A3B) emit a reasoning trace
        # that can consume the token budget and leak into the JSON body. Disable it for
        # structured planning/review calls on the SiliconFlow gateway. Venus proxies the
        # same base model, so apply the same flag; gateways that ignore extra_body are
        # unaffected.
        if self._is_thinking_model:
            kwargs["extra_body"] = {"enable_thinking": False}
        try:
            response = self._client.chat.completions.create(
                **kwargs,
                response_format={"type": "json_object"},
            )
        except BadRequestError:
            # Some OpenAI-compatible gateways do not implement response_format.
            response = self._client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        _record_usage("complete_json", self.spec.model, getattr(response, "usage", None))
        return extract_json_object(text)

    def complete_json_with_image(
        self,
        *,
        system: str,
        user: str,
        image_b64: str,
        image_media: str = "image/jpeg",
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Multimodal JSON completion: send an image (base64) plus text, parse JSON.

        Uses OpenAI-style vision content. The model MUST be a VL model (e.g.
        Qwen3-VL); the base LlmProvider.spec.model is overridden by the caller
        via a dedicated VL provider.
        """
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image_media};base64,{image_b64}"},
                        },
                        {"type": "text", "text": user},
                    ],
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._is_thinking_model:
            kwargs["extra_body"] = {"enable_thinking": False}
        try:
            response = self._client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except BadRequestError:
            response = self._client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        _record_usage("complete_json_with_image", self.spec.model, getattr(response, "usage", None))
        return extract_json_object(text)

    def health(self) -> tuple[bool, str]:
        try:
            models = self._client.models.list()
            count = len(getattr(models, "data", []) or [])
            return True, f"reachable ({count} models listed)"
        except Exception as exc:  # provider gateways may not implement /models
            return False, f"unverified: {type(exc).__name__}: {str(exc)[:120]}"

    def chat_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> tuple[dict[str, Any], list["ToolCall"]]:
        """Run one tool-calling turn against an OpenAI-compatible chat endpoint.

        Returns (assistant_message, tool_calls). If the model does not support
        tool-calling for the configured model, raises ToolCallingUnsupported so
        callers can fall back to a single-shot JSON path.
        """
        if not tools:
            # No tools registered: ask for plain text and treat as no decisions.
            response = self._client.chat.completions.create(
                model=self.spec.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
            )
            assistant_message = {
                "role": "assistant",
                "content": response.choices[0].message.content or "",
            }
            _record_usage("chat_with_tools", self.spec.model, getattr(response, "usage", None))
            return assistant_message, []

        try:
            response = self._client.chat.completions.create(
                model=self.spec.model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except BadRequestError as exc:
            body = exc.body or {}
            message = str(body.get("message", exc.message)).lower()
            if "tool" in message or "function" in message:
                raise ToolCallingUnsupported(str(exc)) from exc
            raise

        message = response.choices[0].message
        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
        }
        tool_calls: list[ToolCall] = []
        if getattr(message, "tool_calls", None):
            assistant_message["tool_calls"] = []
            for raw in message.tool_calls:
                if isinstance(raw, ChatCompletionMessageToolCall):
                    call_id = raw.id
                    name = raw.function.name
                    arguments = raw.function.arguments
                else:
                    call_id = str(raw.get("id", ""))
                    func = raw.get("function", {})
                    name = func.get("name", "")
                    arguments = func.get("arguments", "")
                try:
                    args = json.loads(arguments) if isinstance(arguments, str) else arguments
                except json.JSONDecodeError:
                    args = {}
                assistant_message["tool_calls"].append(
                    {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                )
                tool_calls.append(ToolCall(call_id=call_id, name=name, arguments=args))
        _record_usage("chat_with_tools", self.spec.model, getattr(response, "usage", None))
        return assistant_message, tool_calls


@dataclass(frozen=True)
class ToolCall:
    """One function/tool call emitted by a model in a tool-calling loop."""

    call_id: str
    name: str
    arguments: dict[str, Any]


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response does not contain a JSON object")
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model response JSON must be an object")
    return data


def provider_spec(settings: Settings, name: str) -> ProviderSpec:
    if name == "siliconflow":
        return ProviderSpec(
            name=name,
            base_url=settings.siliconflow_base_url,
            api_key=settings.siliconflow_api_key,
            model=settings.siliconflow_model,
        )
    if name == "venus":
        return ProviderSpec(
            name=name,
            base_url=settings.venus_base_url,
            api_key=settings.venus_api_key,
            model=settings.venus_model,
        )
    raise ValueError(f"unknown provider: {name}")


def make_provider(settings: Settings, name: str) -> LLMProvider:
    return LLMProvider(provider_spec(settings, name), settings.request_timeout_s)


def make_vl_provider(settings: Settings) -> LLMProvider:
    """Build a VL (vision-language) provider on the SiliconFlow gateway.

    Reuses the SiliconFlow base_url/api_key but swaps in the VL model. Used by
    the VL judge to score frame-claim relevance.
    """
    spec = ProviderSpec(
        name="siliconflow",
        base_url=settings.siliconflow_base_url,
        api_key=settings.siliconflow_api_key,
        model=settings.vl_model,
    )
    return LLMProvider(spec, settings.request_timeout_s)
