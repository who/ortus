"""Orchestrate all verification checks for a single clip."""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from video.verify.duration import check_duration
from video.verify.shot_type import check_shot_type
from video.verify.color_grade import check_color_grade
from video.verify.content import check_content
from video.verify.audio import check_audio


def _load_criteria(criteria_arg: str) -> dict:
    """Load criteria from a JSON string or a JSON file path.

    Args:
        criteria_arg: Either a path to a JSON file or a raw JSON string.

    Returns:
        Parsed criteria dict.

    Raises:
        ValueError: If the argument cannot be parsed as JSON or read as a file.
    """
    if os.path.isfile(criteria_arg):
        with open(criteria_arg, "r") as fh:
            return json.load(fh)
    try:
        return json.loads(criteria_arg)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"criteria is neither a valid file path nor valid JSON: {exc}"
        ) from exc


def _run_checks(clip_path: str, criteria: dict) -> list:
    """Run each applicable check based on criteria keys.

    Args:
        clip_path: Path to the video/audio clip.
        criteria: Dict mapping check name to its parameters.

    Returns:
        List of result dicts from each check that was run.
    """
    results = []

    if "duration" in criteria:
        results.append(check_duration(clip_path, criteria["duration"]))

    if "shot_type" in criteria:
        results.append(check_shot_type(clip_path, criteria["shot_type"]))

    if "color_grade" in criteria:
        results.append(check_color_grade(clip_path, criteria["color_grade"]))

    if "content" in criteria:
        content_criteria = criteria["content"]
        required_subjects = content_criteria.get("required_subjects", [])
        prohibited_elements = content_criteria.get("prohibited_elements", None)
        results.append(check_content(clip_path, required_subjects, prohibited_elements))

    if "audio" in criteria:
        results.append(check_audio(clip_path, criteria["audio"]))

    return results


def _write_report(clip_path: str, checks: list) -> str:
    """Write a JSON report to output/reports/ and return the report path.

    Args:
        clip_path: Path to the clip that was verified.
        checks: List of per-check result dicts.

    Returns:
        Absolute path to the written report file.
    """
    overall_status = "pass" if all(c.get("status") == "pass" for c in checks) else "fail"

    report = {
        "clip_path": clip_path,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "overall_status": overall_status,
        "checks": checks,
    }

    reports_dir = os.path.join("output", "reports")
    os.makedirs(reports_dir, exist_ok=True)

    clip_basename = os.path.splitext(os.path.basename(clip_path))[0]
    report_filename = f"{clip_basename}-verify.json"
    report_path = os.path.join(reports_dir, report_filename)

    with open(report_path, "w") as fh:
        json.dump(report, fh, indent=2)

    return report_path


def main():
    """Run verification checks for a single clip and write a JSON report."""
    parser = argparse.ArgumentParser(
        description="Verify a video clip against acceptance criteria.",
        prog="python -m video.verify.runner",
    )
    parser.add_argument(
        "clip_path",
        help="Path to the video/audio clip to verify.",
    )
    parser.add_argument(
        "criteria",
        help=(
            "Acceptance criteria as a JSON string or path to a JSON file. "
            "Keys may include: duration, shot_type, color_grade, content, audio."
        ),
    )
    args = parser.parse_args()

    try:
        criteria = _load_criteria(args.criteria)
    except (ValueError, OSError) as exc:
        print(f"Error loading criteria: {exc}", file=sys.stderr)
        sys.exit(1)

    checks = _run_checks(args.clip_path, criteria)

    report_path = _write_report(args.clip_path, checks)

    overall_status = "pass" if all(c.get("status") == "pass" for c in checks) else "fail"
    print(f"Report written to: {report_path}")
    print(f"Overall status: {overall_status}")

    sys.exit(0 if overall_status == "pass" else 1)


if __name__ == "__main__":
    main()
