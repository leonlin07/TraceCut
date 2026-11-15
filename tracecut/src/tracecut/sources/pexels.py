from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import httpx

from tracecut.schemas import Material


_MAX_VIDEO_BYTES = 500_000_000


class PexelsSource:
    API_URL = "https://api.pexels.com/videos/search"
    LICENSE_URL = "https://www.pexels.com/license/"

    def __init__(self, api_key: str, cache_dir: Path, timeout_s: float = 30.0):
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(timeout_s)
        self.api_client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self.download_client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )

    def close(self) -> None:
        self.api_client.close()
        self.download_client.close()

    def search(self, query: str, per_page: int = 3) -> list[Material]:
        if not self.api_key:
            return []
        response = self.api_client.get(
            self.API_URL,
            params={"query": query, "per_page": min(per_page, 10), "orientation": "portrait"},
            headers={"Authorization": self.api_key},
        )
        response.raise_for_status()
        materials = []
        for video in response.json().get("videos", []):
            selected = self._pick_file(video.get("video_files", []))
            if not selected:
                continue
            source_url = str(video.get("url") or selected["link"])
            material_id = "mat_" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
            materials.append(
                Material(
                    material_id=material_id,
                    provider="pexels",
                    source_url=source_url,
                    download_url=str(selected["link"]),
                    preview_image_url=str(video.get("image") or ""),
                    title=f"Pexels video {video.get('id', '')}",
                    author=str(video.get("user", {}).get("name", "")),
                    license_url=self.LICENSE_URL,
                    duration_s=float(video.get("duration") or 0),
                    width=int(selected.get("width") or 0),
                    height=int(selected.get("height") or 0),
                )
            )
        return materials

    def download(self, material: Material) -> Material:
        if not material.download_url:
            return material
        host = (urlparse(material.download_url).hostname or "").lower()
        if not (host == "pexels.com" or host.endswith(".pexels.com")):
            raise ValueError("Pexels download URL is outside the pexels.com domain")

        path = self.cache_dir / f"{material.material_id}.mp4"
        if not path.exists() or path.stat().st_size < 50_000:
            temp = path.with_suffix(path.suffix + ".part")
            digest = hashlib.sha256()
            total = 0
            try:
                with self.download_client.stream("GET", material.download_url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "video" not in content_type and "octet-stream" not in content_type:
                        raise ValueError(f"unexpected video content type: {content_type}")
                    with temp.open("wb") as output:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > _MAX_VIDEO_BYTES:
                                raise ValueError("Pexels asset exceeds the download size limit")
                            output.write(chunk)
                            digest.update(chunk)
                if total < 50_000:
                    raise ValueError("downloaded Pexels asset is unexpectedly small")
                temp.replace(path)
                material.sha256 = digest.hexdigest()
            finally:
                temp.unlink(missing_ok=True)
        material.local_path = str(path.resolve())
        if not material.sha256:
            material.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        return material

    @staticmethod
    def _pick_file(files: list[dict]) -> dict | None:
        valid = [
            item
            for item in files
            if item.get("link")
            and item.get("width")
            and item.get("height")
            and item.get("file_type", "video/mp4") == "video/mp4"
        ]
        if not valid:
            return None
        valid.sort(
            key=lambda item: (
                item["height"] < item["width"],
                abs(int(item["width"]) - 720),
                -int(item["height"]),
            )
        )
        return valid[0]
