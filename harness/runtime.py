"""Docker runtime: shared image, agent (single/compose), separate verifier, cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import yaml

from .agents import get_adapter
from .agents.base_utils import InfraFailure, ensure_lf_script_copy
from .config import (
    AGENT_ENV_DOCKERFILE,
    AGENT_FLAG_MAP,
    AGENT_INSTALL_SCRIPTS,
    CLASH_HOST_URL,
    COMMON_ENV_DOCKERFILE,
    COMMON_INSTALL_SCRIPT_SRC,
    DOCKER_AGENT_LABEL_KEY,
    DOCKER_INSTANCE_LABEL_KEY,
    DOCKER_MANAGED_LABEL_KEY,
    DOCKER_NAMESPACE_LABEL_KEY,
    DOCKER_ROLE_LABEL_KEY,
    INSTALL_BUDGET_SEC,
    KEEP_TASK_IMAGES,
    LOCAL_IMAGE_PREFIX,
    LOGORYTHIA_VARIANT_BY_ID,
    PROXY_NO_PROXY,
    RETRY_SLEEP_SEC,
    TRAJ_DIR,
)
from .pkg_proxy import (
    APPLY_PKG_PROXY_SH,
    PKG_PROXY_APPLY_CALL,
    clash_container_url,
    container_proxy_env,
    inject_pkg_proxy_into_dockerfile,
    pkg_proxy_url,
)
from .core import (
    ArtifactSpec,
    TaskInfo,
    save_eval_result,
    traj_task_dir,
    write_stats,
)
from .uv_cache import (
    UV_DOCKER_CONTEXT,
    resolve_uv_tarball_ready,
    rewrite_uv_curl_to_cache,
    uv_release_tarball_path,
    uv_version_dir,
)
from .vep_apis_cache import (
    VEP_APIS_CONTEXT,
    detect_vep_release_from_dockerfile,
    resolve_vep_apis_cache_ready,
    rewrite_install_pl_to_cache,
    vep_apis_cache_status_line,
    vep_apis_release_dir,
)

_IMAGE_LOCK = threading.Lock()
_IMAGE_CACHE: dict[str, str] = {}
_KEY_LOCKS: dict[str, threading.Lock] = {}


def _lock_for_image_key(key: str) -> threading.Lock:
    with _IMAGE_LOCK:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock
DOCKER_RUN_NAMESPACE = hashlib.sha1(
    os.path.abspath(TRAJ_DIR).encode("utf-8")
).hexdigest()[:12]


def to_docker_mount_path(path: str) -> str:
    abs_path = os.path.abspath(path)
    if os.name == "nt":
        return abs_path.replace("\\", "/")
    return abs_path


def resolve_container_proxy_url() -> tuple[str, str]:
    """General HTTP_PROXY for agent/verifier containers: always Clash.

    Squid is not a general proxy. apt/dnf use ``TB2_PKG_PROXY`` / pkg
    config files (see ``pkg_proxy``). pip uses Clash + ``PIP_CACHE_DIR``.
    """
    return clash_container_url(), "clash"


def resolve_build_proxy_url() -> tuple[str, str]:
    """Host ``docker build`` / ``docker pull`` CLI env for registry traffic.

    BuildKit ``load metadata`` / FROM pull uses the host network. That path
    must go through Clash (``127.0.0.1:7897``), never Squid ``:3144``.
    """
    return CLASH_HOST_URL, "clash"


def resolve_build_run_proxy_url() -> tuple[str, str]:
    """HTTP_PROXY build-args for Dockerfile RUN git/curl/other: always Clash.

    apt/dnf in the same RUN layers are steered to Squid via the
    ``TB2_PKG_PROXY`` snippet injected after ``FROM``. pip uses Clash
    plus ``PIP_CACHE_DIR``.
    """
    return clash_container_url(), "clash"


def merge_subprocess_env(*, for_docker_build: bool = False) -> dict[str, str]:
    """Env for host docker CLI (pull, build metadata, compose).

    Registry traffic always uses Clash. Do not inherit shell ``HTTP_PROXY``
    when that is Squid — image pull is not a pkg-cache hit.

    Dockerfile RUN proxies are ``--build-arg`` only (see docker_build_args).
    """
    env = os.environ.copy()
    no_proxy = PROXY_NO_PROXY or "localhost,127.0.0.1,host.docker.internal"
    clash = CLASH_HOST_URL
    env["HTTP_PROXY"] = clash
    env["HTTPS_PROXY"] = clash
    env["http_proxy"] = clash
    env["https_proxy"] = clash
    env["ALL_PROXY"] = clash
    env["all_proxy"] = clash
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    if for_docker_build:
        env["DOCKER_BUILDKIT"] = "1"
        env.setdefault("UV_HTTP_TIMEOUT", "600")
    return env


@dataclass(frozen=True)
class BuildNetworkPolicy:
    """Network policy for docker builds (base / common-env / verifier / compose).

    FROM/image pull: host Clash. Dockerfile RUN ``HTTP_PROXY``: Clash (git/curl/pip).
    apt/dnf: Squid via ``TB2_PKG_PROXY`` (injected after FROM in the build copy).
    """

    squid: bool
    proxy_url: str
    run_proxy_url: str
    pkg_proxy_url: str
    clash_url: str
    env: dict[str, str] = field(repr=False)
    summary: str

    def docker_build_args(self) -> list[str]:
        """Flags + RUN-layer proxy build-args (not BuildKit image pull).

        ``--pull=false``: use a local FROM tag if present; otherwise pull Hub
        via the host Clash env. ``HTTP_PROXY`` is Clash; ``TB2_PKG_PROXY`` is
        Squid for apt/dnf (empty when the cache is down).
        """
        run_proxy = self.run_proxy_url
        no_proxy = PROXY_NO_PROXY or "localhost,127.0.0.1,host.docker.internal"
        return [
            "--pull=false",
            "--build-arg",
            f"HTTP_PROXY={run_proxy}",
            "--build-arg",
            f"HTTPS_PROXY={run_proxy}",
            "--build-arg",
            f"http_proxy={run_proxy}",
            "--build-arg",
            f"https_proxy={run_proxy}",
            "--build-arg",
            f"NO_PROXY={no_proxy}",
            "--build-arg",
            f"no_proxy={no_proxy}",
            "--build-arg",
            f"TB2_PKG_PROXY={self.pkg_proxy_url}",
        ]


def resolve_build_network_policy() -> BuildNetworkPolicy:
    proxy, _from_mode = resolve_build_proxy_url()
    run_proxy, run_mode = resolve_build_run_proxy_url()
    pkg = pkg_proxy_url()
    env = merge_subprocess_env(for_docker_build=True)
    pkg_part = f"pkg=squid:{pkg}" if pkg else "pkg=off (apt/dnf also Clash)"
    summary = (
        f"from_proxy=clash:{proxy}; "
        f"run_http={run_mode}:{run_proxy}; "
        f"{pkg_part}"
    )
    return BuildNetworkPolicy(
        squid=bool(pkg),
        proxy_url=proxy,
        run_proxy_url=run_proxy,
        pkg_proxy_url=pkg,
        clash_url=clash_container_url(),
        env=env,
        summary=summary,
    )


def merge_env_for_task_dockerfile_build() -> dict[str, str]:
    """Deprecated alias — prefer resolve_build_network_policy().env."""
    return resolve_build_network_policy().env


def _container_runtime_env() -> dict[str, str]:
    """Agent/verifier env: Clash for general HTTP/pip, Squid only for apt/dnf."""
    return container_proxy_env()


def prepare_dockerfile_for_build(
    dockerfile_path: str,
    action_log: str | None = None,
) -> list[str]:
    """Offline GitHub caches + apt/dnf Squid snippet on the build copy."""
    extra = apply_offline_github_caches(dockerfile_path, action_log)
    if inject_pkg_proxy_into_dockerfile(dockerfile_path) and action_log:
        append_action_log(
            action_log,
            "pkg-proxy: apt/dnf snippet after FROM (HTTP_PROXY stays Clash)",
        )
    return extra


def apply_offline_github_caches(
    dockerfile_path: str,
    action_log: str | None = None,
) -> list[str]:
    """Rewrite GitHub-dependent RUNs to host BuildKit contexts when caches ready.

    Handles:
      - uv curl from astral-sh/uv releases (verifier Dockerfile pin 0.9.7)
      - VEP ``INSTALL.pl --AUTO a`` (Ensembl Perl APIs via api.github.com)

    Returns extra ``docker build`` args (``--build-context ...``).
    """
    path = Path(dockerfile_path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    extra: list[str] = []
    notes: list[str] = []

    uv_new, uv_ver = rewrite_uv_curl_to_cache(text)
    if uv_new and uv_ver and resolve_uv_tarball_ready(uv_ver):
        text = uv_new
        extra.extend(
            ["--build-context", f"{UV_DOCKER_CONTEXT}={uv_version_dir(uv_ver)}"]
        )
        notes.append(f"uv={uv_ver} <- {uv_release_tarball_path(uv_ver)}")

    release = detect_vep_release_from_dockerfile(text)
    if release and resolve_vep_apis_cache_ready(release):
        vep_new = rewrite_install_pl_to_cache(text, release)
        if vep_new:
            text = vep_new
            extra.extend(
                [
                    "--build-context",
                    f"{VEP_APIS_CONTEXT}={vep_apis_release_dir(release)}",
                ]
            )
            notes.append(f"vep-apis={release} <- {vep_apis_release_dir(release)}")

    if notes:
        path.write_text(text, encoding="utf-8", newline="\n")
        msg = "offline github caches: " + "; ".join(notes)
        if action_log:
            append_action_log(action_log, msg)
    elif action_log and (
        "INSTALL.pl" in text or "astral-sh/uv/releases" in text
    ):
        append_action_log(
            action_log,
            "offline github caches: not applied "
            f"({vep_apis_cache_status_line()}; "
            "uv tarball for Dockerfile pin may be missing — "
            ".\\.cache\\cache_uv.ps1 -Version 0.9.7 / "
            ".\\.cache\\cache_vep_apis.ps1)",
        )
    return extra


def docker_label_args(
    task_name: str,
    agent_id: str,
    *,
    role: str = "agent",
) -> list[str]:
    instance = f"{DOCKER_RUN_NAMESPACE}:{task_name}:{agent_id}"
    return [
        "--label",
        f"{DOCKER_MANAGED_LABEL_KEY}=true",
        "--label",
        f"{DOCKER_NAMESPACE_LABEL_KEY}={DOCKER_RUN_NAMESPACE}",
        "--label",
        f"{DOCKER_INSTANCE_LABEL_KEY}={instance}",
        "--label",
        f"{DOCKER_AGENT_LABEL_KEY}={agent_id}",
        "--label",
        f"{DOCKER_ROLE_LABEL_KEY}={role}",
    ]


def docker_container_filter_args(task_name: str, agent_id: str) -> list[str]:
    instance = f"{DOCKER_RUN_NAMESPACE}:{task_name}:{agent_id}"
    return [
        "--filter",
        f"label={DOCKER_MANAGED_LABEL_KEY}=true",
        "--filter",
        f"label={DOCKER_NAMESPACE_LABEL_KEY}={DOCKER_RUN_NAMESPACE}",
        "--filter",
        f"label={DOCKER_INSTANCE_LABEL_KEY}={instance}",
    ]


def build_container_name(task_name: str, agent_id: str, *, role: str = "agent") -> str:
    slug_t = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in task_name.lower())[:20]
    slug_a = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in agent_id.lower())[:12]
    suffix = hashlib.sha1(
        f"{DOCKER_RUN_NAMESPACE}:{task_name}:{agent_id}:{role}:{os.getpid()}:{time.time_ns()}".encode()
    ).hexdigest()[:8]
    return f"tb2-{DOCKER_RUN_NAMESPACE[:6]}-{slug_t}-{slug_a}-{role[:3]}-{suffix}"


def compose_project_name(task_name: str, agent_id: str) -> str:
    slug_t = "".join(ch if ch.isalnum() else "" for ch in task_name.lower())[:18]
    slug_a = "".join(ch if ch.isalnum() else "" for ch in agent_id.lower())[:10]
    return f"tb2{DOCKER_RUN_NAMESPACE[:6]}{slug_t}{slug_a}"[:63]


def run_best_effort(cmd: list[str]):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return None


def run_captured(cmd: list[str], **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)


def append_action_log(log_path: str, message: str) -> None:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {message}\n")


_T = TypeVar("_T")


def retry_until_image(
    action_log: str,
    label: str,
    fn: Callable[[], _T],
) -> _T:
    """Retry docker image pull/build forever until success (5s between attempts)."""
    while True:
        try:
            return fn()
        except InfraFailure as exc:
            append_action_log(
                action_log,
                f"{label} failed: {exc}; retry in {RETRY_SLEEP_SEC}s",
            )
            time.sleep(RETRY_SLEEP_SEC)


def docker_image_present(ref: str) -> bool:
    inspect = run_best_effort(["docker", "image", "inspect", ref])
    return bool(inspect and inspect.returncode == 0)


def ensure_pulled_image(ref: str, action_log: str) -> str:
    """Harbor-style: pull a prebuilt image. Infinite 5s retry. Reuse if already local."""
    attempt = 0
    while True:
        if docker_image_present(ref):
            append_action_log(action_log, f"image present locally: {ref}")
            return ref
        attempt += 1
        append_action_log(action_log, f"docker pull {ref} (attempt {attempt})")
        result = run_captured(
            ["docker", "pull", "--platform", "linux/amd64", ref],
            env=merge_subprocess_env(),
        )
        tail = (
            f"pull exit={result.returncode}\n"
            f"{(result.stdout or '')[-2000:]}\n{(result.stderr or '')[-2000:]}"
        )
        append_action_log(action_log, tail)
        if result.returncode == 0 and docker_image_present(ref):
            return ref
        append_action_log(
            action_log,
            f"docker pull {ref} failed; retry in {RETRY_SLEEP_SEC}s",
        )
        time.sleep(RETRY_SLEEP_SEC)


def run_docker_build_to_action_log(
    cmd: list[str],
    *,
    action_log: str,
    env: dict[str, str],
    timeout: int,
) -> int:
    """Run docker build with --progress=plain; append full output to image.log."""
    os.makedirs(os.path.dirname(action_log), exist_ok=True)
    if "--progress=plain" not in cmd:
        try:
            idx = cmd.index("build")
            cmd = cmd[: idx + 1] + ["--progress=plain"] + cmd[idx + 1 :]
        except ValueError:
            cmd = list(cmd) + ["--progress=plain"]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(action_log, "a", encoding="utf-8", errors="replace") as log_fh:
        log_fh.write(f"[{ts}] docker build begin\n")
        log_fh.write(f"# cmd={' '.join(cmd)}\n")
        log_fh.flush()
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                log_fh.write(line)
                log_fh.flush()
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            log_fh.write(f"[{ts}] docker build timed out after {timeout}s\n")
            raise
        end_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_fh.write(f"[{end_ts}] build exit={returncode}\n")
        return returncode


def local_image_tag(task: TaskInfo) -> str:
    return f"{LOCAL_IMAGE_PREFIX}/{task.name}:base"


def common_env_image_tag(task: TaskInfo) -> str:
    return f"{LOCAL_IMAGE_PREFIX}/{task.name}:common-env"


def agent_env_image_tag(task: TaskInfo, agent_id: str) -> str:
    return f"{LOCAL_IMAGE_PREFIX}/{task.name}:{agent_id}"


def compose_sidecar_image_tag(task: TaskInfo, service: str) -> str:
    return f"{LOCAL_IMAGE_PREFIX}/{task.name}:{service}"


def _tests_content_hash(tests_dir: str) -> str:
    h = hashlib.sha256()
    root = Path(tests_dir)
    if not root.is_dir():
        return "missing"
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix().encode()
        data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        h.update(rel)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()[:12]


def verifier_image_tag(task: TaskInfo) -> str:
    return f"{LOCAL_IMAGE_PREFIX}/{task.name}:verifier-{_tests_content_hash(task.tests_dir)}"


def ensure_task_image(task: TaskInfo, action_log: str) -> str:
    cache_key = f"{task.name}::base"
    with _IMAGE_LOCK:
        if cache_key in _IMAGE_CACHE:
            return _IMAGE_CACHE[cache_key]

    # Harbor: [environment].docker_image is pulled, never built from Dockerfile
    # unless the task has no prebuilt image. Bad networks: 5s infinite retry.
    if task.docker_image:
        pulled = ensure_pulled_image(task.docker_image, action_log)
        with _IMAGE_LOCK:
            _IMAGE_CACHE[cache_key] = pulled
        return pulled

    with _IMAGE_LOCK:
        if cache_key in _IMAGE_CACHE:
            return _IMAGE_CACHE[cache_key]
        tag = local_image_tag(task)
        dockerfile = os.path.join(task.environment_dir, "Dockerfile")
        if not os.path.isfile(dockerfile):
            raise InfraFailure(f"No docker_image and no Dockerfile for {task.name}")

        policy = resolve_build_network_policy()
        append_action_log(action_log, f"build network: {policy.summary}")

        shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
        os.makedirs(shared_dir, exist_ok=True)
        # Windows checkouts leave CRLF in environment/*.sh (e.g. ENTRYPOINT
        # scripts); normalize before COPY so shebangs work in Linux images.
        env_ctx = os.path.join(shared_dir, "env-ctx")
        _copy_tree_ctx_lf(task.environment_dir, env_ctx)
        dockerfile_ctx = os.path.join(env_ctx, "Dockerfile")
        if not os.path.isfile(dockerfile_ctx):
            raise InfraFailure(f"No Dockerfile in env-ctx for {task.name}")

        offline_args = prepare_dockerfile_for_build(dockerfile_ctx, action_log)

        build_cmd = [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--add-host=host.docker.internal:host-gateway",
            "-t",
            tag,
            "-f",
            dockerfile_ctx,
            *policy.docker_build_args(),
            *offline_args,
            env_ctx,
        ]
        append_action_log(
            action_log,
            f"docker build -t {tag} -f {dockerfile_ctx} {env_ctx} "
            f"(LF-normalized env; {policy.summary})",
        )
        try:
            returncode = run_docker_build_to_action_log(
                build_cmd,
                action_log=action_log,
                env=policy.env,
                timeout=int(task.build_timeout_sec) + 120,
            )
        except subprocess.TimeoutExpired as exc:
            raise InfraFailure(
                f"docker build timed out for {task.name}"
            ) from exc
        if returncode != 0:
            raise InfraFailure(f"docker build failed for {task.name}")
        _IMAGE_CACHE[cache_key] = tag
        return tag


def ensure_common_env_image(task: TaskInfo, base_image: str, action_log: str) -> str:
    tag = common_env_image_tag(task)
    cache_key = f"{task.name}::common-env"
    shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
    os.makedirs(shared_dir, exist_ok=True)

    with _IMAGE_LOCK:
        if cache_key in _IMAGE_CACHE:
            return _IMAGE_CACHE[cache_key]

        inspect = run_best_effort(["docker", "image", "inspect", tag])
        if inspect and inspect.returncode == 0:
            env_out = run_best_effort(
                [
                    "docker",
                    "image",
                    "inspect",
                    tag,
                    "--format",
                    "{{json .Config.Env}}",
                ]
            )
            env_text = (env_out.stdout or "") if env_out else ""
            if "TB2_COMMON_ENV_PREBUILT=1" in env_text:
                _IMAGE_CACHE[cache_key] = tag
                _IMAGE_CACHE[task.name] = tag
                return tag
            append_action_log(
                action_log,
                f"{tag} exists but is not a TB2 common-env image; rebuilding",
            )

        if not os.path.isfile(COMMON_ENV_DOCKERFILE):
            raise InfraFailure(f"missing {COMMON_ENV_DOCKERFILE}")
        if not os.path.isfile(COMMON_INSTALL_SCRIPT_SRC):
            raise InfraFailure(f"missing {COMMON_INSTALL_SCRIPT_SRC}")

        policy = resolve_build_network_policy()
        append_action_log(action_log, f"build network: {policy.summary}")

        ctx = os.path.join(shared_dir, "docker-ctx")
        shutil.rmtree(ctx, ignore_errors=True)
        os.makedirs(ctx, exist_ok=True)
        shutil.copy2(COMMON_INSTALL_SCRIPT_SRC, os.path.join(ctx, "env_install_common.sh"))
        text = Path(ctx, "env_install_common.sh").read_text(encoding="utf-8")
        Path(ctx, "env_install_common.sh").write_text(
            text.replace("\r\n", "\n"), encoding="utf-8", newline="\n"
        )

        build_timeout = max(int(task.build_timeout_sec) + 120, int(INSTALL_BUDGET_SEC) + 300)
        build_cmd = [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--add-host=host.docker.internal:host-gateway",
            "--progress=plain",
            "-t",
            tag,
            "-f",
            COMMON_ENV_DOCKERFILE,
            *policy.docker_build_args(),
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            ctx,
        ]
        append_action_log(
            action_log,
            f"docker build common-env -t {tag} FROM {base_image} "
            "(BuildKit cache id=tb2-pip)",
        )
        try:
            returncode = run_docker_build_to_action_log(
                build_cmd,
                action_log=action_log,
                env=policy.env,
                timeout=build_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(ctx, ignore_errors=True)
            raise InfraFailure(
                f"common-env docker build timed out for {task.name}"
            ) from exc

        shutil.rmtree(ctx, ignore_errors=True)
        if returncode != 0:
            raise InfraFailure(
                f"common-env docker build failed for {task.name} (exit={returncode})"
            )
        _IMAGE_CACHE[cache_key] = tag
        _IMAGE_CACHE[task.name] = tag
        return tag


def _image_inspect_text(ref: str, fmt: str) -> str:
    out = run_best_effort(["docker", "image", "inspect", ref, "--format", fmt])
    if not out or out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _populate_agent_build_ctx(agent_id: str, ctx: str) -> None:
    extra = os.path.join(ctx, "extra")
    os.makedirs(extra, exist_ok=True)
    Path(extra, "placeholder").write_text("tb2-agent-extra\n", encoding="utf-8")

    script_src = AGENT_INSTALL_SCRIPTS.get(agent_id)
    if not script_src or not os.path.isfile(script_src):
        raise InfraFailure(f"missing env_install script for {agent_id}: {script_src}")
    ensure_lf_script_copy(script_src, os.path.join(ctx, "env_install_agent.sh"))

    variant = LOGORYTHIA_VARIANT_BY_ID.get(agent_id)
    if variant is None:
        raise InfraFailure(f"unknown logorythia variant: {agent_id}")
    if not os.path.isfile(variant.package_src):
        raise InfraFailure(f"Logorythia package missing ({variant.package_src})")
    shutil.copy2(variant.package_src, os.path.join(extra, "logorythia.zip"))


def ensure_agent_env_image(
    task: TaskInfo,
    common_image: str,
    agent_id: str,
    action_log: str,
) -> str:
    """Bake agent install into `{task}:{agent_id}` using BuildKit cache id=tb2-pip."""
    tag = agent_env_image_tag(task, agent_id)
    cache_key = f"{task.name}::{agent_id}"
    lock = _lock_for_image_key(cache_key)
    with lock:
        with _IMAGE_LOCK:
            cached = _IMAGE_CACHE.get(cache_key)
        if cached:
            return cached

        if docker_image_present(tag):
            env_text = _image_inspect_text(tag, "{{json .Config.Env}}")
            label_base = _image_inspect_text(
                tag, '{{index .Config.Labels "tb2.common_env"}}'
            )
            if "TB2_AGENT_PREBUILT=1" in env_text and label_base == common_image:
                append_action_log(action_log, f"reuse agent-env image {tag}")
                with _IMAGE_LOCK:
                    _IMAGE_CACHE[cache_key] = tag
                return tag
            append_action_log(
                action_log,
                f"{tag} exists but is stale (base={label_base!r}, "
                f"want {common_image}); rebuilding",
            )

        if not os.path.isfile(AGENT_ENV_DOCKERFILE):
            raise InfraFailure(f"missing {AGENT_ENV_DOCKERFILE}")

        policy = resolve_build_network_policy()
        append_action_log(action_log, f"build network: {policy.summary}")

        shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
        os.makedirs(shared_dir, exist_ok=True)
        ctx = os.path.join(shared_dir, f"docker-ctx-agent-{agent_id}")
        shutil.rmtree(ctx, ignore_errors=True)
        os.makedirs(ctx, exist_ok=True)
        try:
            _populate_agent_build_ctx(agent_id, ctx)
        except InfraFailure:
            shutil.rmtree(ctx, ignore_errors=True)
            raise

        build_timeout = max(
            int(task.build_timeout_sec) + 120, int(INSTALL_BUDGET_SEC) + 300
        )
        build_cmd = [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--add-host=host.docker.internal:host-gateway",
            "--progress=plain",
            "-t",
            tag,
            "-f",
            AGENT_ENV_DOCKERFILE,
            *policy.docker_build_args(),
            "--build-arg",
            f"BASE_IMAGE={common_image}",
            "--build-arg",
            f"AGENT_ID={agent_id}",
            ctx,
        ]
        append_action_log(
            action_log,
            f"docker build agent-env -t {tag} FROM {common_image} "
            f"agent={agent_id} (BuildKit cache id=tb2-pip)",
        )
        try:
            returncode = run_docker_build_to_action_log(
                build_cmd,
                action_log=action_log,
                env=policy.env,
                timeout=build_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(ctx, ignore_errors=True)
            raise InfraFailure(
                f"agent-env docker build timed out for {task.name}/{agent_id}"
            ) from exc

        shutil.rmtree(ctx, ignore_errors=True)
        if returncode != 0:
            raise InfraFailure(
                f"agent-env docker build failed for {task.name}/{agent_id} "
                f"(exit={returncode})"
            )
        with _IMAGE_LOCK:
            _IMAGE_CACHE[cache_key] = tag
        return tag


def _rmtree_retry(path: str, attempts: int = 8) -> None:
    """Remove a directory tree; retry on Windows pending-delete / locked files."""
    for i in range(attempts):
        if not os.path.lexists(path):
            return
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            return
        time.sleep(0.05 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)


def _is_copytree_exists_error(exc: BaseException) -> bool:
    if isinstance(exc, FileExistsError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 183:
        return True
    return False


def _cleanup_stale_tests_upload(shared_dir: str) -> None:
    """Drop leftover tests-upload dirs from interrupted runs (not in-flight mkdtemp)."""
    if not os.path.isdir(shared_dir):
        return
    for name in os.listdir(shared_dir):
        if name == "tests-upload" or name.startswith("tests-upload-"):
            _rmtree_retry(os.path.join(shared_dir, name))


def _copy_tree_ctx_lf(src_dir: str, ctx: str) -> None:
    """Copy a directory into a build context with CRLF→LF for text scripts."""
    _rmtree_retry(ctx)
    try:
        shutil.copytree(src_dir, ctx)
    except OSError as exc:
        if not _is_copytree_exists_error(exc):
            raise
        _rmtree_retry(ctx)
        shutil.copytree(src_dir, ctx)
    text_suffixes = {
        ".sh",
        ".bash",
        ".py",
        ".R",
        ".r",
        ".txt",
        ".toml",
        ".yml",
        ".yaml",
        ".md",
        ".cfg",
        ".ini",
        ".json",
    }
    for root, _dirs, files in os.walk(ctx):
        for name in files:
            path = os.path.join(root, name)
            ext = os.path.splitext(name)[1]
            if name == "Dockerfile" or ext in text_suffixes or name.endswith("Dockerfile"):
                try:
                    raw = Path(path).read_bytes()
                except OSError:
                    continue
                if b"\r" not in raw:
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                Path(path).write_text(
                    text.replace("\r\n", "\n").replace("\r", "\n"),
                    encoding="utf-8",
                    newline="\n",
                )


def _copy_tests_ctx_lf(tests_dir: str, ctx: str) -> None:
    """Copy tests/ into build context with CRLF→LF for shell/python scripts."""
    _copy_tree_ctx_lf(tests_dir, ctx)


def _load_compose_services(compose_file: str) -> dict:
    with open(compose_file, "rb") as fh:
        data = yaml.safe_load(fh) or {}
    services = data.get("services") or {}
    return services if isinstance(services, dict) else {}


def _compose_build_paths(env_ctx: str, build) -> tuple[str, str, str | None]:
    """Return (context_dir, dockerfile_path, target) for a compose build spec."""
    if isinstance(build, str):
        context = os.path.normpath(os.path.join(env_ctx, build))
        return context, os.path.join(context, "Dockerfile"), None
    if not isinstance(build, dict):
        raise InfraFailure(f"unsupported compose build spec: {build!r}")
    context_rel = build.get("context", ".") or "."
    context = os.path.normpath(os.path.join(env_ctx, context_rel))
    df = build.get("dockerfile", "Dockerfile") or "Dockerfile"
    dockerfile = df if os.path.isabs(df) else os.path.normpath(os.path.join(context, df))
    target = build.get("target")
    return context, dockerfile, str(target) if target else None


def ensure_compose_sidecar_images(task: TaskInfo, action_log: str) -> dict[str, str]:
    """Build non-main compose services once (shared by all agents)."""
    if not task.compose_file or not os.path.isfile(task.compose_file):
        return {}
    try:
        services = _load_compose_services(task.compose_file)
    except Exception as exc:
        raise InfraFailure(f"invalid compose file for {task.name}: {exc}") from exc

    shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
    env_ctx = os.path.join(shared_dir, "env-ctx")
    if not os.path.isdir(env_ctx):
        _copy_tree_ctx_lf(task.environment_dir, env_ctx)

    policy = resolve_build_network_policy()
    tags: dict[str, str] = {}
    for name, cfg in services.items():
        if name == "main" or not isinstance(cfg, dict) or "build" not in cfg:
            continue
        tag = compose_sidecar_image_tag(task, name)
        cache_key = f"{task.name}::compose::{name}"
        with _IMAGE_LOCK:
            if cache_key in _IMAGE_CACHE:
                tags[name] = _IMAGE_CACHE[cache_key]
                continue
            inspect = run_best_effort(["docker", "image", "inspect", tag])
            if inspect and inspect.returncode == 0:
                _IMAGE_CACHE[cache_key] = tag
                tags[name] = tag
                continue

            context, dockerfile, target = _compose_build_paths(env_ctx, cfg["build"])
            if not os.path.isfile(dockerfile):
                raise InfraFailure(
                    f"compose service {name} missing Dockerfile at {dockerfile}"
                )
            offline_args = prepare_dockerfile_for_build(dockerfile, action_log)
            build_cmd = [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--add-host=host.docker.internal:host-gateway",
                "-t",
                tag,
                "-f",
                dockerfile,
                *policy.docker_build_args(),
                *offline_args,
            ]
            if target:
                build_cmd.extend(["--target", target])
            build_cmd.append(context)
            append_action_log(
                action_log,
                f"compose sidecar build {name} -> {tag} ({policy.summary})",
            )
            try:
                returncode = run_docker_build_to_action_log(
                    build_cmd,
                    action_log=action_log,
                    env=policy.env,
                    timeout=int(task.build_timeout_sec) + 120,
                )
            except subprocess.TimeoutExpired as exc:
                raise InfraFailure(
                    f"docker build timed out for compose service {name}"
                ) from exc
            if returncode != 0:
                raise InfraFailure(f"docker build failed for compose service {name}")
            _IMAGE_CACHE[cache_key] = tag
            tags[name] = tag
    return tags


def ensure_verifier_image(task: TaskInfo, action_log: str) -> str:
    tag = verifier_image_tag(task)
    cache_key = f"{task.name}::verifier::{tag}"
    with _IMAGE_LOCK:
        if cache_key in _IMAGE_CACHE:
            return _IMAGE_CACHE[cache_key]
        inspect = run_best_effort(["docker", "image", "inspect", tag])
        if inspect and inspect.returncode == 0:
            _IMAGE_CACHE[cache_key] = tag
            return tag

        # Windows checkouts leave CRLF in tests/test.sh; normalize before COPY.
        dockerfile = os.path.join(task.tests_dir, "Dockerfile")
        if not os.path.isfile(dockerfile):
            raise InfraFailure(f"missing tests/Dockerfile for {task.name}")

        policy = resolve_build_network_policy()
        shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
        os.makedirs(shared_dir, exist_ok=True)
        # Verifier build logs go to shared image.log (one place for all builds).
        shared_log = os.path.join(shared_dir, "image.log")
        append_action_log(action_log, f"build network: {policy.summary}")
        append_action_log(shared_log, f"build network: {policy.summary}")
        append_action_log(
            action_log,
            f"docker build verifier -t {tag} (log -> traj/_shared/{task.name}/image.log)",
        )

        ctx = os.path.join(shared_dir, "verifier-ctx")
        _copy_tests_ctx_lf(task.tests_dir, ctx)
        dockerfile_ctx = os.path.join(ctx, "Dockerfile")
        offline_args = prepare_dockerfile_for_build(dockerfile_ctx, shared_log)
        if offline_args:
            append_action_log(action_log, f"offline build-context: {offline_args}")

        build_cmd = [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--add-host=host.docker.internal:host-gateway",
            "-t",
            tag,
            "-f",
            dockerfile_ctx,
            *policy.docker_build_args(),
            *offline_args,
            ctx,
        ]
        try:
            returncode = run_docker_build_to_action_log(
                build_cmd,
                action_log=shared_log,
                env=policy.env,
                timeout=int(task.build_timeout_sec) + 600,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(ctx, ignore_errors=True)
            append_action_log(action_log, "verifier build timed out")
            raise InfraFailure(
                f"verifier docker build timed out for {task.name}"
            ) from exc
        shutil.rmtree(ctx, ignore_errors=True)
        append_action_log(action_log, f"verifier build exit={returncode}")
        if returncode != 0:
            raise InfraFailure(f"verifier docker build failed for {task.name}")
        _IMAGE_CACHE[cache_key] = tag
        return tag


def stop_rm_container(name: str) -> None:
    run_best_effort(["docker", "rm", "-f", name])


def cleanup_task_agent_containers(task_name: str, agent_id: str) -> None:
    result = run_best_effort(
        ["docker", "ps", "-aq", *docker_container_filter_args(task_name, agent_id)]
    )
    if not result or result.returncode != 0:
        return
    ids = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    if ids:
        run_best_effort(["docker", "rm", "-f", *ids])
    # Compose projects for this task+agent
    project = compose_project_name(task_name, agent_id)
    run_best_effort(
        ["docker", "compose", "-p", project, "down", "--remove-orphans", "-v"]
    )


def _inject_env_flags(cmd: list[str], env: dict[str, str]) -> None:
    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])


def docker_run_agent(
    *,
    image: str,
    task: TaskInfo,
    agent_id: str,
    run_dir: str,
    mounts: list[tuple[str, str, str]],
    entryscript_host: str,
    action_log: str,
) -> tuple[str, int, bool, dict[str, str]]:
    """Start single-container agent. Returns (name, exit, host_ceiling, service_map)."""
    name = build_container_name(task.name, agent_id, role="agent")
    stop_rm_container(name)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--user",
        "root",
        "--platform",
        "linux/amd64",
        "--security-opt",
        "seccomp=unconfined",
        "--add-host=host.docker.internal:host-gateway",
        "--workdir",
        "/app",
        "-v",
        f"{to_docker_mount_path(run_dir)}:/run",
        "-v",
        f"{to_docker_mount_path(entryscript_host)}:/entryscript.sh:ro",
    ]
    cmd.extend(docker_label_args(task.name, agent_id, role="agent"))
    if task.cpus:
        cmd.extend(["--cpus", str(task.cpus)])
    if task.memory_mb:
        cmd.extend(["--memory", f"{task.memory_mb}m"])
    for host, dest, mode in mounts:
        flag = "ro" if mode == "ro" else "rw"
        cmd.extend(["-v", f"{to_docker_mount_path(host)}:{dest}:{flag}"])
    _inject_env_flags(cmd, _container_runtime_env())
    cmd.extend([image, "sleep", "infinity"])

    append_action_log(action_log, f"docker run -> {name}")
    result = run_captured(cmd, env=merge_subprocess_env())
    if result.returncode != 0:
        raise InfraFailure(f"docker run failed: {result.stderr}")

    exit_code, host_hit = _exec_entryscript(
        name, task, agent_id, run_dir, entryscript_host, action_log
    )
    return name, exit_code, host_hit, {"main": name}


def _write_compose_override(
    *,
    path: str,
    image: str,
    run_dir: str,
    entryscript_host: str,
    mounts: list[tuple[str, str, str]],
    task: TaskInfo,
    agent_id: str,
    sidecar_images: dict[str, str] | None = None,
) -> None:
    volumes = [
        f"{to_docker_mount_path(run_dir)}:/run",
        f"{to_docker_mount_path(entryscript_host)}:/entryscript.sh:ro",
    ]
    for host, dest, mode in mounts:
        flag = "ro" if mode == "ro" else "rw"
        volumes.append(f"{to_docker_mount_path(host)}:{dest}:{flag}")

    labels = {
        DOCKER_MANAGED_LABEL_KEY: "true",
        DOCKER_NAMESPACE_LABEL_KEY: DOCKER_RUN_NAMESPACE,
        DOCKER_INSTANCE_LABEL_KEY: f"{DOCKER_RUN_NAMESPACE}:{task.name}:{agent_id}",
        DOCKER_AGENT_LABEL_KEY: agent_id,
        DOCKER_ROLE_LABEL_KEY: "agent",
    }

    # Detect network_mode: service:<svc> — extra_hosts cannot be set on main then;
    # put host.docker.internal on the shared-network service instead.
    network_mode = None
    share_svc = None
    if task.compose_file and os.path.isfile(task.compose_file):
        try:
            with open(task.compose_file, "rb") as fh:
                base = yaml.safe_load(fh) or {}
            main_cfg = (base.get("services") or {}).get("main") or {}
            network_mode = main_cfg.get("network_mode")
            if isinstance(network_mode, str) and network_mode.startswith("service:"):
                share_svc = network_mode.split(":", 1)[1].strip() or None
        except Exception:
            network_mode = None
            share_svc = None

    runtime_env = _container_runtime_env()
    sidecar_names = list(sidecar_images or {})
    compose_hosts = ["main", *sidecar_names]
    try:
        for name in _load_compose_services(task.compose_file or "").keys():
            if name not in compose_hosts:
                compose_hosts.append(name)
    except Exception:
        pass
    extra_no_proxy = ",".join(compose_hosts)
    for key in ("NO_PROXY", "no_proxy"):
        current = (runtime_env.get(key) or "").strip()
        runtime_env[key] = f"{current},{extra_no_proxy}" if current else extra_no_proxy

    main: dict = {
        "image": image,
        "pull_policy": "never",
        "user": "root",
        "platform": "linux/amd64",
        "command": ["sleep", "infinity"],
        "working_dir": "/app",
        "volumes": volumes,
        "labels": labels,
        "environment": runtime_env,
        "security_opt": ["seccomp=unconfined"],
    }
    if not share_svc:
        main["extra_hosts"] = ["host.docker.internal:host-gateway"]
    if task.cpus:
        main["cpus"] = float(task.cpus)
    if task.memory_mb:
        main["mem_limit"] = f"{task.memory_mb}m"

    data: dict = {"services": {"main": main}}
    if share_svc:
        data["services"][share_svc] = {
            "extra_hosts": ["host.docker.internal:host-gateway"],
        }
    for svc_name, svc_image in (sidecar_images or {}).items():
        svc = data["services"].setdefault(svc_name, {})
        svc["image"] = svc_image
        svc["pull_policy"] = "never"
        svc.setdefault("extra_hosts", ["host.docker.internal:host-gateway"])
        # Sidecars: no general HTTP_PROXY (healthchecks / docker-DNS). Build already
        # baked apt/dnf Squid config when their Dockerfiles were built.
    dumped = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    # Compose merge keeps `build:` from the task file. Insert !reset so `up`
    # cannot rebuild/overwrite prebuilt main/sidecar images.
    lines: list[str] = []
    for line in dumped.splitlines(keepends=True):
        lines.append(line)
        if line.startswith("    image:"):
            lines.append("    build: !reset\n")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)


def docker_compose_run_agent(
    *,
    image: str,
    task: TaskInfo,
    agent_id: str,
    run_dir: str,
    mounts: list[tuple[str, str, str]],
    entryscript_host: str,
    action_log: str,
) -> tuple[str, int, bool, dict[str, str]]:
    """Compose up with main=image; returns (main_id, exit, host_hit, service_map)."""
    if not task.compose_file:
        raise InfraFailure(f"compose missing for {task.name}")

    project = compose_project_name(task.name, agent_id)
    sidecar_images = ensure_compose_sidecar_images(task, action_log)
    override = os.path.join(run_dir, "docker-compose.override.yml")
    _write_compose_override(
        path=override,
        image=image,
        run_dir=run_dir,
        entryscript_host=entryscript_host,
        mounts=mounts,
        task=task,
        agent_id=agent_id,
        sidecar_images=sidecar_images,
    )

    # Tear down any prior project
    run_best_effort(
        ["docker", "compose", "-p", project, "down", "--remove-orphans", "-v"]
    )

    up_cmd = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        task.compose_file,
        "-f",
        override,
        "up",
        "-d",
        "--no-build",
        "--remove-orphans",
    ]
    append_action_log(
        action_log,
        f"compose up project={project} (no-build; sidecars={sidecar_images})",
    )
    result = run_captured(
        up_cmd,
        cwd=task.environment_dir,
        env=merge_subprocess_env(),
        timeout=int(task.build_timeout_sec) + 600,
    )
    append_action_log(
        action_log,
        f"compose up exit={result.returncode}\n{(result.stdout or '')[-2000:]}\n{(result.stderr or '')[-2000:]}",
    )
    if result.returncode != 0:
        raise InfraFailure(f"docker compose up failed for {task.name}")

    service_map: dict[str, str] = {}
    # Discover service container IDs
    ps = run_captured(
        ["docker", "compose", "-p", project, "ps", "-a", "--format", "json"],
        cwd=task.environment_dir,
        env=merge_subprocess_env(),
    )
    # Fallback: query each known service from compose file
    try:
        with open(task.compose_file, "rb") as fh:
            compose_data = yaml.safe_load(fh) or {}
        services = list((compose_data.get("services") or {}).keys())
    except Exception:
        services = ["main"]
    if "main" not in services:
        services.insert(0, "main")

    for svc in services:
        q = run_captured(
            ["docker", "compose", "-p", project, "ps", "-q", svc],
            cwd=task.environment_dir,
            env=merge_subprocess_env(),
        )
        cid = (q.stdout or "").strip().splitlines()
        if cid and cid[0].strip():
            service_map[svc] = cid[0].strip()

    main_id = service_map.get("main")
    if not main_id:
        raise InfraFailure(f"compose main container missing for {task.name}")

    exit_code, host_hit = _exec_entryscript(
        main_id, task, agent_id, run_dir, entryscript_host, action_log
    )
    return main_id, exit_code, host_hit, service_map


def _exec_entryscript(
    container: str,
    task: TaskInfo,
    agent_id: str,
    run_dir: str,
    entryscript_host: str,
    action_log: str,
) -> tuple[int, bool]:
    task_traj_dir = os.path.dirname(action_log)
    out_path = os.path.join(task_traj_dir, "agent_container_output.log")
    try:
        shutil.copy2(entryscript_host, os.path.join(task_traj_dir, "agent_entryscript.sh"))
    except Exception:
        pass

    exec_cmd = ["docker", "exec", "-w", "/app", container, "bash", "/entryscript.sh"]
    host_ceiling = int(task.agent_timeout_sec) + max(0, int(INSTALL_BUDGET_SEC))
    append_action_log(
        action_log,
        f"docker exec entryscript (ceiling={host_ceiling}s) -> agent_container_output.log",
    )

    timed_out = False
    returncode = 1
    timer = None
    process = None
    try:
        with open(out_path, "ab") as log_file:
            log_file.write(f"\n===== agent attempt container={container} =====\n".encode())
            log_file.flush()
            process = subprocess.Popen(
                exec_cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=merge_subprocess_env(),
            )

            def _kill_on_timeout():
                nonlocal timed_out
                timed_out = True
                run_best_effort(["docker", "kill", container])
                try:
                    process.kill()
                except Exception:
                    pass

            timer = threading.Timer(host_ceiling, _kill_on_timeout)
            timer.daemon = True
            timer.start()
            process.wait()
            returncode = process.returncode if process.returncode is not None else 1
    except Exception as exc:
        raise InfraFailure(f"agent container run exception: {exc}") from exc
    finally:
        if timer is not None:
            timer.cancel()

    if timed_out:
        append_action_log(action_log, f"host ceiling hit ({host_ceiling}s)")
        return 124, True
    append_action_log(action_log, f"agent exit={returncode}")
    return returncode, False


def _service_container(service_map: dict[str, str], service: str) -> str | None:
    return service_map.get(service) or service_map.get("main") or None


def _is_sidecar_service(service: str) -> bool:
    return (service or "main") != "main"


def _service_exec(cid: str, command: str, *, is_sidecar: bool) -> list[str]:
    """Harbor: main uses bash -lc; sidecars use POSIX sh -c (alpine often has no bash)."""
    if is_sidecar:
        return ["docker", "exec", cid, "sh", "-c", command]
    return ["docker", "exec", cid, "bash", "-lc", command]


def run_collect_hooks(
    task: TaskInfo,
    service_map: dict[str, str],
    action_log: str,
) -> None:
    for i, hook in enumerate(task.collect_hooks, start=1):
        cid = _service_container(service_map, hook.service)
        if not cid:
            append_action_log(
                action_log,
                f"collect[{i}] skip: no container for service={hook.service}",
            )
            continue
        append_action_log(
            action_log,
            f"collect[{i}] service={hook.service} timeout={hook.timeout_sec}s",
        )
        proc = run_captured(
            _service_exec(
                cid, hook.command, is_sidecar=_is_sidecar_service(hook.service)
            ),
            timeout=int(hook.timeout_sec) + 30,
            env=merge_subprocess_env(),
        )
        append_action_log(
            action_log,
            f"collect[{i}] exit={proc.returncode}\n{(proc.stdout or '')[-1500:]}\n{(proc.stderr or '')[-1500:]}",
        )
        # Collect hooks often use `|| true`; non-zero is infra only if hard fail.
        if proc.returncode != 0:
            append_action_log(action_log, f"collect[{i}] non-zero (continuing)")


def _host_stage_path(staging: str, source: str) -> str:
    rel = source.lstrip("/").replace("/", os.sep)
    return os.path.join(staging, rel)


def copy_artifacts_to_staging(
    task: TaskInfo,
    service_map: dict[str, str],
    staging: str,
    action_log: str,
) -> None:
    """Collect declared artifacts from agent containers (Harbor best-effort).

    Missing files are logged and skipped; verification still runs and typically
    yields reward=0 (Acc FAIL), not an infra ERROR.
    """
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    for art in task.artifacts:
        cid = _service_container(service_map, art.service)
        if not cid:
            append_action_log(
                action_log,
                f"artifact skip: no container for service={art.service} ({art.source})",
            )
            continue
        host_path = _host_stage_path(staging, art.source.rstrip("/") or art.source)
        parent = os.path.dirname(host_path)
        os.makedirs(parent, exist_ok=True)
        append_action_log(
            action_log,
            f"artifact copy service={art.service} {art.source} -> {host_path}",
        )

        if art.exclude:
            # Stream a filtered tarball from the container.
            excludes = " ".join(f"--exclude={e}" for e in art.exclude)
            src = art.source.rstrip("/")
            tar_cmd = (
                f"if [ -d '{src}' ]; then tar -C '{src}' {excludes} -cf - .; "
                f"elif [ -e '{src}' ]; then tar -C \"$(dirname '{src}')\" "
                f"{excludes} -cf - \"$(basename '{src}')\"; "
                f"else echo 'missing {src}' >&2; exit 1; fi"
            )
            proc = subprocess.run(
                _service_exec(
                    cid, tar_cmd, is_sidecar=_is_sidecar_service(art.service)
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merge_subprocess_env(),
            )
            if proc.returncode != 0:
                err = proc.stderr.decode(errors="replace")[-500:]
                append_action_log(
                    action_log,
                    f"artifact missing/failed (best-effort skip): {art.source}: {err}",
                )
                continue
            os.makedirs(host_path, exist_ok=True)
            untar = subprocess.run(
                ["tar", "-xf", "-", "-C", host_path],
                input=proc.stdout,
                capture_output=True,
            )
            if untar.returncode != 0:
                # Windows may lack tar of this form; fall back to docker cp then prune.
                append_action_log(
                    action_log, "host tar failed; falling back to docker cp + prune"
                )
                if _docker_cp_artifact(cid, art, host_path, action_log):
                    _prune_excludes(host_path, art.exclude)
            continue

        _docker_cp_artifact(cid, art, host_path, action_log)


def _docker_cp_artifact(
    cid: str, art: ArtifactSpec, host_path: str, action_log: str
) -> bool:
    """Copy one artifact from container. Returns False if missing/failed (best-effort)."""
    src = art.source
    # docker cp container:src host_path
    # If source is a directory, ensure host_path exists as dir
    check = run_captured(
        _service_exec(
            cid,
            f"if [ -d '{src}' ]; then echo DIR; "
            f"elif [ -e '{src}' ]; then echo FILE; else echo MISSING; fi",
            is_sidecar=_is_sidecar_service(art.service),
        )
    )
    kind = (check.stdout or "").strip()
    if kind == "MISSING":
        append_action_log(
            action_log,
            f"artifact missing in container (best-effort skip): {src}",
        )
        return False
    if kind == "DIR":
        os.makedirs(host_path, exist_ok=True)
        cp = run_captured(["docker", "cp", f"{cid}:{src}/.", host_path])
    else:
        os.makedirs(os.path.dirname(host_path), exist_ok=True)
        cp = run_captured(["docker", "cp", f"{cid}:{src}", host_path])
    if cp.returncode != 0:
        append_action_log(
            action_log,
            f"docker cp artifact failed (best-effort skip): {src}: {cp.stderr}",
        )
        return False
    return True


def _prune_excludes(root: str, excludes: list[str]) -> None:
    root_path = Path(root)
    if not root_path.exists():
        return
    for pattern in excludes:
        for match in root_path.rglob(pattern):
            if match.is_dir():
                shutil.rmtree(match, ignore_errors=True)
            else:
                try:
                    match.unlink()
                except OSError:
                    pass


def copy_staging_to_verifier(
    task: TaskInfo,
    staging: str,
    verifier: str,
    action_log: str,
) -> None:
    """Re-materialize staged artifacts into verifier (Harbor: skip if host missing)."""
    for art in task.artifacts:
        host_path = _host_stage_path(staging, art.source.rstrip("/") or art.source)
        dest = art.source.rstrip("/") if art.source.endswith("/") else art.source
        if not os.path.exists(host_path):
            append_action_log(
                action_log,
                f"staged artifact missing (best-effort skip upload): {host_path}",
            )
            continue
        # Ensure parent dir in verifier
        parent = os.path.dirname(dest) or "/"
        run_captured(
            ["docker", "exec", verifier, "bash", "-lc", f"mkdir -p '{parent}'"]
        )
        if os.path.isdir(host_path):
            run_captured(
                ["docker", "exec", verifier, "bash", "-lc", f"mkdir -p '{dest}'"]
            )
            cp = run_captured(["docker", "cp", f"{host_path}/.", f"{verifier}:{dest}/"])
        else:
            cp = run_captured(["docker", "cp", host_path, f"{verifier}:{dest}"])
        append_action_log(action_log, f"staged -> verifier {dest} exit={cp.returncode}")
        if cp.returncode != 0:
            append_action_log(
                action_log,
                f"copy to verifier failed (best-effort skip): {dest}: {cp.stderr}",
            )


def _parse_reward(text: str) -> int | None:
    """Parse Harbor reward.txt (numeric Acc: exactly 0 or 1)."""
    t = (text or "").strip()
    if t in {"0", "1"}:
        return int(t)
    try:
        v = float(t)
        if v == 0.0:
            return 0
        if v == 1.0:
            return 1
    except ValueError:
        pass
    return None


def _parse_reward_json(text: str) -> int | None:
    """Parse Harbor reward.json → Acc 0/1 (reward==1 PASS, else FAIL)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "reward" not in data:
        return None
    try:
        v = float(data["reward"])
    except (TypeError, ValueError):
        return None
    return 1 if v == 1.0 else 0


