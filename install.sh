#!/bin/sh
# install.sh — Install ortus globally via uv.
#
# Requires `uv` on PATH (NFR-004). Does NOT install uv itself.
# Usage:
#   curl -fsSL https://github.com/who/ortus/releases/latest/download/install.sh | sh
#   ./install.sh                 # if you already cloned the repo
#   ./install.sh --version 0.1.0 # pin a specific ortus release

set -eu

UV_INSTALL_URL="https://docs.astral.sh/uv/getting-started/installation/"
UV_CURL_HINT="curl -LsSf https://astral.sh/uv/install.sh | sh"

VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --version=*)
            VERSION="${1#--version=}"
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Usage: install.sh [--version X.Y.Z]
Installs ortus globally via `uv tool install`.

Requires `uv` to be available on PATH. We deliberately do not auto-install
uv — see https://docs.astral.sh/uv/getting-started/installation/.
EOF
            exit 0
            ;;
        *)
            printf 'install.sh: unknown argument: %s\n' "$1" >&2
            exit 1
            ;;
    esac
done

err() {
    printf 'install.sh: %s\n' "$1" >&2
}

# Precondition: uv must be on PATH.
if ! command -v uv >/dev/null 2>&1; then
    err "uv is required but not found on PATH."
    err ""
    err "Install uv first, then re-run:"
    err "  ${UV_CURL_HINT}"
    err ""
    err "Docs: ${UV_INSTALL_URL}"
    exit 1
fi

uv_version=$(uv --version 2>&1 || printf 'unknown')
printf 'install.sh: using %s\n' "${uv_version}"

# Install ortus. uv handles Python install on its own if needed.
target="ortus"
if [ -n "${VERSION}" ]; then
    target="ortus==${VERSION}"
fi
printf 'install.sh: running: uv tool install %s\n' "${target}"
uv tool install "${target}"

# Verify the install. ortus should now be on PATH under uv's tool bin dir.
if ! command -v ortus >/dev/null 2>&1; then
    err "ortus installed but is not on PATH."
    err "Add uv's tool bin dir to PATH; uv prints the directory on install."
    exit 1
fi

ortus_version=$(ortus --version 2>&1 || printf '(version probe failed)')
printf '\ninstall.sh: success — %s\n' "${ortus_version}"
printf '\nNext steps:\n'
printf '  ortus init ~/code/your-project   # bootstrap a fresh repo\n'
printf '  ortus check                       # verify prereqs in $PWD\n'
printf '  ortus grind                       # drain the bd queue (in a project repo)\n'
