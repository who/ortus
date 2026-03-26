"""Kling video generation provider."""

import json
import os
import urllib.request
import urllib.error

from video.providers.base import GenerationResult, VideoProvider

_API_BASE = "https://api.klingai.com/v1"


class KlingProvider(VideoProvider):

    def __init__(self, config: dict) -> None:
        api_key_env = config.get("api_key_env", "KLING_API_KEY")
        self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key:
            raise ValueError(f"API key environment variable {api_key_env!r} is not set")
        self._model = config.get("model", "kling-v2")

    def _request(self, url: str, data: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"Kling API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Kling API connection error: {e.reason}") from e

    def submit(self, prompt: str, duration: int, resolution: str) -> str:
        url = f"{_API_BASE}/videos/text2video"
        w, h = _parse_resolution(resolution)
        payload = {
            "model_name": self._model,
            "prompt": prompt,
            "duration": str(duration),
            "aspect_ratio": f"{w}:{h}",
        }
        result = self._request(url, payload)
        data = result.get("data", {})
        task_id = data.get("task_id")
        if not task_id:
            raise RuntimeError(f"Kling API did not return a task id: {result}")
        return task_id

    def poll(self, job_id: str) -> GenerationResult:
        url = f"{_API_BASE}/videos/text2video/{job_id}"
        result = self._request(url)
        data = result.get("data", {})
        status = data.get("task_status", "")
        if status == "succeed":
            return GenerationResult(job_id=job_id, status="completed")
        if status == "failed":
            error_msg = data.get("task_status_msg", "Unknown error")
            return GenerationResult(job_id=job_id, status="failed", error=str(error_msg))
        return GenerationResult(job_id=job_id, status="pending")

    def download(self, job_id: str, output_path: str) -> None:
        url = f"{_API_BASE}/videos/text2video/{job_id}"
        result = self._request(url)
        data = result.get("data", {})
        videos = data.get("task_result", {}).get("videos", [])
        if not videos:
            raise RuntimeError("No videos in Kling response")
        video_url = videos[0].get("url", "")
        if not video_url:
            raise RuntimeError("No video URL in Kling response")
        try:
            urllib.request.urlretrieve(video_url, output_path)
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to download video: {e.reason}") from e


def _parse_resolution(resolution: str) -> tuple[int, int]:
    try:
        w, h = resolution.split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return 1280, 720
