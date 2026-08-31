"""Claude Code adapter (DeepSeek Anthropic-compatible)."""

from __future__ import annotations

import json
import os
import shutil
import textwrap

from ..config import (
    ANTHROPIC_BASE_URL,
    API_KEYS,
    CLAUDE_CACHE_DIR,
    CLAUDE_INSTALL_SCRIPT_DEST,
    CLAUDE_INSTALL_SCRIPT_SRC,
    CLAUDE_PACKAGES_DEST,
    CLAUDE_SETTINGS_SRC,
)
from ..core import TaskInfo
from .base import AgentAdapter
from .base_utils import (
    ensure_lf_script_copy,
    resolve_required_host_dir,
    resolve_required_host_file,
)
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
)


class ClaudeCodeAdapter(AgentAdapter):
    id = "claude-code"
    display_name = "claude-code"

    def prepare_run_dir(self, run_dir: str, task: TaskInfo) -> None:
        os.makedirs(run_dir, exist_ok=True)
        prepare_common_files(run_dir)
        ensure_lf_script_copy(
            CLAUDE_INSTALL_SCRIPT_SRC,
            os.path.join(run_dir, "env_install_claude.sh"),
        )
        shutil.copy2(task.instruction_path, os.path.join(run_dir, "prompt.txt"))
        with open(CLAUDE_SETTINGS_SRC, "r", encoding="utf-8-sig") as fh:
            settings = json.load(fh)
        env = settings.setdefault("env", {})
        # DeepSeek /anthropic accepts Bearer via ANTHROPIC_AUTH_TOKEN.
        env.pop("ANTHROPIC_API_KEY", None)
        env["ANTHROPIC_AUTH_TOKEN"] = API_KEYS["claude-code"]
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL.rstrip("/")
        env.setdefault("ANTHROPIC_MODEL", "deepseek-v4-flash-vision-exp[1m]")
        env.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", "deepseek-v4-flash-vision-exp[1m]")
        env.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", "deepseek-v4-flash-vision-exp[1m]")
        env.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", "deepseek-v4-flash-vision-exp[1m]")
        env.setdefault("CLAUDE_CODE_SUBAGENT_MODEL", "deepseek-v4-flash-vision-exp[1m]")
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        # Thinking on DeepSeek Anthropic endpoint at effort=low.
        # CLAUDE_CODE_EFFORT_LEVEL maps to reasoning_effort / output_config.effort.
        env["CLAUDE_CODE_EFFORT_LEVEL"] = "low"
        env.pop("MAX_THINKING_TOKENS", None)
        # DeepSeek 1M context; avoid Claude Code default 32k output cap.
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "1000000"
        env.setdefault("IS_SANDBOX", "1")
        settings["permissions"] = {"defaultMode": "bypassPermissions"}
        settings["skipDangerousModePermissionPrompt"] = True
        out = os.path.join(run_dir, "claude_settings.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    def install_mounts(self) -> list[tuple[str, str, str]]:
        install_script = resolve_required_host_file(
            CLAUDE_INSTALL_SCRIPT_SRC, "env_install_claude.sh"
        )
        claude_cache = resolve_required_host_dir(
            CLAUDE_CACHE_DIR, "Claude Code 缓存", env_var="CLAUDE_CACHE_DIR"
        )
        return merge_mounts(
            common_install_mounts(),
            [
                (install_script, CLAUDE_INSTALL_SCRIPT_DEST, "ro"),
                (claude_cache, CLAUDE_PACKAGES_DEST, "ro"),
            ],
        )

    def build_entryscript(self, task: TaskInfo) -> str:
        timeout = int(task.agent_timeout_sec)
        return textwrap.dedent(
            f"""\
            #!/bin/bash
            set -e
            {common_bootstrap()}
            {run_common_install_cmd()}

            INSTALL_SH="{CLAUDE_INSTALL_SCRIPT_DEST}"
            if [ -f /run/env_install_claude.sh ]; then INSTALL_SH=/run/env_install_claude.sh; fi
            {skip_baked_agent_install_cmd(f'''
            echo "[1/3] 安装 Claude Code agent..."
            if ! CLAUDE_PACKAGES_DIR="{CLAUDE_PACKAGES_DEST}" sh "$INSTALL_SH"; then
                echo "[ERROR] Claude Code 安装失败"
                exit 1
            fi
            ''')}

            echo "[2/3] 配置 Claude settings..."
            mkdir -p "$HOME/.claude"
            cp /run/claude_settings.json "$HOME/.claude/settings.json"
            if command -v python3 >/dev/null 2>&1; then
              python3 -c 'import json,shlex; s=json.load(open("/run/claude_settings.json",encoding="utf-8")); env=s.get("env") or dict(); open("/tmp/claude_env.sh","w",encoding="utf-8").write("\\n".join("export %s=%s"%(k,shlex.quote(str(v))) for k,v in env.items())+"\\n")'
              # shellcheck disable=SC1091
              . /tmp/claude_env.sh
            fi
            unset ANTHROPIC_API_KEY || true

            echo "[3/3] 运行 Claude Code (timeout {timeout}s)..."
            {start_xvfb_cmd()}
            cd /app
            {agent_only_timeout_banner(timeout)}
            {mark_agent_phase_started_cmd()}
            START_TIME=$(date +%s)
            set +e
            # -- ends option parsing: many TB prompts start with "- " list markers.
            timeout {timeout} env IS_SANDBOX=1 claude -p -- "$(cat /run/prompt.txt)" --dangerously-skip-permissions
            AGENT_EXIT=$?
            set -e
            END_TIME=$(date +%s)
            ELAPSED_SECONDS=$((END_TIME - START_TIME))
            echo "{{\\"start_time\\": $START_TIME, \\"end_time\\": $END_TIME, \\"elapsed_seconds\\": $ELAPSED_SECONDS, \\"exit_code\\": $AGENT_EXIT}}" > /run/agent_stats.json
            {stop_xvfb_cmd()}
            if [ "$AGENT_EXIT" -ne 0 ]; then
                echo "[ERROR] claude 退出码: $AGENT_EXIT"
                exit "$AGENT_EXIT"
            fi
            echo "Agent 阶段完成"
            """
        )
