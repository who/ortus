"""Shot type classification using vision model."""

import base64
import json
import os
import subprocess
import tempfile
import urllib.request


VALID_SHOT_TYPES = [
    "extreme close-up",
    "close-up",
    "medium",
    "extreme wide",
    "wide",
]


def _extract_frame(clip_path: str) -> bytes:
    """Extract a single representative frame from the middle of the clip."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_path = os.path.join(tmp_dir, "frame.png")
        # Extract a single frame from the middle using thumbnail filter
        subprocess.run(
            [
                "ffmpeg", "-i", clip_path,
                "-vf", "thumbnail",
                "-frames:v", "1",
                "-q:v", "2",
                frame_path,
                "-y",
            ],
            capture_output=True,
            check=True,
        )
        with open(frame_path, "rb") as f:
            return f.read()


def _classify_shot_type(frame_data: bytes) -> str:
    """Send frame to vision model and get shot type classification."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")

    image_b64 = base64.b64encode(frame_data).decode("utf-8")

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 50,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Classify the camera shot type of this image into exactly one of these categories: "
                            "extreme close-up, close-up, medium, wide, extreme wide. "
                            "Respond with ONLY the category name, nothing else."
                        ),
                    },
                ],
            }
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    raw = result["content"][0]["text"].strip().lower()

    # Match against valid shot types
    for shot_type in VALID_SHOT_TYPES:
        if shot_type in raw:
            return shot_type

    return raw


def check_shot_type(clip_path: str, expected_type: str) -> dict:
    """Check if a clip's shot type matches the expected type.

    Args:
        clip_path: Path to the video clip.
        expected_type: Expected shot type (one of VALID_SHOT_TYPES).

    Returns:
        Dict with check, status, expected, actual, message fields.
    """
    try:
        frame_data = _extract_frame(clip_path)
        actual_type = _classify_shot_type(frame_data)

        expected_norm = expected_type.strip().lower()
        status = "pass" if actual_type == expected_norm else "fail"

        if status == "pass":
            message = f"Shot type matches: {actual_type}"
        else:
            message = f"Shot type mismatch: expected {expected_norm}, got {actual_type}"

        return {
            "check": "shot_type",
            "status": status,
            "expected": expected_norm,
            "actual": actual_type,
            "message": message,
        }
    except FileNotFoundError:
        return {
            "check": "shot_type",
            "status": "fail",
            "expected": expected_type,
            "actual": None,
            "message": f"File not found: {clip_path}",
        }
    except ValueError as e:
        return {
            "check": "shot_type",
            "status": "fail",
            "expected": expected_type,
            "actual": None,
            "message": str(e),
        }
    except Exception as e:
        return {
            "check": "shot_type",
            "status": "fail",
            "expected": expected_type,
            "actual": None,
            "message": f"Shot type check failed: {e}",
        }
