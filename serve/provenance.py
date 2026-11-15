"""Provenance-graph visualization for the TraceCut demo.

The TraceCut differentiator is its claim → evidence → scene → material graph
(an "evidence-grounded, traceable" guarantee). This module turns a run manifest
into (a) a structured node/edge dict for programmatic consumers and (b) a
self-contained HTML table view for the Gradio UI / demo.
"""

from __future__ import annotations

import html
from typing import Any


def build_provenance_graph(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return {nodes, edges} describing the provenance graph."""
    plan = manifest.get("plan", {})
    evidence = manifest.get("evidence", [])
    claims = plan.get("claims", [])
    scenes = plan.get("scenes", [])

    nodes = {
        "claims": [
            {
                "id": c["claim_id"],
                "text": c.get("text", ""),
                "evidence_ids": c.get("evidence_ids", []),
            }
            for c in claims
        ],
        "evidences": [
            {
                "id": e["evidence_id"],
                "title": e.get("title", ""),
                "url": e.get("url", ""),
            }
            for e in evidence
        ],
        "scenes": [
            {
                "id": s["scene_id"],
                "narration": s.get("narration", ""),
                "claim_ids": s.get("claim_ids", []),
                "evidence_ids": s.get("evidence_ids", []),
                "material_source": (s.get("material") or {}).get("source_url", ""),
            }
            for s in scenes
        ],
    }
    edges = {
        "claim_to_evidence": [
            (c["claim_id"], ev) for c in claims for ev in c.get("evidence_ids", [])
        ],
        "scene_to_claim": [
            (s["scene_id"], cid) for s in scenes for cid in s.get("claim_ids", [])
        ],
        "scene_to_evidence": [
            (s["scene_id"], ev) for s in scenes for ev in s.get("evidence_ids", [])
        ],
        "scene_to_material": [
            (s["scene_id"], (s.get("material") or {}).get("material_id", ""))
            for s in scenes
            if s.get("material")
        ],
    }
    return {"nodes": nodes, "edges": edges}


def _esc(text: str) -> str:
    return html.escape(text or "")


def render_provenance_html(manifest: dict[str, Any]) -> str:
    """Render a self-contained HTML table: one row per claim, showing its
    evidence, the scenes that cite it, and the B-roll material attached."""
    plan = manifest.get("plan", {})
    evidence = manifest.get("evidence", [])
    claims = plan.get("claims", [])
    scenes = plan.get("scenes", [])

    ev_by_id = {e["evidence_id"]: e for e in evidence}
    scenes_by_claim: dict[str, list[dict]] = {}
    for s in scenes:
        for cid in s.get("claim_ids", []):
            scenes_by_claim.setdefault(cid, []).append(s)

    rows = []
    for c in claims:
        cid = c["claim_id"]
        ev_cells = []
        for ev in c.get("evidence_ids", []):
            doc = ev_by_id.get(ev)
            title = doc.get("title", ev) if doc else ev
            url = doc.get("url", "") if doc else ""
            if url:
                ev_cells.append(f'<a href="{_esc(url)}" target="_blank">{_esc(title)}</a>')
            else:
                ev_cells.append(_esc(title))
        scene_cells = []
        for s in scenes_by_claim.get(cid, []):
            mat = (s.get("material") or {}).get("source_url", "")
            if mat:
                scene_cells.append(
                    f"{_esc(s['scene_id'])} → <a href='{_esc(mat)}' target='_blank'>素材</a>"
                )
            else:
                scene_cells.append(f"{_esc(s['scene_id'])} (无素材)")
        rows.append(
            "<tr>"
            f"<td><code>{_esc(cid)}</code></td>"
            f"<td>{_esc(c['text'])}</td>"
            f"<td>{'<br>'.join(ev_cells) or '<em>无</em>'}</td>"
            f"<td>{'<br>'.join(scene_cells) or '<em>无</em>'}</td>"
            "</tr>"
        )

    body = "\n".join(rows) if rows else "<tr><td colspan='4'>暂无 claims</td></tr>"
    return (
        "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        "<thead><tr style='background:#1a2542;color:#fff'>"
        "<th style='padding:8px'>Claim</th><th style='padding:8px'>主张</th>"
        "<th style='padding:8px'>证据</th><th style='padding:8px'>分镜/素材</th>"
        "</tr></thead><tbody>"
        + body
        + "</tbody></table>"
    )
