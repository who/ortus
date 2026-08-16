#!/usr/bin/env bash
# Install (or reinstall) the ortus checkout containing this script as the
# global `ortus` CLI via uv.
#
# setuptools-scm computes the version stamp at build time from git state, and
# uv will happily reuse a cached wheel — both have produced installs whose
# `ortus --version` did not match the code actually running (see the
# 2026-08-16 field report). This script forces a fresh build and prints the
# before/after versions so a stale stamp is visible immediately.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is not on PATH (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

before="$(command -v ortus >/dev/null 2>&1 && ortus --version 2>/dev/null || echo "not installed")"

if [ -n "$(git status --porcelain)" ]; then
    echo "warn: working tree is dirty — the setuptools-scm version will carry a" >&2
    echo "      .dYYYYMMDD suffix and won't identify a commit. Commit first for" >&2
    echo "      a trustworthy generated-by stamp." >&2
fi

echo "installing $repo_root as the global ortus CLI..."
uv tool install --force --reinstall "$repo_root"

after="$(ortus --version 2>/dev/null || true)"
echo
echo "ortus version: $before -> $after"
echo "HEAD:          $(git rev-parse --short HEAD)$([ -n "$(git status --porcelain)" ] && echo ' (dirty)')"
