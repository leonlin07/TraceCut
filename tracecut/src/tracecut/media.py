from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from tracecut.schemas import Scene, VideoPlan
from tracecut.tts import TTSBackend, write_silence

if TYPE_CHECKING:
    from tracecut.providers.llm import LLMProvider


# A CJK-capable font is mandatory — drawtext (libfreetype) and libass both fall
# back to a Latin-only font when no CJK face is named, rendering every Chinese
# glyph as a box. Probe a few well-known locations across macOS/Linux/Windows so
# the title card and subtitles work out of the box. Returns (fontfile, family_name).
_CJK_FONT_CANDIDATES = [
    # (fontfile path, family name for libass force_style)
    ("/System/Library/Fonts/STHeiti Medium.ttc", "Heiti SC"),
    ("/System/Library/Fonts/PingFang.ttc", "PingFang SC"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
    ("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc", "WenQuanYi Zen Hei"),
    ("C:/Windows/Fonts/msyh.ttc", "Microsoft YaHei"),
    ("C:/Windows/Fonts/simhei.ttf", "SimHei"),
    ("C:/Windows/Fonts/simsun.ttc", "SimSun"),
]


# BGR hex for ASS: orange = #FFB020 -> BGR = 0x20B0FF
_ORANGE_BGR = "&H20B0FF&"
_WHITE_BGR = "&HFFFFFF&"
_BLACK_BGR = "&H000000&"

# The ASS script resolution matches the 720x1280 canvas 1:1, so FontSize is in
# output pixels. ~44px reads comfortably on a 9:16 short (12 CJK chars/line =>
# ~528px < 720px). The previous value of 10 rendered ~10px glyphs: unreadable.
_SUB_FONT_SIZE = 44


def find_cjk_font() -> tuple[str, str]:
    for fontfile, family in _CJK_FONT_CANDIDATES:
        if Path(fontfile).exists():
            return fontfile, family
    # Last resort: let drawtext/fontconfig pick (may garble CJK, but won't crash).
    return "", "Sans"


def find_ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def find_ffprobe() -> str:
    return shutil.which("ffprobe") or ""


def probe_duration(path: Path) -> float:
    ffprobe = find_ffprobe()
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0:
            try:
                return float(result.stdout.strip())
            except ValueError:
                pass
    return 0.0


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


_CAPTION_MAX_CHARS = 26   # total chars per caption block (across up to 2 lines)
_CAPTION_LINE_CHARS = 13  # soft target width for one visual line


def _wrap_block(block: str) -> str:
    """Wrap a block into at most two balanced lines joined by ASS ``\\N``.

    Prefer a punctuation break close to the middle so a line never ends on a
    dangling single character or an awkwardly isolated clause.
    """
    if len(block) <= _CAPTION_LINE_CHARS:
        return block
    mid = len(block) // 2
    # Prefer punctuation (strong), then a space (weak), closest to the middle so
    # mixed Chinese/English text ("Moonshot AI") breaks at a word boundary instead
    # of tearing a Latin word in half.
    best = mid
    best_score: tuple[int, int] | None = None
    for i, ch in enumerate(block):
        if not (2 <= i <= len(block) - 2):
            continue
        if ch in "，。！？；：、":
            score = (0, abs(i - mid))
        elif ch == " ":
            score = (1, abs(i - mid))
        else:
            continue
        if best_score is None or score < best_score:
            best, best_score = i, score
    if block[best] in "，。！？；：、":
        # keep the punctuation with the first line
        return block[:best + 1] + "\\N" + block[best + 1:]
    if block[best] == " ":
        # drop the space
        return block[:best] + "\\N" + block[best + 1:]
    return block[:best] + "\\N" + block[best:]


def split_caption(text: str, max_chars: int = _CAPTION_MAX_CHARS) -> list[str]:
    """Split narration into caption blocks that read as complete clauses.

    - Segment first at sentence punctuation, then pack short clauses into blocks
      up to ``max_chars`` (instead of the old 12-char fragments that left orphan
      trailing words on their own caption).
    - A block wider than one line is wrapped into two balanced lines via an ASS
      ``\\N``, so long sentences display on two lines rather than being cut apart.
    - Only hard-wrap when a single clause exceeds ``max_chars``, and break at a
      space so Latin words stay whole.
    """
    segs = [s.strip() for s in re.split(r"(?<=[，。！？；])", text) if s.strip()]
    if not segs:
        segs = [text.strip()] if text.strip() else []

    units: list[str] = []
    for seg in segs:
        while len(seg) > max_chars:
            cut = seg.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars  # no space — hard-wrap (CJK has no spaces anyway)
            units.append(seg[:cut].strip())
            seg = seg[cut:].strip()
        if seg:
            units.append(seg)

    blocks: list[str] = []
    buf = ""
    for unit in units:
        cand = (buf + unit) if buf else unit
        if len(cand) <= max_chars:
            buf = cand
        else:
            if buf:
                blocks.append(buf)
            buf = unit
    if buf:
        blocks.append(buf)

    return [_wrap_block(b) for b in blocks]


def _xml_escape(text: str) -> str:
    """Escape a string for ASS event text."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _ass_time(seconds: float) -> str:
    cs = max(0, seconds)
    h = int(cs // 3600)
    m = int((cs % 3600) // 60)
    s = int(cs % 60)
    ms = int(round((cs % 1) * 100))
    return f"{h}:{m:02d}:{s:02d}.{ms:02d}"


def _highlight_keywords(line: str, keywords: list[str]) -> str:
    """Wrap the first matching keyword in an ASS override for orange + larger size."""
    text = _xml_escape(line)
    if not keywords:
        return text
    # Prefer longer keywords; only highlight the first match in the line.
    for kw in sorted(keywords, key=len, reverse=True):
        kw = str(kw).strip()
        if 1 < len(kw) <= 10 and kw in text:
            return text.replace(
                kw,
                f"{{\\c&H20B0FF&\\fs{_SUB_FONT_SIZE + 4}}}{kw}"
                f"{{\\c&HFFFFFF&\\fs{_SUB_FONT_SIZE}}}",
                1,
            )
    return text


def write_ass(
    plan: VideoPlan,
    scene_durations: dict[str, float],
    output: Path,
    *,
    time_offset: float = 0.0,
) -> None:
    """Write an ASS subtitle file with keyword highlighting.

    PlayResX/PlayResY matches the output canvas 1:1, so the style FontSize is in
    output pixels (see ``_SUB_FONT_SIZE``), and the bottom margin keeps captions
    clear of the platform UI safe area.
    """
    font_name = find_cjk_font()[1]
    cursor = time_offset
    events: list[str] = []

    for scene in plan.scenes:
        duration = scene_durations[scene.scene_id]
        captions = split_caption(scene.narration)
        # Strip weak punctuation for cleaner on-screen captions.
        captions = [re.sub(r"[，、；]$", "", c) for c in captions]
        weight = sum(len(c.replace("\\N", "")) for c in captions) or 1
        local = cursor
        for caption in captions:
            visible_len = len(caption.replace("\\N", ""))
            span = max(0.6, duration * visible_len / weight)
            end = min(cursor + duration, local + span)
            # Highlight per visual line so the ASS \N line break survives escaping.
            lines = caption.split("\\N")
            text = "\\N".join(_highlight_keywords(ln, scene.keywords or []) for ln in lines)
            events.append(
                f"Dialogue: 0,{_ass_time(local)},{_ass_time(end)},Default,,0,0,0,,{text}"
            )
            local = end
        cursor += duration

    header = f"""[Script Info]
Title: TraceCut ASS
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{_SUB_FONT_SIZE},{_WHITE_BGR},{_WHITE_BGR},{_BLACK_BGR},{_BLACK_BGR},1,0,0,0,100,100,0,0,1,3,1,2,20,20,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    output.write_text(header + "\n".join(events), encoding="utf-8")


def escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(" ", "\\ ")


def _safe_drawtext(text: str) -> str:
    """Escape a string for use inside a drawtext text='...' argument."""
    cleaned = re.sub(r"[^\w一-鿿 \-，。！？、：；]", "", text)
    return cleaned.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def _wrap_lines(text: str, max_lines: int = 2) -> list[str]:
    """Split text into at most ``max_lines`` balanced lines (CJK-friendly)."""
    text = text.strip()
    if not text:
        return [""]
    per = max(1, math.ceil(len(text) / max_lines))
    return [text[i:i + per] for i in range(0, len(text), per)]


def _fit_fontsize(
    lines: list[str],
    base: int,
    width: int,
    *,
    ratio: float = 0.9,
    min_size: int = 22,
) -> int:
    """Shrink ``base`` until the longest line fits within ``ratio`` of the width.

    CJK glyphs are ~1em wide, so ``len(line) * fontsize`` is a good (slightly
    conservative) width estimate; without this the title text ran off the canvas.
    """
    longest = max((len(line) for line in lines), default=1)
    return max(min_size, min(base, int(width * ratio / max(1, longest))))


def _drawtext_block(
    lines: list[str],
    font_opt: str,
    fontsize: int,
    color: str,
    center_y: float,
    width: int,
    height: int,
    *,
    gap: float = 1.3,
    shadow: int = 2,
) -> list[str]:
    """Stack centered drawtext filters, vertically centered on ``center_y``."""
    line_h = int(fontsize * gap)
    top = int(height * center_y - line_h * len(lines) / 2)
    return [
        f"drawtext={font_opt}text='{line}':fontcolor={color}:fontsize={fontsize}:"
        f"x=(w-text_w)/2:y={top + i * line_h}:"
        f"shadowx={shadow}:shadowy={shadow}:shadowcolor=black@0.6"
        for i, line in enumerate(lines)
    ]


def _zoompan_filter(width: int, height: int, *, zoom_start: float = 1.0, zoom_end: float = 1.08, duration_s: float = 3.0) -> str:
    """Build a slow Ken Burns zoompan filter string."""
    # zoompan uses frame count d; fps=30
    d = max(1, int(round(duration_s * 30)))
    return (
        f"zoompan=z='min({zoom_start}+({zoom_end}-{zoom_start})*on/{d},{zoom_end})':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={d}:s={width}x{height}:fps=30"
    )


def _first_material_path(plan: VideoPlan) -> Path | None:
    for scene in plan.scenes:
        if scene.material and scene.material.local_path:
            p = Path(scene.material.local_path)
            if p.exists():
                return p
    return None


def render_title_clip(
    plan: VideoPlan,
    output: Path,
    duration: float,
    width: int,
    height: int,
) -> None:
    """A Douyin-style opening title card: dynamic zoom, large title, hook subtitle,
    with a darkened background from the first available B-roll material if possible.
    """
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    fontfile, _ = find_cjk_font()
    ff = escape_filter_path(Path(fontfile)) if fontfile else ""
    font_opt = f"fontfile='{ff}':" if ff else ""

    # Wrap + auto-fit: a 14-char title at 68px is ~950px wide and used to run
    # off the 720px canvas. Now the font shrinks to fit and long text wraps.
    title_lines = _wrap_lines(_safe_drawtext((plan.hook_title or plan.title)[:14]), 2)
    hook_lines = _wrap_lines(_safe_drawtext(plan.hook[:34]), 2)
    zoom = _zoompan_filter(width, height, duration_s=duration)
    vignette = "vignette=PI/4.5"

    material_path = _first_material_path(plan)
    if material_path and material_path.exists():
        # Use first material frame as background, darkened + vignette + zoompan.
        text_chain = ",".join(
            _drawtext_block(title_lines, font_opt, _fit_fontsize(title_lines, 68, width),
                            "white", 0.36, width, height, shadow=3)
            + _drawtext_block(hook_lines, font_opt, _fit_fontsize(hook_lines, 32, width),
                              "0xd0e0ff", 0.54, width, height)
        )
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"eq=brightness=-0.12:saturation=0.85,{zoom},{vignette},{text_chain}"
        )
        cmd = [ffmpeg, "-y", "-stream_loop", "-1", "-i", str(material_path),
               "-t", f"{duration:.3f}", "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
               "-pix_fmt", "yuv420p", str(output)]
    else:
        bg = f"color=c=0x0b1224:s={width}x{height}:r=30:d={duration:.3f}"
        text_chain = ",".join(
            _drawtext_block(title_lines, font_opt, _fit_fontsize(title_lines, 68, width),
                            "white", 0.38, width, height, shadow=3)
            + _drawtext_block(hook_lines, font_opt, _fit_fontsize(hook_lines, 30, width),
                              "0x9fb4d0", 0.55, width, height)
        )
        vf = f"{zoom},{vignette},{text_chain}"
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", bg, "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
               "-pix_fmt", "yuv420p", str(output)]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-800:])


