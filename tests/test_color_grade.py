"""Tests for video/verify/color_grade.py."""

import os
import subprocess
import tempfile

import pytest

# Add repo root to path so we can import video package
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.verify.color_grade import check_color_grade, _rgb_to_cct, _measure_frame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip(color_hex: str, tmp_dir: str, filename: str = "clip.mp4") -> str:
    """Create a 1-second synthetic solid-color video clip using ffmpeg.

    Args:
        color_hex: Color in 0xRRGGBB format understood by ffmpeg lavfi.
        tmp_dir: Directory to write the clip into.
        filename: Output filename within tmp_dir.

    Returns:
        Absolute path to the created clip.
    """
    out_path = os.path.join(tmp_dir, filename)
    result = subprocess.run(
        [
            "ffmpeg",
            "-f", "lavfi",
            "-i", f"color=c={color_hex}:s=320x240:d=1",
            "-c:v", "libx264",
            "-t", "1",
            out_path,
            "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
    return out_path


# ---------------------------------------------------------------------------
# Unit tests for _rgb_to_cct
# ---------------------------------------------------------------------------

class TestRgbToCct:
    """Unit tests for the McCamy CCT formula implementation."""

    def test_neutral_gray_is_near_daylight(self):
        # Equal RGB channels should give approximately D65 (~6500 K)
        cct = _rgb_to_cct(0.5, 0.5, 0.5)
        assert abs(cct - 6503) < 500, f"Expected ~6503K for neutral gray, got {cct:.0f}K"

    def test_warm_orange_is_low_kelvin(self):
        # Orange (R=1.0, G=0.667, B=0.267) should be warm (<4500 K)
        cct = _rgb_to_cct(255 / 255, 170 / 255, 68 / 255)
        assert cct < 4500, f"Expected warm CCT (<4500K) for orange, got {cct:.0f}K"
        assert cct > 2000, f"Expected realistic CCT (>2000K) for orange, got {cct:.0f}K"

    def test_black_image_returns_default(self):
        # Pure black should return the default fallback (6500 K)
        cct = _rgb_to_cct(0.0, 0.0, 0.0)
        assert cct == 6500.0


# ---------------------------------------------------------------------------
# Integration tests using synthetic ffmpeg clips
# ---------------------------------------------------------------------------

class TestCheckColorGrade:

    def test_known_color_clip_measurements_in_range(self, tmp_path):
        """Warm orange clip should produce measurements within known ranges."""
        clip = _make_clip("0xFFAA44", str(tmp_path))
        result = check_color_grade(clip, {})

        assert result["check"] == "color_grade"
        assert result["status"] == "pass"

        measurements = result["measurements"]
        cct = measurements["color_temp"]
        sat = measurements["saturation"]

        # Warm orange: expected ~3776 K
        assert cct is not None
        assert abs(cct - 3776) < 500, f"Unexpected CCT {cct:.0f}K for orange clip"

        # Orange is highly saturated in HSV
        assert sat is not None
        assert sat > 100, f"Expected high saturation for orange clip, got {sat:.1f}"

    def test_pass_within_range_warm_clip(self, tmp_path):
        """Criteria window that contains the warm clip's CCT should pass."""
        clip = _make_clip("0xFFAA44", str(tmp_path))
        # Warm orange is ~3776 K; set a generous window around it
        criteria = {"color_temp_range": [2500, 5000]}
        result = check_color_grade(clip, criteria)

        assert result["status"] == "pass", (
            f"Expected pass for orange clip within [2500, 5000]K. "
            f"Got: {result['message']}"
        )
        assert result["measurements"]["color_temp"] is not None

    def test_fail_outside_range_warm_clip(self, tmp_path):
        """Criteria window that excludes the warm clip's CCT should fail."""
        clip = _make_clip("0xFFAA44", str(tmp_path))
        # Warm orange is ~3776 K; require cool daylight range
        criteria = {"color_temp_range": [6000, 8000]}
        result = check_color_grade(clip, criteria)

        assert result["status"] == "fail", (
            f"Expected fail for orange clip outside [6000, 8000]K. "
            f"Got: {result['message']}"
        )
        assert "color temp" in result["message"]

    def test_nonexistent_file_returns_fail(self, tmp_path):
        """Passing a nonexistent path should return a fail status, not raise."""
        fake_path = str(tmp_path / "does_not_exist.mp4")
        result = check_color_grade(fake_path, {"color_temp_range": [3000, 7000]})

        assert result["check"] == "color_grade"
        assert result["status"] == "fail"
        assert result["measurements"]["color_temp"] is None
        assert result["measurements"]["saturation"] is None
        assert "Could not measure clip" in result["message"]

    def test_max_saturation_check_highly_saturated_clip(self, tmp_path):
        """Pure red clip should have very high saturation and fail a tight max_saturation."""
        clip = _make_clip("0xFF0000", str(tmp_path))

        # First verify the measured saturation is high
        result_measure = check_color_grade(clip, {})
        sat = result_measure["measurements"]["saturation"]
        assert sat is not None
        assert sat > 200, f"Expected saturation >200 for pure red clip, got {sat:.1f}"

        # Now check that a low max_saturation threshold causes a fail
        criteria_fail = {"max_saturation": 50.0}
        result_fail = check_color_grade(clip, criteria_fail)
        assert result_fail["status"] == "fail", (
            f"Expected fail when saturation {sat:.1f} > max 50. "
            f"Got: {result_fail['message']}"
        )
        assert "saturation" in result_fail["message"]

        # And a permissive threshold should pass
        criteria_pass = {"max_saturation": 255.0}
        result_pass = check_color_grade(clip, criteria_pass)
        assert result_pass["status"] == "pass", (
            f"Expected pass when max_saturation=255. Got: {result_pass['message']}"
        )

    def test_neutral_gray_low_saturation(self, tmp_path):
        """Neutral gray clip should have near-zero saturation."""
        clip = _make_clip("0x808080", str(tmp_path))
        result = check_color_grade(clip, {})

        assert result["status"] == "pass"
        sat = result["measurements"]["saturation"]
        assert sat is not None
        assert sat < 20, f"Expected near-zero saturation for gray clip, got {sat:.1f}"

        # Gray should also be near neutral daylight CCT
        cct = result["measurements"]["color_temp"]
        assert abs(cct - 6503) < 500, f"Expected ~6503K for gray, got {cct:.0f}K"

    def test_result_structure(self, tmp_path):
        """Result dict should always have the expected keys."""
        clip = _make_clip("0x808080", str(tmp_path))
        result = check_color_grade(clip, {"color_temp_range": [5000, 8000], "max_saturation": 100})

        assert set(result.keys()) == {"check", "status", "measurements", "expected", "message"}
        assert result["check"] == "color_grade"
        assert result["status"] in ("pass", "fail")
        assert isinstance(result["measurements"], dict)
        assert "color_temp" in result["measurements"]
        assert "saturation" in result["measurements"]
        assert isinstance(result["message"], str)
