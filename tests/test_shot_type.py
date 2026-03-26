"""Tests for video/verify/shot_type.py."""

import io
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add repo root to path so we can import video package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.verify.shot_type import (
    VALID_SHOT_TYPES,
    _classify_shot_type,
    _extract_frame,
    check_shot_type,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip(tmp_dir) -> str:
    """Create a 1-second synthetic solid-color video clip using ffmpeg.

    Args:
        tmp_dir: Directory (str or Path) to write the clip into.

    Returns:
        Absolute path to the created clip.
    """
    clip_path = os.path.join(str(tmp_dir), "test.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-f", "lavfi",
            "-i", "color=c=0x336699:s=320x240:d=1",
            "-c:v", "libx264",
            "-t", "1",
            clip_path,
            "-y",
        ],
        capture_output=True,
        check=True,
    )
    return clip_path


def _make_api_response(text: str) -> MagicMock:
    """Build a mock urllib response returning a minimal Anthropic API JSON body."""
    body = json.dumps({
        "content": [{"text": text}]
    }).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# TestExtractFrame
# ---------------------------------------------------------------------------

class TestExtractFrame:
    """Tests for the _extract_frame helper."""

    def test_returns_bytes_from_valid_clip(self, tmp_path):
        """_extract_frame should return non-empty bytes for a valid clip."""
        clip = _make_clip(tmp_path)
        frame_data = _extract_frame(clip)

        assert isinstance(frame_data, bytes)
        assert len(frame_data) > 0

    def test_raises_on_nonexistent_file(self, tmp_path):
        """_extract_frame should raise when the clip path does not exist."""
        fake_path = str(tmp_path / "does_not_exist.mp4")

        with pytest.raises(Exception):
            _extract_frame(fake_path)


# ---------------------------------------------------------------------------
# TestClassifyShotType
# ---------------------------------------------------------------------------

class TestClassifyShotType:
    """Tests for the _classify_shot_type helper."""

    def test_raises_when_api_key_missing(self, monkeypatch):
        """_classify_shot_type should raise ValueError when API key is absent."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            _classify_shot_type(b"fake-frame-data")

    @pytest.mark.parametrize("shot_type", [
        "extreme close-up",
        "close-up",
        "medium",
        "extreme wide",
        "wide",
    ])
    def test_parses_each_valid_shot_type(self, monkeypatch, shot_type):
        """_classify_shot_type should return the correct type for each valid label."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        mock_resp = _make_api_response(shot_type)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _classify_shot_type(b"fake-frame-data")

        assert result == shot_type

    def test_parses_extreme_wide_shot_type(self, monkeypatch):
        """_classify_shot_type should correctly parse 'extreme wide'."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        mock_resp = _make_api_response("extreme wide")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _classify_shot_type(b"fake-frame-data")

        assert result == "extreme wide"

    def test_parses_response_with_surrounding_text(self, monkeypatch):
        """_classify_shot_type should extract the shot type even if extra text is present."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        mock_resp = _make_api_response("This is a medium shot.")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _classify_shot_type(b"fake-frame-data")

        assert result == "medium"

    def test_returns_raw_text_when_no_valid_type_matches(self, monkeypatch):
        """_classify_shot_type should return the raw lowercased text for unknown labels."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        mock_resp = _make_api_response("bird's eye view")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _classify_shot_type(b"fake-frame-data")

        assert result == "bird's eye view"


# ---------------------------------------------------------------------------
# TestCheckShotType
# ---------------------------------------------------------------------------

class TestCheckShotType:
    """Integration-style tests for check_shot_type."""

    def test_result_structure_has_expected_keys(self, tmp_path, monkeypatch):
        """Result dict should always contain the five required keys with correct types."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        with patch("video.verify.shot_type._classify_shot_type", return_value="medium"):
            result = check_shot_type(clip, "medium")

        assert set(result.keys()) == {"check", "status", "expected", "actual", "message"}
        assert result["check"] == "shot_type"
        assert result["status"] in ("pass", "fail")
        assert isinstance(result["expected"], str)
        assert isinstance(result["message"], str)

    def test_nonexistent_file_returns_fail(self, tmp_path):
        """check_shot_type should return a fail dict for a missing clip, not raise."""
        fake_path = str(tmp_path / "missing.mp4")
        result = check_shot_type(fake_path, "wide")

        assert result["check"] == "shot_type"
        assert result["status"] == "fail"
        assert result["actual"] is None
        assert "not found" in result["message"].lower() or "missing" in result["message"].lower() or fake_path in result["message"]

    def test_missing_api_key_returns_fail(self, tmp_path, monkeypatch):
        """check_shot_type should return a fail dict when ANTHROPIC_API_KEY is absent."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        clip = _make_clip(tmp_path)

        result = check_shot_type(clip, "close-up")

        assert result["check"] == "shot_type"
        assert result["status"] == "fail"
        assert result["actual"] is None
        assert "ANTHROPIC_API_KEY" in result["message"]

    def test_pass_when_actual_matches_expected(self, tmp_path, monkeypatch):
        """check_shot_type should pass when the classifier returns the expected type."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        with patch("video.verify.shot_type._classify_shot_type", return_value="wide"):
            result = check_shot_type(clip, "wide")

        assert result["status"] == "pass"
        assert result["expected"] == "wide"
        assert result["actual"] == "wide"
        assert "matches" in result["message"]

    def test_fail_when_actual_differs_from_expected(self, tmp_path, monkeypatch):
        """check_shot_type should fail when the classifier returns a different type."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        with patch("video.verify.shot_type._classify_shot_type", return_value="extreme wide"):
            result = check_shot_type(clip, "close-up")

        assert result["status"] == "fail"
        assert result["expected"] == "close-up"
        assert result["actual"] == "extreme wide"
        assert "mismatch" in result["message"]

    def test_expected_type_is_normalized_to_lowercase(self, tmp_path, monkeypatch):
        """check_shot_type should normalize expected_type to lowercase before comparing."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        with patch("video.verify.shot_type._classify_shot_type", return_value="medium"):
            result = check_shot_type(clip, "Medium")

        assert result["status"] == "pass"
        assert result["expected"] == "medium"
