"""Check clip duration against spec using ffprobe."""

import json
import re
import subprocess


def _parse_spec(spec: str) -> tuple[float, float]:
    """Parse duration spec like '5-7s' or '5s' into (min, max) seconds."""
    spec = spec.strip().rstrip("s")
    if "-" in spec:
        parts = spec.split("-", 1)
        return float(parts[0]), float(parts[1])
    val = float(spec)
    return val, val


def _get_duration(clip_path: str) -> float:
    """Extract duration in seconds from a media file using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            clip_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    duration = data.get("format", {}).get("duration")
    if duration is None:
        raise RuntimeError(f"ffprobe returned no duration for {clip_path}")
    return float(duration)


def check_duration(clip_path: str, spec: str) -> dict:
    """Check clip duration against a spec string.

    Args:
        clip_path: Path to the video/audio clip.
        spec: Duration spec, e.g. '5-7s' or '6s'.

    Returns:
        Dict with keys: check, status, expected, actual, message.
    """
    try:
        actual = _get_duration(clip_path)
    except (RuntimeError, json.JSONDecodeError, FileNotFoundError) as exc:
        return {
            "check": "duration",
            "status": "fail",
            "expected": spec,
            "actual": None,
            "message": f"Could not read duration: {exc}",
        }

    try:
        lo, hi = _parse_spec(spec)
    except (ValueError, IndexError) as exc:
        return {
            "check": "duration",
            "status": "fail",
            "expected": spec,
            "actual": actual,
            "message": f"Invalid spec format: {exc}",
        }

    tolerance = 1.0
    passed = (lo - tolerance) <= actual <= (hi + tolerance)

    return {
        "check": "duration",
        "status": "pass" if passed else "fail",
        "expected": spec,
        "actual": actual,
        "message": (
            f"Duration {actual:.1f}s is within {spec} (±{tolerance}s)"
            if passed
            else f"Duration {actual:.1f}s is outside {spec} (±{tolerance}s)"
        ),
    }
