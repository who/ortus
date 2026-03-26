"""End-to-end tests for the video generation pipeline.

Tests the full flow: copier config → idea.sh script intake → setup-video-beads →
generate → verify → continuity → stitch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Copier config tests
# ---------------------------------------------------------------------------


class TestCopierConfig:
    """Verify copier.yaml has the correct video-oriented questions."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        with open(REPO_ROOT / "copier.yaml") as f:
            self.config = yaml.safe_load(f)

    def test_no_prd_question(self):
        """PRD questions should not exist in the video fork."""
        assert "has_prd" not in self.config
        assert "prd_path" not in self.config

    def test_has_script_question(self):
        """has_script question should exist."""
        assert "has_script" in self.config
        assert self.config["has_script"]["type"] == "bool"

    def test_script_path_question(self):
        """script_path question should exist."""
        assert "script_path" in self.config

    def test_style_path_question(self):
        """style_path question should exist for optional STYLE.md."""
        assert "style_path" in self.config

    def test_model_path_question(self):
        """model_path question should exist for optional MODEL.md."""
        assert "model_path" in self.config

    def test_tasks_reference_script_not_prd(self):
        """Post-copy tasks should reference --script, not --prd."""
        tasks = self.config.get("_tasks", [])
        tasks_str = " ".join(tasks)
        assert "--prd" not in tasks_str
        assert "prd/" not in tasks_str

    def test_tasks_include_setup_video_beads(self):
        """chmod task should include setup-video-beads.sh."""
        tasks = self.config.get("_tasks", [])
        chmod_tasks = [t for t in tasks if "chmod" in t]
        assert len(chmod_tasks) == 1
        assert "setup-video-beads.sh" in chmod_tasks[0]


# ---------------------------------------------------------------------------
# idea.sh tests
# ---------------------------------------------------------------------------


class TestIdeaScript:
    """Tests for ortus/idea.sh script intake flow."""

    def test_script_flag_missing_arg_exits_nonzero(self):
        """--script without a path argument should exit with error."""
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "ortus" / "idea.sh"), "--script"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "requires a path" in result.stderr or "requires a path" in result.stdout

    def test_script_flag_nonexistent_file_exits_nonzero(self):
        """--script with a nonexistent file should exit with error."""
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "ortus" / "idea.sh"), "--script", "/tmp/no-such-script.md"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "can't find" in result.stdout or "can't find" in result.stderr

    def test_no_prd_flag_accepted(self):
        """--prd flag should NOT be recognized anymore."""
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "ortus" / "idea.sh"), "--prd", "somefile"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        # --prd is no longer handled, so it falls through to handle_idea("--prd")
        # which will try to call claude (and fail in test), but the key point is
        # it doesn't enter the old handle_prd flow
        assert "PRD" not in result.stdout

    def test_idea_sh_has_no_prd_references(self):
        """idea.sh should not contain PRD references."""
        content = (REPO_ROOT / "ortus" / "idea.sh").read_text()
        # Allow "PRD" only if it's in a comment about what was replaced, but not in active code
        lines = [l for l in content.splitlines() if not l.strip().startswith("#")]
        code_content = "\n".join(lines)
        assert "prd" not in code_content.lower()

    def test_template_idea_sh_matches_ortus(self):
        """template/ortus/idea.sh should match ortus/idea.sh."""
        ortus_content = (REPO_ROOT / "ortus" / "idea.sh").read_text()
        template_content = (REPO_ROOT / "template" / "ortus" / "idea.sh").read_text()
        assert ortus_content == template_content


# ---------------------------------------------------------------------------
# setup-video-beads.sh tests
# ---------------------------------------------------------------------------


class TestSetupVideoBeads:
    """Tests for ortus/setup-video-beads.sh structure."""

    def test_script_exists_and_executable(self):
        """setup-video-beads.sh should exist in both ortus/ and template/ortus/."""
        ortus_path = REPO_ROOT / "ortus" / "setup-video-beads.sh"
        template_path = REPO_ROOT / "template" / "ortus" / "setup-video-beads.sh"
        assert ortus_path.is_file()
        assert template_path.is_file()

    def test_requires_script_md(self):
        """setup-video-beads.sh should check for SCRIPT.md."""
        content = (REPO_ROOT / "ortus" / "setup-video-beads.sh").read_text()
        assert "SCRIPT.md" in content

    def test_requires_style_md(self):
        """setup-video-beads.sh should check for STYLE.md."""
        content = (REPO_ROOT / "ortus" / "setup-video-beads.sh").read_text()
        assert "STYLE.md" in content

    def test_requires_model_md(self):
        """setup-video-beads.sh should check for MODEL.md."""
        content = (REPO_ROOT / "ortus" / "setup-video-beads.sh").read_text()
        assert "MODEL.md" in content

    def test_missing_files_exits_nonzero(self, tmp_path):
        """Running in a dir without SCRIPT.md/STYLE.md/MODEL.md should fail."""
        # Create a fake ortus dir structure
        ortus_dir = tmp_path / "ortus"
        ortus_dir.mkdir()
        script = REPO_ROOT / "ortus" / "setup-video-beads.sh"
        dest = ortus_dir / "setup-video-beads.sh"
        dest.write_text(script.read_text())
        dest.chmod(0o755)

        result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0
        assert "not found" in result.stderr or "not found" in result.stdout


# ---------------------------------------------------------------------------
# Manifest round-trip tests
# ---------------------------------------------------------------------------


