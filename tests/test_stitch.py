"""Tests for video/assemble/stitch.py."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add repo root to path so we can import video package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from video.assemble.stitch import (
    _build_concat_file,
    _get_verified_clips,
    _run_ffmpeg_concat,
    run_stitch,
)


# ---------------------------------------------------------------------------
# Repo root (used for subprocess-based CLI tests)
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_clip(tmp_dir, name, duration, color):
    """Create a synthetic video clip with ffmpeg.

    Args:
        tmp_dir: Directory to create the clip in.
        name: Output filename (e.g. "clip1.mp4").
        duration: Duration in seconds (int or float).
        color: Color name accepted by ffmpeg lavfi (e.g. "red", "blue").

    Returns:
        pathlib.Path pointing to the created clip.
    """
    clip_path = Path(tmp_dir) / name
    result = subprocess.run(
        [
            "ffmpeg",
            "-f", "lavfi",
            "-i", f"color=c={color}:s=320x240:d={duration}",
            "-c:v", "libx264",
            "-t", str(duration),
            str(clip_path),
            "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
    return clip_path


def _write_manifest(tmp_dir, clips_dict):
    """Write a clips-manifest.json in the expected format and return its path.

    Args:
        tmp_dir: Directory to write the manifest into.
        clips_dict: Dict mapping scene_id -> {"path": str, ...}.

    Returns:
        Absolute path string to the written manifest file.
    """
    manifest = {
        "clips": clips_dict,
        "assembly": {
            "continuity_status": None,
            "final_render": None,
        },
    }
    path = os.path.join(str(tmp_dir), "clips-manifest.json")
    with open(path, "w") as fh:
        json.dump(manifest, fh)
    return path


def _get_duration(video_path):
    """Return duration in seconds (float) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# TestGetVerifiedClips
# ---------------------------------------------------------------------------


class TestGetVerifiedClips:
    """Tests for _get_verified_clips."""

    def test_returns_sorted_by_scene_id(self, tmp_path):
        """Clips are returned sorted by scene_id lexicographically."""
        c1 = _make_clip(tmp_path, "a.mp4", 1, "red")
        c2 = _make_clip(tmp_path, "b.mp4", 1, "blue")
        manifest = {
            "clips": {
                "scene-002": {"path": str(c2)},
                "scene-001": {"path": str(c1)},
            }
        }
        result = _get_verified_clips(manifest)
        assert result == [str(c1), str(c2)]

    def test_missing_files_filtered_out(self, tmp_path):
        """Clips whose file does not exist on disk are excluded."""
        c1 = _make_clip(tmp_path, "exists.mp4", 1, "red")
        manifest = {
            "clips": {
                "scene-001": {"path": str(c1)},
                "scene-002": {"path": str(tmp_path / "no_such.mp4")},
            }
        }
        result = _get_verified_clips(manifest)
        assert str(c1) in result
        assert len(result) == 1

    def test_empty_clips_returns_empty_list(self):
        """Empty clips dict returns an empty list."""
        manifest = {"clips": {}}
        result = _get_verified_clips(manifest)
        assert result == []

    def test_returns_list_of_strings(self, tmp_path):
        """Return value is a list of path strings."""
        c1 = _make_clip(tmp_path, "clip.mp4", 1, "green")
        manifest = {"clips": {"scene-001": {"path": str(c1)}}}
        result = _get_verified_clips(manifest)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0] == str(c1)

    def test_all_missing_files_returns_empty(self, tmp_path):
        """When none of the clip files exist, returns an empty list."""
        manifest = {
            "clips": {
                "scene-001": {"path": str(tmp_path / "missing1.mp4")},
                "scene-002": {"path": str(tmp_path / "missing2.mp4")},
            }
        }
        result = _get_verified_clips(manifest)
        assert result == []


# ---------------------------------------------------------------------------
# TestBuildConcatFile
# ---------------------------------------------------------------------------


