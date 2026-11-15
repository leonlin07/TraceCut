"""TTS backends for narration synthesis.

All backends return a 24 kHz mono WAV path and its duration in seconds, which is
exactly what ``media.render_video`` consumes. The default is SiliconFlow
CosyVoice2 (``FunAudioLLM/CosyVoice2-0.5B``) via the OpenAI-compatible audio
endpoint — it runs on the Mac with no local model and no GPU. ``say`` is kept as
a zero-dependency offline fallback.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            try:
                return float(result.stdout.strip())
            except ValueError:
                pass
    return 0.0


def _estimate_duration(text: str) -> float:
    # Rough fallback when no real duration can be measured. ~4.2 chars/sec for
    # Chinese narration.
    return max(1.2, len(text) / 4.2)


def write_silence(path: Path, duration_s: float, sample_rate: int = 24000) -> float:
    frames = max(1, math.ceil(duration_s * sample_rate))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    return frames / sample_rate


@dataclass
class TTSResult:
    wav_path: Path
    duration_s: float


class TTSBackend:
    """Synthesize ``text`` to a 24 kHz mono WAV at ``output_wav``."""

    name: str = "base"

    def synthesize(self, text: str, output_wav: Path) -> TTSResult:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SayTTS(TTSBackend):
    """macOS ``say`` → AIFF → FFmpeg → 24 kHz mono WAV. Zero dependencies."""

    name = "say"

    def __init__(self, voice: str = "Tingting"):
        self.voice = voice

    def synthesize(self, text: str, output_wav: Path) -> TTSResult:
        say = shutil.which("say")
        ffmpeg = shutil.which("ffmpeg")
        if not say or not ffmpeg:
            dur = write_silence(output_wav, _estimate_duration(text))
            return TTSResult(output_wav, dur)
        aiff = output_wav.with_suffix(".aiff")
        result = subprocess.run(
            [say, "-v", self.voice, "-o", str(aiff), text],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            dur = write_silence(output_wav, _estimate_duration(text))
            return TTSResult(output_wav, dur)
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(aiff), "-ar", "24000", "-ac", "1", str(output_wav)],
            capture_output=True, timeout=120,
        )
        aiff.unlink(missing_ok=True)
        if result.returncode != 0:
            dur = write_silence(output_wav, _estimate_duration(text))
            return TTSResult(output_wav, dur)
        return TTSResult(output_wav, _probe_duration(output_wav) or _estimate_duration(text))


class CosyVoiceTTS(TTSBackend):
    """SiliconFlow ``FunAudioLLM/CosyVoice2-0.5B`` via ``/v1/audio/speech``.

    Returns 24 kHz mono PCM WAV directly — a drop-in for the FFmpeg pipeline.
    Supports an optional emotion/prosody instruction prepended before the
    ``<|endofprompt|>`` marker. No GPU, no local model.
    """

    name = "cosyvoice"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "FunAudioLLM/CosyVoice2-0.5B",
        voice: str = "FunAudioLLM/CosyVoice2-0.5B:alex",
        instruction: str = "",
        timeout_s: float = 90.0,
    ):
        if not api_key:
            raise ValueError("CosyVoiceTTS requires an API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.instruction = instruction
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            trust_env=False,
        )

    def synthesize(self, text: str, output_wav: Path) -> TTSResult:
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        if self.instruction:
            payload_text = f"{self.instruction}<|endofprompt|>{text}"
        else:
            payload_text = text
        try:
            response = self._http.post(
                f"{self.base_url}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": payload_text,
                    "voice": self.voice,
                    "response_format": "wav",
                    "stream": False,
                },
            )
        except httpx.HTTPError:
            dur = write_silence(output_wav, _estimate_duration(text))
            return TTSResult(output_wav, dur)
        if response.status_code != 200:
            dur = write_silence(output_wav, _estimate_duration(text))
            return TTSResult(output_wav, dur)
        output_wav.write_bytes(response.content)
        return TTSResult(output_wav, _probe_duration(output_wav) or _estimate_duration(text))

    def close(self) -> None:
        self._http.close()


def make_tts_backend(
    *,
    backend: str,
    siliconflow_api_key: str = "",
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
    voice: str = "Tingting",
    cosyvoice_voice: str = "FunAudioLLM/CosyVoice2-0.5B:alex",
    tts_instruction: str = "",
) -> TTSBackend:
    """Construct the requested TTS backend.

    ``voice`` is the macOS ``say`` voice (used only by the ``say`` backend).
    ``cosyvoice_voice`` is the CosyVoice2 preset voice id.
    """
    if backend == "cosyvoice":
        return CosyVoiceTTS(
            api_key=siliconflow_api_key,
            base_url=siliconflow_base_url,
            voice=cosyvoice_voice,
            instruction=tts_instruction,
        )
    if backend == "say":
        return SayTTS(voice=voice)
    raise ValueError(f"unknown TTS backend: {backend}")
