"""Paths and constants for the DIY Docker harness (Terminal-Bench 2.1)."""

import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = (
    os.getenv("TB2_TASKS_DIR", "").strip()
    or os.path.join(PROJECT_ROOT, "tasks")
)
# Backward-compat alias.
BENCH_DIR = TASKS_DIR
TRAJ_DIR = os.path.join(PROJECT_ROOT, "traj")
CACHE_DIR = os.path.join(PROJECT_ROOT, ".cache")

LOGORYTHIA_PACKAGE_SRC = (
    os.getenv("LOGORYTHIA_PACKAGE_SRC", "").strip()
    or os.path.join(PROJECT_ROOT, "logorythia_linux_x86.zip")
)
LOGORYTHIA_PACKAGE_DEST = "/logorythia_linux_x86.zip"
LOGORYTHIA_INSTALL_SCRIPT_SRC = os.path.join(PROJECT_ROOT, "env_install_logorythia.sh")
LOGORYTHIA_INSTALL_SCRIPT_DEST = "/env_install_logorythia.sh"
AGENT_INFO_SRC = os.path.join(PROJECT_ROOT, "syAgentInfo.json")
AGENT_INFO_DEST_DIR = "/root/Documents/Logorythia"
AGENT_INFO_DEST = f"{AGENT_INFO_DEST_DIR}/syAgentInfo.json"

COMMON_INSTALL_SCRIPT_SRC = os.path.join(PROJECT_ROOT, "env_install_common.sh")
COMMON_INSTALL_SCRIPT_DEST = "/env_install_common.sh"
COMMON_ENV_DOCKERFILE = os.path.join(PROJECT_ROOT, "harness", "Dockerfile.common-env")
COMMON_ENV_MARKER = "/opt/tb2-common-env.ok"
AGENT_ENV_DOCKERFILE = os.path.join(PROJECT_ROOT, "harness", "Dockerfile.agent-env")
AGENT_ENV_MARKER = "/opt/tb2-agent.ok"
AGENTS_CACHE_DEST = "/agent-packages"

CLAUDE_INSTALL_SCRIPT_SRC = os.path.join(PROJECT_ROOT, "env_install_claude.sh")
CLAUDE_INSTALL_SCRIPT_DEST = "/env_install_claude.sh"
CLAUDE_SETTINGS_SRC = os.path.join(PROJECT_ROOT, "claude_settings.json")
CLAUDE_CACHE_DIR = (
    os.getenv("CLAUDE_CACHE_DIR", "").strip()
    or os.path.join(CACHE_DIR, "claude-code")
)
CLAUDE_PACKAGES_DEST = "/claude-packages"

# Copied into Dockerfile.agent-env as env_install_agent.sh.
AGENT_INSTALL_SCRIPTS = {
    "autogen": os.path.join(PROJECT_ROOT, "env_install_auto.sh"),
    "swe-agent": os.path.join(PROJECT_ROOT, "env_install_swe.sh"),
    "agentflow": os.path.join(PROJECT_ROOT, "env_install_agentflow.sh"),
    "claude-code": CLAUDE_INSTALL_SCRIPT_SRC,
    "logorythia": LOGORYTHIA_INSTALL_SCRIPT_SRC,
}
AGENTS_NEED_PYTHON_310 = frozenset({"autogen", "swe-agent"})

DNF_CACHER_PORT = os.getenv("TB2_DNF_CACHER_PORT", "3144").strip() or "3144"
DNF_CACHER_HOST = (
    os.getenv("TB2_DNF_CACHER_HOST", "host.docker.internal").strip()
    or "host.docker.internal"
)
DNF_CACHER_URL = (
    os.getenv("TB2_DNF_CACHER_URL", "").strip()
    or f"http://{DNF_CACHER_HOST}:{DNF_CACHER_PORT}"
)

AGENTS_CACHE_DIR = os.path.join(CACHE_DIR, "agents")
DNF_CACHER_CACHE_DIR = os.path.join(CACHE_DIR, "dnf-cacher")

# Linux/amd64 pip HTTP+wheel cache (populated by scripts/cache_agent_packages.ps1).
# Do not reuse the Windows user profile ~/.cache/pip.
PIP_CACHE_DIR = (
    os.getenv("TB2_PIP_CACHE_DIR", "").strip()
    or os.path.join(CACHE_DIR, "pip")
)
PIP_CACHE_DEST = "/root/.cache/pip"

