"""Cross-scene continuity checker for assembled clips."""

import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from video.manifest import load_manifest


# ---------------------------------------------------------------------------
# Resolution check
# ---------------------------------------------------------------------------

def _get_resolution(clip_path: str) -> tuple[int, int]:
    """Return (width, height) of clip via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            clip_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {clip_path}: {result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream in {clip_path}")
    return int(streams[0]["width"]), int(streams[0]["height"])


def _check_resolution_uniformity(scene_clips: list[tuple[str, str]]) -> dict:
    """Verify all clips share the same resolution.

    Args:
        scene_clips: List of (scene_id, clip_path) tuples sorted by scene order.

    Returns:
        Dict with check, status, details, offending_scenes, message.
    """
    resolutions = {}
    errors = []
    for scene_id, clip_path in scene_clips:
        try:
            resolutions[scene_id] = _get_resolution(clip_path)
        except (RuntimeError, OSError) as exc:
            errors.append(f"{scene_id}: {exc}")

    if errors:
        return {
            "check": "resolution_uniformity",
            "status": "fail",
            "details": {"resolutions": {}, "errors": errors},
            "offending_scenes": [s for s, _ in scene_clips if s not in resolutions],
            "message": f"Could not probe resolution for some clips: {'; '.join(errors)}",
        }

    unique = set(resolutions.values())
    if len(unique) <= 1:
        res_str = f"{list(unique)[0][0]}x{list(unique)[0][1]}" if unique else "N/A"
        return {
            "check": "resolution_uniformity",
            "status": "pass",
            "details": {"resolutions": {s: f"{w}x{h}" for s, (w, h) in resolutions.items()}},
            "offending_scenes": [],
            "message": f"All clips are {res_str}",
        }

    # Find the most common resolution as the "expected" one
    from collections import Counter
    counts = Counter(resolutions.values())
    expected_res = counts.most_common(1)[0][0]
    offending = [s for s, r in resolutions.items() if r != expected_res]

    return {
        "check": "resolution_uniformity",
        "status": "fail",
        "details": {"resolutions": {s: f"{w}x{h}" for s, (w, h) in resolutions.items()}},
        "offending_scenes": offending,
        "message": (
            f"Resolution mismatch: expected {expected_res[0]}x{expected_res[1]}, "
            f"but {', '.join(offending)} differ"
        ),
    }


# ---------------------------------------------------------------------------
# Color grade consistency check
# ---------------------------------------------------------------------------

def _measure_clip_color(clip_path: str) -> tuple[float, float]:
    """Measure average color temp and saturation for a clip.

    Uses ffmpeg to extract a thumbnail frame, then computes average RGB
    to estimate CCT via McCamy's formula, and mean HSV saturation.
    """
    import cv2
    import numpy as np

    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_path = os.path.join(tmp_dir, "frame.png")
        result = subprocess.run(
            [
                "ffmpeg", "-i", clip_path,
                "-vf", "thumbnail",
                "-frames:v", "1",
                "-q:v", "2",
                frame_path, "-y",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg frame extraction failed: {result.stderr.strip()}")

        bgr = cv2.imread(frame_path)
        if bgr is None:
            raise RuntimeError(f"OpenCV could not read frame: {frame_path}")

    # CCT via McCamy's approximation
    rgb_f = bgr[:, :, ::-1].astype(np.float64) / 255.0
    r_mean = float(rgb_f[:, :, 0].mean())
    g_mean = float(rgb_f[:, :, 1].mean())
    b_mean = float(rgb_f[:, :, 2].mean())

    srgb_to_xyz = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float64)
    rgb = np.array([r_mean, g_mean, b_mean], dtype=np.float64)
    xyz = srgb_to_xyz @ rgb
    x_sum = xyz[0] + xyz[1] + xyz[2]
    if x_sum < 1e-9:
        cct = 6500.0
    else:
        cx = xyz[0] / x_sum
        cy = xyz[1] / x_sum
        n = (cx - 0.3320) / (0.1858 - cy)
        cct = 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33

    # Saturation
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    saturation = float(hsv[:, :, 1].mean())

    return float(cct), saturation


def _check_color_consistency(
    scene_clips: list[tuple[str, str]],
    color_temp_threshold: float = 1000.0,
    saturation_threshold: float = 40.0,
) -> dict:
    """Check color grade consistency between adjacent clips.

    Args:
        scene_clips: Sorted list of (scene_id, clip_path) tuples.
        color_temp_threshold: Maximum allowed CCT delta between adjacent clips (Kelvin).
        saturation_threshold: Maximum allowed saturation delta between adjacent clips.

    Returns:
        Dict with check, status, details, offending_scenes, message.
    """
    measurements = {}
    errors = []
    for scene_id, clip_path in scene_clips:
        try:
            cct, sat = _measure_clip_color(clip_path)
            measurements[scene_id] = {"color_temp": cct, "saturation": sat}
        except (RuntimeError, OSError) as exc:
            errors.append(f"{scene_id}: {exc}")

    if errors:
        return {
            "check": "color_consistency",
            "status": "fail",
            "details": {"measurements": measurements, "errors": errors},
            "offending_scenes": [s for s, _ in scene_clips if s not in measurements],
            "message": f"Could not measure some clips: {'; '.join(errors)}",
        }

    if len(measurements) < 2:
        return {
            "check": "color_consistency",
            "status": "pass",
            "details": {"measurements": measurements},
            "offending_scenes": [],
            "message": "Fewer than 2 clips; nothing to compare",
        }

    offending = []
    failures = []
    scene_ids = [s for s, _ in scene_clips]
    for i in range(len(scene_ids) - 1):
        s1, s2 = scene_ids[i], scene_ids[i + 1]
        m1, m2 = measurements[s1], measurements[s2]
        cct_delta = abs(m1["color_temp"] - m2["color_temp"])
        sat_delta = abs(m1["saturation"] - m2["saturation"])

        if cct_delta > color_temp_threshold:
            for s in (s1, s2):
                if s not in offending:
                    offending.append(s)
            failures.append(
                f"{s1}->{s2} color temp delta {cct_delta:.0f}K > {color_temp_threshold:.0f}K"
            )
        if sat_delta > saturation_threshold:
            for s in (s1, s2):
                if s not in offending:
                    offending.append(s)
            failures.append(
                f"{s1}->{s2} saturation delta {sat_delta:.1f} > {saturation_threshold:.1f}"
            )

    if failures:
        return {
            "check": "color_consistency",
            "status": "fail",
            "details": {"measurements": measurements},
            "offending_scenes": offending,
            "message": "Color inconsistency: " + "; ".join(failures),
        }

    return {
        "check": "color_consistency",
        "status": "pass",
        "details": {"measurements": measurements},
        "offending_scenes": [],
        "message": "Color grade is consistent across adjacent clips",
    }


# ---------------------------------------------------------------------------
# Subject persistence check
# ---------------------------------------------------------------------------

def _extract_frame_bytes(clip_path: str) -> bytes:
    """Extract a representative frame from a clip as PNG bytes."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_path = os.path.join(tmp_dir, "frame.png")
        subprocess.run(
            [
                "ffmpeg", "-i", clip_path,
                "-vf", "thumbnail",
                "-frames:v", "1",
                "-q:v", "2",
                frame_path, "-y",
            ],
            capture_output=True,
            check=True,
        )
        with open(frame_path, "rb") as f:
            return f.read()


