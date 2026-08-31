# Terminal-Bench 2.1 多 Agent 评测脚手架

在 **Terminal-Bench 2.1** 题库上，用 **自研 Docker harness** 评测：

- **logorythia（神衍）**
- **swe-agent**
- **AutoGen**（Microsoft AgentChat 0.7 / Magentic-One，不是 `pip install ag2` 1.0）
- **AgentFlow**
- **Claude Code**

模型统一为 `deepseek-v4-flash-vision-exp`。指标为官方 Acc（separate verifier 写出的 `/logs/verifier/reward.txt == 1`）。

默认题库目录：本仓库 `tasks/`（从 Terminal-Bench 2.1 拷入，**89 题**）。不依赖 Harbor CLI。

## 快速开始

```powershell
.\setup_venv.ps1
.\.venv\Scripts\Activate.ps1

# 缓存服务（apt/dnf 走 Squid→Clash；pip 走 BuildKit id=tb2-pip + Clash；git/curl/LLM 走 Clash）
.\.cache\start_dnf_cacher.ps1
# AgentFlow 源码；可选把主机 .cache/pip 灌进 BuildKit id=tb2-pip（灌完可删主机目录）
.\.cache\cache_agent_packages.ps1

python run_bench.py 1 --logorythia --swe --auto --agentflow --claude
```

可选：预拉题库 `docker_image` / `FROM`：

```powershell
python scripts/pull_tb2_base_images.py
```

### 命令说明

```text
python run_bench.py <start> [--end M] [--logorythia] [--swe] [--auto] [--agentflow] [--claude] [--redo]
```

| 参数 | 含义 |
| --- | --- |
| `<start>` | 从第 N 题开始（**1-based**，按题名 ASCII 字典序） |
| `--end M` | 跑到第 M 题结束（含） |
| `--logorythia` 等 | 本轮跑哪些 agent（至少选一个） |
| `--redo` | 强制重跑已在 `eval_results.json` 中的题 |
| `--n-concurrent` | 同题多 agent 并发（默认=本轮 pending 数；`1` 串行） |

## 目录说明

```text
terminal-bench-2/
  run_bench.py                 # 入口
  harness/                     # DIY 编排、缓存、runtime、agents
  tasks/                       # TB 2.1 题库
  traj/<agent>/<task>/         # 每题轨迹与结果
  .cache/                      # Squid / pip / uv / vep / agent 源码缓存
  env_install_common.sh        # 烤进共享镜像 common-env
  env_install_*.sh             # agent 专属安装
  syAgentInfo.json             # 神衍模型配置
  claude_settings.json         # Claude Code 配置
```

汇总 Acc：`traj/<agent>/eval_results.json`。

## 架构要点

1. **Agent 阶段**：任务 `environment/Dockerfile` 或 `docker_image` → 烤 `env_install_common.sh` 成 `common-env` → 再烤一层 `{task}:{agent}`（`env_install_*.sh`，pip 走同一块 BuildKit 缓存 `id=tb2-pip`）。运行时看到 `/opt/tb2-agent.ok` 就跳过安装。Python &lt; 3.10 的题对 AutoGen / swe-agent 不烤这一层，仍用 common-env 并记 `python_lt_3_10`。
2. **计分**：`[[verifier.collect]]` → 按 `artifacts` 拷贝（Harbor **best-effort**）→ 独立 `tests/Dockerfile` verifier → `test.sh` → `reward.txt` 或 `reward.json`。
3. **Acc 语义（对齐 Harbor）**：
   - 优先读 `reward.txt`，否则读 `reward.json` 的 `"reward"`；`== 1` → PASS；`== 0`（或其它非 1 数值）→ FAIL（计入 Acc）。
   - **缺声明 artifact**：跳过拷贝、仍跑 verifier，通常得到 `reward=0` → **FAIL / Acc=0**（不是 ERROR）。
   - **缺失/非法 reward 文件**：Harbor 式 **ERROR**（`reward=null` / `verifier_error`），**不重试**。
   - **无限重试**仅用于镜像 pull/build（`retry_until_image`）以及 verifier 容器 `docker run` / `InfraFailure`（网络/infra）；verify timeout 与缺 reward 均不重试。
4. **代理 / 包缓存**（题库 `tasks/` 不改；只动构建副本）：
   - **FROM / `docker pull`**：Clash `127.0.0.1:7897`，**不走** Squid。`--pull=false`：本地已有底图就用，缺了再拉 Hub。
   - **Dockerfile `RUN` 的 git/curl/其他**：`HTTP_PROXY` → Clash `host.docker.internal:7897`。
   - **构建 + 运行/测试里的 apt / dnf**：Squid `host.docker.internal:3144`（`.\.cache\start_dnf_cacher.ps1`），Squid 上游是 Clash。通过 `TB2_PKG_PROXY` 写 apt.conf / dnf.conf，**不**把 Squid 设成全局 `HTTP_PROXY`。Squid 未启动时 apt/dnf 也回退 Clash。
   - **pip**：官方 PyPI，经 Clash；构建用 BuildKit 缓存 `id=tb2-pip`。每题结束会 `docker builder prune -af --filter type!=exec.cachemount`（清层缓存、保留 pip cache mount）。不要无过滤的 `prune -af`。主机 `.cache/pip` 用于灌种，**建议留着当备份**（Docker Desktop **Clean / Purge data** 会把 `id=tb2-pip` 和所有 Hub 底图一起清掉）。
5. 每题全部 agent 结束后删除本题 `tb2-local` 镜像（含 `:common-env` 与 agent 层），清理构建层缓存，**保留** `id=tb2-pip` 与预拉的 Hub 底图。
   - WSL 的 `docker_data.vhdx` 删了内部数据也不会自动缩小。空闲时只做 **compact**（`Optimize-VHD` / `diskpart compact`，或 Docker Desktop 里单独的 Compact VHD）。
   - **不要**点 Troubleshoot → **Clean / Purge data**：那会清空镜像、容器、BuildKit（含 `id=tb2-pip`），不是压缩虚拟盘。
   - **不要** `docker image prune -a`：会把预拉的 Hub 底图删掉。
