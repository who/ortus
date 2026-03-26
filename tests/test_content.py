"""Tests for video/verify/content.py."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.verify.content import (
    _extract_frame,
    _check_with_vision,
    check_content,
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
# TestCheckWithVision
# ---------------------------------------------------------------------------

class TestCheckWithVision:
    """Tests for the _check_with_vision helper."""

    def test_raises_when_api_key_missing(self, monkeypatch):
        """_check_with_vision should raise ValueError when API key is absent."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            _check_with_vision(b"fake-frame-data", ["cat"], [])

    def test_parses_valid_json_response(self, monkeypatch):
        """_check_with_vision should parse a valid JSON response correctly."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        response_json = json.dumps({
            "subjects": {"cat": True},
            "prohibited": {"watermark": False},
        })
        mock_resp = _make_api_response(response_json)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _check_with_vision(b"fake-frame-data", ["cat"], ["watermark"])

        assert result["subjects"] == {"cat": True}
        assert result["prohibited"] == {"watermark": False}

    def test_handles_json_in_markdown_code_block(self, monkeypatch):
        """_check_with_vision should parse JSON even when wrapped in a markdown code block."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        inner_json = json.dumps({
            "subjects": {"cat": True},
            "prohibited": {"watermark": False},
        })
        wrapped = f"```json\n{inner_json}\n```"
        mock_resp = _make_api_response(wrapped)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _check_with_vision(b"fake-frame-data", ["cat"], ["watermark"])

        assert result["subjects"] == {"cat": True}
        assert result["prohibited"] == {"watermark": False}


# ---------------------------------------------------------------------------
# TestCheckContent
# ---------------------------------------------------------------------------

class TestCheckContent:
    """Integration-style tests for check_content."""

    def test_result_structure_has_expected_keys(self, tmp_path, monkeypatch):
        """Result dict should always contain the five required keys."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        vision_result = {
            "subjects": {"dog": True},
            "prohibited": {"watermark": False},
        }
        with patch("video.verify.content._check_with_vision", return_value=vision_result):
            result = check_content(clip, ["dog"], ["watermark"])

        assert set(result.keys()) == {"check", "status", "subjects", "prohibited", "message"}

    def test_pass_when_all_subjects_present_no_prohibited(self, tmp_path, monkeypatch):
        """check_content should pass when all subjects are present and nothing prohibited."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        vision_result = {
            "subjects": {"cat": True, "dog": True},
            "prohibited": {"watermark": False, "logo": False},
        }
        with patch("video.verify.content._check_with_vision", return_value=vision_result):
            result = check_content(clip, ["cat", "dog"], ["watermark", "logo"])

        assert result["status"] == "pass"

    def test_fail_when_subject_missing(self, tmp_path, monkeypatch):
        """check_content should fail when a required subject is not present."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        vision_result = {
            "subjects": {"cat": True, "dog": False},
            "prohibited": {"watermark": False},
        }
        with patch("video.verify.content._check_with_vision", return_value=vision_result):
            result = check_content(clip, ["cat", "dog"], ["watermark"])

        assert result["status"] == "fail"

    def test_fail_when_prohibited_element_found(self, tmp_path, monkeypatch):
        """check_content should fail when a prohibited element is detected."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        vision_result = {
            "subjects": {"cat": True},
            "prohibited": {"watermark": True},
        }
        with patch("video.verify.content._check_with_vision", return_value=vision_result):
            result = check_content(clip, ["cat"], ["watermark"])

        assert result["status"] == "fail"

    def test_nonexistent_file_returns_fail(self, tmp_path):
        """check_content should return a fail dict for a missing clip, not raise."""
        fake_path = str(tmp_path / "missing.mp4")
        result = check_content(fake_path, ["cat"], ["watermark"])

        assert result["check"] == "content"
        assert result["status"] == "fail"
        assert result["subjects"] == {}
        assert result["prohibited"] == {}

    def test_missing_api_key_returns_fail(self, tmp_path, monkeypatch):
        """check_content should return a fail dict when ANTHROPIC_API_KEY is absent."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        clip = _make_clip(tmp_path)

        result = check_content(clip, ["cat"], ["watermark"])

        assert result["check"] == "content"
        assert result["status"] == "fail"

    def test_no_prohibited_elements_defaults_to_empty(self, tmp_path, monkeypatch):
        """check_content should work with only required_subjects and no prohibited_elements."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        vision_result = {
            "subjects": {"cat": True},
            "prohibited": {},
        }
        with patch("video.verify.content._check_with_vision", return_value=vision_result):
            result = check_content(clip, ["cat"])

        assert result["status"] == "pass"
        assert result["prohibited"] == {}

    def test_check_field_equals_content(self, tmp_path, monkeypatch):
        """result['check'] should always equal 'content'."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        clip = _make_clip(tmp_path)

        vision_result = {
            "subjects": {"cat": True},
            "prohibited": {},
        }
        with patch("video.verify.content._check_with_vision", return_value=vision_result):
            result = check_content(clip, ["cat"])

        assert result["check"] == "content"