def render_cta_clip(
    plan: VideoPlan,
    output: Path,
    duration: float,
    width: int,
    height: int,
) -> None:
    """A 2.5s end CTA card: gradient background + large CTA + topic hashtags."""
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    fontfile, _ = find_cjk_font()
    ff = escape_filter_path(Path(fontfile)) if fontfile else ""
    font_opt = f"fontfile='{ff}':" if ff else ""

    cta_lines = _wrap_lines(_safe_drawtext((plan.cta or f"关注我，3 分钟看懂 {plan.topic}")[:30]), 2)
    tags = " ".join(plan.hashtags[:5]) or f"#{plan.topic[:6]}"
    tag_lines = _wrap_lines(_safe_drawtext(tags[:40]), 2)

    bg = f"color=c=0x0f1622:s={width}x{height}:r=30:d={duration:.3f}"
    text_chain = ",".join(
        _drawtext_block(cta_lines, font_opt, _fit_fontsize(cta_lines, 54, width),
                        "white", 0.40, width, height, shadow=3)
        + _drawtext_block(tag_lines, font_opt, _fit_fontsize(tag_lines, 30, width),
                          "0x20B0FF", 0.56, width, height)
    )
    vf = (
        f"drawbox=x=0:y=0:w={width}:h={height}:color=0x1a2542@0.9:t=fill,"
        f"{text_chain}"
    )
    cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", bg, "-t", f"{duration:.3f}",
           "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "22", "-pix_fmt", "yuv420p", str(output)]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-800:])


