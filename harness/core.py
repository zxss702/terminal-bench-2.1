"""Task discovery, traj paths, Acc aggregation."""

from __future__ import annotations

import json
import os
import random
import threading
from dataclasses import dataclass, field
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

from .config import SAMPLE_SEED, SAMPLE_SIZE, TASKS_DIR, TRAJ_DIR


_EVAL_LOCK = threading.Lock()


@dataclass
class CollectHook:
    command: str
    service: str = "main"
    timeout_sec: float = 60.0


@dataclass
class ArtifactSpec:
    source: str
    service: str = "main"
    exclude: list[str] = field(default_factory=list)


@dataclass
class TaskInfo:
    name: str
    index: int  # 1-based, alphabetical
    path: str
    instruction_path: str
    tests_dir: str
    environment_dir: str
    docker_image: str | None
    agent_timeout_sec: float
    verifier_timeout_sec: float
    build_timeout_sec: float
    cpus: int
    memory_mb: int
    storage_mb: int
    allow_internet: bool
    artifacts: list[ArtifactSpec] = field(default_factory=list)
    collect_hooks: list[CollectHook] = field(default_factory=list)
    verifier_env: dict[str, str] = field(default_factory=dict)
    verifier_cpus: int | None = None
    verifier_memory_mb: int | None = None
    environment_mode: str = "separate"
    schema_version: str = ""
    needs_compose: bool = False
    compose_file: str | None = None


def _parse_artifacts(raw: Any) -> list[ArtifactSpec]:
    out: list[ArtifactSpec] = []
    if not raw:
        return out
    for item in raw:
        if isinstance(item, str):
            out.append(ArtifactSpec(source=item))
        elif isinstance(item, dict):
            source = str(item.get("source") or "").strip()
            if not source:
                continue
            exclude = item.get("exclude") or []
            if isinstance(exclude, str):
                exclude = [exclude]
            out.append(
                ArtifactSpec(
                    source=source,
                    service=str(item.get("service") or "main"),
                    exclude=[str(x) for x in exclude],
                )
            )
    return out


def _parse_collect(raw: Any) -> list[CollectHook]:
    out: list[CollectHook] = []
    if not raw:
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("command") or "").strip()
        if not cmd:
            continue
        out.append(
            CollectHook(
                command=cmd,
                service=str(item.get("service") or "main"),
                timeout_sec=float(item.get("timeout_sec") or 60.0),
            )
        )
    return out


def list_task_names() -> list[str]:
    """Alphabetical task names with a task.toml."""
    if not os.path.isdir(TASKS_DIR):
        return []
    names = []
    for entry in os.listdir(TASKS_DIR):
        task_dir = os.path.join(TASKS_DIR, entry)
        if not os.path.isdir(task_dir):
            continue
        if not os.path.isfile(os.path.join(task_dir, "task.toml")):
            continue
        names.append(entry)
    return sorted(names)


def list_all_task_names() -> list[str]:
    return list_task_names()


def load_task_toml(task_dir: str) -> dict[str, Any]:
    path = os.path.join(task_dir, "task.toml")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _resolve_environment_mode(data: dict[str, Any]) -> str:
    """Harbor default is shared (verifier in the agent container)."""
    verifier = data.get("verifier") or {}
    ver_env = verifier.get("environment")
    has_ver_env = isinstance(ver_env, dict) and bool(ver_env)
    mode = str(verifier.get("environment_mode") or "").strip().lower()
    if mode == "shared":
        return "shared"
    if mode == "separate":
        return "separate"
    if has_ver_env:
        return "separate"
    return "shared"


def load_task(name: str, index: int) -> TaskInfo:
    task_dir = os.path.join(TASKS_DIR, name)
    data = load_task_toml(task_dir)
    env = data.get("environment") or {}
    agent = data.get("agent") or {}
    verifier = data.get("verifier") or {}
    ver_env_block = verifier.get("environment") or {}
    compose_path = os.path.join(task_dir, "environment", "docker-compose.yaml")
    v_env = verifier.get("env") or {}
    verifier_env = {str(k): str(v) for k, v in v_env.items()} if isinstance(v_env, dict) else {}
    return TaskInfo(
        name=name,
        index=index,
        path=task_dir,
        instruction_path=os.path.join(task_dir, "instruction.md"),
        tests_dir=os.path.join(task_dir, "tests"),
        environment_dir=os.path.join(task_dir, "environment"),
        docker_image=(env.get("docker_image") or None),
        agent_timeout_sec=float(agent.get("timeout_sec") or 900.0),
        verifier_timeout_sec=float(verifier.get("timeout_sec") or 900.0),
        build_timeout_sec=float(env.get("build_timeout_sec") or 600.0),
        cpus=int(env.get("cpus") or 1),
        memory_mb=int(env.get("memory_mb") or 2048),
        storage_mb=int(env.get("storage_mb") or 10240),
        allow_internet=bool(env.get("allow_internet", True)),
        artifacts=_parse_artifacts(data.get("artifacts")),
        collect_hooks=_parse_collect(verifier.get("collect")),
        verifier_env=verifier_env,
        verifier_cpus=int(ver_env_block["cpus"]) if ver_env_block.get("cpus") is not None else None,
        verifier_memory_mb=(
            int(ver_env_block["memory_mb"])
            if ver_env_block.get("memory_mb") is not None
            else None
        ),
        environment_mode=_resolve_environment_mode(data),
        schema_version=str(data.get("schema_version") or ""),
        needs_compose=os.path.isfile(compose_path),
        compose_file=compose_path if os.path.isfile(compose_path) else None,
    )


