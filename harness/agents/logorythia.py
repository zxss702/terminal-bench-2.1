"""Logorythia (神衍) adapter — per-variant zip + syAgentInfo, run CLI."""

from __future__ import annotations

import os
import shutil
import textwrap

from ..config import (
    AGENT_INFO_DEST,
    AGENT_INFO_DEST_DIR,
    LOGORYTHIA_INSTALL_SCRIPT_DEST,
    LOGORYTHIA_INSTALL_SCRIPT_SRC,
    LOGORYTHIA_PACKAGE_DEST,
    LogorythiaVariant,
)
from ..core import TaskInfo
from .base import AgentAdapter
from .base_utils import ensure_lf_script_copy, resolve_required_host_file
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


class LogorythiaAdapter(AgentAdapter):
    def __init__(self, variant: LogorythiaVariant) -> None:
        self.variant = variant
        self.id = variant.agent_id
        self.display_name = variant.agent_id

    def prepare_run_dir(self, run_dir: str, task: TaskInfo) -> None:
        os.makedirs(run_dir, exist_ok=True)
        prepare_common_files(run_dir)
        ensure_lf_script_copy(
            LOGORYTHIA_INSTALL_SCRIPT_SRC,
            os.path.join(run_dir, "env_install_logorythia.sh"),
        )
        shutil.copy2(task.instruction_path, os.path.join(run_dir, "prompt.txt"))

        # 变体配置原样拷贝；容器内目标文件名固定为 syAgentInfo.json。
        info_src = resolve_required_host_file(
            self.variant.info_src, f"{self.id} syAgentInfo"
        )
        shutil.copy2(info_src, os.path.join(run_dir, "syAgentInfo.json"))

    def install_mounts(self) -> list[tuple[str, str, str]]:
        package = resolve_required_host_file(
            self.variant.package_src,
            f"{self.id} 安装包",
        )
        install_script = resolve_required_host_file(
            LOGORYTHIA_INSTALL_SCRIPT_SRC, "env_install_logorythia.sh"
        )
        return merge_mounts(
            common_install_mounts(),
            [
                (install_script, LOGORYTHIA_INSTALL_SCRIPT_DEST, "ro"),
                (package, LOGORYTHIA_PACKAGE_DEST, "ro"),
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

            INSTALL_SH="{LOGORYTHIA_INSTALL_SCRIPT_DEST}"
            if [ -f /run/env_install_logorythia.sh ]; then
                INSTALL_SH=/run/env_install_logorythia.sh
            fi
            {skip_baked_agent_install_cmd(f'''
            echo "[1/3] 安装 logorythia agent ({self.id})..."
            if ! PACKAGE_PATH="{LOGORYTHIA_PACKAGE_DEST}" sh "$INSTALL_SH"; then
                echo "[ERROR] logorythia 安装失败"
                exit 1
            fi
            ''')}

            echo "[2/3] 配置 syAgentInfo -> {AGENT_INFO_DEST}"
            if [ ! -f /run/syAgentInfo.json ]; then
                echo "[ERROR] 缺少 /run/syAgentInfo.json"
                exit 1
            fi
            mkdir -p '{AGENT_INFO_DEST_DIR}'
            cp -f /run/syAgentInfo.json '{AGENT_INFO_DEST}'
            ls -la '{AGENT_INFO_DEST}'
            echo "[INFO] syAgentInfo 已就位"

            echo "[3/3] 运行: logorythia file /run/prompt.txt  (timeout {timeout}s)"
            if [ ! -f /run/prompt.txt ]; then
                echo "[ERROR] 缺少 /run/prompt.txt"
                exit 1
            fi
            {start_xvfb_cmd()}
            cd /app
            {agent_only_timeout_banner(timeout)}
            {mark_agent_phase_started_cmd()}
            START_TIME=$(date +%s)
            set +e
            if command -v stdbuf >/dev/null 2>&1; then
                stdbuf -oL -eL timeout {timeout} logorythia file /run/prompt.txt
            elif command -v timeout >/dev/null 2>&1; then
                timeout {timeout} logorythia file /run/prompt.txt
            else
                logorythia file /run/prompt.txt
            fi
            AGENT_EXIT=$?
            set -e
            END_TIME=$(date +%s)
            ELAPSED_SECONDS=$((END_TIME - START_TIME))
            echo "{{\\"start_time\\": $START_TIME, \\"end_time\\": $END_TIME, \\"elapsed_seconds\\": $ELAPSED_SECONDS, \\"exit_code\\": $AGENT_EXIT}}" > /run/agent_stats.json
            {stop_xvfb_cmd()}
            if [ "$AGENT_EXIT" -ne 0 ]; then
                echo "[ERROR] logorythia 退出码: $AGENT_EXIT"
                exit "$AGENT_EXIT"
            fi
            echo "Agent 阶段完成"
            """
        )