def render_scene_clip(
    scene: Scene,
    output: Path,
    duration: float,
    width: int,
    height: int,
    *,
    badge: str | None = None,
) -> None:
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    fontfile, _ = find_cjk_font()
    ff = escape_filter_path(Path(fontfile)) if fontfile else ""
    font_opt = f"fontfile='{ff}':" if ff else ""
    material_path = Path(scene.material.local_path) if scene.material and scene.material.local_path else None
    if material_path and material_path.exists():
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30"
        )
        if badge:
            # Small "示意图" tag in the top-right so generic stock footage is not
            # mistaken for the real scientific image it is illustrating.
            vf += (
                f",drawtext={font_opt}text='{badge}':fontcolor=white@0.9:fontsize=26:"
                f"box=1:boxcolor=black@0.5:boxborderw=10:x=w-text_w-20:y=20"
            )
        cmd = [ffmpeg, "-y", "-stream_loop", "-1", "-i", str(material_path), "-t", f"{duration:.3f}", "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", str(output)]
    else:
        text_lines = _wrap_lines(_safe_drawtext(scene.narration[:22]), 2)
        fontfile, _ = find_cjk_font()
        ff = escape_filter_path(Path(fontfile)) if fontfile else ""
        font_opt = f"fontfile='{ff}':" if ff else ""
        fs = _fit_fontsize(text_lines, 42, width)
        line_h = int(fs * 1.3)
        top = int(height / 2 - line_h * len(text_lines) / 2)
        draw = ",".join(
            f"drawtext={font_opt}text='{line}':fontcolor=white:fontsize={fs}:"
            f"x=(w-text_w)/2:y={top + i * line_h}:"
            f"box=1:boxcolor=black@0.45:boxborderw=24"
            for i, line in enumerate(text_lines)
        )
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c=0x172033:s={width}x{height}:r=30:d={duration:.3f}", "-vf", draw, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-800:])


