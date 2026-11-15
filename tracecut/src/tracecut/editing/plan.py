"""tracecut.editing.plan — EditPlan data structures + asset whitelist.

The editing agent's render decisions land here; content edits mutate the
VideoPlan directly (see editing/tools.py). EditPlan attaches to VideoPlan as
an Optional field with default None, so existing planner prompts and stored
manifests validate unchanged.

This module must NOT import schemas.py — schemas imports EditPlan from here,
and importing schemas back would create a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

BGM_DIR_ENV = "TRACECUT_BGM_DIR"
SFX_DIR_ENV = "TRACECUT_SFX_DIR"

_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# Packaged defaults so BGM/SFX work out of the box; the env vars override with
# a user-supplied library.
_PACKAGED_ASSETS = Path(__file__).resolve().parent.parent / "assets"


def asset_dir(env_var: str) -> Path | None:
    """Resolve the asset dir for an env var.

    Rules:
    - env var SET (even to "")  → use it as the override; never fall through to
      the packaged default. This keeps tests/CI able to say "no audio library"
      while packaged defaults exist on disk.
    - env var UNSET             → fall back to the packaged default if it is a
      real directory.
    """
    if env_var in os.environ:
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            return None  # explicitly set-but-empty: caller means "disabled"
        directory = Path(raw).expanduser()
        return directory if directory.is_dir() else None
    fallback = _PACKAGED_ASSETS / ("bgm" if env_var == BGM_DIR_ENV else "sfx")
    return fallback if fallback.is_dir() else None


def available_tracks(env_var: str) -> set[str]:
    """Basenames of audio files in the resolved asset dir.

    Empty set when no dir resolves or it is empty — callers treat that as
    "this capability does not exist" and never register the tool.
    """
    directory = asset_dir(env_var)
    if directory is None:
        return set()
    return {
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_SUFFIXES
    }


def _validate_track_basename(track: str) -> str:
    """Block path traversal: a track must be a plain basename."""
    if Path(track).name != track:
        raise ValueError(f"track must be a basename, got {track!r}")
    return track


class TransitionSpec(BaseModel):
    """A real visual transition leading INTO `scene_id` (boundary between it
    and the previous scene). Duration is capped at 0.8s so the crossfade stays
    inside the 0.4s narration tail silence and voice never overlaps."""

    scene_id: str = Field(min_length=1, max_length=64)
    kind: Literal["fade", "slideleft", "slideright", "wipeleft", "circleopen"]
    duration_s: float = Field(default=0.4, ge=0.2, le=0.8)


class EmphasisCaption(BaseModel):
    """Screen-center/top emphasis text baked into one scene via drawtext.
    Timing is a fraction of the scene's own duration (scene-local, so no
    global timeline math is needed)."""

    scene_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=24)
    position: Literal["top", "center"] = "top"
    start_frac: float = Field(default=0.0, ge=0.0, le=1.0)
    end_frac: float = Field(default=1.0, ge=0.0, le=1.0)


class BgmSpec(BaseModel):
    """One background track for the whole video. `track` must be a basename
    present in TRACECUT_BGM_DIR at render time."""

    track: str = Field(min_length=1, max_length=200)
    volume: float = Field(default=0.15, ge=0.0, le=0.6)

    _basename = field_validator("track")(_validate_track_basename)


class SfxSpec(BaseModel):
    """One-shot sound effect inside a scene. offset_s is relative to the
    scene's own start; media.py converts to absolute time via
    compute_scene_offsets."""

    scene_id: str = Field(min_length=1, max_length=64)
    track: str = Field(min_length=1, max_length=200)
    offset_s: float = Field(default=0.0, ge=0.0, le=10.0)
    volume: float = Field(default=0.8, ge=0.0, le=1.0)

    _basename = field_validator("track")(_validate_track_basename)


class EditPlan(BaseModel):
    """The editing agent's render-layer decisions. Empty by default — the
    plain hard-cut render stays the baseline and the agent only adds polish
    on key beats."""

    transitions: list[TransitionSpec] = Field(default_factory=list)
    captions: list[EmphasisCaption] = Field(default_factory=list)
    bgm: BgmSpec | None = None
    sfx: list[SfxSpec] = Field(default_factory=list)

    def summary(self) -> str:
        """One-line digest for logs and the get_storyboard self-check tool."""
        parts = []
        if self.transitions:
            parts.append(
                "transitions: "
                + ", ".join(f"{t.scene_id}<-{t.kind}({t.duration_s}s)" for t in self.transitions)
            )
        if self.captions:
            parts.append(
                "captions: " + ", ".join(f"{c.scene_id}={c.text!r}" for c in self.captions)
            )
        if self.bgm:
            parts.append(f"bgm: {self.bgm.track}@{self.bgm.volume}")
        if self.sfx:
            parts.append(
                "sfx: " + ", ".join(f"{s.scene_id}+{s.offset_s}s={s.track}" for s in self.sfx)
            )
        return "; ".join(parts) if parts else "(empty — plain hard-cut render)"
