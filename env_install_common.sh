#!/bin/sh
# Shared ENVIRONMENT baseline for ALL TB2 agents (control variables).
# Install the UNION of dependencies — NEVER install agent binaries/packages here.
# Agent-specific env_install_*.sh installs ONLY that agent (no apt / no extra deps).
#
# Browser / Swift toolchain intentionally DISABLED:
#   - no WebKit/GTK for Logorythia WebWorker
#   - no official Swift tarball extract
# Keeps common-env builds small and fast.

set -e

info() { printf '%s\n' "[INFO][common] $1"; }
warn() { printf '%s\n' "[WARN][common] $1"; }
error() { printf '%s\n' "[ERROR][common] $1" >&2; exit 1; }

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        "$@"
    fi
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Keep in sync with harness/pkg_proxy.py APPLY_PKG_PROXY_SH.
# Squid is apt/dnf only. pip uses HTTP_PROXY (Clash) + PIP_CACHE_DIR.
tb2_apply_pkg_proxy() {
    if [ -f /etc/pip.conf ] && grep -q tb2-pkg-proxy /etc/pip.conf 2>/dev/null; then
        rm -f /etc/pip.conf
    fi
    pkg="${TB2_PKG_PROXY:-}"
    if [ -z "$pkg" ]; then
        rm -f /etc/apt/apt.conf.d/99tb2-squid 2>/dev/null || true
        return 0
    fi
    if [ -d /etc/apt ]; then
        mkdir -p /etc/apt/apt.conf.d
        printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
            "$pkg" "$pkg" > /etc/apt/apt.conf.d/99tb2-squid
    fi
    if [ -f /etc/dnf/dnf.conf ]; then
        if ! grep -q tb2-pkg-proxy /etc/dnf/dnf.conf 2>/dev/null; then
            printf '\n# tb2-pkg-proxy\nproxy=%s\n' "$pkg" >> /etc/dnf/dnf.conf
        fi
    fi
    if [ -f /etc/yum.conf ]; then
        if ! grep -q tb2-pkg-proxy /etc/yum.conf 2>/dev/null; then
            printf '\n# tb2-pkg-proxy\nproxy=%s\n' "$pkg" >> /etc/yum.conf
        fi
    fi
}

ensure_apt_packages() {
    if ! command_exists apt-get; then
        return 1
    fi

    export DEBIAN_FRONTEND=noninteractive
    info "apt-get update..."
    run_as_root apt-get -o Acquire::Retries=8 -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 update

    info "installing shared apt baseline (no browser / no Swift)..."
    run_as_root apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        unzip \
        tar \
        ripgrep \
        rsync \
        python3 \
        python3-pip \
        python3-venv \
        xvfb \
        binutils \
        || run_as_root apt-get install -y --no-install-recommends \
            ca-certificates curl git unzip tar ripgrep rsync \
            python3 python3-pip python3-venv xvfb binutils

    return 0
}

ensure_dnf_packages() {
    # Fedora/RHEL baseline — same roles as apt list (unzip required by logorythia zip).
    pkg=
    if command_exists dnf; then
        pkg=dnf
    elif command_exists yum; then
        pkg=yum
    else
        return 1
    fi

    info "$pkg install shared baseline (no browser / no Swift)..."
    # python3-pip provides pip; venv is in python3 stdlib on Fedora.
    run_as_root "$pkg" install -y \
        ca-certificates \
        curl \
        git \
        unzip \
        tar \
        ripgrep \
        rsync \
        python3 \
        python3-pip \
        xorg-x11-server-Xvfb \
        binutils \
        || run_as_root "$pkg" install -y \
            ca-certificates curl git unzip tar rsync \
            python3 python3-pip binutils

    return 0
}

ensure_os_packages() {
    if ensure_apt_packages; then
        :
    elif ensure_dnf_packages; then
        :
    else
        warn "no apt-get/dnf/yum; skip OS package baseline"
        return 0
    fi

    info "python3: $(command -v python3 2>/dev/null || echo missing) ($(python3 --version 2>&1 || true))"
    if command_exists Xvfb; then
        info "Xvfb: $(command -v Xvfb)"
    else
        warn "Xvfb binary missing after package install"
    fi
    if command_exists unzip; then
        info "unzip: $(command -v unzip)"
    else
        warn "unzip missing after package install"
    fi
    if command_exists rg; then
        info "ripgrep: $(command -v rg)"
    else
        warn "ripgrep (rg) missing"
    fi
    info "browser/Swift: disabled (no WebKit/GTK, no Swift toolchain)"
}

ensure_python_tools() {
    if ! command_exists python3; then
        error "python3 still missing after OS package baseline"
    fi

    VENV_DIR="${TB2_VENV_DIR:-/opt/tb2-venv}"
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        info "creating shared venv at $VENV_DIR"
        python3 -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
    export PATH="$VENV_DIR/bin:$PATH"
    export TB2_VENV_DIR="$VENV_DIR"
    export VIRTUAL_ENV="$VENV_DIR"

    pip install -U pip setuptools wheel >/dev/null 2>&1 || \
        python3 -m pip install -U pip setuptools wheel

    # Shared libraries for ALL agents (control variables).
    # Includes MetaGPT runtime deps (not metagpt itself — that stays in env_install_meta.sh).
    # Skip faiss-cpu: MetaGPT pins 1.7.4 which has no cp312 wheel.
    info "installing shared pip libraries (openai + metagpt runtime deps)..."
    # MetaGPT 0.8.1 eagerly imports providers/tools; faiss-cpu skipped (no cp312 wheel).
    # semantic-kernel NOT installed: 0.4.3.dev0 breaks on pydantic v2; stubbed in run_metagpt.py.
    SHARED_PKGS="openai numpy pandas aiohttp tiktoken pydantic PyYAML GitPython typing_extensions tenacity rich typer loguru python-docx openpyxl beautifulsoup4 fire aiofiles tqdm wrapt typing-inspect libcst websocket-client socksio gitignore-parser websockets networkx anytree Pillow nbclient nbformat ipython ipykernel scikit-learn imap-tools chardet playwright google-generativeai anthropic zhipuai qianfan dashscope jieba rank-bm25"

    # shellcheck disable=SC2086
    pip install $SHARED_PKGS
    info "shared venv ready: $VENV_DIR (agent packages installed by each env_install_*.sh)"
}

info "=== TB2 common ENV baseline START (deps only; no agents; no Swift/browser) ==="
tb2_apply_pkg_proxy
if [ -n "${TB2_PKG_PROXY:-}" ]; then
    info "pkg proxy (apt/dnf): $TB2_PKG_PROXY"
fi
if [ -n "${PIP_CACHE_DIR:-}" ]; then
    info "pip cache: $PIP_CACHE_DIR"
fi
if [ "${TB2_SWIFT_ONLY:-0}" = "1" ]; then
    info "TB2_SWIFT_ONLY ignored: Swift/browser toolchain disabled in this harness"
    info "=== TB2 Swift-only install SKIPPED ==="
    exit 0
fi
ensure_os_packages
ensure_python_tools
# Marker used by harness: image layer prebuild + runtime skip of full reinstall
run_as_root mkdir -p /opt
run_as_root touch /opt/tb2-common-env.ok
info "=== TB2 common ENV baseline DONE ==="