def _xfade_concat(clips: list[Path], output: Path, transition_dur: float = 0.5) -> None:
    """Concatenate video clips with xfade transitions (fade).

    The offset for the i-th transition is the cumulative duration of all previous
    clips minus the total overlap introduced by earlier transitions, so the
    fade starts exactly at the boundary between clips.
    """
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    if len(clips) < 2:
        # Single clip: just copy.
        subprocess.run([ffmpeg, "-y", "-i", str(clips[0]), "-c", "copy", str(output)],
                         check=True, capture_output=True, timeout=300)
        return

    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]

    n = len(clips)
    chain_parts = []
    cumulative = 0.0
    for i in range(1, n):
        cumulative += _probe_duration(clips[i - 1])
        offset = cumulative - i * transition_dur
        label_in = "0:v" if i == 1 else f"v{i - 1}"
        label_out = f"v{i}"
        chain_parts.append(
            f"[{label_in}][{i}:v]xfade=transition=fade:duration={transition_dur:.3f}:"
            f"offset={offset:.3f}[{label_out}]"
        )
    last_label = f"v{n - 1}"

    fc = ";".join(chain_parts)
    subprocess.run([ffmpeg, "-y", *inputs, "-filter_complex", fc, "-map", f"[{last_label}]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-pix_fmt", "yuv420p", str(output)],
                   check=True, capture_output=True, timeout=600)


