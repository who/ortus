"""Concatenate approved clips into a final video using ffmpeg."""

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from video.manifest import load_manifest, update_assembly, MANIFEST_PATH


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_verified_clips(manifest: dict) -> list[str]:
    """Extract clip paths from manifest in sorted scene_id order.

    Only includes clips whose file exists on disk.

    Args:
        manifest: Loaded clips-manifest.json dict.

    Returns:
        List of clip file paths sorted by scene_id.
    """
    clips = manifest.get("clips", {})
    paths = []
    for scene_id in sorted(clips.keys()):
        clip_data = clips[scene_id]
        clip_path = clip_data.get("path", "")
        if clip_path and os.path.isfile(clip_path):
            paths.append(clip_path)
    return paths


def _build_concat_file(clip_paths: list[str], concat_path: str) -> None:
    """Write an ffmpeg concat demuxer file listing all clip paths.

    Args:
        clip_paths: Ordered list of clip file paths.
        concat_path: Destination path for the concat file.
    """
    with open(concat_path, "w") as f:
        for path in clip_paths:
            f.write(f"file '{path}'\n")


def _run_ffmpeg_concat(concat_path: str, output_path: str) -> None:
    """Run ffmpeg concat demuxer to produce the final video.

    Args:
        concat_path: Path to the ffmpeg concat file list.
        output_path: Destination path for the output video.

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero return code.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_path,
            "-c", "copy",
            output_path,
            "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_stitch(manifest_path: str | None = None) -> dict:
    """Stitch verified clips into a final video.

    Args:
        manifest_path: Path to clips-manifest.json (default: auto-detect).

    Returns:
        Dict with status, output_path, clips_count, and message.
    """
    try:
        manifest = load_manifest(manifest_path)

        clip_paths = _get_verified_clips(manifest)
        if not clip_paths:
            return {"status": "fail", "message": "No clips found in manifest"}

        output_path = os.path.join("output", "renders", "final.mp4")
        os.makedirs(os.path.join("output", "renders"), exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as tmp:
            concat_path = tmp.name

        try:
            _build_concat_file(clip_paths, concat_path)
            _run_ffmpeg_concat(concat_path, output_path)
        finally:
            if os.path.isfile(concat_path):
                os.unlink(concat_path)

        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        update_assembly(
            "final_render",
            {"path": output_path, "timestamp": timestamp},
            manifest_path,
        )

        clips_count = len(clip_paths)
        return {
            "status": "pass",
            "output_path": output_path,
            "clips_count": clips_count,
            "message": f"Successfully stitched {clips_count} clips",
        }

    except Exception as exc:
        return {"status": "fail", "message": str(exc)}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for clip stitching."""
    parser = argparse.ArgumentParser(
        description="Concatenate approved clips into a final video using ffmpeg.",
        prog="python -m video.assemble.stitch",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to clips-manifest.json (default: auto-detect).",
    )
    args = parser.parse_args()

    result = run_stitch(manifest_path=args.manifest)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
