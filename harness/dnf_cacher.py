"""Unified Squid package cache (apt / dnf only).

Historically named dnf-cacher (port 3144). pip uses Clash plus a host
``.cache/pip`` bind mount. General HTTP (git, curl, LLM APIs) must NOT use
this proxy — those go through Clash. Squid's upstream peer is Clash
(see start_dnf_cacher.ps1).
"""

import os
import socket

from .config import DNF_CACHER_PORT, DNF_CACHER_URL, CACHE_DIR


DNF_CACHER_CONTAINER_NAME = "tb2-dnf-cacher"
DNF_CACHER_CACHE_DIR = os.path.join(CACHE_DIR, "dnf-cacher")
DNF_CACHER_PROBE_HOST = "127.0.0.1"

# Aliases — Squid is the pkg cache for apt/dnf (not pip).
SQUID_CACHER_URL = DNF_CACHER_URL
SQUID_CACHER_PORT = DNF_CACHER_PORT
# BuildKit on Docker Desktop uses host network; reach published ports via localhost.
SQUID_BUILD_CACHER_URL = f"http://{DNF_CACHER_PROBE_HOST}:{DNF_CACHER_PORT}"


def probe_dnf_cacher(host=DNF_CACHER_PROBE_HOST, port=None, timeout=1.0):
    port = int(port or DNF_CACHER_PORT)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_dnf_cacher_enabled():
    """True when Squid pkg cache should be used (env override or port up)."""
    for key in (
        "TB2_USE_SQUID_CACHER",
        "TB2_USE_PKG_CACHER",
        "TB2_USE_DNF_CACHER",
    ):
        override = os.getenv(key, "").strip().lower()
        if override in {"0", "false", "no", "off"}:
            return False
        if override in {"1", "true", "yes", "on"}:
            return True
    return probe_dnf_cacher()


# Prefer these names in new code.
probe_squid_cacher = probe_dnf_cacher
resolve_squid_cacher_enabled = resolve_dnf_cacher_enabled


def dnf_cacher_status_line():
    if resolve_dnf_cacher_enabled():
        return f"pkg 缓存 (Squid): 已启用 ({DNF_CACHER_URL}) — 仅 apt/dnf"
    return (
        "pkg 缓存 (Squid): 未启用（先运行 .\\.cache\\start_dnf_cacher.ps1；"
        "未启用时 apt/dnf 也走 Clash）"
    )


squid_cacher_status_line = dnf_cacher_status_line
