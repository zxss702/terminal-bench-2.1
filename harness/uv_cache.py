"""Host-side Astral uv/uvx binary cache for TB verify (test.sh) and docker builds."""

from __future__ import annotations

import os
import re

from .config import UV_CACHE_DIR, UV_CACHE_DEST, UV_VERSION

UV_LINUX_ARCH = "x86_64-unknown-linux-gnu"
UV_DOCKER_CONTEXT = "tb2_uv"

# Official install.sh and GitHub release pins used by tasks/*/tests/Dockerfile.
_ASTRAL_INSTALL_RUN_RE = re.compile(
    r"^RUN\s+curl\s+\S*\s+https://astral\.sh/uv/"
    r"([0-9]+\.[0-9]+\.[0-9]+)/install\.sh\s*\|\s*sh\s*$"
)


def uv_version_dir(version: str | None = None) -> str:
    ver = (version or UV_VERSION).strip() or "0.9.5"
    return os.path.join(UV_CACHE_DIR, ver)


def uv_bin_paths(version: str | None = None) -> tuple[str, str]:
    d = uv_version_dir(version)
    return os.path.join(d, "uv"), os.path.join(d, "uvx")


def uv_release_tarball_path(version: str | None = None) -> str:
    ver = (version or UV_VERSION).strip() or "0.9.5"
    return os.path.join(
        uv_version_dir(ver), f"uv-{UV_LINUX_ARCH}.tar.gz"
    )


