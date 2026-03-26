"""Read and write clips-manifest.json with atomic saves."""

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "clips-manifest.json"


def load_manifest(path: Optional[str] = None) -> dict:
    """Read clips-manifest.json and return its contents as a dict."""
    p = Path(path) if path else MANIFEST_PATH
    with open(p) as f:
        return json.load(f)


def save_manifest(data: dict, path: Optional[str] = None) -> None:
    """Write dict to clips-manifest.json atomically (temp file + rename)."""
    p = Path(path) if path else MANIFEST_PATH
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        os.unlink(tmp)
        raise


def get_clip(scene_id: str, path: Optional[str] = None) -> Optional[dict]:
    """Return clip data for a scene, or None if not found."""
    manifest = load_manifest(path)
    return manifest.get("clips", {}).get(scene_id)


def update_clip(scene_id: str, clip_data: dict, path: Optional[str] = None) -> None:
    """Update a specific clip entry in the manifest."""
    manifest = load_manifest(path)
    if "clips" not in manifest:
        manifest["clips"] = {}
    manifest["clips"][scene_id] = clip_data
    save_manifest(manifest, path)


def update_assembly(field: str, value, path: Optional[str] = None) -> None:
    """Update a field in the assembly section of the manifest."""
    manifest = load_manifest(path)
    if "assembly" not in manifest:
        manifest["assembly"] = {}
    manifest["assembly"][field] = value
    save_manifest(manifest, path)