def _concat_demuxer(clips: list[Path], output: Path) -> None:
    """Fallback hard-cut concat using the concat demuxer."""
    ffmpeg = find_ffmpeg()
    concat_list = output.with_suffix(".concat.txt")
    concat_list.write_text("\n".join(f"file '{path.as_posix()}'" for path in clips), encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-c", "copy", str(output)],
                   check=True, capture_output=True, timeout=300)


def generate_cover(
    plan: VideoPlan,
    output: Path,
    *,
    width: int = 720,
    height: int = 1280,
) -> Path:
    """Generate a cover image: first material frame with large hook_title overlay."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for cover generation")
    fontfile, _ = find_cjk_font()
    ff = escape_filter_path(Path(fontfile)) if fontfile else ""
    font_opt = f"fontfile='{ff}':" if ff else ""

    material_path = _first_material_path(plan)
    hook_line = _safe_drawtext((plan.hook_title or plan.title or plan.topic)[:12])
    esc = lambda s: s.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    cover_fs = _fit_fontsize([hook_line], 64, width)

    if material_path and material_path.exists():
        probe = probe_duration(material_path)
        ss = max(0.0, probe / 3) if probe > 1 else 0.0
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"eq=brightness=-0.08,"
            f"drawbox=x=0:y={int(height * 0.52)}:w={width}:h={int(height * 0.2)}:color=black@0.35:t=fill,"
            f"drawtext={font_opt}text='{esc(hook_line)}':fontcolor=white:fontsize={cover_fs}:"
            f"x=(w-text_w)/2:y={int(height * 0.58)}:shadowx=2:shadowy=2:shadowcolor=black@0.7"
        )
        cmd = [ffmpeg, "-y", "-ss", f"{ss:.3f}", "-i", str(material_path),
               "-frames:v", "1", "-q:v", "2", "-vf", vf, str(output)]
    else:
        bg = f"color=c=0x0b1224:s={width}x{height}:d=1"
        vf = (
            f"drawtext={font_opt}text='{esc(hook_line)}':fontcolor=white:fontsize={cover_fs}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:shadowx=2:shadowy=2:shadowcolor=black@0.7"
        )
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", bg, "-frames:v", "1", "-q:v", "2",
               "-vf", vf, str(output)]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-800:])
    return output


def _pad_audio_to(src: Path, dst: Path, duration: float) -> Path:
    """Return a 24kHz mono WAV of exactly `duration` seconds."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return src
    if src.resolve() == dst.resolve():
        dst = dst.with_suffix(".padded.wav")
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-t", f"{duration:.3f}",
         "-af", f"apad=whole_dur={duration:.3f}", "-ar", "24000", "-ac", "1",
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, timeout=120,
    )
    return dst if result.returncode == 0 else src


