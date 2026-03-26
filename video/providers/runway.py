"""Runway Gen-4.5 video generation provider."""

import json
import os
import urllib.request
import urllib.error

from video.providers.base import GenerationResult, VideoProvider

_API_BASE = "https://api.dev.runwayml.com/v1"


class RunwayProvider(VideoProvider):

    def __init__(self, config: dict) -> None:
        api_key_env = config.get("api_key_env", "RUNWAY_API_KEY")
        self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key:
            raise ValueError(f"API key environment variable {api_key_env!r} is not set")
        self._model = config.get("model", "gen4_turbo")

    def _request(self, url: str, data: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Runway-Version": "2024-11-06",
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
            raise RuntimeError(f"Runway API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Runway API connection error: {e.reason}") from e

    def submit(self, prompt: str, duration: int, resolution: str) -> str:
        url = f"{_API_BASE}/image_to_video"
        w, h = _parse_resolution(resolution)
        payload = {
            "model": self._model,
            "promptText": prompt,
            "duration": duration,
            "ratio": f"{w}:{h}",
        }
        result = self._request(url, payload)
        task_id = result.get("id")
        if not task_id:
            raise RuntimeError(f"Runway API did not return a task id: {result}")
        return task_id

    def poll(self, job_id: str) -> GenerationResult:
        url = f"{_API_BASE}/tasks/{job_id}"
        result = self._request(url)
        status = result.get("status", "")
        if status == "SUCCEEDED":
            return GenerationResult(job_id=job_id, status="completed")
        if status in ("FAILED", "CANCELLED"):
            error_msg = result.get("failure", "Unknown error")
            return GenerationResult(job_id=job_id, status="failed", error=str(error_msg))
        return GenerationResult(job_id=job_id, status="pending")

    def download(self, job_id: str, output_path: str) -> None:
        url = f"{_API_BASE}/tasks/{job_id}"
        result = self._request(url)
        output = result.get("output", [])
        if not output:
            raise RuntimeError("No output in Runway response")
        video_url = output[0]
        if not video_url:
            raise RuntimeError("No video URL in Runway response")
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