def _fetch_verifier_reward(vname: str) -> tuple[int | None, str]:
    """Read Harbor reward channel: reward.txt preferred, else reward.json.

    Returns (reward, note). reward is None on missing/invalid (Harbor ERROR).
    """
    probe = run_captured(
        [
            "docker",
            "exec",
            vname,
            "bash",
            "-lc",
            "if [ -f /logs/verifier/reward.txt ]; then "
            "echo __KIND__=txt; cat /logs/verifier/reward.txt; "
            "elif [ -f /logs/verifier/reward.json ]; then "
            "echo __KIND__=json; cat /logs/verifier/reward.json; "
            "else echo __KIND__=missing; fi",
        ]
    )
    raw = probe.stdout or ""
    lines = raw.splitlines()
    kind = "missing"
    body_lines: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("__KIND__="):
            kind = line.split("=", 1)[1].strip()
            body_lines = lines[i + 1 :]
            break
    body = "\n".join(body_lines)

    if kind == "txt":
        reward = _parse_reward(body)
        if reward is not None:
            return reward, "reward.txt"
        return None, f"invalid reward.txt ({body!r})"
    if kind == "json":
        reward = _parse_reward_json(body)
        if reward is not None:
            return reward, "reward.json"
        return None, f"invalid reward.json ({body!r})"
    return None, "missing reward.txt and reward.json"


