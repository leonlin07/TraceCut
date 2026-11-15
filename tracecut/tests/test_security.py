from pathlib import Path

import pytest
from pydantic import ValidationError

from tracecut.schemas import Claim, Scene, VideoPlan
from tracecut.sources.pexels import PexelsSource
from tracecut.sources.web import _is_public_http_url


def test_private_urls_are_rejected():
    assert not _is_public_http_url("http://127.0.0.1:8000/secret")
    assert not _is_public_http_url("http://169.254.169.254/latest/meta-data")
    assert not _is_public_http_url("file:///etc/passwd")


def test_scene_id_cannot_escape_work_dir():
    with pytest.raises(ValidationError):
        VideoPlan(
            topic="topic",
            title="title",
            hook="hook",
            claims=[Claim(claim_id="c_1", text="claim", evidence_ids=["ev_1"])],
            scenes=[
                Scene(
                    scene_id="../outside",
                    narration="narration",
                    visual_query_en="city",
                    claim_ids=["c_1"],
                    evidence_ids=["ev_1"],
                )
            ],
        )


def test_pexels_download_client_has_no_api_key(tmp_path: Path):
    source = PexelsSource("secret-value", tmp_path)
    try:
        assert "Authorization" not in source.download_client.headers
        assert source.api_client.headers.get("Authorization") is None
    finally:
        source.close()
