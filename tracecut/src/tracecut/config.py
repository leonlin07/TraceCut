from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_default_env() -> list[Path]:
    explicit = os.getenv("TRACECUT_ENV_FILE")
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path.cwd() / ".env",
        PROJECT_ROOT / ".env",
        WORKSPACE_ROOT / ".env",
    ]
    loaded: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            load_env_file(resolved)
            loaded.append(resolved)
    return loaded


@dataclass(frozen=True)
class Settings:
    siliconflow_api_key: str
    siliconflow_base_url: str
    siliconflow_model: str
    venus_api_key: str
    venus_base_url: str
    venus_model: str
    pexels_api_key: str
    brave_api_key: str
    brave_base_url: str
    output_dir: Path
    tts_voice: str
    tts_backend: str
    cosyvoice_voice: str
    tts_instruction: str
    vl_model: str
    bgm_dir: Path
    request_timeout_s: float = 90.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_default_env()
        output_raw = os.getenv("TRACECUT_OUTPUT_DIR", "runs")
        output_dir = Path(output_raw).expanduser()
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir
        return cls(
            siliconflow_api_key=os.getenv("SILICONFLOW_API_KEY", ""),
            siliconflow_base_url=os.getenv(
                "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
            ).rstrip("/"),
            # SiliconFlow is the default public gateway. Venus is an optional self-hosted
            # OpenAI-compatible gateway for the same base model; leave VENUS_BASE_URL empty
            # to disable it.
            siliconflow_model=os.getenv("SILICONFLOW_MODEL", "Qwen/Qwen3.5-35B-A3B"),
            venus_api_key=os.getenv("VENUS_API_KEY", ""),
            venus_base_url=os.getenv("VENUS_BASE_URL", "").rstrip("/"),
            venus_model=os.getenv("VENUS_MODEL", "Qwen/Qwen3.5-35B-A3B"),
            pexels_api_key=os.getenv("PEXELS_API_KEY", ""),
            brave_api_key=os.getenv("BRAVE_API_KEY", ""),
            brave_base_url=os.getenv("BRAVE_BASE_URL", "https://api.search.brave.com"),
            output_dir=output_dir.resolve(),
            tts_voice=os.getenv("TRACECUT_TTS_VOICE", "Tingting"),
            tts_backend=os.getenv("TRACECUT_TTS", "cosyvoice"),
            cosyvoice_voice=os.getenv(
                "TRACECUT_COSYVOICE_VOICE", "FunAudioLLM/CosyVoice2-0.5B:alex"
            ),
            tts_instruction=os.getenv("TRACECUT_TTS_INSTRUCTION", ""),
            vl_model=os.getenv("TRACECUT_VL_MODEL", "Qwen/Qwen3-VL-8B-Instruct"),
            bgm_dir=Path(
                os.getenv("TRACECUT_BGM_DIR", str(PROJECT_ROOT / ".." / "branch_7" / "assets" / "bgm"))
            ).expanduser(),
            request_timeout_s=float(os.getenv("TRACECUT_REQUEST_TIMEOUT_S", "90")),
        )

    def provider_ready(self, name: str) -> bool:
        if name == "siliconflow":
            return bool(self.siliconflow_api_key and self.siliconflow_base_url)
        if name == "venus":
            return bool(self.venus_api_key and self.venus_base_url)
        return False
