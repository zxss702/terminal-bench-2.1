#!/bin/sh
# Install mini-swe-agent via Clash + host PIP_CACHE_DIR. NO apt.
set -e

info() { printf '%s\n' "[INFO][swe] $1"; }
error() { printf '%s\n' "[ERROR][swe] $1" >&2; exit 1; }

VENV_DIR="${TB2_VENV_DIR:-/opt/tb2-venv}"
if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
    export PATH="$VENV_DIR/bin:$PATH"
fi

command -v python3 >/dev/null 2>&1 || error "python3 missing; common-env not baked?"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    error "mini-swe-agent requires Python >=3.10; got $(python3 -V 2>&1)"
fi

export PIP_DEFAULT_TIMEOUT=120
info "pip install mini-swe-agent..."
python3 -m pip install mini-swe-agent \
    || error "pip mini-swe-agent failed"

if ! command -v mini >/dev/null 2>&1 && ! command -v mini-swe-agent >/dev/null 2>&1; then
    error "mini-swe-agent installed but mini CLI not on PATH"
fi
info "mini-swe-agent ready: $(command -v mini 2>/dev/null || command -v mini-swe-agent)"
