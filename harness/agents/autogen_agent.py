"""Microsoft AutoGen AgentChat / Magentic-One via OpenAI-compatible DeepSeek."""

from __future__ import annotations

import json
import os
import shutil
import textwrap

from ..config import (
    AGENTS_CACHE_DEST,
    API_KEYS,
    DEEPSEEK_MODEL,
    openai_compatible_base_url,
    PROJECT_ROOT,
)
from ..core import TaskInfo
from .base import AgentAdapter
from .base_utils import resolve_required_host_file, ensure_lf_script_copy
from .common import (
    agent_only_timeout_banner,
    common_bootstrap,
    common_install_mounts,
    mark_agent_phase_started_cmd,
    merge_mounts,
    prepare_common_files,
    run_common_install_cmd,
    skip_baked_agent_install_cmd,
    start_xvfb_cmd,
    stop_xvfb_cmd,
    venv_python_cmd,
)

INSTALL_SCRIPT_SRC = os.path.join(PROJECT_ROOT, "env_install_auto.sh")
INSTALL_SCRIPT_DEST = "/env_install_auto.sh"


class AutogenAdapter(AgentAdapter):
    id = "autogen"
    display_name = "autogen"

    def prepare_run_dir(self, run_dir: str, task: TaskInfo) -> None:
        os.makedirs(run_dir, exist_ok=True)
        prepare_common_files(run_dir)
        shutil.copy2(task.instruction_path, os.path.join(run_dir, "prompt.txt"))
        cfg = {
            "model": DEEPSEEK_MODEL,
            "base_url": openai_compatible_base_url(),
            "api_key": API_KEYS["autogen"],
            "workdir": "/app",
            "timeout_sec": int(task.agent_timeout_sec),
        }
        with open(os.path.join(run_dir, "agent_config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        runner_src = os.path.join(PROJECT_ROOT, "harness", "agents", "run_autogen.py")
        if not os.path.isfile(runner_src):
            raise FileNotFoundError(f"missing AutoGen runner: {runner_src}")
        shutil.copy2(runner_src, os.path.join(run_dir, "run_autogen.py"))
        ensure_lf_script_copy(INSTALL_SCRIPT_SRC, os.path.join(run_dir, "env_install_auto.sh"))

    def install_mounts(self) -> list[tuple[str, str, str]]:
        install_script = resolve_required_host_file(INSTALL_SCRIPT_SRC, "env_install_auto.sh")
        return merge_mounts(
            common_install_mounts(),
            [(install_script, INSTALL_SCRIPT_DEST, "ro")],
        )

    def build_entryscript(self, task: TaskInfo) -> str:
        timeout = int(task.agent_timeout_sec)
        install_block = skip_baked_agent_install_cmd(
            f"""
            echo "[1/2] 安装 AutoGen Magentic-One..."
            if ! AGENTS_CACHE_DIR="{AGENTS_CACHE_DEST}" sh "$INSTALL_SH"; then
                echo "[ERROR] AutoGen 安装失败"
                exit 1
            fi
            """
        )
        return textwrap.dedent(
            f"""\
            #!/bin/bash
            set -e
            {common_bootstrap()}
            {run_common_install_cmd()}

            INSTALL_SH="{INSTALL_SCRIPT_DEST}"
            if [ -f /run/env_install_auto.sh ]; then INSTALL_SH=/run/env_install_auto.sh; fi

            # Magentic-One 0.7.5 needs Python >=3.10 (official requires-python).
            if ! command -v python3 >/dev/null 2>&1 \\
                || ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
                echo "[ERROR] AutoGen Magentic-One 需要 Python >=3.10；当前: $(python3 -V 2>&1 || echo missing)"
                {mark_agent_phase_started_cmd()}
                START_TIME=$(date +%s)
                echo "{{\\"start_time\\": $START_TIME, \\"end_time\\": $START_TIME, \\"elapsed_seconds\\": 0, \\"exit_code\\": 1, \\"reason\\": \\"python_lt_3_10\\"}}" > /run/agent_stats.json
                exit 1
            fi

            {install_block}

            echo "[2/2] 运行 AutoGen Magentic-One (timeout {timeout}s)..."
            {start_xvfb_cmd()}
            cd /app
            {agent_only_timeout_banner(timeout)}
            {mark_agent_phase_started_cmd()}
            {venv_python_cmd()}
            echo "[INFO] AutoGen using PYBIN=$PYBIN ($($PYBIN -V 2>&1))"
            START_TIME=$(date +%s)
            set +e
            timeout {timeout} "$PYBIN" /run/run_autogen.py
            AGENT_EXIT=$?
            set -e
            END_TIME=$(date +%s)
            ELAPSED_SECONDS=$((END_TIME - START_TIME))
            echo "{{\\"start_time\\": $START_TIME, \\"end_time\\": $END_TIME, \\"elapsed_seconds\\": $ELAPSED_SECONDS, \\"exit_code\\": $AGENT_EXIT}}" > /run/agent_stats.json
            {stop_xvfb_cmd()}
            if [ "$AGENT_EXIT" -ne 0 ]; then
                echo "[ERROR] AutoGen 退出码: $AGENT_EXIT"
                exit "$AGENT_EXIT"
            fi
            echo "Agent 阶段完成"
            """
        )
