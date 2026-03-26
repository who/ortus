"""Check audio stream presence and RMS level using ffprobe and ffmpeg."""

import json
import os
import re
import subprocess


def _has_audio_stream(clip_path: str) -> bool:
    """Return True if the file contains at least one audio stream.

    Args:
        clip_path: Path to the media file.

    Raises:
        FileNotFoundError: If clip_path does not exist.
        RuntimeError: If ffprobe fails.
    """
    if not os.path.exists(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "a",
            clip_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    return len(streams) > 0


def _get_rms_level(clip_path: str) -> float:
    """Measure RMS audio level in dB using ffmpeg astats filter.

    Args:
        clip_path: Path to the media file.

    Raises:
        RuntimeError: If ffmpeg fails or RMS level cannot be parsed.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-i", clip_path,
            "-af", "astats=metadata=1:reset=1",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    # ffmpeg outputs stats to stderr; non-zero return is expected for -f null
    stderr = result.stderr

    # Match "RMS level dB: -23.45" or "RMS level dB:          -23.45"
    match = re.search(r"RMS level dB:\s*(-inf|[-\d.]+e[+-]\d+|[-\d.]+)", stderr)
    if not match:
        raise RuntimeError(
            f"Could not parse RMS level from ffmpeg output for {clip_path}"
        )

    raw = match.group(1)
    if raw == "-inf":
        return float("-inf")
    return float(raw)


def check_audio(clip_path: str, criteria: dict) -> dict:
    """Check audio stream presence and RMS level against criteria.

    Args:
        clip_path: Path to the video/audio clip.
        criteria: Dict with optional keys:
            - ``ambient_present``: If True, also verify RMS level is above
              silence floor.
            - ``silence_floor_db``: Override the default silence floor of
              -60 dB (only used when ``ambient_present`` is True).

    Returns:
        Dict with keys: check, status, has_audio_stream, rms_level, message.
    """
    try:
        has_stream = _has_audio_stream(clip_path)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        return {
            "check": "audio",
            "status": "fail",
            "has_audio_stream": None,
            "rms_level": None,
            "message": f"Could not read audio stream info: {exc}",
        }

    try:
        ambient_present = criteria.get("ambient_present", False)
        silence_floor_db = float(criteria.get("silence_floor_db", -60.0))
    except (TypeError, ValueError) as exc:
        return {
            "check": "audio",
            "status": "fail",
            "has_audio_stream": has_stream,
            "rms_level": None,
            "message": f"Invalid criteria format: {exc}",
        }

    if not has_stream:
        return {
            "check": "audio",
            "status": "fail",
            "has_audio_stream": False,
            "rms_level": None,
            "message": "No audio stream found in clip",
        }

    if not ambient_present:
        return {
            "check": "audio",
            "status": "pass",
            "has_audio_stream": True,
            "rms_level": None,
            "message": "Audio stream present",
        }

    # ambient_present is True: also measure RMS level
    try:
        rms = _get_rms_level(clip_path)
    except (RuntimeError, OSError) as exc:
        return {
            "check": "audio",
            "status": "fail",
            "has_audio_stream": True,
            "rms_level": None,
            "message": f"Could not measure RMS level: {exc}",
        }

    passed = rms > silence_floor_db

    return {
        "check": "audio",
        "status": "pass" if passed else "fail",
        "has_audio_stream": True,
        "rms_level": rms,
        "message": (
            f"Audio present and RMS level {rms:.1f} dB is above silence floor"
            f" {silence_floor_db:.1f} dB"
            if passed
            else f"Audio track appears silent: RMS level {rms:.1f} dB is at or"
            f" below silence floor {silence_floor_db:.1f} dB"
        ),
    }
