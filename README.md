# 神衍 2×2 消融评测脚手架

在 **Terminal-Bench 2.1** 题库上，用 **自研 Docker harness** 跑神衍（Logorythia）四变体消融：

| 变体 | 安装包 | 配置 |
| --- | --- | --- |
| `logorythia1` (`--sy1`) | `logorythia_linux_x861.zip` | `syAgentInfo1.json` |
| `logorythia2` (`--sy2`) | `logorythia_linux_x862.zip` | `syAgentInfo2.json` |
| `logorythia3` (`--sy3`) | `logorythia_linux_x863.zip` | `syAgentInfo3.json` |
| `logorythia4` (`--sy4`) | `logorythia_linux_x864.zip` | `syAgentInfo4.json` |

每题每个变体独立镜像/容器；配置挂进容器后仍是 `/root/Documents/Logorythia/syAgentInfo.json`。

题库默认本仓库 `tasks/`（89 题）。每次运行用 **固定 seed=42 随机抽 30 题**（可复现）。不依赖 Harbor CLI。

## 快速开始

```powershell
.\setup_venv.ps1
.\.venv\Scripts\Activate.ps1

# apt/dnf 走 Squid→Clash；pip 走 BuildKit id=tb2-pip + Clash
.\.cache\start_dnf_cacher.ps1

# 默认：seed=42 抽 30 题，四变体全跑
python run_bench.py

# 只跑抽样列表的前 8 题
python run_bench.py 1 --end 8

# 只跑某一臂 / 多臂
python run_bench.py --sy1
python run_bench.py 1 --end 8 --sy1 --sy3 --n-concurrent 2
```

可选：预拉题库 `docker_image` / `FROM`：

```powershell
python scripts/pull_tb2_base_images.py
```

### 命令说明

```text
python run_bench.py [start] [--end M] [--sy1] [--sy2] [--sy3] [--sy4] [--redo] [--n-concurrent N]
```

| 参数 | 含义 |
| --- | --- |
| `start` | 从抽样列表第 N 题开始（**1-based**，默认 1） |
| `--end M` | 跑到抽样列表第 M 题结束（含）；省略则到抽样末尾 |
| `--sy1`…`--sy4` | 本轮跑哪些变体；**都不写则四个全跑** |
| `--redo` | 强制重跑已在 `eval_results.json` 中的题 |
| `--n-concurrent` | 同题多变体并发（默认=本轮 pending 数；`1` 串行） |

抽样常量在 `harness/config.py`：`SAMPLE_SIZE=30`，`SAMPLE_SEED=42`。区间是对这 30 题切的，不是对全库 89 题。

## 目录说明

```text
terminal-bench-2x2/
  run_bench.py                 # 入口
  harness/                     # DIY 编排、缓存、runtime、agents
  tasks/                       # TB 2.1 题库
  traj/logorythiaN/<task>/     # 各变体轨迹与结果
  .cache/                      # Squid / pip / uv / vep 缓存
  env_install_common.sh        # 烤进共享镜像 common-env
  env_install_logorythia.sh    # 神衍安装（四变体共用）
  logorythia_linux_x86N.zip    # 四份安装包
  syAgentInfoN.json            # 四份神衍配置（拷进容器后改名为 syAgentInfo.json）
```

汇总 Acc：`traj/logorythiaN/eval_results.json`。

## 架构要点

1. **Agent 阶段**：任务 `environment/Dockerfile` 或 `docker_image` → 烤 `env_install_common.sh` 成 `common-env` → 再烤一层 `{task}:logorythiaN`（对应 zip + `env_install_logorythia.sh`）。运行时看到 `/opt/tb2-agent.ok` 就跳过安装。
2. **计分**：`[[verifier.collect]]` → 按 `artifacts` 拷贝（Harbor **best-effort**）→ 独立 `tests/Dockerfile` verifier → `test.sh` → `reward.txt` 或 `reward.json`。
3. **Acc 语义（对齐 Harbor）**：
   - 优先读 `reward.txt`，否则读 `reward.json` 的 `"reward"`；`== 1` → PASS；`== 0`（或其它非 1 数值）→ FAIL（计入 Acc）。
   - **缺声明 artifact**：跳过拷贝、仍跑 verifier，通常得到 `reward=0` → **FAIL / Acc=0**（不是 ERROR）。
   - **缺失/非法 reward 文件**：Harbor 式 **ERROR**（`reward=null` / `verifier_error`），**不重试**。
   - **无限重试**仅用于镜像 pull/build（`retry_until_image`）以及 verifier 容器 `docker run` / `InfraFailure`（网络/infra）；verify timeout 与缺 reward 均不重试。
4. **代理 / 包缓存**（题库 `tasks/` 不改；只动构建副本）：
   - **FROM / `docker pull`**：Clash `127.0.0.1:7897`，**不走** Squid。`--pull=false`：本地已有底图就用，缺了再拉 Hub。
   - **Dockerfile `RUN` 的 git/curl/其他**：`HTTP_PROXY` → Clash `host.docker.internal:7897`。
   - **构建 + 运行/测试里的 apt / dnf**：Squid `host.docker.internal:3144`（`.\.cache\start_dnf_cacher.ps1`），Squid 上游是 Clash。
   - **pip**：官方 PyPI，经 Clash；构建用 BuildKit 缓存 `id=tb2-pip`。每题结束会 `docker builder prune -af --filter type!=exec.cachemount`。不要无过滤的 `prune -af`。
5. 每题全部变体结束后删除本题 `tb2-local` 镜像（含 `:common-env` 与 agent 层），清理构建层缓存，**保留** `id=tb2-pip` 与预拉的 Hub 底图。
   - **不要**点 Troubleshoot → **Clean / Purge data**：会清空镜像、容器、BuildKit（含 `id=tb2-pip`）。
   - **不要** `docker image prune -a`：会把预拉的 Hub 底图删掉。
