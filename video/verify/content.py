"""Content verification using vision model."""

import base64
import json
import os
import subprocess
import tempfile
import urllib.request


def _extract_frame(clip_path: str) -> bytes:
    """Extract a single representative frame from the middle of the clip."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_path = os.path.join(tmp_dir, "frame.png")
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


def _check_with_vision(
    frame_data: bytes,
    required_subjects: list[str],
    prohibited_elements: list[str],
) -> dict:
    """Send frame to vision model and check for required subjects and prohibited elements."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")

    image_b64 = base64.b64encode(frame_data).decode("utf-8")

    subjects_list = "\n".join(f"- {s}" for s in required_subjects)
    prohibited_list = "\n".join(f"- {p}" for p in prohibited_elements)

    prompt_parts = [
        "Analyze this image and answer the following questions.",
        "",
        "Required subjects (answer true if present, false if absent):",
        subjects_list if subjects_list else "(none)",
    ]
    if prohibited_elements:
        prompt_parts += [
            "",
            "Prohibited elements (answer true if detected, false if absent):",
            prohibited_list,
        ]

    subjects_json = "{" + ", ".join(f'"{s}": true or false' for s in required_subjects) + "}"
    prohibited_json = "{" + ", ".join(f'"{p}": true or false' for p in prohibited_elements) + "}" if prohibited_elements else "{}"

    prompt_parts += [
        "",
        f'Respond with ONLY a JSON object in this exact format, no other text: {{"subjects": {subjects_json}, "prohibited": {prohibited_json}}}',
    ]

    prompt_text = "\n".join(prompt_parts)

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
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
                        "text": prompt_text,
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

    raw = result["content"][0]["text"].strip()
    # Handle JSON wrapped in markdown code blocks
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.startswith("```")]
        raw = "\n".join(lines).strip()
    return json.loads(raw)


def check_content(
    clip_path: str,
    required_subjects: list[str],
    prohibited_elements: list[str] | None = None,
) -> dict:
    """Check that required subjects are present and prohibited elements are absent.

    Args:
        clip_path: Path to the video clip.
        required_subjects: List of subjects that must be present in the clip.
        prohibited_elements: List of elements that must not appear in the clip.

    Returns:
        Dict with check, status, subjects, prohibited, message fields.
    """
    if prohibited_elements is None:
        prohibited_elements = []

    try:
        frame_data = _extract_frame(clip_path)
        vision_result = _check_with_vision(frame_data, required_subjects, prohibited_elements)

        subjects = vision_result.get("subjects", {})
        prohibited = vision_result.get("prohibited", {})

        all_subjects_present = all(subjects.get(s, False) for s in required_subjects)
        no_prohibited_found = all(not prohibited.get(p, False) for p in prohibited_elements)

        status = "pass" if all_subjects_present and no_prohibited_found else "fail"

        if status == "pass":
            message = "All required subjects present and no prohibited elements found"
        else:
            missing = [s for s in required_subjects if not subjects.get(s, False)]
            found_prohibited = [p for p in prohibited_elements if prohibited.get(p, False)]
            parts = []
            if missing:
                parts.append(f"missing subjects: {', '.join(missing)}")
            if found_prohibited:
                parts.append(f"prohibited elements detected: {', '.join(found_prohibited)}")
            message = "; ".join(parts)

        return {
            "check": "content",
            "status": status,
            "subjects": subjects,
            "prohibited": prohibited,
            "message": message,
        }
    except FileNotFoundError:
        return {
            "check": "content",
            "status": "fail",
            "subjects": {},
            "prohibited": {},
            "message": f"File not found: {clip_path}",
        }
    except ValueError as e:
        return {
            "check": "content",
            "status": "fail",
            "subjects": {},
            "prohibited": {},
            "message": str(e),
        }
    except Exception as e:
        return {
            "check": "content",
            "status": "fail",
            "subjects": {},
            "prohibited": {},
            "message": f"Content check failed: {e}",
        }
