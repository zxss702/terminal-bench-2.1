"""mini-swe-agent / sweagent adapter via OpenAI-compatible DeepSeek."""

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

INSTALL_SCRIPT_SRC = os.path.join(PROJECT_ROOT, "env_install_swe.sh")
INSTALL_SCRIPT_DEST = "/env_install_swe.sh"


class SweAgentAdapter(AgentAdapter):
    id = "swe-agent"
    display_name = "swe-agent"

    def prepare_run_dir(self, run_dir: str, task: TaskInfo) -> None:
        os.makedirs(run_dir, exist_ok=True)
        prepare_common_files(run_dir)
        shutil.copy2(task.instruction_path, os.path.join(run_dir, "prompt.txt"))
        cfg = {
            "model": DEEPSEEK_MODEL,
            "base_url": openai_compatible_base_url(),
            "api_key": API_KEYS["swe-agent"],
            "workdir": "/app",
        }
        with open(os.path.join(run_dir, "agent_config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        # Runner: only mini-swe-agent (no ReAct / alternate fallbacks).
        runner = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, subprocess, shutil, sys
            from pathlib import Path

            cfg = json.loads(Path("/run/agent_config.json").read_text(encoding="utf-8"))
            prompt = Path("/run/prompt.txt").read_text(encoding="utf-8")
            workdir = cfg.get("workdir", "/app")
            os.chdir(workdir)

            model = cfg["model"]
            model_name = model if "/" in model else f"openai/{model}"
            api_key = cfg["api_key"]
            base_url = cfg["base_url"]

            os.environ["OPENAI_API_KEY"] = api_key
            os.environ["OPENAI_BASE_URL"] = base_url
            os.environ["OPENAI_API_BASE"] = base_url
            os.environ["MSWEA_MODEL_NAME"] = model_name
            os.environ["MSWEA_CONFIGURED"] = "true"
            # deepseek-v4-flash-vision-exp is not in litellm price map
            os.environ["MSWEA_COST_TRACKING"] = "ignore_errors"

            cfg_dir = Path.home() / ".config" / "mini-swe-agent"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / ".env").write_text(
                "\\n".join([
                    "MSWEA_CONFIGURED=true",
                    "MSWEA_COST_TRACKING=ignore_errors",
                    f"MSWEA_MODEL_NAME={model_name}",
                    f"OPENAI_API_KEY={api_key}",
                    f"OPENAI_BASE_URL={base_url}",
                    f"OPENAI_API_BASE={base_url}",
                    "",
                ]),
                encoding="utf-8",
            )

            # mini-swe-agent v2: a lone -c file replaces defaults and drops
            # required agent templates. Load builtin "mini" then overlay model.
            agent_cfg = Path("/run/mini_swe_config.yaml")
            # DeepSeek V4: thinking enabled at reasoning_effort=low.
            agent_cfg.write_text(
                "\\n".join([
                    "agent:",
                    "  cost_limit: 0",
                    "  step_limit: 0",
                    "model:",
                    f"  model_name: {model_name!r}",
                    "  model_kwargs:",
                    "    drop_params: true",
                    '    reasoning_effort: "low"',
                    "    extra_body:",
                    "      thinking:",
                    '        type: "enabled"',
                    "",
                ]),
                encoding="utf-8",
            )

            mini_bin = shutil.which("mini") or shutil.which("mini-swe-agent")
            if not mini_bin:
                print("[ERROR] mini / mini-swe-agent not found on PATH", flush=True)
                raise SystemExit(1)

            argv = [
                mini_bin, "-y", "--exit-immediately",
                "-c", "mini.yaml",
                "-c", str(agent_cfg),
                "-m", model_name,
                "-t", prompt,
            ]
            print(f"[INFO] model={model_name} base_url={base_url} thinking=enabled effort=low", flush=True)
            print(
                f"[INFO] exec: {mini_bin} -y --exit-immediately -c mini.yaml -c {agent_cfg} "
                f"-m {model_name} -t <prompt>",
                flush=True,
            )
            rc = subprocess.call(argv, env=os.environ.copy())
            if rc != 0:
                print(f"[ERROR] mini-swe-agent exited {rc}", flush=True)
            raise SystemExit(rc)
            """
        )
        runner_path = os.path.join(run_dir, "run_swe_agent.py")
        with open(runner_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(runner)
        ensure_lf_script_copy(INSTALL_SCRIPT_SRC, os.path.join(run_dir, "env_install_swe.sh"))

    def install_mounts(self) -> list[tuple[str, str, str]]:
        install_script = resolve_required_host_file(INSTALL_SCRIPT_SRC, "env_install_swe.sh")
        return merge_mounts(
            common_install_mounts(),
            [(install_script, INSTALL_SCRIPT_DEST, "ro")],
        )

    def build_entryscript(self, task: TaskInfo) -> str:
        timeout = int(task.agent_timeout_sec)
        install_block = skip_baked_agent_install_cmd(
            f"""
            echo "[1/2] 安装 swe-agent..."
            if ! AGENTS_CACHE_DIR="{AGENTS_CACHE_DEST}" sh "$INSTALL_SH"; then
                echo "[ERROR] swe-agent 安装失败"
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
            if [ -f /run/env_install_swe.sh ]; then INSTALL_SH=/run/env_install_swe.sh; fi

            # mini-swe-agent needs Python >=3.10: score Acc=0 once (no install-retry loop).
            if ! command -v python3 >/dev/null 2>&1 \\
                || ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
                echo "[ERROR] swe-agent 需要 Python >=3.10；当前: $(python3 -V 2>&1 || echo missing)"
                {mark_agent_phase_started_cmd()}
                START_TIME=$(date +%s)
                echo "{{\\"start_time\\": $START_TIME, \\"end_time\\": $START_TIME, \\"elapsed_seconds\\": 0, \\"exit_code\\": 1, \\"reason\\": \\"python_lt_3_10\\"}}" > /run/agent_stats.json
                exit 1
            fi

            {install_block}

            echo "[2/2] 运行 swe-agent (timeout {timeout}s)..."
            {start_xvfb_cmd()}
            cd /app
            {agent_only_timeout_banner(timeout)}
            {mark_agent_phase_started_cmd()}
            {venv_python_cmd()}
            START_TIME=$(date +%s)
            set +e
            timeout {timeout} "$PYBIN" /run/run_swe_agent.py
            AGENT_EXIT=$?
            set -e
            END_TIME=$(date +%s)
            ELAPSED_SECONDS=$((END_TIME - START_TIME))
            echo "{{\\"start_time\\": $START_TIME, \\"end_time\\": $END_TIME, \\"elapsed_seconds\\": $ELAPSED_SECONDS, \\"exit_code\\": $AGENT_EXIT}}" > /run/agent_stats.json
            {stop_xvfb_cmd()}
            if [ "$AGENT_EXIT" -ne 0 ]; then
                echo "[ERROR] swe-agent 退出码: $AGENT_EXIT"
                exit "$AGENT_EXIT"
            fi
            echo "Agent 阶段完成"
            """
        )
