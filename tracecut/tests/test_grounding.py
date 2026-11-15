from tracecut.evaluate import deterministic_evaluation
from tracecut.schemas import Claim, EvidenceDoc, Scene, VideoPlan


def test_grounding_metrics_find_unsupported_claim():
    evidence = [
        EvidenceDoc(
            evidence_id="ev_1",
            title="source",
            url="https://example.com",
            excerpt="A sufficiently detailed factual source excerpt.",
        )
    ]
    plan = VideoPlan(
        topic="topic",
        title="title",
        hook="hook",
        claims=[
            Claim(claim_id="c_1", text="supported", evidence_ids=["ev_1"]),
            Claim(claim_id="c_2", text="unsupported", evidence_ids=["ev_missing"]),
        ],
        scenes=[
            Scene(scene_id="s_1", narration="n", visual_query_en="city", claim_ids=["c_1"], evidence_ids=["ev_1"]),
            Scene(scene_id="s_2", narration="n", visual_query_en="lab", claim_ids=["c_2"], evidence_ids=["ev_missing"]),
        ],
    )

    result = deterministic_evaluation(plan, evidence)

    assert result.claim_provenance_coverage == 0.5
    assert result.scene_evidence_coverage == 0.5
    assert result.unsupported_claim_ids == ["c_2"]
