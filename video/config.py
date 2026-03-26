"""Parse MODEL.md config files into Python dicts."""

from pathlib import Path
from typing import Optional

REQUIRED_FIELDS = ("provider", "model", "api_key_env")

DEFAULTS = {
    "poll_interval": 15,
    "max_poll_attempts": 40,
    "output_format": "mp4",
    "resolution": "1280x720",
}

_INT_FIELDS = {"poll_interval", "max_poll_attempts"}


def parse_model_config(path: Optional[str] = None) -> dict:
    """Read MODEL.md and return a config dict.

    MODEL.md uses 'key: value' format. Required fields: provider, model,
    api_key_env. Optional fields get sensible defaults.
    """
    if path is None:
        path = str(Path(__file__).resolve().parent.parent / "MODEL.md")

    text = Path(path).read_text()
    config: dict = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            config[key] = value

    for field in REQUIRED_FIELDS:
        if field not in config:
            raise ValueError(f"Missing required field in MODEL.md: {field}")

    for field, default in DEFAULTS.items():
        if field not in config:
            config[field] = default

    for field in _INT_FIELDS:
        if isinstance(config[field], str):
            config[field] = int(config[field])

    return config