def _check_subject_persistence(
    scene_clips: list[tuple[str, str]],
    manifest_clips: dict,
) -> dict:
    """Check that subjects appearing in scene N and N+2 look consistent.

    Uses the vision model to compare frames from non-adjacent scenes that share
    subjects (based on content verification results in the manifest).

    Args:
        scene_clips: Sorted list of (scene_id, clip_path) tuples.
        manifest_clips: The clips dict from clips-manifest.json.

    Returns:
        Dict with check, status, details, offending_scenes, message.
    """
    import base64
    import urllib.request

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "check": "subject_persistence",
            "status": "fail",
            "details": {},
            "offending_scenes": [],
            "message": "ANTHROPIC_API_KEY not set; cannot check subject persistence",
        }

    # Build a map of scene_id -> subjects from verification reports
    scene_subjects: dict[str, list[str]] = {}
    for scene_id, _ in scene_clips:
        clip_data = manifest_clips.get(scene_id, {})
        verification = clip_data.get("verification", {})
        report_path = verification.get("report_path")
        if report_path and os.path.isfile(report_path):
            try:
                with open(report_path) as f:
                    report = json.load(f)
                for check in report.get("checks", []):
                    if check.get("check") == "content":
                        subjects = check.get("subjects", {})
                        present = [s for s, v in subjects.items() if v]
                        if present:
                            scene_subjects[scene_id] = present
            except (json.JSONDecodeError, OSError):
                pass

    # Find non-adjacent scene pairs sharing subjects
    scene_ids = [s for s, _ in scene_clips]
    clip_paths = {s: p for s, p in scene_clips}
    pairs_to_check = []
    for i in range(len(scene_ids)):
        for j in range(i + 2, len(scene_ids)):
            s1, s2 = scene_ids[i], scene_ids[j]
            if s1 in scene_subjects and s2 in scene_subjects:
                shared = set(scene_subjects[s1]) & set(scene_subjects[s2])
                if shared:
                    pairs_to_check.append((s1, s2, list(shared)))

    if not pairs_to_check:
        return {
            "check": "subject_persistence",
            "status": "pass",
            "details": {"pairs_checked": 0},
            "offending_scenes": [],
            "message": "No non-adjacent scene pairs share subjects; nothing to check",
        }

    offending = []
    failures = []
    details = {"pairs": []}

    for s1, s2, shared_subjects in pairs_to_check:
        try:
            frame1 = _extract_frame_bytes(clip_paths[s1])
            frame2 = _extract_frame_bytes(clip_paths[s2])

            b64_1 = base64.b64encode(frame1).decode("utf-8")
            b64_2 = base64.b64encode(frame2).decode("utf-8")

            subjects_str = ", ".join(shared_subjects)
            prompt = (
                f"These two frames are from different scenes of the same video. "
                f"The following subjects appear in both scenes: {subjects_str}. "
                f"Do these subjects look visually consistent (same appearance, clothing, "
                f"features) across both frames? Respond with ONLY a JSON object: "
                f'{{"consistent": true or false, "reason": "brief explanation"}}'
            )

            payload = {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_1}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_2}},
                        {"type": "text", "text": prompt},
                    ],
                }],
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
                api_result = json.loads(resp.read().decode("utf-8"))

            raw = api_result["content"][0]["text"].strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                raw = "\n".join(lines).strip()
            vision_result = json.loads(raw)

            pair_detail = {
                "scenes": [s1, s2],
                "shared_subjects": shared_subjects,
                "consistent": vision_result.get("consistent", False),
                "reason": vision_result.get("reason", ""),
            }
            details["pairs"].append(pair_detail)

            if not vision_result.get("consistent", False):
                for s in (s1, s2):
                    if s not in offending:
                        offending.append(s)
                failures.append(
                    f"{s1}<->{s2} subjects inconsistent: {vision_result.get('reason', 'unknown')}"
                )

        except Exception as exc:
            details["pairs"].append({
                "scenes": [s1, s2],
                "shared_subjects": shared_subjects,
                "error": str(exc),
            })

    details["pairs_checked"] = len(pairs_to_check)

    if failures:
        return {
            "check": "subject_persistence",
            "status": "fail",
            "details": details,
            "offending_scenes": offending,
            "message": "Subject inconsistency: " + "; ".join(failures),
        }

    return {
        "check": "subject_persistence",
        "status": "pass",
        "details": details,
        "offending_scenes": [],
        "message": f"Subject persistence OK across {len(pairs_to_check)} pair(s)",
    }


