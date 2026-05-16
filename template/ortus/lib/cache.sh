# ortus/lib/cache.sh — sourceable cache-relocation block
#
# Side effects on source:
#   - mkdir -p .cache/{uv,pip,npm,cargo,go-mod,go-build} under $PWD
#   - export XDG_CACHE_HOME, UV_CACHE_DIR, PIP_CACHE_DIR, npm_config_cache,
#     CARGO_HOME, GOMODCACHE, GOCACHE — all pointed under $PWD/.cache
#
# Sourced by ortus/ralph.sh (and, in Phase 2, ortus/goal.sh) so the canonical
# definitions live in one place and ralph.sh/goal.sh invariant parity (FR-022)
# is structurally assertable rather than copy-paste.
#
# No dependency on log() — this module emits no diagnostics.

# Cache relocation — the OS sandbox profile mounts ~/.cache
# read-only, which blocks package-manager writes (uv/pip/npm/cargo). Point
# XDG and per-tool cache dirs into a project-local .cache/ inside the
# sandbox-writable filesystem. Bounded, cleanable, and matches the
# minimal-writable-surface stance.
mkdir -p .cache/uv .cache/pip .cache/npm .cache/cargo .cache/go-mod .cache/go-build
export XDG_CACHE_HOME="$PWD/.cache"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PIP_CACHE_DIR="$PWD/.cache/pip"
export npm_config_cache="$PWD/.cache/npm"
export CARGO_HOME="$PWD/.cache/cargo"
export GOMODCACHE="$PWD/.cache/go-mod"
export GOCACHE="$PWD/.cache/go-build"
