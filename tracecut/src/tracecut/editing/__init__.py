"""tracecut.editing — the Director: a tool-calling editing agent that owns
content, B-roll and render decisions for the whole pipeline.

Replaces the old embedded "rubric soft-edit iteration": once the planner emits
a skeleton VideoPlan and the reviewer has audited the evidence graph, the
rubric (`tracecut.rubric`, scoring-only) hands low-scoring dimensions to this
agent, which revises over multiple turns — calling structured tools whose
results it can see — rather than guessing once.

Three decision families (all guarded so the provenance graph cannot drift):

- Content / structure: edit_scene, edit_title_hook, reorder_scenes, and
  add_scene / remove_scene (the latter may only cite claims/evidence already
  present in the plan — the agent restructures, it never extends the factual
  base). Content edits run through rubric.check_soft_edit_invariants.
- B-roll (subsumes the old fixed --vl-loop): search_broll / attach_broll /
  rerank_broll (VL-rerank) / get_scene_media. Registered only when a Pexels
  source is wired; rerank only when a VL provider is available. The pipeline's
  deterministic `_attach_media` runs after the agent and fills scenes the
  agent skipped (deduped by material_id).
- Render polish: set_transition, emphasis captions, BGM, SFX. Asset tools are
  registered only when their asset dirs are configured — a capability the
  model cannot see is one it cannot hallucinate.

Agent failure must never block the pipeline. Degradation ladder: tool loop →
single-shot JSON replayed through the same guards → empty EditPlan (no-op).

Modules:
  plan.py  — EditPlan models + asset whitelist (no schemas import — schemas
             imports EditPlan from here)
  tools.py — tool JSON schemas + guarded dispatch (content/asset/render)
  loop.py  — run_editing_agent(): multi-turn tool loop + degradation ladder
"""

from tracecut.editing.plan import (
    BgmSpec,
    EditPlan,
    EmphasisCaption,
    SfxSpec,
    TransitionSpec,
    available_tracks,
)

__all__ = [
    "BgmSpec",
    "EditPlan",
    "EmphasisCaption",
    "SfxSpec",
    "TransitionSpec",
    "available_tracks",
    "run_editing_agent",
]


def __getattr__(name: str):
    # Lazy: loop.py imports schemas.py, which imports editing.plan — a direct
    # top-level import here would create a cycle via the package __init__.
    if name == "run_editing_agent":
        from tracecut.editing.loop import run_editing_agent

        return run_editing_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