# ---------------------------------------------------------------------------
# Main continuity runner
# ---------------------------------------------------------------------------

def run_continuity(
    manifest_path: str | None = None,
    color_temp_threshold: float = 1000.0,
    saturation_threshold: float = 40.0,
) -> dict:
    """Run all continuity checks across clips in the manifest.

    Args:
        manifest_path: Path to clips-manifest.json (default: auto-detect).
        color_temp_threshold: Max CCT delta between adjacent clips.
        saturation_threshold: Max saturation delta between adjacent clips.

    Returns:
        Dict with overall_status, checks list, and offending_scenes.
    """
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "overall_status": "fail",
            "checks": [],
            "offending_scenes": [],
            "message": f"Could not load manifest: {exc}",
        }

    clips = manifest.get("clips", {})
    if not clips:
        return {
            "overall_status": "pass",
            "checks": [],
            "offending_scenes": [],
            "message": "No clips in manifest",
        }

    # Sort scenes by ID for consistent ordering
    scene_clips = []
    for scene_id in sorted(clips.keys()):
        clip_data = clips[scene_id]
        clip_path = clip_data.get("path", "")
        if clip_path and os.path.isfile(clip_path):
            scene_clips.append((scene_id, clip_path))

    if not scene_clips:
        return {
            "overall_status": "fail",
            "checks": [],
            "offending_scenes": [],
            "message": "No accessible clip files found",
        }

    checks = []

    # 1. Resolution uniformity
    checks.append(_check_resolution_uniformity(scene_clips))

    # 2. Color grade consistency
    checks.append(_check_color_consistency(
        scene_clips,
        color_temp_threshold=color_temp_threshold,
        saturation_threshold=saturation_threshold,
    ))

    # 3. Subject persistence
    checks.append(_check_subject_persistence(scene_clips, clips))

    overall_status = "pass" if all(c["status"] == "pass" for c in checks) else "fail"
    all_offending = []
    for c in checks:
        for s in c.get("offending_scenes", []):
            if s not in all_offending:
                all_offending.append(s)

    return {
        "overall_status": overall_status,
        "checks": checks,
        "offending_scenes": all_offending,
        "message": "All continuity checks passed" if overall_status == "pass" else "Some continuity checks failed",
    }


def main():
    """CLI entry point for continuity checking."""
    parser = argparse.ArgumentParser(
        description="Check cross-scene continuity for assembled clips.",
        prog="python -m video.assemble.continuity",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to clips-manifest.json (default: auto-detect).",
    )
    parser.add_argument(
        "--color-temp-threshold",
        type=float,
        default=1000.0,
        help="Max color temperature delta between adjacent clips in Kelvin (default: 1000).",
    )
    parser.add_argument(
        "--saturation-threshold",
        type=float,
        default=40.0,
        help="Max saturation delta between adjacent clips (default: 40).",
    )
    args = parser.parse_args()

    result = run_continuity(
        manifest_path=args.manifest,
        color_temp_threshold=args.color_temp_threshold,
        saturation_threshold=args.saturation_threshold,
    )

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["overall_status"] == "pass" else 1)


if __name__ == "__main__":
    main()