UV_VERSION = os.getenv("TB2_UV_VERSION", "0.9.5").strip() or "0.9.5"
UV_CACHE_DIR = (
    os.getenv("TB2_UV_CACHE_DIR", "").strip()
    or os.path.join(CACHE_DIR, "uv")
)
UV_CACHE_DEST = "/tb2-uv-cache"

# Ensembl/VEP Perl APIs (INSTALL.pl --AUTO a); used by atrx-vep-crispr builds.
VEP_APIS_RELEASE = os.getenv("TB2_VEP_APIS_RELEASE", "115").strip() or "115"
VEP_APIS_CACHE_DIR = (
    os.getenv("TB2_VEP_APIS_CACHE_DIR", "").strip()
    or os.path.join(CACHE_DIR, "vep-apis")
)

# DeepSeek OpenAI-compatible endpoint for comparison agents.
DEEPSEEK_BASE_URL = os.getenv(
    "TB2_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
).strip() or "https://api.deepseek.com"
DEEPSEEK_MODEL = os.getenv("TB2_DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp").strip() or (
    "deepseek-v4-flash-vision-exp"
)
# Claude Code talks Anthropic Messages via DeepSeek's /anthropic gateway.
ANTHROPIC_BASE_URL = os.getenv(
    "TB2_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
).strip() or "https://api.deepseek.com/anthropic"


def openai_compatible_base_url(root: str | None = None) -> str:
    """Return an OpenAI SDK base_url that already ends with /v1 (no double suffix)."""
    url = (root or DEEPSEEK_BASE_URL).rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


API_KEYS = {
    "logorythia": "sk-d00e19ca51394eb891d02236e4ad7af7",
    "swe-agent": "sk-9da9ad62f8ee4df9a47670ae144eb50a",
    "autogen": "sk-8475daef219e4c29add745446b807ce0",
    "agentflow": "sk-0b6ff99cafc44f1ab5bedbdcadb3e8e9",
    "claude-code": "sk-d9a22952aba94967bec9164f09fe3b04",
}

PROXY_HOST_URL = (
    os.getenv("TB2_PROXY", "").strip()
    or os.getenv("SWE_BENCH_PROXY", "").strip()
    or os.getenv("HTTP_PROXY", "").strip()
    or os.getenv("http_proxy", "").strip()
    or "http://127.0.0.1:7897"
)
PROXY_CONTAINER_URL = (
    os.getenv("TB2_CONTAINER_PROXY", "").strip()
    or os.getenv("SWE_BENCH_CONTAINER_PROXY", "").strip()
    or "http://host.docker.internal:7897"
)
# Host Clash for docker pull / BuildKit FROM metadata. Never Squid :3144.
# Do not inherit HTTP_PROXY — that is often the pkg cache.
CLASH_HOST_URL = (
    os.getenv("TB2_CLASH_PROXY", "").strip() or "http://127.0.0.1:7897"
)
PROXY_NO_PROXY = (
    os.getenv("NO_PROXY", "").strip()
    or os.getenv("no_proxy", "").strip()
    or "localhost,127.0.0.1,host.docker.internal"
)

LOCAL_IMAGE_PREFIX = os.getenv("TB2_LOCAL_IMAGE_PREFIX", "tb2-local").strip() or (
    "tb2-local"
)
DOCKER_MANAGED_LABEL_KEY = "tb2.managed"
DOCKER_NAMESPACE_LABEL_KEY = "tb2.namespace"
DOCKER_INSTANCE_LABEL_KEY = "tb2.instance_id"
DOCKER_AGENT_LABEL_KEY = "tb2.agent"
DOCKER_ROLE_LABEL_KEY = "tb2.role"

RETRY_SLEEP_SEC = 5
INSTALL_BUDGET_SEC = int(os.getenv("TB2_INSTALL_BUDGET_SEC", "3600").strip() or "3600")

_KEEP_RAW = os.getenv("TB2_KEEP_TASK_IMAGES", "").strip().lower()
KEEP_TASK_IMAGES = _KEEP_RAW in {"1", "true", "yes", "on"}
_REMOVE_PULLED_RAW = os.getenv("TB2_REMOVE_PULLED_IMAGES", "1").strip().lower()
REMOVE_PULLED_IMAGES = (not KEEP_TASK_IMAGES) and _REMOVE_PULLED_RAW not in {
    "0",
    "false",
    "no",
    "off",
}

AGENT_FLAG_MAP = {
    "logorythia": "logorythia",
    "swe": "swe-agent",
    "auto": "autogen",
    "agentflow": "agentflow",
    "claude": "claude-code",
}
