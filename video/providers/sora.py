"""OpenAI Sora video generation provider."""

import json
import os
import urllib.request
import urllib.error

from video.providers.base import GenerationResult, VideoProvider

_API_BASE = "https://api.openai.com/v1"


class SoraProvider(VideoProvider):
    """Provider adapter for OpenAI Sora video generation API."""

    def __init__(self, config: dict) -> None:
        api_key_env = config.get("api_key_env", "OPENAI_API_KEY")
        self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key:
            raise ValueError(
                f"API key environment variable {api_key_env!r} is not set"
            )
        self._model = config.get("model", "sora-2-pro")

    def _request(
        self, url: str, data: dict | None = None, method: str | None = None
    ) -> dict:
        """Make an authenticated request to the OpenAI API."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        if method is None:
            method = "POST" if data is not None else "GET"

        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
        else:
            req = urllib.request.Request(url, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"OpenAI API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"OpenAI API connection error: {e.reason}"
            ) from e

    def submit(self, prompt: str, duration: int, resolution: str) -> str:
        """Submit a video generation job to OpenAI Sora.

        Returns the job ID.
        """
        url = f"{_API_BASE}/video/generations"
        w, h = _parse_resolution(resolution)
        payload = {
            "model": self._model,
            "prompt": prompt,
            "duration": duration,
            "width": w,
            "height": h,
        }

        result = self._request(url, payload)
        job_id = result.get("id")
        if not job_id:
            raise RuntimeError(
                f"OpenAI API did not return a job id: {result}"
            )
        return job_id

    def poll(self, job_id: str) -> GenerationResult:
        """Poll the status of a Sora generation job."""
        url = f"{_API_BASE}/video/generations/{job_id}"
        result = self._request(url)

        status = result.get("status", "")

        if status == "completed":
            return GenerationResult(
                job_id=job_id,
                status="completed",
            )

        if status == "failed":
            error_msg = result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", "Unknown error")
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
        """Download the completed video from OpenAI Sora."""
        url = f"{_API_BASE}/video/generations/{job_id}"
        result = self._request(url)

        generations = result.get("generations", [])
        if not generations:
            raise RuntimeError("No generations in Sora response")

        video_url = generations[0].get("url")
        if not video_url:
            raise RuntimeError("No video URL in Sora response")

        try:
            req = urllib.request.Request(video_url)
            req.add_header("Authorization", f"Bearer {self._api_key}")
            with urllib.request.urlopen(req) as resp:
                with open(output_path, "wb") as f:
                    f.write(resp.read())
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Failed to download video: {e.reason}"
            ) from e


def _parse_resolution(resolution: str) -> tuple[int, int]:
    """Convert a resolution string like '1280x720' to (width, height)."""
    try:
        w, h = resolution.split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return 1280, 720
