"""fal.ai video generation provider."""

import json
import os
import urllib.request
import urllib.error

from video.providers.base import GenerationResult, VideoProvider

_API_BASE = "https://queue.fal.run"


class FalProvider(VideoProvider):
    """Provider adapter for fal.ai video generation API."""

    def __init__(self, config: dict) -> None:
        api_key_env = config.get("api_key_env", "FAL_KEY")
        self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key:
            raise ValueError(
                f"API key environment variable {api_key_env!r} is not set"
            )
        self._model = config.get("model", "fal-ai/minimax-video-01/text-to-video")

    def _request(self, url: str, data: dict | None = None) -> dict:
        """Make an authenticated request to the fal.ai API."""
        headers = {
            "Authorization": f"Key {self._api_key}",
            "Content-Type": "application/json",
        }

        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"fal API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"fal API connection error: {e.reason}") from e

    def submit(self, prompt: str, duration: int, resolution: str) -> str:
        """Submit a video generation job to fal.ai.

        Returns the request ID (job ID).
        """
        url = f"{_API_BASE}/{self._model}"
        w, h = _parse_resolution(resolution)
        payload = {
            "prompt": prompt,
            "duration": duration,
            "width": w,
            "height": h,
        }

        result = self._request(url, payload)
        request_id = result.get("request_id")
        if not request_id:
            raise RuntimeError(
                f"fal API did not return a request_id: {result}"
            )
        return request_id

    def poll(self, job_id: str) -> GenerationResult:
        """Poll the status of a fal.ai generation job."""
        url = f"{_API_BASE}/{self._model}/requests/{job_id}/status"
        result = self._request(url)

        status = result.get("status", "")

        if status == "COMPLETED":
            return GenerationResult(
                job_id=job_id,
                status="completed",
            )

        if status in ("FAILED", "CANCELLED"):
            error_msg = result.get("error", "Unknown error")
            return GenerationResult(
                job_id=job_id,
                status="failed",
                error=str(error_msg),
            )

        return GenerationResult(
            job_id=job_id,
            status="pending",
        )

    def download(self, job_id: str, output_path: str) -> None:
        """Download the completed video from fal.ai."""
        url = f"{_API_BASE}/{self._model}/requests/{job_id}"
        result = self._request(url)

        video = result.get("video", {})
        video_url = video.get("url") if isinstance(video, dict) else None
        if not video_url:
            raise RuntimeError("No video URL in fal response")

        try:
            urllib.request.urlretrieve(video_url, output_path)
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to download video: {e.reason}") from e


def _parse_resolution(resolution: str) -> tuple[int, int]:
    """Convert a resolution string like '1280x720' to (width, height)."""
    try:
        w, h = resolution.split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return 1280, 720
