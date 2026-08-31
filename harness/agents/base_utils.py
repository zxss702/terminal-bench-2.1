"""Shared host-path helpers for adapters."""

from __future__ import annotations

import os


class InfraFailure(RuntimeError):
    """Infrastructure failure that should trigger container recreate + retry."""


def resolve_required_host_file(path: str, label: str, env_var: str | None = None) -> str:
    resolved = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(resolved):
        return resolved
    hint = f"；可通过 {env_var} 指定实际路径" if env_var else ""
    raise InfraFailure(f"{label} 不存在: {resolved}{hint}")


def resolve_required_host_dir(path: str, label: str, env_var: str | None = None) -> str:
    resolved = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(resolved):
        return resolved
    hint = f"；可通过 {env_var} 指定实际路径" if env_var else ""
    if os.path.isfile(resolved):
        raise InfraFailure(
            f"{label} 路径是文件而非目录: {resolved}{hint}\n"
            f"请删除该文件后创建所需缓存目录"
        )
    raise InfraFailure(f"{label} 不存在: {resolved}{hint}")


def ensure_lf_script_copy(src: str, dest: str) -> str:
    """Copy a shell script to dest with Unix LF endings (Windows CRLF breaks `set -e`)."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    raw = open(src, "rb").read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    with open(dest, "wb") as fh:
        fh.write(raw)
    return dest