def _upload_tests_to_container(
    task: TaskInfo,
    cid: str,
    action_log: str,
) -> None:
    """Harbor shared mode: copy tests/ into /tests (LF-normalized)."""
    tests_dir = task.tests_dir
    if not os.path.isdir(tests_dir):
        raise InfraFailure(f"missing tests/ for {task.name}")
    test_sh = os.path.join(tests_dir, "test.sh")
    if not os.path.isfile(test_sh):
        raise InfraFailure(f"missing tests/test.sh for {task.name}")
    shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
    os.makedirs(shared_dir, exist_ok=True)
    ctx = tempfile.mkdtemp(prefix="tests-upload-", dir=shared_dir)
    try:
        _copy_tests_ctx_lf(tests_dir, ctx)
        mkdir = run_captured(
            ["docker", "exec", cid, "bash", "-lc", "mkdir -p /tests /logs/verifier"]
        )
        if mkdir.returncode != 0:
            raise InfraFailure(f"mkdir /tests failed: {mkdir.stderr}")
        cp = run_captured(["docker", "cp", f"{ctx}/.", f"{cid}:/tests/"])
        if cp.returncode != 0:
            raise InfraFailure(f"docker cp tests -> /tests failed: {cp.stderr}")
        append_action_log(action_log, f"uploaded tests/ -> {cid}:/tests")
    finally:
        _rmtree_retry(ctx)


