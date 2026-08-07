"""`ortus spec` — print the readiness schema v1 authoring contract.

Teaching surface only: the text is package data generated from
``ortus.core.readiness``, so the verb needs no bd workspace, no backend, and
no repo argument.
"""

from __future__ import annotations

import typer

from ortus.core import output
from ortus.core.readiness import READINESS_SCHEMA_VERSION, spec_markdown


def spec() -> None:
    """Print the readiness schema issue-authoring contract."""
    output.progress("spec", f"readiness schema {READINESS_SCHEMA_VERSION}")
    # Straight to stdout, unwrapped and unstyled: the contract is the verb's
    # result and must survive being piped or redirected byte-for-byte.
    typer.echo(spec_markdown(), nl=False)
    output.progress("spec", "done (contract written to stdout)")
