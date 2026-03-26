"""Tests for video.verify.runner module."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.verify.runner import _load_criteria, _run_checks, _write_report


# ---------------------------------------------------------------------------
# Repo root (used for subprocess-based CLI tests)
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


# ---------------------------------------------------------------------------
# TestLoadCriteria
# ---------------------------------------------------------------------------


class TestLoadCriteria:
    """Tests for _load_criteria."""

    def test_load_from_json_string(self):
        """Passing a raw JSON string returns the parsed dict."""
        raw = '{"duration": {"min_seconds": 5, "max_seconds": 30}}'
        result = _load_criteria(raw)

        assert result == {"duration": {"min_seconds": 5, "max_seconds": 30}}

    def test_load_from_file(self, tmp_path):
        """Passing a path to a JSON file returns the parsed dict."""
        criteria = {"audio": {"ambient_present": True}}
        criteria_file = tmp_path / "criteria.json"
        criteria_file.write_text(json.dumps(criteria))

        result = _load_criteria(str(criteria_file))

        assert result == criteria

    def test_invalid_json(self):
        """Passing an invalid string that is not a file path raises ValueError."""
        with pytest.raises(ValueError):
            _load_criteria("not-valid-json-{{{")


# ---------------------------------------------------------------------------
# TestRunChecks
# ---------------------------------------------------------------------------


class TestRunChecks:
    """Tests for _run_checks with all check functions mocked."""

    PASS_RESULT = {"check": "duration", "status": "pass", "message": "ok"}

    def test_runs_duration_check(self):
        """When criteria contains 'duration', check_duration is called once."""
        criteria = {"duration": {"min_seconds": 5, "max_seconds": 30}}
        with patch("video.verify.runner.check_duration", return_value=self.PASS_RESULT) as mock_dur:
            results = _run_checks("clip.mp4", criteria)

        mock_dur.assert_called_once_with("clip.mp4", criteria["duration"])
        assert len(results) == 1

    def test_runs_audio_check(self):
        """When criteria contains 'audio', check_audio is called once."""
        criteria = {"audio": {"ambient_present": True}}
        audio_result = {"check": "audio", "status": "pass", "message": "ok"}
        with patch("video.verify.runner.check_audio", return_value=audio_result) as mock_audio:
            results = _run_checks("clip.mp4", criteria)

        mock_audio.assert_called_once_with("clip.mp4", criteria["audio"])
        assert len(results) == 1

    def test_runs_only_specified_checks(self):
        """Only checks whose key appears in criteria are invoked."""
        criteria = {"duration": {"min_seconds": 5, "max_seconds": 30}}
        with (
            patch("video.verify.runner.check_duration", return_value=self.PASS_RESULT),
            patch("video.verify.runner.check_audio") as mock_audio,
            patch("video.verify.runner.check_shot_type") as mock_shot,
            patch("video.verify.runner.check_color_grade") as mock_color,
            patch("video.verify.runner.check_content") as mock_content,
        ):
            _run_checks("clip.mp4", criteria)

        mock_audio.assert_not_called()
        mock_shot.assert_not_called()
        mock_color.assert_not_called()
        mock_content.assert_not_called()

    def test_content_unpacks_correctly(self):
        """check_content receives required_subjects and prohibited_elements unpacked from criteria."""
        criteria = {
            "content": {
                "required_subjects": ["dog", "cat"],
                "prohibited_elements": ["watermark"],
            }
        }
        content_result = {"check": "content", "status": "pass", "message": "ok"}
        with patch("video.verify.runner.check_content", return_value=content_result) as mock_content:
            _run_checks("clip.mp4", criteria)

        mock_content.assert_called_once_with("clip.mp4", ["dog", "cat"], ["watermark"])


# ---------------------------------------------------------------------------
# TestWriteReport
# ---------------------------------------------------------------------------


class TestWriteReport:
    """Tests for _write_report."""

    def test_writes_report_file(self, tmp_path, monkeypatch):
        """A JSON report file is created under output/reports/."""
        monkeypatch.chdir(tmp_path)
        checks = [{"check": "duration", "status": "pass", "message": "ok"}]

        report_path = _write_report("myclip.mp4", checks)

        assert os.path.isfile(report_path)
        with open(report_path) as fh:
            data = json.load(fh)
        assert "clip_path" in data
        assert "timestamp" in data
        assert "overall_status" in data
        assert "checks" in data

    def test_report_overall_pass(self, tmp_path, monkeypatch):
        """overall_status is 'pass' when all checks pass."""
        monkeypatch.chdir(tmp_path)
        checks = [
            {"check": "duration", "status": "pass", "message": "ok"},
            {"check": "audio", "status": "pass", "message": "ok"},
        ]

        report_path = _write_report("clip.mp4", checks)

        with open(report_path) as fh:
            data = json.load(fh)
        assert data["overall_status"] == "pass"

    def test_report_overall_fail(self, tmp_path, monkeypatch):
        """overall_status is 'fail' when at least one check fails."""
        monkeypatch.chdir(tmp_path)
        checks = [
            {"check": "duration", "status": "pass", "message": "ok"},
            {"check": "audio", "status": "fail", "message": "no audio stream"},
        ]

        report_path = _write_report("clip.mp4", checks)

        with open(report_path) as fh:
            data = json.load(fh)
        assert data["overall_status"] == "fail"

    def test_report_filename_derived_from_clip(self, tmp_path, monkeypatch):
        """Report filename is derived from the clip basename with '-verify.json' suffix."""
        monkeypatch.chdir(tmp_path)
        checks = [{"check": "duration", "status": "pass", "message": "ok"}]

        report_path = _write_report("scene-003.mp4", checks)

        assert os.path.basename(report_path) == "scene-003-verify.json"


# ---------------------------------------------------------------------------
# TestMain (CLI integration)
# ---------------------------------------------------------------------------


class TestMain:
    """CLI integration tests for the runner entry point."""

    def test_help_flag(self):
        """Running with --help exits 0 and prints usage information."""
        result = subprocess.run(
            [sys.executable, "-m", "video.verify.runner", "--help"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        assert result.returncode == 0
        assert "usage" in result.stdout.lower()

    def test_exit_code_pass(self, tmp_path, monkeypatch):
        """Runner exits 0 when all checks pass."""
        monkeypatch.chdir(tmp_path)
        criteria_str = json.dumps({"duration": {"min_seconds": 1, "max_seconds": 10}})
        pass_checks = [{"check": "duration", "status": "pass", "message": "ok"}]

        with (
            patch("video.verify.runner._run_checks", return_value=pass_checks),
            patch("sys.argv", ["runner", "clip.mp4", criteria_str]),
            pytest.raises(SystemExit) as exc_info,
        ):
            from video.verify.runner import main
            main()

        assert exc_info.value.code == 0

    def test_exit_code_fail(self, tmp_path, monkeypatch):
        """Runner exits 1 when at least one check fails."""
        monkeypatch.chdir(tmp_path)
        criteria_str = json.dumps({"duration": {"min_seconds": 1, "max_seconds": 10}})
        fail_checks = [{"check": "duration", "status": "fail", "message": "too short"}]

        with (
            patch("video.verify.runner._run_checks", return_value=fail_checks),
            patch("sys.argv", ["runner", "clip.mp4", criteria_str]),
            pytest.raises(SystemExit) as exc_info,
        ):
            from video.verify.runner import main
            main()

        assert exc_info.value.code == 1