def verify_shared(
    task: TaskInfo,
    service_map: dict[str, str],
    test_log_path: str,
    action_log: str,
) -> tuple[int, int | None]:
    """Harbor default: run /tests/test.sh in the agent container.

    Infinite retry only for docker infra (container gone / cp failed). Completed
    test.sh with missing reward is ERROR — not retried.
    """
    cid = _service_container(service_map, "main")
    if not cid:
        append_action_log(action_log, "shared verify: no agent container")
        return 1, None

    run_collect_hooks(task, service_map, action_log)

    attempts = 0
    while True:
        attempts += 1
        append_action_log(action_log, f"verify shared attempt {attempts}")
        started = run_best_effort(["docker", "start", cid])
        if started and started.returncode != 0:
            append_action_log(
                action_log,
                f"docker start {cid} failed; retry in {RETRY_SLEEP_SEC}s",
            )
            time.sleep(RETRY_SLEEP_SEC)
            continue

        try:
            _upload_tests_to_container(task, cid, action_log)
            ver_timeout = int(task.verifier_timeout_sec) + 300
            proc = run_captured(
                [
                    "docker",
                    "exec",
                    "-w",
                    "/app",
                    cid,
                    "bash",
                    "-lc",
                    "rm -f /logs/verifier/reward.txt /logs/verifier/reward.json "
                    "/logs/verifier/ctrf.json; "
                    "bash /tests/test.sh",
                ],
                timeout=ver_timeout,
                env=merge_subprocess_env(),
            )
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            with open(test_log_path, "a", encoding="utf-8") as fh:
                fh.write(
                    f"\n===== verify shared attempt {attempts} "
                    f"exit={proc.returncode} =====\n"
                )
                fh.write(out)
                fh.write("\n")
            append_action_log(action_log, f"verify exit={proc.returncode}")

            reward, note = _fetch_verifier_reward(cid)
            if reward is not None:
                append_action_log(action_log, f"reward={reward} from {note}")
                return attempts, reward

            append_action_log(
                action_log,
                f"{note}; verifier ERROR (no Acc reward), not retrying",
            )
            return attempts, None
        except InfraFailure as exc:
            append_action_log(action_log, f"verify infra: {exc}; retry")
        except subprocess.TimeoutExpired:
            append_action_log(action_log, "verify timeout; verifier ERROR, not retrying")
            return attempts, None

        time.sleep(RETRY_SLEEP_SEC)