def resolve_uv_cache_ready(version: str | None = None) -> bool:
    """True when host cache has executable uv + uvx for the pinned version."""
    override = os.getenv("TB2_USE_UV_CACHE", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    uv, uvx = uv_bin_paths(version)
    return os.path.isfile(uv) and os.path.isfile(uvx)


def resolve_uv_tarball_ready(version: str | None = None) -> bool:
    """True when release tarball exists for offline docker builds (linux/amd64)."""
    override = os.getenv("TB2_USE_UV_CACHE", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    path = uv_release_tarball_path(version)
    return os.path.isfile(path) and os.path.getsize(path) > 1_000_000


# Known amd64 digests for Dockerfile pins we rewrite offline.
_UV_AMD64_CHECKSUMS = {
    "0.9.7": "b26fcc8dfa1c39b5a5613445af3be3eefda45d9a39359bee271eafe34913583e",
}


def uv_cache_status_line() -> str:
    ver = UV_VERSION
    parts: list[str] = []
    if resolve_uv_cache_ready(ver):
        parts.append(f"verify={UV_CACHE_DEST}/{ver}")
    build_vers = [v for v in _UV_AMD64_CHECKSUMS if resolve_uv_tarball_ready(v)]
    if build_vers:
        parts.append("docker-build=" + ",".join(build_vers))
    if parts:
        return f"uv 缓存: 已启用 ({'; '.join(parts)})"
    return (
        f"uv 缓存: 未就绪（verify/build 仍会走 GitHub；先运行 "
        f".\\.cache\\cache_uv.ps1 -Version {ver}）"
    )


def _uv_offline_install_lines(version: str) -> list[str]:
    checksum = _UV_AMD64_CHECKSUMS[version]
    tarball = f"uv-{UV_LINUX_ARCH}.tar.gz"
    return [
        f"ARG UV_VERSION={version}",
        "# tb2: uv from host cache (skip astral.sh / GitHub curl)",
        f"COPY --from={UV_DOCKER_CONTEXT} {tarball} /tmp/uv.tar.gz",
        "RUN set -eux; \\",
        f'    target="{UV_LINUX_ARCH}"; \\',
        f'    checksum="{checksum}"; \\',
        '    echo "${checksum}  /tmp/uv.tar.gz" | sha256sum -c -; \\',
        "    tar -xzf /tmp/uv.tar.gz -C /tmp; \\",
        '    install -m 0755 "/tmp/uv-${target}/uv" /usr/local/bin/uv; \\',
        '    install -m 0755 "/tmp/uv-${target}/uvx" /usr/local/bin/uvx; \\',
        '    rm -rf /tmp/uv.tar.gz "/tmp/uv-${target}"',
    ]


def _run_block_end(lines: list[str], run_start: int) -> int:
    """Index after the last line of a RUN (honors trailing \\ continuations)."""
    end = run_start
    while end < len(lines):
        stripped = lines[end].rstrip()
        end += 1
        if stripped.endswith("\\"):
            continue
        break
    return end


def rewrite_uv_curl_to_cache(dockerfile_text: str) -> tuple[str | None, str | None]:
    """Replace curl-based uv install with COPY from BuildKit context.

    Handles:
      - ARG UV_VERSION=… + RUN curl … astral-sh/uv/releases/download/…
      - RUN curl … https://astral.sh/uv/<ver>/install.sh | sh

    Returns (rewritten_text, uv_version) or (None, None) if no match / cache miss.
    Bench builds are always --platform linux/amd64.
    """
    text = dockerfile_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    arg_re = re.compile(r"^ARG\s+UV_VERSION=(\S+)\s*$")
    start: int | None = None
    end: int | None = None
    version: str | None = None

    for i, line in enumerate(lines):
        m = arg_re.match(line)
        if m:
            j = i + 1
            while j < len(lines) and (
                not lines[j].strip() or lines[j].strip().startswith("#")
            ):
                j += 1
            if j >= len(lines) or not lines[j].lstrip().startswith("RUN "):
                continue
            block = "\n".join(lines[j:])
            if "astral-sh/uv/releases/download" not in block:
                continue
            start = i
            version = m.group(1).strip()
            end = _run_block_end(lines, j)
            break

        m_astral = _ASTRAL_INSTALL_RUN_RE.match(line)
        if m_astral:
            start = i
            version = m_astral.group(1)
            end = _run_block_end(lines, i)
            break

    if start is None or end is None or not version:
        return None, None
    if version not in _UV_AMD64_CHECKSUMS or not resolve_uv_tarball_ready(version):
        return None, None

    replacement = _uv_offline_install_lines(version)
    new_lines = lines[:start] + replacement + lines[end:]
    return "\n".join(new_lines), version


def seed_uv_from_cache_cmd() -> str:
    """Bash fragment: copy cached uv/uvx into $HOME/.local/bin before test.sh.

    Official test.sh always runs `curl astral.sh/uv/... | sh`. When GitHub SSL
    flakes, that install fails and uvx is missing — but reward.txt is still
    written as 0 with exit 0. Seeding first keeps verify runnable offline of
    GitHub Releases.
    """
    return f"""\
seed_uv_from_tb2_cache() {{
    CACHE_ROOT="{UV_CACHE_DEST}"
    VER="{UV_VERSION}"
    SRC="$CACHE_ROOT/$VER"
    if [ ! -x "$SRC/uv" ] || [ ! -x "$SRC/uvx" ]; then
        echo "[WARN] uv cache not mounted/ready at $SRC (host: run .cache/cache_uv.ps1)"
        return 0
    fi
    mkdir -p "$HOME/.local/bin"
    cp -f "$SRC/uv" "$HOME/.local/bin/uv"
    cp -f "$SRC/uvx" "$HOME/.local/bin/uvx"
    chmod +x "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"
    # test.sh does: source $HOME/.local/bin/env
    cat > "$HOME/.local/bin/env" <<'EOF'
# seeded by tb2 uv cache
export PATH="$HOME/.local/bin:$PATH"
EOF
    export PATH="$HOME/.local/bin:$PATH"
    echo "[INFO] seeded uv $VER from $SRC -> $HOME/.local/bin"
    "$HOME/.local/bin/uv" --version 2>/dev/null || true
}}
seed_uv_from_tb2_cache
"""
