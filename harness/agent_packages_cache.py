"""Host-side AgentFlow source + linux pip HTTP/wheel cache."""

from __future__ import annotations

import os

from .config import AGENTS_CACHE_DIR, PIP_CACHE_DIR

AGENTFLOW_SRC_DIR = os.path.join(AGENTS_CACHE_DIR, "agentflow")


def resolve_agentflow_src_ready() -> bool:
    if not os.path.isdir(AGENTFLOW_SRC_DIR):
        return False
    return (
        os.path.isfile(os.path.join(AGENTFLOW_SRC_DIR, "pyproject.toml"))
        or os.path.isfile(os.path.join(AGENTFLOW_SRC_DIR, "setup.py"))
        or os.path.isfile(os.path.join(AGENTFLOW_SRC_DIR, "agentflow", "pyproject.toml"))
    )


def resolve_pip_cache_ready() -> bool:
    if not os.path.isdir(PIP_CACHE_DIR):
        return False
    for name in ("http", "http-v2", "wheels"):
        if os.path.isdir(os.path.join(PIP_CACHE_DIR, name)):
            return True
    return False


def pip_cache_status_line() -> str:
    if resolve_pip_cache_ready():
        return (
            f"pip 缓存: 主机 {PIP_CACHE_DIR} 仍在"
            "（构建用 BuildKit id=tb2-pip；主机目录作备份，Clean/Purge 后可再灌）"
        )
    return (
        "pip 缓存: 构建走 BuildKit id=tb2-pip（清理时 prune type!=exec.cachemount，勿无过滤 -af）；"
        "未命中时走 Clash 拉官方 PyPI"
    )


def agent_packages_status_line() -> str:
    if resolve_agentflow_src_ready():
        return f"agent 包缓存: AgentFlow 源码已就绪 ({AGENTFLOW_SRC_DIR})"
    return (
        "agent 包缓存: 缺少 AgentFlow 源码 "
        f"({AGENTFLOW_SRC_DIR}) — 先运行 .\\.cache\\cache_agent_packages.ps1"
    )
