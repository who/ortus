"""Tests for video.verify.audio module."""
import os
import sys
import subprocess
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.verify.audio import check_audio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip_with_audio(tmp_path) -> str:
    """Create a 1-second synthetic clip with a 440 Hz sine audio track using ffmpeg.

    Args:
        tmp_path: Directory (str or Path) to write the clip into.

    Returns:
        Absolute path to the created clip.
    """
    clip_path = os.path.join(str(tmp_path), "with_audio.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi",
            "-i", "color=black:size=320x240:duration=1",
            "-shortest",
            clip_path,
        ],
        capture_output=True,
        check=True,
    )
    return clip_path


def _make_clip_without_audio(tmp_path) -> str:
    """Create a 1-second synthetic video clip with no audio stream using ffmpeg.

    Args:
        tmp_path: Directory (str or Path) to write the clip into.

    Returns:
        Absolute path to the created clip.
    """
    clip_path = os.path.join(str(tmp_path), "no_audio.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", "color=black:size=320x240:duration=1",
            clip_path,
        ],
        capture_output=True,
        check=True,
    )
    return clip_path


def _make_silent_clip(tmp_path) -> str:
    """Create a 1-second synthetic clip with a silent (null) audio track using ffmpeg.

    Args:
        tmp_path: Directory (str or Path) to write the clip into.

    Returns:
        Absolute path to the created clip.
    """
    clip_path = os.path.join(str(tmp_path), "silent.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=mono",
            "-f", "lavfi",
            "-i", "color=black:size=320x240:duration=1",
            "-t", "1",
            clip_path,
        ],
        capture_output=True,
        check=True,
    )
    return clip_path


# ---------------------------------------------------------------------------
# TestCheckAudio
# ---------------------------------------------------------------------------

class TestCheckAudio:
    """Integration-style tests for check_audio."""

    def test_clip_with_audio_passes(self, tmp_path):
        """A clip with a 440 Hz tone and ambient_present=True should pass."""
        clip = _make_clip_with_audio(tmp_path)
        result = check_audio(clip, {"ambient_present": True})

        assert result["status"] == "pass", (
            f"Expected pass for clip with audio. Got: {result['message']}"
        )

    def test_clip_without_audio_fails(self, tmp_path):
        """A clip with no audio stream and ambient_present=True should fail."""
        clip = _make_clip_without_audio(tmp_path)
        result = check_audio(clip, {"ambient_present": True})

        assert result["status"] == "fail", (
            f"Expected fail for clip without audio stream. Got: {result['message']}"
        )
        assert result["has_audio_stream"] is False

    def test_silent_clip_fails(self, tmp_path):
        """A clip with a silent audio track and ambient_present=True should fail (RMS below floor)."""
        clip = _make_silent_clip(tmp_path)
        result = check_audio(clip, {"ambient_present": True})

        assert result["status"] == "fail", (
            f"Expected fail for silent clip (RMS below floor). Got: {result['message']}"
        )

    def test_no_ambient_required_with_audio(self, tmp_path):
        """A clip with audio and empty criteria should pass (only stream presence checked)."""
        clip = _make_clip_with_audio(tmp_path)
        result = check_audio(clip, {})

        assert result["status"] == "pass", (
            f"Expected pass for clip with audio and no ambient requirement. Got: {result['message']}"
        )

    def test_no_ambient_required_without_audio(self, tmp_path):
        """A clip without an audio stream and empty criteria should still fail."""
        clip = _make_clip_without_audio(tmp_path)
        result = check_audio(clip, {})

        assert result["status"] == "fail", (
            f"Expected fail for clip with no audio stream even without ambient requirement. "
            f"Got: {result['message']}"
        )

    def test_nonexistent_file_returns_fail(self, tmp_path):
        """check_audio should return a fail dict for a missing clip, not raise."""
        fake_path = str(tmp_path / "does_not_exist.mp4")
        result = check_audio(fake_path, {"ambient_present": True})

        assert result["check"] == "audio"
        assert result["status"] == "fail"

    def test_result_structure(self, tmp_path):
        """Result dict should always contain all expected keys."""
        clip = _make_clip_with_audio(tmp_path)
        result = check_audio(clip, {"ambient_present": True})

        assert set(result.keys()) == {
            "check", "status", "has_audio_stream", "rms_level", "message"
        }
        assert result["check"] == "audio"
        assert result["status"] in ("pass", "fail")
        assert isinstance(result["has_audio_stream"], bool)
        assert isinstance(result["message"], str)

    def test_custom_silence_floor(self, tmp_path):
        """A clip with audio against a very low silence_floor_db (-80) should pass."""
        clip = _make_clip_with_audio(tmp_path)
        result = check_audio(clip, {"ambient_present": True, "silence_floor_db": -80})

        assert result["status"] == "pass", (
            f"Expected pass with silence_floor_db=-80 for a clip with audio. "
            f"Got: {result['message']}"
        )

    def test_check_name_is_audio(self, tmp_path):
        """result['check'] should always equal 'audio'."""
        clip = _make_clip_with_audio(tmp_path)
        result = check_audio(clip, {})

        assert result["check"] == "audio"
