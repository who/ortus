"""Load the operator's environment defaults without evaluating shell code."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Mapping

from ortus.core.output import warn


def load_user_ortus_env(home: Path | None = None) -> None:
    """Fill missing or empty process values from the user file, never a repo file."""
    path = (home if home is not None else Path.home()) / ".config" / "ortus" / ".env"
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except (OSError, UnicodeError):
        warn("Could not read the Ortus user environment file; using process environment")
        return
    for line in contents.splitlines():
        key, separator, raw = line.strip().partition("=")
        key = key.strip()
        if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            continue
        try:
            value = " ".join(shlex.split(raw, comments=True, posix=True))
        except ValueError:
            continue
        if "\0" in value:
            continue
        if not os.environ.get(key):
            os.environ[key] = value


def judge_environment(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """Explicit environments remain isolated from the operator's defaults."""
    if environ is not None:
        return environ
    load_user_ortus_env()
    return os.environ
