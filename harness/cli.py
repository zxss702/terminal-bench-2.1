"""CLI for Terminal-Bench 2.1 multi-agent DIY Docker runs."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys

from .agent_packages_cache import (
    agent_packages_status_line,
    pip_cache_status_line,
    resolve_agentflow_src_ready,
)
from .dnf_cacher import dnf_cacher_status_line
from .uv_cache import uv_cache_status_line
from .vep_apis_cache import vep_apis_cache_status_line
from .config import (
    AGENT_FLAG_MAP,
    AGENT_INFO_SRC,
    CLAUDE_CACHE_DIR,
    CLAUDE_SETTINGS_SRC,
    LOGORYTHIA_PACKAGE_SRC,
    PROJECT_ROOT,
    TASKS_DIR,
    TRAJ_DIR,
)
from .console import bold, cyan, green, red, yellow
from .core import (
    accuracy,
    is_task_done,
    load_all_tasks,
    load_eval_results,
    reconcile_eval_results,
)
from .runtime import cleanup_task_artifacts, prepare_shared_image, run_agent_on_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terminal-Bench 2.1 multi-agent evaluation (Windows + Docker DIY)",
    )
    parser.add_argument(
        "start",
        type=int,
        help="从第 N 题开始（1-based，按题名字典序）",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="跑到第 M 题结束（含；1-based）。省略则一直跑到最后一题。",
    )
    parser.add_argument("--logorythia", action="store_true", help="跑神衍")
    parser.add_argument("--swe", action="store_true", help="跑 swe-agent")
    parser.add_argument("--auto", action="store_true", help="跑 AutoGen")
    parser.add_argument("--agentflow", action="store_true", help="跑 AgentFlow")
    parser.add_argument("--claude", action="store_true", help="跑 Claude Code")
    parser.add_argument(
        "--redo",
        action="store_true",
        help="强制重跑：忽略 eval_results，并强制重跑 agent（不 resume verify-only）",
    )
    parser.add_argument(
        "--n-concurrent",
        type=int,
        default=0,
        help="同题多 agent 并发数（默认=pending agent 数；设 1 串行）",
    )
    return parser


def selected_agents(args) -> list[str]:
    agents = []
    for flag, agent_id in AGENT_FLAG_MAP.items():
        if getattr(args, flag, False):
            agents.append(agent_id)
    return agents


def ensure_prerequisites(agents: list[str]) -> None:
    docker_check = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if docker_check.returncode != 0:
        print(f"{red('[FATAL]')} Docker 不可用，请确保 Docker Desktop 正在运行")
        sys.exit(1)

    if "logorythia" in agents:
        if not os.path.isfile(LOGORYTHIA_PACKAGE_SRC):
            print(f"{red('[FATAL]')} 缺少 {LOGORYTHIA_PACKAGE_SRC}")
            sys.exit(1)
        if not os.path.isfile(AGENT_INFO_SRC):
            print(f"{red('[FATAL]')} 缺少 {AGENT_INFO_SRC}")
            sys.exit(1)
    if "claude-code" in agents:
        if not os.path.isfile(CLAUDE_SETTINGS_SRC):
            print(f"{red('[FATAL]')} 缺少 {CLAUDE_SETTINGS_SRC}")
            sys.exit(1)
        cache = os.path.abspath(os.path.expanduser(CLAUDE_CACHE_DIR))
        if os.path.isfile(cache):
            print(f"{red('[FATAL]')} Claude 缓存路径是文件而非目录: {cache}")
            sys.exit(1)
        if not os.path.isdir(cache):
            print(
                f"{red('[FATAL]')} Claude 缓存目录不存在: {cache}\n"
                f"  请先运行: .\\.cache\\cache_claude.ps1"
            )
            sys.exit(1)
        claude_bin = os.path.join(cache, "linux-x64", "claude")
        if not os.path.isfile(claude_bin):
            print(
                f"{red('[FATAL]')} 缺少 Claude 二进制: {claude_bin}\n"
                f"  请先运行: .\\.cache\\cache_claude.ps1"
            )
            sys.exit(1)
    if "agentflow" in agents:
        install = os.path.join(PROJECT_ROOT, "env_install_agentflow.sh")
        if not os.path.isfile(install):
            print(f"{red('[FATAL]')} 缺少 {install}")
            sys.exit(1)
        if not resolve_agentflow_src_ready():
            print(
                f"{red('[FATAL]')} 缺少 AgentFlow 源码缓存 (.cache/agents/agentflow)\n"
                f"  请先运行: .\\.cache\\cache_agent_packages.ps1"
            )
            sys.exit(1)


def print_acc_summary(agents: list[str], denom: int) -> None:
    print(bold("\n=== Acc 汇总 ==="))
    for agent_id in agents:
        results = load_eval_results(agent_id)
        passed, completed, acc = accuracy(results, exclude_skipped=True)
        print(
            f"  {agent_id}: {green(str(passed))}/{completed} scored "
            f"| Acc={acc:.4f} | progress {completed}/{denom}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    agents = selected_agents(args)
    if not agents:
        parser.error(
            "请至少指定一个 agent: --logorythia --swe --auto --agentflow --claude"
        )

    ensure_prerequisites(agents)
    os.makedirs(TRAJ_DIR, exist_ok=True)

    tasks = load_all_tasks()
    if not tasks:
        print(f"{red('[FATAL]')} 未找到任务目录: {TASKS_DIR}")
        sys.exit(1)

    if args.start < 1 or args.start > len(tasks):
        print(f"{red('错误:')} 起始索引 {args.start} 超出范围 [1, {len(tasks)}]")
        sys.exit(1)

    end = args.end if args.end is not None else len(tasks)
    if end < 1 or end > len(tasks):
        print(f"{red('错误:')} 结束索引 {end} 超出范围 [1, {len(tasks)}]")
        sys.exit(1)
    if end < args.start:
        print(f"{red('错误:')} --end ({end}) 不能小于 start ({args.start})")
        sys.exit(1)

    todo = tasks[args.start - 1 : end]
    denom = len(tasks)

    print(bold("Terminal-Bench 2.1 DIY harness"))
    print(f"  项目: {PROJECT_ROOT}")
    print(f"  题库: {TASKS_DIR}")
    print(
        f"  题数: {len(tasks)} runnable | 区间 [{args.start}, {end}] "
        f"({todo[0].name} .. {todo[-1].name})，本轮 {len(todo)} 题"
    )
    print(f"  Agents: {', '.join(agents)}")
    print(f"  {dnf_cacher_status_line()}")
    print(f"  {pip_cache_status_line()}")
    print(f"  {uv_cache_status_line()}")
    print(f"  {vep_apis_cache_status_line()}")
    print(f"  {agent_packages_status_line()}")
    print(f"  redo={args.redo}")
    print(
        "  构建网络: FROM/pull→Clash；RUN HTTP_PROXY→Clash；仅 apt/dnf→Squid(:3144)；"
        "pip→BuildKit id=tb2-pip + Clash"
    )
    print("  计分: collect → artifacts(best-effort) → verifier → reward.txt|reward.json")
    print(
        "  Acc: reward=1 PASS / 0 FAIL；缺 artifact→FAIL；"
        "缺/非法 reward→ERROR(不重试)；仅镜像 ensure/起容器可无限重试"
    )
    print(
        "  Resume: 有 run/agent_stats.json 时跳过 agent，直接走 verify（--redo 强制重跑 agent）"
    )
    print(
        "  每题结束后删除本题镜像并清理构建缓存（保留 BuildKit pip 缓存 id=tb2-pip；Hub 底图不删）"
    )
    print(
        "  腾盘: 只 compact vhdx / 带过滤的 builder prune；"
        "不要 Docker Desktop Clean/Purge data（会清空镜像和 tb2-pip）"
    )

    print(cyan("  核对 eval_results.json ← 各题 stats.json ..."))
    for agent_id in agents:
        before = load_eval_results(agent_id)
        after = reconcile_eval_results(agent_id)
        print(f"    {agent_id}: {len(before)} → {len(after)} recorded")

    print_acc_summary(agents, denom)

    for task in todo:
        print(bold(f"\n[{task.index}/{len(tasks)}] {task.name}"))

        if not args.redo:
            pending = [a for a in agents if not is_task_done(a, task.name)]
            skipped = [a for a in agents if a not in pending]
            if skipped:
                print(yellow(f"  跳过已完成: {', '.join(skipped)}"))
            if not pending:
                print(yellow("  全部 agent 已完成，跳过（加 --redo 强制重跑）"))
                print_acc_summary(agents, denom)
                continue
        else:
            pending = list(agents)

        print(cyan("  构建/拉取任务镜像，并预装 common-env 与各 agent 层..."))
        print(cyan(f"  构建日志: traj/_shared/{task.name}/image.log"))
        image = prepare_shared_image(task, pending)
        print(f"  image: {image}")
        if task.needs_compose:
            print(cyan("  本题使用 docker-compose（agent 注入 main）"))

        def _run_one(agent_id: str):
            print(cyan(f"  → {agent_id} 开始"))
            try:
                stats = run_agent_on_task(
                    task, agent_id, image, force_agent=args.redo
                )
                mark = green("PASS") if stats["passed"] else red("FAIL")
                resume_note = " [verify-only]" if stats.get("resumed_verify") else ""
                print(
                    f"  ← {agent_id} {mark}{resume_note} "
                    f"elapsed={stats['elapsed_seconds']}s "
                    f"attempts={stats['agent_attempts']}/{stats['verify_attempts']}"
                )
                return stats
            except Exception as exc:
                print(f"  ← {agent_id} {red('ERROR')}: {exc}")
                raise

        workers = args.n_concurrent if args.n_concurrent > 0 else len(pending)
        workers = max(1, min(workers, len(pending)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, a): a for a in pending}
            for fut in concurrent.futures.as_completed(futures):
                agent_id = futures[fut]
                try:
                    fut.result()
                except Exception:
                    print(f"  {red('agent failed')}: {agent_id}")

        print(
            cyan(
                "  清理本题容器/镜像与构建缓存（保留 BuildKit pip 缓存 id=tb2-pip）..."
            )
        )
        cleanup_task_artifacts(task, image, pending)
        print_acc_summary(agents, denom)

    print(bold("\n全部完成"))
    print_acc_summary(agents, denom)


if __name__ == "__main__":
    main()