def load_all_tasks() -> list[TaskInfo]:
    return [
        load_task(name, i)
        for i, name in enumerate(list_task_names(), start=1)
    ]


def sample_tasks(
    n: int = SAMPLE_SIZE,
    seed: int = SAMPLE_SEED,
) -> list[TaskInfo]:
    """Fixed-seed random sample of n tasks (alphabetical corpus, reproducible)."""
    names = list_task_names()
    if not names:
        return []
    if n > len(names):
        raise ValueError(
            f"SAMPLE_SIZE={n} exceeds corpus size {len(names)}"
        )
    rng = random.Random(seed)
    chosen = sorted(rng.sample(names, n))
    # index = 1-based position in the full alphabetical corpus
    rank = {name: i for i, name in enumerate(names, start=1)}
    return [load_task(name, rank[name]) for name in chosen]


def traj_agent_dir(agent_id: str) -> str:
    return os.path.join(TRAJ_DIR, agent_id)


def traj_task_dir(agent_id: str, task_name: str) -> str:
    return os.path.join(TRAJ_DIR, agent_id, task_name)


def eval_results_path(agent_id: str) -> str:
    return os.path.join(traj_agent_dir(agent_id), "eval_results.json")


def _is_skipped_entry(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("skipped"))


def load_eval_results(agent_id: str) -> dict[str, Any]:
    path = eval_results_path(agent_id)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out: dict[str, Any] = {}
    for k, v in data.items():
        key = str(k)
        if isinstance(v, bool):
            out[key] = v
    return out


def _passed_from_stats(stats: dict[str, Any]) -> bool | None:
    # Harbor-style verifier ERROR (missing/invalid reward): Acc FAIL, not skip.
    if stats.get("verifier_error"):
        return False
    if "reward" not in stats:
        return None
    if stats.get("reward") is None:
        return False
    try:
        return float(stats["reward"]) == 1.0
    except (TypeError, ValueError):
        return bool(stats.get("passed"))


def read_task_stats(agent_id: str, task_name: str) -> dict[str, Any] | None:
    stats_path = os.path.join(traj_task_dir(agent_id, task_name), "stats.json")
    if not os.path.isfile(stats_path):
        return None
    try:
        with open(stats_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def reconcile_eval_results(agent_id: str) -> dict[str, Any]:
    known = set(list_task_names())
    agent_dir = traj_agent_dir(agent_id)
    rebuilt: dict[str, Any] = {}
    if os.path.isdir(agent_dir):
        for entry in sorted(os.listdir(agent_dir)):
            if entry not in known:
                continue
            task_dir = os.path.join(agent_dir, entry)
            if not os.path.isdir(task_dir):
                continue
            stats = read_task_stats(agent_id, entry)
            if not stats:
                continue
            passed = _passed_from_stats(stats)
            if passed is None:
                continue
            rebuilt[entry] = passed

    with _EVAL_LOCK:
        path = eval_results_path(agent_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rebuilt, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    return rebuilt


def save_eval_result(
    agent_id: str,
    task_name: str,
    passed: bool | None = None,
) -> None:
    with _EVAL_LOCK:
        results = load_eval_results(agent_id)
        results[task_name] = bool(passed)
        path = eval_results_path(agent_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)


def is_task_done(agent_id: str, task_name: str) -> bool:
    results = load_eval_results(agent_id)
    return task_name in results


def write_stats(agent_id: str, task_name: str, stats: dict[str, Any]) -> None:
    task_dir = traj_task_dir(agent_id, task_name)
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, "stats.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def accuracy(
    eval_results: dict[str, Any],
    *,
    exclude_skipped: bool = True,
) -> tuple[int, int, float]:
    scored: list[bool] = []
    for v in eval_results.values():
        if exclude_skipped and _is_skipped_entry(v):
            continue
        if isinstance(v, bool):
            scored.append(v)
    completed = len(scored)
    passed = sum(1 for v in scored if v)
    acc = (passed / completed) if completed else 0.0
    return passed, completed, acc