class TestBuildConcatFile:
    """Tests for _build_concat_file."""

    def test_creates_file(self, tmp_path):
        """concat file is created at the given path."""
        concat_path = str(tmp_path / "concat.txt")
        _build_concat_file(["/a/clip1.mp4", "/a/clip2.mp4"], concat_path)
        assert os.path.isfile(concat_path)

    def test_file_entries_present(self, tmp_path):
        """Each clip path appears as a 'file' entry in the concat file."""
        clips = ["/clips/a.mp4", "/clips/b.mp4", "/clips/c.mp4"]
        concat_path = str(tmp_path / "concat.txt")
        _build_concat_file(clips, concat_path)
        with open(concat_path) as fh:
            content = fh.read()
        for clip in clips:
            assert f"file '{clip}'" in content

    def test_correct_number_of_entries(self, tmp_path):
        """The concat file contains exactly as many 'file' lines as clips."""
        clips = ["/a.mp4", "/b.mp4"]
        concat_path = str(tmp_path / "concat.txt")
        _build_concat_file(clips, concat_path)
        with open(concat_path) as fh:
            lines = fh.readlines()
        file_lines = [l for l in lines if l.strip().startswith("file")]
        assert len(file_lines) == len(clips)

    def test_empty_clips_list(self, tmp_path):
        """Empty clip list produces an empty file."""
        concat_path = str(tmp_path / "concat.txt")
        _build_concat_file([], concat_path)
        with open(concat_path) as fh:
            content = fh.read()
        assert content.strip() == ""


# ---------------------------------------------------------------------------
# TestRunFfmpegConcat
# ---------------------------------------------------------------------------


class TestRunFfmpegConcat:
    """Tests for _run_ffmpeg_concat (subprocess mocked)."""

    def test_calls_subprocess_run(self, tmp_path):
        """_run_ffmpeg_concat calls subprocess.run at least once."""
        concat_path = str(tmp_path / "concat.txt")
        output_path = str(tmp_path / "out.mp4")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _run_ffmpeg_concat(concat_path, output_path)
        mock_run.assert_called_once()

    def test_raises_on_nonzero_returncode(self, tmp_path):
        """RuntimeError is raised when ffmpeg exits with a non-zero code."""
        concat_path = str(tmp_path / "concat.txt")
        output_path = str(tmp_path / "out.mp4")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ffmpeg error output"
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError):
                _run_ffmpeg_concat(concat_path, output_path)

    def test_passes_concat_and_output_paths(self, tmp_path):
        """concat_path and output_path are forwarded to the ffmpeg invocation."""
        concat_path = str(tmp_path / "concat.txt")
        output_path = str(tmp_path / "final.mp4")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _run_ffmpeg_concat(concat_path, output_path)
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert concat_path in cmd or any(concat_path in str(a) for a in cmd)
        assert output_path in cmd or any(output_path in str(a) for a in cmd)


# ---------------------------------------------------------------------------
# TestRunStitch
# ---------------------------------------------------------------------------


