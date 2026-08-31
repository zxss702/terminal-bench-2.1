"""CLI for 神衍 2×2 消融评测 (Terminal-Bench 2.1 DIY Docker)."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys

from .config import (
    AGENT_FLAG_MAP,
    ALL_AGENT_IDS,
    LOGORYTHIA_VARIANT_BY_ID,
    LOGORYTHIA_VARIANTS,
    PIP_CACHE_DIR,
    PROJECT_ROOT,
    SAMPLE_SEED,
    SAMPLE_SIZE,
    TASKS_DIR,
    TRAJ_DIR,
)
from .console import bold, cyan, green, red, yellow
from .core import (
    accuracy,
    is_task_done,
    list_task_names,
    load_eval_results,
    reconcile_eval_results,
    sample_tasks,
)
from .dnf_cacher import dnf_cacher_status_line
from .runtime import cleanup_task_artifacts, prepare_shared_image, run_agent_on_task
from .uv_cache import uv_cache_status_line
from .vep_apis_cache import vep_apis_cache_status_line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="神衍 2×2 消融评测 (Windows + Docker DIY)",
    )
    parser.add_argument(
        "start",
        type=int,
        nargs="?",
        default=1,
        help="从抽样列表第 N 题开始（1-based；默认 1）",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="跑到抽样列表第 M 题结束（含；1-based）。省略则一直跑到抽样末尾。",
    )
    for variant in LOGORYTHIA_VARIANTS:
        parser.add_argument(
            f"--{variant.flag}",
            action="store_true",
            help=f"跑 {variant.agent_id}（{os.path.basename(variant.package_src)} + "
            f"{os.path.basename(variant.info_src)}）",
        )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="强制重跑：忽略 eval_results，并强制重跑 agent（不 resume verify-only）",
    )
    parser.add_argument(
        "--n-concurrent",
        type=int,
        default=0,
        help="同题多变体并发数（默认=pending 数；设 1 串行）",
    )
    return parser


def selected_agents(args) -> list[str]:
    agents = []
    for flag, agent_id in AGENT_FLAG_MAP.items():
        if getattr(args, flag, False):
            agents.append(agent_id)
    if not agents:
        return list(ALL_AGENT_IDS)
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

    for agent_id in agents:
        variant = LOGORYTHIA_VARIANT_BY_ID[agent_id]
        if not os.path.isfile(variant.package_src):
            print(f"{red('[FATAL]')} 缺少 {variant.package_src}")
            sys.exit(1)
        if not os.path.isfile(variant.info_src):
            print(f"{red('[FATAL]')} 缺少 {variant.info_src}")
            sys.exit(1)


def _pip_cache_status_line() -> str:
    ready = False
    if os.path.isdir(PIP_CACHE_DIR):
        for name in ("http", "http-v2", "wheels"):
            if os.path.isdir(os.path.join(PIP_CACHE_DIR, name)):
                ready = True
                break
    if ready:
        return (
            f"pip 缓存: 主机 {PIP_CACHE_DIR} 仍在"
            "（构建用 BuildKit id=tb2-pip；主机目录作备份，Clean/Purge 后可再灌）"
        )
    return (
        "pip 缓存: 构建走 BuildKit id=tb2-pip（清理时 prune type!=exec.cachemount，勿无过滤 -af）；"
        "未命中时走 Clash 拉官方 PyPI"
    )


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

    ensure_prerequisites(agents)
    os.makedirs(TRAJ_DIR, exist_ok=True)

    corpus = list_task_names()
    if not corpus:
        print(f"{red('[FATAL]')} 未找到任务目录: {TASKS_DIR}")
        sys.exit(1)

    try:
        sampled = sample_tasks(n=SAMPLE_SIZE, seed=SAMPLE_SEED)
    except ValueError as exc:
        print(f"{red('[FATAL]')} {exc}")
        sys.exit(1)

    if args.start < 1 or args.start > len(sampled):
        print(
            f"{red('错误:')} 起始索引 {args.start} 超出抽样范围 [1, {len(sampled)}]"
        )
        sys.exit(1)

    end = args.end if args.end is not None else len(sampled)
    if end < 1 or end > len(sampled):
        print(f"{red('错误:')} 结束索引 {end} 超出抽样范围 [1, {len(sampled)}]")
        sys.exit(1)
    if end < args.start:
        print(f"{red('错误:')} --end ({end}) 不能小于 start ({args.start})")
        sys.exit(1)

    todo = sampled[args.start - 1 : end]
    denom = len(sampled)

    print(bold("神衍 2×2 消融 harness"))
    print(f"  项目: {PROJECT_ROOT}")
    print(f"  题库: {TASKS_DIR}")
    print(
        f"  抽样: seed={SAMPLE_SEED} n={SAMPLE_SIZE} "
        f"（全库 {len(corpus)} 题）"
    )
    print(
        f"  区间: [{args.start}, {end}] "
        f"({todo[0].name} .. {todo[-1].name})，本轮 {len(todo)} 题"
    )
    print("  本轮题目:")
    for i, t in enumerate(todo, start=args.start):
        print(f"    [{i}/{denom}] corpus#{t.index} {t.name}")
    print(f"  Variants: {', '.join(agents)}")
    print(f"  {dnf_cacher_status_line()}")
    print(f"  {_pip_cache_status_line()}")
    print(f"  {uv_cache_status_line()}")
    print(f"  {vep_apis_cache_status_line()}")
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

    print(cyan("  核对 eval_results.json ← 各题 stats.json ..."))
    for agent_id in agents:
        before = load_eval_results(agent_id)
        after = reconcile_eval_results(agent_id)
        print(f"    {agent_id}: {len(before)} → {len(after)} recorded")

    print_acc_summary(agents, denom)

    for offset, task in enumerate(todo):
        sample_i = args.start + offset
        print(
            bold(
                f"\n[{sample_i}/{denom}] {task.name} (corpus #{task.index})"
            )
        )

        if not args.redo:
            pending = [a for a in agents if not is_task_done(a, task.name)]
            skipped = [a for a in agents if a not in pending]
            if skipped:
                print(yellow(f"  跳过已完成: {', '.join(skipped)}"))
            if not pending:
                print(yellow("  全部变体已完成，跳过（加 --redo 强制重跑）"))
                print_acc_summary(agents, denom)
                continue
        else:
            pending = list(agents)

        print(cyan("  构建/拉取任务镜像，并预装 common-env 与各变体层..."))
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