def render_video(
    plan: VideoPlan,
    output: Path,
    work_dir: Path,
    *,
    tts: TTSBackend,
    width: int = 720,
    height: int = 1280,
    bgm_dir: Path | None = None,
    bgm_llm: "LLMProvider | None" = None,
    with_transitions: bool = True,
    generate_cover_flag: bool = True,
    misleading_scene_ids: set[str] | None = None,
) -> tuple[Path, Path, dict[str, float], Path]:
    """Render a Douyin-ready vertical video.

    Returns (video_path, subtitle_path, scene_durations, cover_path).
    """
    from tracecut.bgm import attach_bgm  # avoid circular import at module load

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for rendering")
    work_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    audio_files: list[Path] = []
    durations: dict[str, float] = {}

    SCENE_TAIL_S = 0.4

    # Opening title card — read the title/hook aloud so the video starts with a
    # spoken hook instead of silence.
    title_text = plan.hook_title or plan.title or plan.topic
    title_tts = tts.synthesize(title_text, work_dir / "title_raw.wav")
    title_dur = max(2.6, title_tts.duration_s + SCENE_TAIL_S)
    title_clip = work_dir / "title.mp4"
    render_title_clip(plan, title_clip, title_dur, width, height)
    clips.append(title_clip)
    title_audio = _pad_audio_to(title_tts.wav_path, work_dir / "title.wav", title_dur)
    audio_files.append(title_audio)
    title_offset = title_dur

    # Scene clips
    for scene in plan.scenes:
        audio = work_dir / f"{scene.scene_id}.wav"
        result = tts.synthesize(scene.narration, audio)
        duration = max(1.0, result.duration_s) + SCENE_TAIL_S
        padded = _pad_audio_to(result.wav_path, audio, duration)
        durations[scene.scene_id] = duration
        audio_files.append(padded)
        clip = work_dir / f"{scene.scene_id}.mp4"
        badge = "示意图" if (misleading_scene_ids and scene.scene_id in misleading_scene_ids) else None
        render_scene_clip(scene, clip, duration, width, height, badge=badge)
        clips.append(clip)

    # CTA outro card
    cta_dur = 2.5
    cta_clip = work_dir / "cta.mp4"
    render_cta_clip(plan, cta_clip, cta_dur, width, height)
    clips.append(cta_clip)
    cta_silence = work_dir / "cta.wav"
    write_silence(cta_silence, cta_dur)
    audio_files.append(cta_silence)

    # Concat video
    subtitle_path = output.with_suffix(".ass")
    write_ass(plan, durations, subtitle_path, time_offset=title_offset)
    raw_video = work_dir / "raw.mp4"
    try:
        _xfade_concat(clips, raw_video, transition_dur=0.5)
    except Exception:
        # Fallback to hard-cut concat if xfade fails (older FFmpeg / edge cases).
        _concat_demuxer(clips, raw_video)

    # Concat audio
    voice_track = work_dir / "voice.wav"
    audio_concat = work_dir / "audio_concat.txt"
    audio_concat.write_text("\n".join(f"file '{path.as_posix()}'" for path in audio_files), encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(audio_concat),
                    "-c:a", "pcm_s16le", str(voice_track)],
                   check=True, capture_output=True, timeout=300)

    output.parent.mkdir(parents=True, exist_ok=True)
    _, cjk_family = find_cjk_font()
    sub_style = (
        f"FontName={cjk_family},FontSize={_SUB_FONT_SIZE},Bold=1,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H80000000,BorderStyle=1,Outline=3,Shadow=1,"
        "Alignment=2,MarginL=20,MarginR=20,MarginV=110"
    )
    # The subtitles filter arg is parsed twice; on Windows an absolute path's
    # drive-letter colon is eaten by the second pass (read as original_size) and
    # the filter fails. Solve it by giving ffmpeg a bare filename and running it
    # with cwd=work_dir, so keep a copy of the .ass inside work_dir.
    work_ass = work_dir / "subs.ass"
    work_ass.write_text(subtitle_path.read_text(encoding="utf-8"), encoding="utf-8")
    vf = f"subtitles={work_ass.name}:force_style='{sub_style}'"

    # Initial mux with subtitles (bare relative filenames + cwd=work_dir).
    muxed = work_dir / "muxed.mp4"
    mux_result = subprocess.run(
        [ffmpeg, "-y", "-i", raw_video.name, "-i", voice_track.name, "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", muxed.name],
        capture_output=True, timeout=600, cwd=str(work_dir),
    )
    if mux_result.returncode != 0:
        raise RuntimeError(
            "subtitle mux failed:\n"
            + mux_result.stderr.decode("utf-8", errors="replace")[-1000:]
        )

    # Add BGM with ducking
    total_dur = title_dur + sum(durations.values()) + cta_dur
    mixed_audio, _bgm_spec = attach_bgm(
        voice_track, total_dur, work_dir,
        topic=plan.topic, bgm_dir=bgm_dir, llm=bgm_llm,
    )
    # Remux final with BGM
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(muxed), "-i", str(mixed_audio),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-shortest", "-movflags", "+faststart", str(output)],
        capture_output=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1000:])

    # Cover
    cover_path = output.with_name("cover.jpg")
    if generate_cover_flag:
        try:
            generate_cover(plan, cover_path, width=width, height=height)
        except Exception:
            # Non-fatal: cover is optional.
            cover_path = Path("")
    else:
        cover_path = Path("")

    return output, subtitle_path, durations, cover_path
