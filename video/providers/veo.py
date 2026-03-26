"""Google Veo video generation provider."""

import json
import os
import urllib.request
import urllib.error

from video.providers.base import GenerationResult, VideoProvider

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class VeoProvider(VideoProvider):
    """Provider adapter for Google Veo video generation API."""

    def __init__(self, config: dict) -> None:
        api_key_env = config.get("api_key_env", "GOOGLE_API_KEY")
        self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key:
            raise ValueError(
                f"API key environment variable {api_key_env!r} is not set"
            )
        self._model = config.get("model", "veo-2.0-generate-001")

    def _request(self, url: str, data: dict | None = None) -> dict:
        """Make an authenticated request to the Veo API."""
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}key={self._api_key}"

        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(
                full_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            req = urllib.request.Request(full_url, method="GET")

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(
                f"Veo API error {e.code}: {body}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Veo API connection error: {e.reason}") from e

    def submit(self, prompt: str, duration: int, resolution: str) -> str:
        """Submit a video generation job to Google Veo.

        Returns the operation name (job ID).
        """
        url = f"{_API_BASE}/models/{self._model}:predictLongRunning"
        payload = {
            "instances": [
                {
                    "prompt": prompt,
                }
            ],
            "parameters": {
                "sampleCount": 1,
                "durationSeconds": duration,
                "aspectRatio": _resolution_to_aspect(resolution),
            },
        }

        result = self._request(url, payload)
        operation_name = result.get("name")
        if not operation_name:
            raise RuntimeError(
                f"Veo API did not return an operation name: {result}"
            )
        return operation_name

    def poll(self, job_id: str) -> GenerationResult:
        """Poll the status of a Veo generation operation."""
        url = f"{_API_BASE}/operations/{job_id}"
        result = self._request(url)

        done = result.get("done", False)
        error = result.get("error")

        if error:
            return GenerationResult(
                job_id=job_id,
                status="failed",
                error=error.get("message", str(error)),
            )

        if done:
            return GenerationResult(
                job_id=job_id,
                status="completed",
            )

        return GenerationResult(
            job_id=job_id,
            status="pending",
        )

    def download(self, job_id: str, output_path: str) -> None:
        """Download the completed video from Veo."""
        url = f"{_API_BASE}/operations/{job_id}"
        result = self._request(url)

        response_data = result.get("response", {})
        videos = response_data.get("generatedSamples", [])
        if not videos:
            raise RuntimeError("No generated video found in Veo response")

        video_uri = videos[0].get("video", {}).get("uri")
        if not video_uri:
            raise RuntimeError("No video URI in Veo response")

        try:
            urllib.request.urlretrieve(video_uri, output_path)
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to download video: {e.reason}") from e


def _resolution_to_aspect(resolution: str) -> str:
    """Convert a resolution string like '1280x720' to an aspect ratio."""
    try:
        w, h = resolution.split("x")
        w, h = int(w), int(h)
    except (ValueError, AttributeError):
        return "16:9"

    ratio = w / h
    if abs(ratio - 16 / 9) < 0.1:
        return "16:9"
    elif abs(ratio - 9 / 16) < 0.1:
        return "9:16"
    elif abs(ratio - 1.0) < 0.1:
        return "1:1"
    else:
        return "16:9"
