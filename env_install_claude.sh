#!/bin/sh
# Install ONLY the Claude Code agent binary. NO apt. NO extra deps.
# Environment deps come from env_install_common.sh.
set -e

info() { printf '%s\n' "[INFO][claude] $1" >&2; }
error() { printf '%s\n' "[ERROR][claude] $1" >&2; exit 1; }

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        error "need root to install claude"
    fi
}

CLAUDE_PACKAGES_DIR="${CLAUDE_PACKAGES_DIR:-/claude-packages}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"

resolve_claude_binary() {
    is_musl=0
    if [ -f /etc/alpine-release ]; then
        is_musl=1
    elif command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | head -n 1 | grep -qi musl; then
        is_musl=1
    elif [ -L /lib/ld-musl-x86_64.so.1 ] || [ -e /lib/ld-musl-x86_64.so.1 ]; then
        is_musl=1
    fi
    if [ "$is_musl" = "1" ]; then
        preferred="linux-x64-musl"
        fallback="linux-x64"
    else
        preferred="linux-x64"
        fallback="linux-x64-musl"
    fi
    if [ -n "${CLAUDE_PACKAGE_PATH:-}" ] && [ -f "$CLAUDE_PACKAGE_PATH" ]; then
        echo "$CLAUDE_PACKAGE_PATH"
        return 0
    fi
    for platform in "$preferred" "$fallback"; do
        candidate="$CLAUDE_PACKAGES_DIR/$platform/claude"
        if [ -f "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    if [ -f "$CLAUDE_PACKAGES_DIR/claude" ]; then
        echo "$CLAUDE_PACKAGES_DIR/claude"
        return 0
    fi
    found="$(find "$CLAUDE_PACKAGES_DIR" -type f -name claude 2>/dev/null | head -n 1 || true)"
    if [ -n "$found" ] && [ -f "$found" ]; then
        echo "$found"
        return 0
    fi
    return 1
}

if command -v claude >/dev/null 2>&1; then
    info "already installed: $(command -v claude)"
    claude --version 2>/dev/null || true
    exit 0
fi

package="$(resolve_claude_binary || true)"
[ -n "$package" ] || error "claude binary not found under $CLAUDE_PACKAGES_DIR (run .cache/cache_claude.ps1)"

info "installing claude from $package..."
run_as_root mkdir -p "$INSTALL_DIR"
run_as_root cp -f "$package" "$INSTALL_DIR/claude"
run_as_root chmod +x "$INSTALL_DIR/claude"
hash -r 2>/dev/null || true

command -v claude >/dev/null 2>&1 || error "claude not on PATH after install"
if ! claude --version >/dev/null 2>&1; then
    error "claude cannot execute (glibc/musl mismatch? re-run cache_claude.ps1)"
fi
info "agent ready: $(command -v claude)"
claude --version 2>/dev/null || true