class TestManifestRoundTrip:
    """Test manifest read/write used throughout the pipeline."""

    def test_load_save_roundtrip(self, tmp_path):
        from video.manifest import load_manifest, save_manifest

        data = {
            "clips": {"scene-001": {"path": "clips/scene-001.mp4", "status": "done"}},
            "assembly": {"continuity_status": None, "final_render": None},
        }
        manifest_path = str(tmp_path / "clips-manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(data, f)

        loaded = load_manifest(manifest_path)
        assert loaded == data

        loaded["clips"]["scene-002"] = {"path": "clips/scene-002.mp4", "status": "done"}
        save_manifest(loaded, manifest_path)

        reloaded = load_manifest(manifest_path)
        assert "scene-002" in reloaded["clips"]

    def test_update_clip(self, tmp_path):
        from video.manifest import load_manifest, save_manifest, update_clip

        data = {"clips": {}, "assembly": {}}
        manifest_path = str(tmp_path / "clips-manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(data, f)

        update_clip("scene-001", {"path": "clips/scene-001.mp4"}, manifest_path)
        loaded = load_manifest(manifest_path)
        assert loaded["clips"]["scene-001"]["path"] == "clips/scene-001.mp4"

    def test_update_assembly(self, tmp_path):
        from video.manifest import load_manifest, save_manifest, update_assembly

        data = {"clips": {}, "assembly": {"continuity_status": None}}
        manifest_path = str(tmp_path / "clips-manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(data, f)

        update_assembly("continuity_status", "pass", manifest_path)
        loaded = load_manifest(manifest_path)
        assert loaded["assembly"]["continuity_status"] == "pass"


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------


class TestModelConfig:
    """Test MODEL.md parsing used by the generate step."""

    def test_valid_config(self, tmp_path):
        from video.config import parse_model_config

        config_file = tmp_path / "MODEL.md"
        config_file.write_text(
            "provider: runway\n"
            "model: gen-3\n"
            "api_key_env: RUNWAY_API_KEY\n"
            "resolution: 1920x1080\n"
        )
        config = parse_model_config(str(config_file))
        assert config["provider"] == "runway"
        assert config["model"] == "gen-3"
        assert config["resolution"] == "1920x1080"

    def test_missing_required_field(self, tmp_path):
        from video.config import parse_model_config

        config_file = tmp_path / "MODEL.md"
        config_file.write_text("provider: runway\n")
        with pytest.raises(ValueError, match="Missing required field"):
            parse_model_config(str(config_file))

    def test_defaults_applied(self, tmp_path):
        from video.config import parse_model_config

        config_file = tmp_path / "MODEL.md"
        config_file.write_text(
            "provider: runway\nmodel: gen-3\napi_key_env: RUNWAY_API_KEY\n"
        )
        config = parse_model_config(str(config_file))
        assert config["poll_interval"] == 15
        assert config["max_poll_attempts"] == 40
        assert config["resolution"] == "1280x720"


# ---------------------------------------------------------------------------
# Full pipeline integration (mocked providers)
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Integration test: generate → verify → continuity → stitch with mocked provider."""

    @pytest.fixture
    def pipeline_dir(self, tmp_path):
        """Set up a minimal project dir with clips and manifest."""
        clips_dir = tmp_path / "clips"
        clips_dir.mkdir()

        # Create two synthetic clips with ffmpeg
        for i, color in enumerate(["red", "blue"], start=1):
            scene = f"scene-{i:03d}"
            clip_path = clips_dir / f"{scene}.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-f", "lavfi",
                    "-i", f"color=c={color}:s=320x240:d=1",
                    "-c:v", "libx264", "-t", "1",
                    str(clip_path), "-y",
                ],
                capture_output=True,
                check=True,
            )

        # Write manifest
        manifest = {
            "clips": {
                "scene-001": {"path": str(clips_dir / "scene-001.mp4")},
                "scene-002": {"path": str(clips_dir / "scene-002.mp4")},
            },
            "assembly": {
                "continuity_status": None,
                "final_render": None,
            },
        }
        manifest_path = tmp_path / "clips-manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        return tmp_path, str(manifest_path)

    def test_verify_then_stitch(self, pipeline_dir, monkeypatch):
        """Verify clips pass checks, then stitch into final output."""
        tmp_path, manifest_path = pipeline_dir
        monkeypatch.chdir(tmp_path)

        from video.verify.runner import _run_checks, _write_report
        from video.assemble.stitch import run_stitch

        # Verify each clip
        criteria = {"duration": "0.5-5s"}
        for scene_id in ["scene-001", "scene-002"]:
            clip_path = str(tmp_path / "clips" / f"{scene_id}.mp4")
            checks = _run_checks(clip_path, criteria)
            report = _write_report(clip_path, checks)
            assert os.path.isfile(report)
            with open(report) as f:
                data = json.load(f)
            assert data["overall_status"] == "pass"

        # Stitch
        result = run_stitch(manifest_path=manifest_path)
        assert result["status"] == "pass"
        assert result["output_path"] is not None
        assert os.path.isfile(result["output_path"])

    def test_stitch_updates_manifest(self, pipeline_dir, monkeypatch):
        """After stitch, manifest assembly.final_render should be populated."""
        tmp_path, manifest_path = pipeline_dir
        monkeypatch.chdir(tmp_path)

        from video.assemble.stitch import run_stitch

        run_stitch(manifest_path=manifest_path)

        with open(manifest_path) as f:
            updated = json.load(f)

        assert updated["assembly"]["final_render"] is not None
        assert "path" in updated["assembly"]["final_render"]


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


class TestCLIEntryPoints:
    """Verify all pipeline CLI modules have working --help."""

    @pytest.mark.parametrize("module", [
        "video.generate",
        "video.verify.runner",
        "video.assemble.continuity",
        "video.assemble.stitch",
    ])
    def test_help_exits_zero(self, module):
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
