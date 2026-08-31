"""Shared shell snippets injected into container entryscripts."""

from __future__ import annotations

import os
import textwrap

from ..config import (
    AGENTS_CACHE_DIR,
    AGENTS_CACHE_DEST,
    COMMON_INSTALL_SCRIPT_DEST,
    COMMON_INSTALL_SCRIPT_SRC,
    UV_CACHE_DIR,
    UV_CACHE_DEST,
)
from ..pkg_proxy import APPLY_PKG_PROXY_SH, PKG_PROXY_APPLY_CALL
from .base_utils import ensure_lf_script_copy


def common_bootstrap() -> str:
    """Path/venv setup + apt/dnf → Squid; pip → Clash + PIP_CACHE_DIR."""
    return (
        textwrap.dedent(
            """\
            # /usr/bin first, then activate prepends venv so python3 uses /opt/tb2-venv.
            export PATH="/usr/bin:$PATH:$HOME/.local/bin:/usr/local/bin:/root/.local/bin"
            if [ -f /opt/tb2-venv/bin/activate ]; then
                # shellcheck disable=SC1091
                . /opt/tb2-venv/bin/activate
            elif [ -d /opt/tb2-venv/bin ]; then
                export PATH="/opt/tb2-venv/bin:$PATH"
            fi
            export TB2_VENV_DIR="/opt/tb2-venv"
            export VIRTUAL_ENV="/opt/tb2-venv"
            export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/root/.cache/pip}"
            mkdir -p /logs/agent /logs/verifier /run "$PIP_CACHE_DIR"
            cd /app
            """
        ).strip()
        + "\n"
        + APPLY_PKG_PROXY_SH
        + "\n"
        + PKG_PROXY_APPLY_CALL
        + "\n"
        + textwrap.dedent(
            """\
            echo "[INFO] HTTP_PROXY=${HTTP_PROXY:-} pkg=${TB2_PKG_PROXY:-off} pip_cache=${PIP_CACHE_DIR:-}"
            """
        ).strip()
    )


def prepare_common_files(run_dir: str) -> None:
    """Copy LF common install script into run/ for every agent."""
    os.makedirs(run_dir, exist_ok=True)
    ensure_lf_script_copy(
        COMMON_INSTALL_SCRIPT_SRC,
        os.path.join(run_dir, "env_install_common.sh"),
    )


def common_install_mounts() -> list[tuple[str, str, str]]:
    """Env-only mounts shared by every agent (no agent binaries)."""
    os.makedirs(AGENTS_CACHE_DIR, exist_ok=True)
    os.makedirs(UV_CACHE_DIR, exist_ok=True)
    return [
        (COMMON_INSTALL_SCRIPT_SRC, COMMON_INSTALL_SCRIPT_DEST, "ro"),
        (AGENTS_CACHE_DIR, AGENTS_CACHE_DEST, "ro"),
        (UV_CACHE_DIR, UV_CACHE_DEST, "ro"),
    ]


