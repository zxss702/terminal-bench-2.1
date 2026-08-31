#!/usr/bin/env python3
"""Pre-pull Terminal-Bench 2.1 images (task.toml docker_image, Dockerfile FROM, compose).

Uses Clash at 127.0.0.1:7897 (never Squid :3144).
Images already present locally are skipped. Failed pulls retry every 5s.

Usage:
  python scripts/pull_tb2_base_images.py
  python scripts/pull_tb2_base_images.py --list
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = ROOT / "harness" / "config.py"

_spec = importlib.util.spec_from_file_location("tb2_harness_config", _CONFIG_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"cannot load {_CONFIG_PATH}")
_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config)

TASKS_DIR = Path(_config.TASKS_DIR)
RETRY_SLEEP_SEC = int(_config.RETRY_SLEEP_SEC)
LOCAL_IMAGE_PREFIX = str(_config.LOCAL_IMAGE_PREFIX)

CLASH_PROXY = (
    os.getenv("TB2_CLASH_PROXY", "").strip() or "http://127.0.0.1:7897"
)
PULL_PLATFORM = "linux/amd64"

FROM_RE = re.compile(
    r"^\s*FROM\s+"
    r"(?:--[A-Za-z0-9._-]+=\S+\s+)*"
    r"(\S+)"
    r"(?:\s+AS\s+(\S+))?"
    r"\s*$"
)
COMPOSE_IMAGE_RE = re.compile(
    r"^\s*image:\s*['\"]?([^'\"#\s]+)['\"]?\s*(?:#.*)?$",
    re.IGNORECASE,
)
DOCKER_IMAGE_RE = re.compile(
    r'^\s*docker_image\s*=\s*["\']([^"\']+)["\']\s*$',
    re.MULTILINE,
)
COMPOSE_NAMES = {
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}


def _is_dockerfile(path: Path) -> bool:
    name = path.name
    return name.startswith("Dockerfile") or name.endswith("Dockerfile")


def _is_compose_file(path: Path) -> bool:
    return path.name in COMPOSE_NAMES


def _looks_pullable(ref: str, local_stages: set[str]) -> bool:
    if not ref or ref == "scratch":
        return False
    if ref.startswith("$") or "${" in ref:
        return False
    if ref in local_stages:
        return False
    if ref == LOCAL_IMAGE_PREFIX or ref.startswith(f"{LOCAL_IMAGE_PREFIX}/"):
        return False
    return True


def parse_dockerfile_froms(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    stages: set[str] = set()
    refs: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        match = FROM_RE.match(line)
        if not match:
            continue
        image, stage = match.group(1), match.group(2)
        if _looks_pullable(image, stages):
            refs.append(image)
        if stage:
            stages.add(stage)
    return refs


def parse_compose_images(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    refs: list[str] = []
    for raw in text.splitlines():
        match = COMPOSE_IMAGE_RE.match(raw)
        if not match:
            continue
        image = match.group(1).strip()
        if _looks_pullable(image, set()):
            refs.append(image)
    return refs


def parse_task_toml_images(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    refs: list[str] = []
    for match in DOCKER_IMAGE_RE.finditer(text.replace("\r\n", "\n")):
        image = match.group(1).strip()
        if _looks_pullable(image, set()):
            refs.append(image)
    return refs


def collect_base_images(tasks_dir: Path) -> list[str]:
    found: set[str] = set()
    if not tasks_dir.is_dir():
        raise SystemExit(f"tasks dir missing: {tasks_dir}")
    for task_dir in sorted(p for p in tasks_dir.iterdir() if p.is_dir()):
        toml = task_dir / "task.toml"
        if toml.is_file():
            found.update(parse_task_toml_images(toml))
        for path in task_dir.rglob("*"):
            if not path.is_file():
                continue
            if _is_dockerfile(path):
                found.update(parse_dockerfile_froms(path))
            elif _is_compose_file(path):
                found.update(parse_compose_images(path))
    return sorted(found)


def clash_env() -> dict[str, str]:
    env = os.environ.copy()
    no_proxy = (
        os.getenv("NO_PROXY", "").strip()
        or os.getenv("no_proxy", "").strip()
        or "localhost,127.0.0.1,host.docker.internal"
    )
    env["HTTP_PROXY"] = CLASH_PROXY
    env["HTTPS_PROXY"] = CLASH_PROXY
    env["http_proxy"] = CLASH_PROXY
    env["https_proxy"] = CLASH_PROXY
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    return env


def image_present(ref: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", ref],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def pull_until_present(ref: str, env: dict[str, str]) -> None:
    attempt = 0
    while True:
        if image_present(ref):
            print(f"present  {ref}", flush=True)
            return
        attempt += 1
        print(f"pull     {ref}  (attempt {attempt}, proxy={CLASH_PROXY})", flush=True)
        result = subprocess.run(
            ["docker", "pull", "--platform", PULL_PLATFORM, ref],
            env=env,
        )
        if result.returncode == 0 and image_present(ref):
            print(f"ok       {ref}", flush=True)
            return
        print(
            f"retry    {ref}  (exit={result.returncode}; sleep {RETRY_SLEEP_SEC}s)",
            flush=True,
        )
        time.sleep(RETRY_SLEEP_SEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="print unique image refs and exit (no docker pull)",
    )
    args = parser.parse_args(argv)

    images = collect_base_images(TASKS_DIR)
    present = [ref for ref in images if image_present(ref)]
    missing = [ref for ref in images if ref not in present]
    print(f"# {len(images)} unique images from {TASKS_DIR}", flush=True)
    print(f"# already local: {len(present)}", flush=True)
    print(f"# need pull: {len(missing)}", flush=True)
    if args.list:
        for ref in images:
            mark = "present" if ref in present else "missing"
            print(f"{mark:8} {ref}")
        return 0

    env = clash_env()
    for ref in images:
        pull_until_present(ref, env)
    print(f"# done, {len(images)} images present locally", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
