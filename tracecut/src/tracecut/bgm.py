"""BGM 处理：本地 CC0 曲库选择 / 程序化生成 + 自动闪避混音.

``attach_bgm`` 是主入口，接受一条已合成的旁白 WAV，返回混合后的 WAV。
默认使用程序化和弦垫乐（零外部素材依赖），当 ``TRACECUT_BGM_DIR`` 指向
一个包含 .mp3/.wav 的目录时，会优先从该目录选择曲目并循环/裁切到目标时长。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracecut.providers.llm import LLMProvider


@dataclass
class BgmSpec:
    """BGM 配置摘要，写入 manifest 方便追溯."""

    source: str  # "procedural" | "local:<filename>"
    reason: str = ""
    path: str = ""
    looped: bool = True


# 程序化和弦：C - G - Am - F，每个和弦 4s，组成 16s 循环单元
_CHORDS = [
    [130.81, 164.81, 196.00, 261.63],   # C
    [98.00, 146.83, 196.00, 246.94],    # G
    [110.00, 164.81, 220.00, 261.63],   # Am
    [87.31, 130.81, 174.61, 220.00],    # F
]


def _find_ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-800:])


# BGM sits under narration, so aim its peak at -6 dBFS: clearly audible in the
# pauses yet not fighting the voice. Generated pads used to land near -28 dBFS
# (≈20 dB below the voice) and were practically inaudible.
_BGM_TARGET_PEAK_DB = -6.0


def _measure_max_volume_db(path: Path) -> float:
    """Return the file's max_volume in dBFS, or 0.0 when it cannot be measured."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return 0.0
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", os.devnull],
        capture_output=True, text=True, timeout=180,
    )
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", result.stderr)
    return float(match.group(1)) if match else 0.0