def merge_mounts(
    *groups: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Merge mount groups; first occurrence of each container dest wins."""
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for group in groups:
        for host, dest, mode in group:
            if dest in seen:
                continue
            seen.add(dest)
            out.append((host, dest, mode))
    return out


def skip_baked_agent_install_cmd(body: str) -> str:
    """Skip env_install_*.sh when the agent layer is already in the image."""
    inner = textwrap.indent(body.strip() + "\n", "    ")
    return (
        "if [ -f /opt/tb2-agent.ok ]; then\n"
        "    echo \"[INFO] agent 已预装在镜像中 (/opt/tb2-agent.ok)，跳过安装\"\n"
        "else\n"
        f"{inner}"
        "fi"
    )


def run_common_install_cmd() -> str:
    """Shell fragment: shared ENV baseline (deps only), then agent-specific install.

    If the image was built with Dockerfile.common-env, skip the heavy apt/pip
    reinstall and only activate the prebuilt venv.
    Swift/browser toolchain is intentionally not installed.
    """
    return textwrap.dedent(
        f"""\
        if [ -f /opt/tb2-common-env.ok ] || [ "${{TB2_COMMON_ENV_PREBUILT:-0}}" = "1" ]; then
            echo "[0/N] 公共依赖已预装在镜像中 (/opt/tb2-common-env.ok)，跳过完整 env_install_common.sh"
            echo "[INFO] Swift/browser: disabled (no toolchain top-up)"
        else
            COMMON_SH="{COMMON_INSTALL_SCRIPT_DEST}"
            if [ -f /run/env_install_common.sh ]; then COMMON_SH=/run/env_install_common.sh; fi
            echo "[0/N] 安装公共环境依赖 (镜像未预装；无 Swift/浏览器)..."
            if ! AGENTS_CACHE_DIR="{AGENTS_CACHE_DEST}" sh "$COMMON_SH"; then
                echo "[ERROR] 公共依赖安装失败"
                exit 1
            fi
        fi
        # /usr/bin first, then activate prepends venv (do not re-prepend /usr/bin after).
        export PATH="/usr/bin:$PATH"
        if [ -f /opt/tb2-venv/bin/activate ]; then
            # shellcheck disable=SC1091
            . /opt/tb2-venv/bin/activate
            export TB2_VENV_DIR="/opt/tb2-venv"
            export VIRTUAL_ENV="/opt/tb2-venv"
        fi
        """
    ).strip()


def venv_python_cmd() -> str:
    """Shell expression: prefer shared-venv python3, else PATH python3."""
    return (
        'PYBIN="${TB2_VENV_DIR:-/opt/tb2-venv}/bin/python3"; '
        'if [ ! -x "$PYBIN" ]; then PYBIN="$(command -v python3 || true)"; fi; '
        'if [ -z "$PYBIN" ]; then echo "[ERROR] python3 not found" >&2; exit 1; fi'
    )


def start_xvfb_cmd() -> str:
    """Start virtual display for all agents (same env as logorythia optional boost)."""
    return textwrap.dedent(
        """\
        echo "[INFO] 准备 Xvfb 显示环境 (所有 agent 相同)..."
        export TB2_XVFB_STARTED=0
        if command -v Xvfb >/dev/null 2>&1; then
            pkill Xvfb >/dev/null 2>&1 || true
            rm -f /tmp/.X99-lock
            Xvfb :99 -screen 0 1024x768x24 >/dev/null 2>&1 &
            export DISPLAY=:99
            export TB2_XVFB_STARTED=1
            sleep 1
            echo "[INFO] Xvfb started on DISPLAY=$DISPLAY"
        else
            export DISPLAY=:99
            echo "[WARN] Xvfb 未找到，仍设置 DISPLAY=$DISPLAY"
        fi
        """
    ).strip()


def stop_xvfb_cmd() -> str:
    """Stop Xvfb if this entryscript started it."""
    return textwrap.dedent(
        """\
        if [ "${TB2_XVFB_STARTED:-0}" = "1" ]; then
            pkill Xvfb >/dev/null 2>&1 || true
            rm -f /tmp/.X99-lock
            export TB2_XVFB_STARTED=0
        fi
        """
    ).strip()


def agent_only_timeout_banner(timeout_sec: int) -> str:
    """Mark that the upcoming timeout applies only to the agent binary."""
    return (
        f'echo "[INFO] agent-only timeout={int(timeout_sec)}s '
        f'(task.toml [agent].timeout_sec; env install not counted)"'
    )


def mark_agent_phase_started_cmd() -> str:
    """Harbor/TB: mark that the agent trial has begun (no install-retry after this)."""
    return textwrap.dedent(
        """\
        # Agent trial started — harness must not recreate for install/infra retry.
        date -u +%Y-%m-%dT%H:%M:%SZ > /run/agent_phase_started
        echo "[INFO] agent_phase_started written"
        """
    ).strip()
