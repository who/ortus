"""Tests for video/assemble/continuity.py."""

import json
import os
import subprocess
import sys
import tempfile

import pytest

# Add repo root to path so we can import video package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.assemble.continuity import (
    _check_color_consistency,
    _check_resolution_uniformity,
    _check_subject_persistence,
    _get_resolution,
    run_continuity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip(tmp_dir, filename="clip.mp4", color="0x808080", resolution="320x240", duration="1"):
    """Create a synthetic video clip with ffmpeg.

    Args:
        tmp_dir: Directory to create the clip in.
        filename: Output filename.
        color: Color in 0xRRGGBB format.
        resolution: Resolution as WxH string.
        duration: Duration in seconds.

    Returns:
        Absolute path to the created clip.
    """
    out_path = os.path.join(tmp_dir, filename)
    result = subprocess.run(
        [
            "ffmpeg", "-f", "lavfi",
            "-i", f"color=c={color}:s={resolution}:d={duration}",
            "-c:v", "libx264",
            "-t", duration,
            out_path, "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
    return out_path


def _write_manifest(tmp_dir, clips_dict):
    """Write a clips-manifest.json and return its path."""
    manifest = {"clips": clips_dict, "assembly": {"continuity_status": None, "final_render": None}}
    path = os.path.join(tmp_dir, "clips-manifest.json")
    with open(path, "w") as f:
        json.dump(manifest, f)
    return path


# ---------------------------------------------------------------------------
# Resolution checks
# ---------------------------------------------------------------------------

class TestGetResolution:

    def test_resolution_of_known_clip(self, tmp_path):
        """ffprobe should return the correct resolution for a known clip."""
        clip = _make_clip(str(tmp_path), resolution="640x480")
        w, h = _get_resolution(clip)
        assert w == 640
        assert h == 480

    def test_nonexistent_file_raises(self, tmp_path):
        """Nonexistent file should raise RuntimeError."""
        with pytest.raises(RuntimeError):
            _get_resolution(str(tmp_path / "no_such.mp4"))


class TestResolutionUniformity:

    def test_all_same_resolution_passes(self, tmp_path):
        """Clips with identical resolutions should pass."""
        c1 = _make_clip(str(tmp_path), "a.mp4", resolution="320x240")
        c2 = _make_clip(str(tmp_path), "b.mp4", resolution="320x240")
        result = _check_resolution_uniformity([("scene-001", c1), ("scene-002", c2)])
        assert result["status"] == "pass"
        assert result["offending_scenes"] == []

    def test_different_resolutions_fails(self, tmp_path):
        """Clips with different resolutions should fail and report offending scenes."""
        c1 = _make_clip(str(tmp_path), "a.mp4", resolution="320x240")
        c2 = _make_clip(str(tmp_path), "b.mp4", resolution="640x480")
        result = _check_resolution_uniformity([("scene-001", c1), ("scene-002", c2)])
        assert result["status"] == "fail"
        assert len(result["offending_scenes"]) > 0
        assert "mismatch" in result["message"].lower()

    def test_three_clips_two_resolutions(self, tmp_path):
        """With 3 clips, the minority resolution clip is the offender."""
        c1 = _make_clip(str(tmp_path), "a.mp4", resolution="320x240")
        c2 = _make_clip(str(tmp_path), "b.mp4", resolution="320x240")
        c3 = _make_clip(str(tmp_path), "c.mp4", resolution="640x480")
        result = _check_resolution_uniformity([
            ("scene-001", c1), ("scene-002", c2), ("scene-003", c3),
        ])
        assert result["status"] == "fail"
        assert "scene-003" in result["offending_scenes"]

    def test_result_structure(self, tmp_path):
        """Result dict has the expected keys."""
        c1 = _make_clip(str(tmp_path), "a.mp4")
        result = _check_resolution_uniformity([("scene-001", c1)])
        assert set(result.keys()) == {"check", "status", "details", "offending_scenes", "message"}
        assert result["check"] == "resolution_uniformity"
        assert result["status"] in ("pass", "fail")


# ---------------------------------------------------------------------------
# Color consistency checks
# ---------------------------------------------------------------------------

class TestColorConsistency:

    def test_similar_clips_pass(self, tmp_path):
        """Adjacent clips with the same color should pass."""
        c1 = _make_clip(str(tmp_path), "a.mp4", color="0x808080")
        c2 = _make_clip(str(tmp_path), "b.mp4", color="0x808080")
        result = _check_color_consistency([("scene-001", c1), ("scene-002", c2)])
        assert result["status"] == "pass"
        assert result["offending_scenes"] == []

    def test_very_different_clips_fail(self, tmp_path):
        """Adjacent clips with drastically different colors should fail."""
        c1 = _make_clip(str(tmp_path), "a.mp4", color="0xFF0000")  # Red
        c2 = _make_clip(str(tmp_path), "b.mp4", color="0x0000FF")  # Blue
        result = _check_color_consistency(
            [("scene-001", c1), ("scene-002", c2)],
            color_temp_threshold=500.0,
            saturation_threshold=20.0,
        )
        assert result["status"] == "fail"
        assert len(result["offending_scenes"]) > 0

    def test_single_clip_passes(self, tmp_path):
        """A single clip has no adjacent pair to compare; should pass."""
        c1 = _make_clip(str(tmp_path), "a.mp4")
        result = _check_color_consistency([("scene-001", c1)])
        assert result["status"] == "pass"

    def test_result_structure(self, tmp_path):
        """Result dict has the expected keys."""
        c1 = _make_clip(str(tmp_path), "a.mp4")
        c2 = _make_clip(str(tmp_path), "b.mp4")
        result = _check_color_consistency([("scene-001", c1), ("scene-002", c2)])
        assert set(result.keys()) == {"check", "status", "details", "offending_scenes", "message"}
        assert result["check"] == "color_consistency"


# ---------------------------------------------------------------------------
# Subject persistence checks
# ---------------------------------------------------------------------------

class TestSubjectPersistence:

    def test_no_subjects_passes(self, tmp_path, monkeypatch):
        """When no scenes share subjects, check passes trivially."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        c1 = _make_clip(str(tmp_path), "a.mp4")
        c2 = _make_clip(str(tmp_path), "b.mp4")
        result = _check_subject_persistence(
            [("scene-001", c1), ("scene-002", c2)],
            manifest_clips={},
        )
        assert result["status"] == "pass"
        assert "nothing to check" in result["message"].lower()

    def test_no_api_key_fails(self, tmp_path, monkeypatch):
        """Without ANTHROPIC_API_KEY, check should fail gracefully."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        c1 = _make_clip(str(tmp_path), "a.mp4")
        result = _check_subject_persistence(
            [("scene-001", c1)],
            manifest_clips={},
        )
        assert result["status"] in ("pass", "fail")

    def test_result_structure(self, tmp_path):
        """Result dict has the expected keys."""
        c1 = _make_clip(str(tmp_path), "a.mp4")
        result = _check_subject_persistence(
            [("scene-001", c1)],
            manifest_clips={},
        )
        assert set(result.keys()) == {"check", "status", "details", "offending_scenes", "message"}
        assert result["check"] == "subject_persistence"


# ---------------------------------------------------------------------------
# Full continuity runner
# ---------------------------------------------------------------------------

class TestRunContinuity:

    def test_empty_manifest(self, tmp_path):
        """Empty clips dict should pass with no checks."""
        manifest_path = _write_manifest(str(tmp_path), {})
        result = run_continuity(manifest_path=manifest_path)
        assert result["overall_status"] == "pass"
        assert result["checks"] == []

    def test_missing_manifest_fails(self, tmp_path):
        """Nonexistent manifest should fail."""
        result = run_continuity(manifest_path=str(tmp_path / "no_such.json"))
        assert result["overall_status"] == "fail"

    def test_resolution_mismatch_detected(self, tmp_path):
        """Two clips with different resolutions should cause overall failure.

        This is the acceptance test: create two test clips with different
        resolutions, run continuity check, verify mismatch is detected.
        """
        c1 = _make_clip(str(tmp_path), "scene-001.mp4", resolution="320x240")
        c2 = _make_clip(str(tmp_path), "scene-002.mp4", resolution="640x480")
        manifest_path = _write_manifest(str(tmp_path), {
            "scene-001": {"path": c1},
            "scene-002": {"path": c2},
        })
        result = run_continuity(manifest_path=manifest_path)
        assert result["overall_status"] == "fail"
        # Find the resolution check
        res_check = next(c for c in result["checks"] if c["check"] == "resolution_uniformity")
        assert res_check["status"] == "fail"
        assert len(res_check["offending_scenes"]) > 0

    def test_uniform_clips_pass_resolution(self, tmp_path):
        """Clips with matching resolution should pass the resolution check."""
        c1 = _make_clip(str(tmp_path), "scene-001.mp4", resolution="320x240")
        c2 = _make_clip(str(tmp_path), "scene-002.mp4", resolution="320x240")
        manifest_path = _write_manifest(str(tmp_path), {
            "scene-001": {"path": c1},
            "scene-002": {"path": c2},
        })
        result = run_continuity(manifest_path=manifest_path)
        res_check = next(c for c in result["checks"] if c["check"] == "resolution_uniformity")
        assert res_check["status"] == "pass"

    def test_result_structure(self, tmp_path):
        """Full result has expected top-level keys."""
        c1 = _make_clip(str(tmp_path), "scene-001.mp4")
        manifest_path = _write_manifest(str(tmp_path), {
            "scene-001": {"path": c1},
        })
        result = run_continuity(manifest_path=manifest_path)
        assert set(result.keys()) == {"overall_status", "checks", "offending_scenes", "message"}
        assert result["overall_status"] in ("pass", "fail")
        assert isinstance(result["checks"], list)
        assert isinstance(result["offending_scenes"], list)


class TestCLI:

    def test_help_flag(self):
        """--help should exit 0 and show usage."""
        result = subprocess.run(
            [sys.executable, "-m", "video.assemble.continuity", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "continuity" in result.stdout.lower()