def _normalize_peak(src: Path, dst: Path, *, target_db: float = _BGM_TARGET_PEAK_DB,
                    max_gain_db: float = 26.0) -> Path:
    """Gain-stage ``src`` to ``target_db`` peak, writing 24kHz mono PCM to ``dst``.

    Gain is derived from an actual volumedetect measurement of the file, so a
    quiet synth pad and a loud licensed track both end up at the same level.
    """
    peak = _measure_max_volume_db(src)
    if peak >= target_db or peak == 0.0:
        # Already loud enough, or unmeasurable: pass through unchanged.
        return src
    gain_db = min(target_db - peak, max_gain_db)
    _run([*_base_cmd(), "-i", str(src), "-af", f"volume={gain_db:.2f}dB",
          "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    return dst


def _base_cmd() -> list[str]:
    return [_find_ffmpeg(), "-y", "-hide_banner"]


def list_local_bgm(bgm_dir: Path | None) -> list[Path]:
    if not bgm_dir or not bgm_dir.is_dir():
        return []
    exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
    return sorted([p for p in bgm_dir.iterdir() if p.is_file() and p.suffix.lower() in exts])


def select_bgm(
    topic: str,
    bgm_dir: Path | None,
    llm: "LLMProvider | None" = None,
) -> tuple[Path | None, str]:
    """在本地 BGM 目录中选择一首最适合话题的曲子.

    如果没有 LLM 或目录为空，则随机选择并返回空 reason。
    返回 (文件路径, 选择理由)。
    """
    files = list_local_bgm(bgm_dir)
    if not files:
        return None, "no local BGM files; using procedural BGM"
    if len(files) == 1:
        return files[0], "only local BGM available"
    if llm is None:
        return random.choice(files), "randomly selected"

    file_list = [f.name for f in files]
    prompt = (
        f"Topic: {topic}\n"
        f"Available BGM files: {file_list}\n"
        'Return JSON: {"bgm": "exact filename", "reason": "short Chinese reason"}'
    )
    try:
        raw = llm.complete_json(
            system="You pick a background-music file for a short video. Return ONLY JSON.",
            user=prompt,
            max_tokens=120,
        )
        chosen = str(raw.get("bgm", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        match = next((f for f in files if f.name == chosen), None)
        if match:
            return match, reason or "LLM selected"
    except Exception:
        pass
    return random.choice(files), "LLM selection failed; randomly selected"


def gen_procedural_bgm(total_dur: float, out_wav: Path, work_dir: Path) -> Path:
    """用 FFmpeg lavfi sine 源合成柔和和弦垫乐，零外部素材依赖."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    chord_files: list[Path] = []
    for i, freqs in enumerate(_CHORDS):
        cpath = work_dir / f"bgm_chord_{i}.wav"
        inputs: list[str] = []
        chains: list[str] = []
        for j, f in enumerate(freqs):
            inputs += ["-f", "lavfi", "-t", "4", "-i", f"sine=frequency={f}:sample_rate=24000"]
            chains.append(
                f"[{j}:a]volume=0.45,afade=t=in:st=0:d=0.8,afade=t=out:st=3.2:d=0.8[a{j}]"
            )
        mix_in = "".join(f"[a{j}]" for j in range(len(freqs)))
        # NB: no `normalize=0` on amix — FFmpeg 4.2 (shipped with some static
        # Windows builds) predates that option. Per-chord level is compensated
        # by the explicit volume= below, and the final _normalize_peak() call
        # pins the pad to the target level anyway.
        fc = (
            ";".join(chains)
            + f";{mix_in}amix=inputs={len(freqs)}:duration=longest,volume={len(freqs)},"
            f"aresample=24000[a]"
        )
        _run(["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", "[a]",
              "-c:a", "pcm_s16le", str(cpath)])
        chord_files.append(cpath)

    # 拼接成 16s 循环单元
    loop_unit = work_dir / "bgm_loop16.wav"
    lst = work_dir / "bgm_chords.txt"
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in chord_files), encoding="utf-8")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-c:a", "pcm_s16le", str(loop_unit)])

    # 循环铺满目标时长并柔化，再按实测峰值归一到目标电平
    looped = work_dir / "bgm_pad_raw.wav"
    _run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(loop_unit),
          "-t", f"{total_dur:.3f}", "-af",
          "lowpass=f=900,aecho=0.7:0.7:50|90:0.35|0.25",
          "-c:a", "pcm_s16le", str(looped)])
    return _normalize_peak(looped, out_wav)


def _prepare_bgm_track(
    src: Path,
    total_dur: float,
    out_wav: Path,
    work_dir: Path,
) -> Path:
    """把本地 BGM 统一成 24kHz mono WAV，并循环/裁切到 total_dur，最后归一电平."""
    probe = _probe_duration(src)
    loop = probe <= 0 or probe < total_dur
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if loop:
        cmd += ["-stream_loop", "-1"]
    # `-ac 1` downmixes stereo and passes mono through untouched; the old
    # `pan=mono|c0=0.5*c0+0.5*c1` crashed on mono sources (no c1 channel).
    raw = work_dir / "bgm_local_raw.wav"
    cmd += [
        "-t", f"{total_dur:.3f}",
        "-af", "aresample=24000",
        "-ac", "1",
        "-c:a", "pcm_s16le", str(raw),
    ]
    _run(cmd)
    return _normalize_peak(raw, out_wav)


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe:
        return 0.0
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


def mix_with_duck(voice_wav: Path, bgm_wav: Path, out_wav: Path) -> Path:
    """以旁白为 sidechain 自动闪避 BGM 后混合.

    参数经过调优：threshold=0.05, ratio=4, attack=20ms, release=350ms。
    旁白存在时 BGM 适度压低，停顿处迅速回升，听感上 BGM 始终存在。
    """
    fmt = "aformat=sample_fmts=s16:sample_rates=24000:channel_layouts=mono"
    _run(["ffmpeg", "-y",
          "-i", str(bgm_wav), "-i", str(voice_wav),
          "-filter_complex",
          # BGM enters at 0.9 and ducks by a gentler 4:1 under the voice, so the
          # music stays present between sentences instead of vanishing entirely.
          f"[0:a]volume=0.9,{fmt}[bg];"
          f"[1:a]{fmt}[v];"
          "[v]asplit=2[sc][main];"
          "[bg][sc]sidechaincompress=threshold=0.05:ratio=4:attack=20:release=350[bgd];"
          "[main][bgd]amix=inputs=2:duration=longest,volume=2[a]",
          "-map", "[a]", "-c:a", "pcm_s16le", str(out_wav)])
    return out_wav


def attach_bgm(
    voice_wav: Path,
    total_dur: float,
    work_dir: Path,
    *,
    topic: str = "",
    bgm_dir: Path | None = None,
    llm: "LLMProvider | None" = None,
) -> tuple[Path, BgmSpec]:
    """为旁白轨附加 BGM 并返回混合音频 + BGM 元信息.

    优先级：本地 BGM 目录 > 程序化和弦垫乐。
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for BGM mixing")

    local_file, reason = select_bgm(topic or "short video", bgm_dir, llm)
    bgm_wav = work_dir / "bgm.wav"

    if local_file:
        _prepare_bgm_track(local_file, total_dur, bgm_wav, work_dir)
        spec = BgmSpec(
            source=f"local:{local_file.name}",
            reason=reason,
            path=str(local_file),
            looped=True,
        )
    else:
        gen_procedural_bgm(total_dur, bgm_wav, work_dir)
        spec = BgmSpec(
            source="procedural",
            reason=reason or "programmatic chord pad (no local BGM files)",
            path=str(bgm_wav),
            looped=True,
        )

    mixed_wav = work_dir / "mixed.wav"
    mix_with_duck(voice_wav, bgm_wav, mixed_wav)
    return mixed_wav, spec


def generate_silent_wav(duration_s: float, out_wav: Path, sample_rate: int = 24000) -> Path:
    """生成一段静音 WAV，用于无旁白但保持时长的片段."""
    frames = max(1, math.ceil(duration_s * sample_rate))
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=0:sample_rate={sample_rate}:duration={duration_s:.3f}",
        "-c:a", "pcm_s16le", str(out_wav),
    ])
    return out_wav
