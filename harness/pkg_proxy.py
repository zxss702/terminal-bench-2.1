"""Squid is apt/dnf only. pip uses Clash + a host pip cache mount.

Traffic map
-----------
- Host ``docker pull`` / BuildKit FROM metadata: Clash ``127.0.0.1:7897``
- Dockerfile RUN git/curl/LLM/pip/other + agent/verifier general HTTP: Clash
  ``host.docker.internal:7897`` (``HTTP_PROXY``)
- Dockerfile RUN + runtime/test apt, dnf: Squid ``:3144`` via apt.conf /
  dnf.conf. Squid's parent is Clash.
- pip cache: host ``.cache/pip`` bind-mounted at ``/root/.cache/pip``
  (``PIP_CACHE_DIR``). Misses go to official PyPI through Clash.

Task files under ``tasks/`` are not modified. The harness injects a small
snippet into the LF-normalized build *copy* (env-ctx / verifier-ctx) after
each ``FROM``, because BuildKit's ``HTTP_PROXY`` applies to every RUN.
"""

from __future__ import annotations

from pathlib import Path

from .config import PIP_CACHE_DEST, PROXY_CONTAINER_URL, PROXY_NO_PROXY
from .dnf_cacher import SQUID_CACHER_URL, resolve_squid_cacher_enabled


PKG_PROXY_MARK = "# tb2-pkg-proxy"

# POSIX. Keep in sync with env_install_common.sh.
APPLY_PKG_PROXY_SH = r"""
tb2_apply_pkg_proxy() {
    if [ -f /etc/pip.conf ] && grep -q tb2-pkg-proxy /etc/pip.conf 2>/dev/null; then
        rm -f /etc/pip.conf
    fi
    pkg="${TB2_PKG_PROXY:-}"
    if [ -z "$pkg" ]; then
        rm -f /etc/apt/apt.conf.d/99tb2-squid 2>/dev/null || true
        return 0
    fi
    if [ -d /etc/apt ]; then
        mkdir -p /etc/apt/apt.conf.d
        printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
            "$pkg" "$pkg" > /etc/apt/apt.conf.d/99tb2-squid
    fi
    if [ -f /etc/dnf/dnf.conf ]; then
        if ! grep -q tb2-pkg-proxy /etc/dnf/dnf.conf 2>/dev/null; then
            printf '\n# tb2-pkg-proxy\nproxy=%s\n' "$pkg" >> /etc/dnf/dnf.conf
        fi
    fi
    if [ -f /etc/yum.conf ]; then
        if ! grep -q tb2-pkg-proxy /etc/yum.conf 2>/dev/null; then
            printf '\n# tb2-pkg-proxy\nproxy=%s\n' "$pkg" >> /etc/yum.conf
        fi
    fi
}
""".strip()

PKG_PROXY_APPLY_CALL = "tb2_apply_pkg_proxy"

# Inserted after each eligible FROM in the build-copy Dockerfile.
PKG_PROXY_DOCKERFILE_SNIPPET = """\
# tb2-pkg-proxy
ARG TB2_PKG_PROXY=
RUN if [ -n "${TB2_PKG_PROXY:-}" ]; then \
      if [ -d /etc/apt ]; then \
        mkdir -p /etc/apt/apt.conf.d; \
        printf 'Acquire::http::Proxy "%s";\\nAcquire::https::Proxy "%s";\\n' \
          "$TB2_PKG_PROXY" "$TB2_PKG_PROXY" > /etc/apt/apt.conf.d/99tb2-squid; \
      fi; \
      if [ -f /etc/dnf/dnf.conf ] && ! grep -q tb2-pkg-proxy /etc/dnf/dnf.conf; then \
        printf '\\n# tb2-pkg-proxy\\nproxy=%s\\n' "$TB2_PKG_PROXY" >> /etc/dnf/dnf.conf; \
      fi; \
      if [ -f /etc/yum.conf ] && ! grep -q tb2-pkg-proxy /etc/yum.conf; then \
        printf '\\n# tb2-pkg-proxy\\nproxy=%s\\n' "$TB2_PKG_PROXY" >> /etc/yum.conf; \
      fi; \
      rm -f /etc/pip.conf; \
    fi
"""

_SKIP_FROM_NEEDLES = (
    "distroless",
    "nanoserver",
    "servercore",
    "windows/",
)


def clash_container_url() -> str:
    return PROXY_CONTAINER_URL or "http://host.docker.internal:7897"


def pkg_proxy_url() -> str:
    """Squid URL for apt/dnf, or empty when the cache is off."""
    if resolve_squid_cacher_enabled():
        return SQUID_CACHER_URL
    return ""


def general_proxy_env() -> dict[str, str]:
    """HTTP(S)_PROXY for git/curl/LLM/pip — always Clash, never Squid."""
    proxy = clash_container_url()
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "ALL_PROXY": proxy,
        "all_proxy": proxy,
        "NO_PROXY": PROXY_NO_PROXY,
        "no_proxy": PROXY_NO_PROXY,
    }


def pkg_proxy_runtime_env() -> dict[str, str]:
    """Extra env so apt/dnf use Squid while HTTP_PROXY stays Clash."""
    pkg = pkg_proxy_url()
    if not pkg:
        return {}
    return {
        "TB2_PKG_PROXY": pkg,
        "TB2_USE_SQUID_CACHER": "1",
        "TB2_USE_DNF_CACHER": "1",
        "TB2_SQUID_CACHER_URL": pkg,
        "TB2_DNF_CACHER_URL": pkg,
    }


def container_proxy_env() -> dict[str, str]:
    env = general_proxy_env()
    env.update(pkg_proxy_runtime_env())
    env["PIP_CACHE_DIR"] = PIP_CACHE_DEST
    return env


def _parse_from_image(from_line: str) -> str:
    line = from_line.split("#", 1)[0].strip()
    parts = line.split()
    if not parts or parts[0].upper() != "FROM":
        return ""
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok.startswith("--"):
            if "=" not in tok:
                i += 2
            else:
                i += 1
            continue
        return tok.strip("\"'")
    return ""


def _should_skip_from_image(image: str) -> bool:
    if not image:
        return True
    name = image.split("@", 1)[0]
    if name == "scratch" or name.endswith("/scratch"):
        return True
    lower = name.lower()
    return any(needle in lower for needle in _SKIP_FROM_NEEDLES)


def _is_from_instruction(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return False
    head = stripped.split("#", 1)[0].strip()
    if not head:
        return False
    token = head.split(None, 1)[0]
    return token.upper() == "FROM"


def inject_pkg_proxy_text(text: str) -> str:
    """Insert the apt/dnf snippet after each eligible FROM."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for idx, line in enumerate(lines):
        out.append(line)
        if not _is_from_instruction(line):
            continue
        image = _parse_from_image(line.strip())
        if _should_skip_from_image(image):
            continue
        look = idx + 1
        while look < len(lines) and not lines[look].strip():
            look += 1
        if look < len(lines) and PKG_PROXY_MARK in lines[look]:
            continue
        out.append(PKG_PROXY_DOCKERFILE_SNIPPET.rstrip("\n"))
    return "\n".join(out)


def inject_pkg_proxy_into_dockerfile(path: str) -> bool:
    target = Path(path)
    original = target.read_text(encoding="utf-8")
    updated = inject_pkg_proxy_text(original)
    normalized = original.replace("\r\n", "\n").replace("\r", "\n")
    if updated == normalized:
        return False
    target.write_text(updated, encoding="utf-8", newline="\n")
    return True
