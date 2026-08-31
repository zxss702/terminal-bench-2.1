#!/bin/sh
# Install ONLY the logorythia agent binary. NO apt. NO extra deps.
# Environment deps come from env_install_common.sh.
set -e

info() { printf '%s\n' "[INFO][logorythia] $1"; }
warn() { printf '%s\n' "[WARN][logorythia] $1"; }
error() { printf '%s\n' "[ERROR][logorythia] $1" >&2; exit 1; }

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        error "need root to install logorythia"
    fi
}

PACKAGE_PATH="${PACKAGE_PATH:-/logorythia_linux_x86.zip}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
APP_DIR="${APP_DIR:-/usr/local/share/logorythia}"

[ -f "$PACKAGE_PATH" ] || error "missing package: $PACKAGE_PATH"

if command -v logorythia >/dev/null 2>&1 && [ -x "$APP_DIR/Logorythia" ]; then
    info "already installed: $(command -v logorythia)"
    exit 0
fi

info "installing logorythia from $(basename "$PACKAGE_PATH")..."
run_as_root rm -rf "$APP_DIR"
run_as_root mkdir -p "$APP_DIR"
if ! run_as_root unzip -o "$PACKAGE_PATH" -d "$APP_DIR" >/dev/null 2>&1; then
    error "unzip failed: $PACKAGE_PATH"
fi
[ -f "$APP_DIR/Logorythia" ] || error "Logorythia binary missing in package"
run_as_root chmod +x "$APP_DIR/Logorythia"
[ -f "$APP_DIR/LogorythiaWebWorker" ] && run_as_root chmod +x "$APP_DIR/LogorythiaWebWorker"
run_as_root mkdir -p "$INSTALL_DIR"
run_as_root ln -sf "$APP_DIR/Logorythia" "$INSTALL_DIR/logorythia"
run_as_root rm -f "$INSTALL_DIR/logorythiaWebWorker"
"$APP_DIR/Logorythia" init >/dev/null 2>&1 || true

command -v logorythia >/dev/null 2>&1 || error "logorythia not on PATH after install"
info "agent ready: $(command -v logorythia)"