def verify_task(
    task: TaskInfo,
    service_map: dict[str, str],
    staging_dir: str,
    test_log_path: str,
    action_log: str,
    agent_id: str,
) -> tuple[int, int | None]:
    if task.environment_mode == "separate":
        return verify_separate(
            task, service_map, staging_dir, test_log_path, action_log, agent_id
        )
    return verify_shared(task, service_map, test_log_path, action_log)


def verify_separate(
    task: TaskInfo,
    service_map: dict[str, str],
    staging_dir: str,
    test_log_path: str,
    action_log: str,
    agent_id: str,
) -> tuple[int, int | None]:
    """Collect → artifacts → verifier → reward.txt|reward.json (Harbor Acc).

    Infinite retry only for docker image ensure / verifier container start /
    InfraFailure (network/infra). Missing or invalid reward after a completed
    test.sh is a Harbor-style ERROR (return None) — not retried.
    """
    run_collect_hooks(task, service_map, action_log)
    copy_artifacts_to_staging(task, service_map, staging_dir, action_log)

    attempts = 0
    while True:
        attempts += 1
        append_action_log(action_log, f"verify attempt {attempts}")
        verifier_img = retry_until_image(
            action_log,
            "verifier image ensure",
            lambda: ensure_verifier_image(task, action_log),
        )
        vname = build_container_name(task.name, agent_id, role="verifier")
        stop_rm_container(vname)

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            vname,
            "--user",
            "root",
            "--platform",
            "linux/amd64",
            "--security-opt",
            "seccomp=unconfined",
            "--add-host=host.docker.internal:host-gateway",
            "--workdir",
            "/app",
        ]
        cmd.extend(docker_label_args(task.name, agent_id, role="verifier"))
        cpus = task.verifier_cpus if task.verifier_cpus is not None else task.cpus
        mem = task.verifier_memory_mb if task.verifier_memory_mb is not None else task.memory_mb
        if cpus:
            cmd.extend(["--cpus", str(cpus)])
        if mem:
            cmd.extend(["--memory", f"{mem}m"])
        env = _container_runtime_env()
        env.update(task.verifier_env)
        _inject_env_flags(cmd, env)
        cmd.extend([verifier_img, "sleep", "infinity"])

        result = run_captured(cmd, env=merge_subprocess_env())
        if result.returncode != 0:
            append_action_log(action_log, f"verifier run failed: {result.stderr}")
            time.sleep(RETRY_SLEEP_SEC)
            continue

        try:
            apply_cmd = (
                "mkdir -p /logs/verifier\n"
                + APPLY_PKG_PROXY_SH
                + "\n"
                + PKG_PROXY_APPLY_CALL
            )
            run_captured(
                ["docker", "exec", vname, "bash", "-lc", apply_cmd]
            )
            copy_staging_to_verifier(task, staging_dir, vname, action_log)

            ver_timeout = int(task.verifier_timeout_sec) + 300
            proc = run_captured(
                [
                    "docker",
                    "exec",
                    "-w",
                    "/app",
                    vname,
                    "bash",
                    "-lc",
                    "rm -f /logs/verifier/reward.txt /logs/verifier/reward.json "
                    "/logs/verifier/ctrf.json; "
                    "bash /tests/test.sh",
                ],
                timeout=ver_timeout,
                env=merge_subprocess_env(),
            )
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            with open(test_log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== verify attempt {attempts} exit={proc.returncode} =====\n")
                fh.write(out)
                fh.write("\n")
            append_action_log(action_log, f"verify exit={proc.returncode}")

            reward, note = _fetch_verifier_reward(vname)
            if reward is not None:
                append_action_log(action_log, f"reward={reward} from {note}")
                stop_rm_container(vname)
                return attempts, reward

            # Harbor: RewardFileNotFoundError / parse error — do not infinite-retry.
            append_action_log(
                action_log,
                f"{note}; verifier ERROR (no Acc reward), not retrying",
            )
            stop_rm_container(vname)
            return attempts, None
        except InfraFailure as exc:
            append_action_log(action_log, f"verify infra: {exc}; retry")
        except subprocess.TimeoutExpired:
            # Verifier hung — not a pull/network flake; Harbor-style ERROR.
            append_action_log(action_log, "verify timeout; verifier ERROR, not retrying")
            stop_rm_container(vname)
            return attempts, None
        finally:
            stop_rm_container(vname)

        time.sleep(RETRY_SLEEP_SEC)



def _load_agent_stats_exit(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "exit_code" in data:
            return int(data["exit_code"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _existing_agent_service_map(task: TaskInfo, agent_id: str) -> dict[str, str]:
    """Best-effort leftover agent containers for verify-only resume."""
    if task.needs_compose and task.compose_file and os.path.isfile(task.compose_file):
        project = compose_project_name(task.name, agent_id)
        try:
            with open(task.compose_file, "rb") as fh:
                compose_data = yaml.safe_load(fh) or {}
            services = list((compose_data.get("services") or {}).keys())
        except Exception:
            services = ["main"]
        if "main" not in services:
            services.insert(0, "main")
        service_map: dict[str, str] = {}
        for svc in services:
            q = run_best_effort(["docker", "compose", "-p", project, "ps", "-q", svc])
            if not q or q.returncode != 0:
                continue
            cid = (q.stdout or "").strip().splitlines()
            if cid and cid[0].strip():
                service_map[svc] = cid[0].strip()
        return service_map

    result = run_best_effort(
        [
            "docker",
            "ps",
            "-a",
            "--format",
            "{{.Names}}\t{{.Status}}",
            *docker_container_filter_args(task.name, agent_id),
            "--filter",
            f"label={DOCKER_ROLE_LABEL_KEY}=agent",
        ]
    )
    names: list[tuple[str, bool]] = []
    if result and result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 1)
            name = parts[0].strip()
            status = parts[1] if len(parts) > 1 else ""
            if name:
                names.append((name, status.lower().startswith("up")))
    if not names:
        return {}
    running = [n for n, up in names if up]
    picked = running[0] if running else names[0][0]
    return {"main": picked}


def _is_local_task_image(ref: str | None, task: TaskInfo) -> bool:
    """True for ``tb2-local/<task>:...`` only — never Hub bases."""
    if not ref:
        return False
    prefix = f"{LOCAL_IMAGE_PREFIX}/{task.name}"
    return ref == prefix or ref.startswith(prefix + ":")


def cleanup_task_artifacts(task: TaskInfo, image: str | None, agents: list[str]) -> None:
    for agent_id in agents:
        cleanup_task_agent_containers(task.name, agent_id)

    if not KEEP_TASK_IMAGES:
        refs: list[str] = []
        for candidate in (
            image,
            common_env_image_tag(task),
            local_image_tag(task),
            verifier_image_tag(task),
            *[agent_env_image_tag(task, agent_id) for agent_id in agents],
            *[
                agent_env_image_tag(task, agent_id)
                for agent_id in AGENT_FLAG_MAP.values()
            ],
        ):
            if _is_local_task_image(candidate, task) and candidate not in refs:
                refs.append(candidate)
        # Also drop older verifier-* / sidecar tags for this task.
        listed = run_best_effort(
            [
                "docker",
                "images",
                f"{LOCAL_IMAGE_PREFIX}/{task.name}",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ]
        )
        if listed and listed.returncode == 0:
            for line in (listed.stdout or "").splitlines():
                ref = line.strip()
                if _is_local_task_image(ref, task) and ref not in refs:
                    refs.append(ref)
        for ref in refs:
            run_best_effort(["docker", "rmi", "-f", ref])
        with _IMAGE_LOCK:
            for key in list(_IMAGE_CACHE.keys()):
                if key == task.name or key.startswith(f"{task.name}::"):
                    _IMAGE_CACHE.pop(key, None)

    _cleanup_stale_tests_upload(os.path.join(TRAJ_DIR, "_shared", task.name))
    run_best_effort(["docker", "container", "prune", "-f"])
    run_best_effort(["docker", "image", "prune", "-f"])
    run_best_effort(["docker", "network", "prune", "-f"])
    run_best_effort(["docker", "volume", "prune", "-f"])
    _prune_buildkit_keep_pip()


def _prune_buildkit_keep_pip() -> None:
    """Drop unused BuildKit layers; keep RUN --mount=type=cache (id=tb2-pip).

    Unfiltered ``docker builder prune -af`` would wipe the seeded pip cache.
    Hub/pre-pulled images are not touched (``image prune -f`` is dangling only).
    Docker Desktop Troubleshoot → Clean / Purge data is unrelated and wipes
    images, containers, and this cache mount; never use it to shrink the VHDX.
    """
    run_best_effort(
        [
            "docker",
            "builder",
            "prune",
            "-af",
            "--filter",
            "type!=exec.cachemount",
        ]
    )



def run_agent_on_task(
    task: TaskInfo,
    agent_id: str,
    shared_image: str,
    *,
    force_agent: bool = False,
) -> dict:
    adapter = get_adapter(agent_id)
    task_dir = traj_task_dir(agent_id, task.name)
    run_dir = os.path.join(task_dir, "run")
    action_log = os.path.join(task_dir, "action.log")
    test_log = os.path.join(task_dir, "test.log")
    staging = os.path.join(task_dir, "artifacts_staging")
    os.makedirs(task_dir, exist_ok=True)

    start_wall = time.time()
    start_iso = datetime.now(timezone.utc).isoformat()
    agent_stats_path = os.path.join(run_dir, "agent_stats.json")
    skip_agent = (not force_agent) and os.path.isfile(agent_stats_path)

    container_name = None
    service_map: dict[str, str] = {}
    agent_exit = None
    agent_attempts = 0
    resumed_verify = False

    if skip_agent:
        resumed_verify = True
        agent_exit = _load_agent_stats_exit(agent_stats_path)
        service_map = _existing_agent_service_map(task, agent_id)
        container_name = service_map.get("main")
        append_action_log(
            action_log,
            f"RESUME verify-only: skip agent (found {agent_stats_path}); "
            f"container={container_name!r} (--redo to force agent)",
        )
    else:
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)
        os.makedirs(run_dir, exist_ok=True)
        with open(action_log, "w", encoding="utf-8") as fh:
            fh.write("")
        with open(test_log, "w", encoding="utf-8") as fh:
            fh.write("")
        with open(os.path.join(task_dir, "agent_container_output.log"), "wb") as fh:
            fh.write(b"")

        append_action_log(
            action_log,
            f"start agent={agent_id} task={task.name} image={shared_image} "
            f"compose={task.needs_compose}",
        )
        adapter.prepare_run_dir(run_dir, task)
        mounts = adapter.install_mounts()
        agent_image = retry_until_image(
            action_log,
            f"agent-env {agent_id}",
            lambda: ensure_agent_env_image(
                task, shared_image, agent_id, action_log
            ),
        )
        append_action_log(action_log, f"agent image: {agent_image}")

        while True:
            agent_attempts += 1
            append_action_log(
                action_log, f"agent attempt {agent_attempts}: new container"
            )
            cleanup_task_agent_containers(task.name, agent_id)
            container_name = None
            service_map = {}

            for marker_name in ("agent_phase_started", "agent_stats.json"):
                try:
                    os.remove(os.path.join(run_dir, marker_name))
                except FileNotFoundError:
                    pass

            adapter.prepare_run_dir(run_dir, task)
            script = adapter.build_entryscript(task)
            entryscript_host = os.path.join(run_dir, "entryscript.sh")
            with open(entryscript_host, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(script)

            host_ceiling_hit = False
            try:
                if task.needs_compose:
                    container_name, agent_exit, host_ceiling_hit, service_map = (
                        docker_compose_run_agent(
                            image=agent_image,
                            task=task,
                            agent_id=agent_id,
                            run_dir=run_dir,
                            mounts=mounts,
                            entryscript_host=entryscript_host,
                            action_log=action_log,
                        )
                    )
                else:
                    container_name, agent_exit, host_ceiling_hit, service_map = (
                        docker_run_agent(
                            image=agent_image,
                            task=task,
                            agent_id=agent_id,
                            run_dir=run_dir,
                            mounts=mounts,
                            entryscript_host=entryscript_host,
                            action_log=action_log,
                        )
                    )
            except InfraFailure as exc:
                append_action_log(action_log, f"InfraFailure: {exc}; sleep and retry")
                cleanup_task_agent_containers(task.name, agent_id)
                time.sleep(RETRY_SLEEP_SEC)
                continue

            agent_trial_started = (
                host_ceiling_hit
                or os.path.isfile(os.path.join(run_dir, "agent_phase_started"))
                or os.path.isfile(os.path.join(run_dir, "agent_stats.json"))
            )

            if agent_exit != 0 and not agent_trial_started:
                append_action_log(
                    action_log,
                    f"install/infra exit={agent_exit}; destroy, sleep, retry",
                )
                cleanup_task_agent_containers(task.name, agent_id)
                time.sleep(RETRY_SLEEP_SEC)
                continue

            if agent_exit != 0:
                kind = (
                    "HOST_CEILING"
                    if host_ceiling_hit
                    else ("AGENT_TIMEOUT" if agent_exit == 124 else "AGENT_CRASH")
                )
                append_action_log(
                    action_log,
                    f"{kind} (exit={agent_exit}): proceed to verify leftovers",
                )
            break

        assert container_name is not None and service_map

    if container_name:
        run_best_effort(["docker", "start", container_name])

    verify_attempts, reward = verify_task(
        task, service_map, staging, test_log, action_log, agent_id
    )
    verifier_error = reward is None
    passed = reward == 1
    end_iso = datetime.now(timezone.utc).isoformat()
    elapsed = time.time() - start_wall

    stats = {
        "task": task.name,
        "agent": agent_id,
        "passed": passed,
        "reward": reward,
        "elapsed_seconds": round(elapsed, 3),
        "agent_exit_code": agent_exit,
        "agent_attempts": agent_attempts,
        "verify_attempts": verify_attempts,
        "start_time": start_iso,
        "end_time": end_iso,
        "compose": task.needs_compose,
    }
    if verifier_error:
        stats["verifier_error"] = True
    if resumed_verify:
        stats["resumed_verify"] = True
    write_stats(agent_id, task.name, stats)
    # Acc: reward=1 PASS / 0 FAIL / None (Harbor RewardFile* ERROR) → not PASS
    save_eval_result(agent_id, task.name, passed)
    append_action_log(
        action_log,
        f"done passed={passed} reward={reward}"
        + (" verifier_error=True" if verifier_error else ""),
    )

    cleanup_task_agent_containers(task.name, agent_id)
    return stats


def prepare_shared_image(task: TaskInfo, agents: list[str]) -> str:
    shared_dir = os.path.join(TRAJ_DIR, "_shared", task.name)
    os.makedirs(shared_dir, exist_ok=True)
    _cleanup_stale_tests_upload(shared_dir)
    action_log = os.path.join(shared_dir, "image.log")

    def _build_common() -> str:
        base = ensure_task_image(task, action_log)
        append_action_log(
            action_log,
            f"base image ready: {base}; building common-env layer",
        )
        common = ensure_common_env_image(task, base, action_log)
        if task.needs_compose:
            sidecars = ensure_compose_sidecar_images(task, action_log)
            append_action_log(
                action_log,
                f"compose sidecars ready: {sidecars or 'none'}",
            )
        return common

    common = retry_until_image(action_log, "image ensure", _build_common)
    for agent_id in agents:
        retry_until_image(
            action_log,
            f"agent-env {agent_id}",
            lambda aid=agent_id: ensure_agent_env_image(
                task, common, aid, action_log
            ),
        )
    return common