class TestRunStitch:
    """Integration tests for run_stitch."""

    def test_empty_manifest_fails(self, tmp_path, monkeypatch):
        """Empty clips dict should cause run_stitch to return status 'fail'."""
        monkeypatch.chdir(tmp_path)
        manifest_path = _write_manifest(tmp_path, {})
        result = run_stitch(manifest_path=manifest_path)
        assert result["status"] == "fail"

    def test_missing_manifest_fails(self, tmp_path, monkeypatch):
        """Nonexistent manifest file should return status 'fail'."""
        monkeypatch.chdir(tmp_path)
        result = run_stitch(manifest_path=str(tmp_path / "no_manifest.json"))
        assert result["status"] == "fail"

    def test_two_clips_stitched_successfully(self, tmp_path, monkeypatch):
        """Two 1-second clips stitched together should produce a ~2-second output.

        Acceptance test: create two short clips, run stitch, verify output
        exists and combined duration equals sum of inputs (±0.1s tolerance).
        """
        monkeypatch.chdir(tmp_path)
        clip1 = _make_clip(tmp_path, "scene-001.mp4", 1, "red")
        clip2 = _make_clip(tmp_path, "scene-002.mp4", 1, "blue")
        manifest_path = _write_manifest(tmp_path, {
            "scene-001": {"path": str(clip1)},
            "scene-002": {"path": str(clip2)},
        })

        result = run_stitch(manifest_path=manifest_path)

        assert result["status"] == "pass"

        # Output file must exist
        output_file = tmp_path / "output" / "renders" / "final.mp4"
        assert output_file.is_file(), f"Expected output at {output_file}"

        # Combined duration should be approximately 2 seconds
        expected_duration = _get_duration(clip1) + _get_duration(clip2)
        actual_duration = _get_duration(output_file)
        assert abs(actual_duration - expected_duration) < 0.1, (
            f"Expected duration ~{expected_duration:.2f}s, got {actual_duration:.2f}s"
        )

    def test_manifest_updated_with_final_render(self, tmp_path, monkeypatch):
        """assembly.final_render in the manifest is updated after a successful stitch."""
        monkeypatch.chdir(tmp_path)
        clip1 = _make_clip(tmp_path, "scene-001.mp4", 1, "green")
        clip2 = _make_clip(tmp_path, "scene-002.mp4", 1, "red")
        manifest_path = _write_manifest(tmp_path, {
            "scene-001": {"path": str(clip1)},
            "scene-002": {"path": str(clip2)},
        })

        run_stitch(manifest_path=manifest_path)

        with open(manifest_path) as fh:
            updated = json.load(fh)

        final_render = updated["assembly"]["final_render"]
        assert final_render is not None
        assert "path" in final_render
        assert "timestamp" in final_render

    def test_result_structure_on_pass(self, tmp_path, monkeypatch):
        """Successful result dict has the expected keys."""
        monkeypatch.chdir(tmp_path)
        clip1 = _make_clip(tmp_path, "scene-001.mp4", 1, "blue")
        manifest_path = _write_manifest(tmp_path, {
            "scene-001": {"path": str(clip1)},
        })

        result = run_stitch(manifest_path=manifest_path)

        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("pass", "fail")

    def test_result_structure_on_fail(self, tmp_path, monkeypatch):
        """Failed result dict contains at least 'status' and 'message' keys."""
        monkeypatch.chdir(tmp_path)
        manifest_path = _write_manifest(tmp_path, {})
        result = run_stitch(manifest_path=manifest_path)
        assert set(result.keys()) >= {"status", "message"}

    def test_output_path_in_pass_result(self, tmp_path, monkeypatch):
        """Successful result includes the output file path."""
        monkeypatch.chdir(tmp_path)
        clip1 = _make_clip(tmp_path, "scene-001.mp4", 1, "red")
        clip2 = _make_clip(tmp_path, "scene-002.mp4", 1, "blue")
        manifest_path = _write_manifest(tmp_path, {
            "scene-001": {"path": str(clip1)},
            "scene-002": {"path": str(clip2)},
        })

        result = run_stitch(manifest_path=manifest_path)

        assert result["status"] == "pass"
        assert "output_path" in result
        assert result["output_path"] is not None

    def test_all_missing_clips_fails(self, tmp_path, monkeypatch):
        """Manifest with all missing clip files should fail."""
        monkeypatch.chdir(tmp_path)
        manifest_path = _write_manifest(tmp_path, {
            "scene-001": {"path": str(tmp_path / "missing1.mp4")},
            "scene-002": {"path": str(tmp_path / "missing2.mp4")},
        })
        result = run_stitch(manifest_path=manifest_path)
        assert result["status"] == "fail"


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    """CLI integration tests for the stitch entry point."""

    def test_help_flag(self):
        """--help should exit 0."""
        result = subprocess.run(
            [sys.executable, "-m", "video.assemble.stitch", "--help"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0
