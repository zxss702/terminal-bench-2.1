#!/bin/sh
# Microsoft AutoGen AgentChat (2026 / Magentic-One).
# NOT ag2 v1.0 (no `autogen` import) and NOT AG2 Classic ConversableAgent.
# pip via Clash + PIP_CACHE_DIR. NO apt.
set -e

info() { printf '%s\n' "[INFO][auto] $1"; }
error() { printf '%s\n' "[ERROR][auto] $1" >&2; exit 1; }

VENV_DIR="${TB2_VENV_DIR:-/opt/tb2-venv}"
if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
    export PATH="$VENV_DIR/bin:$PATH"
fi

command -v python3 >/dev/null 2>&1 || error "python3 missing; common-env not baked?"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    error "Magentic-One AgentChat 0.7.5 requires Python >=3.10; got $(python3 -V 2>&1)"
fi

export PIP_DEFAULT_TIMEOUT=120
info "pip install autogen-agentchat==0.7.5 autogen-ext[magentic-one,openai]==0.7.5..."
python3 -m pip install "autogen-agentchat==0.7.5" "autogen-ext[magentic-one,openai]==0.7.5" \
    || error "pip AutoGen AgentChat / Magentic-One failed"

python3 - <<'PY'
"""Fail install unless Magentic-One AgentChat stack imports."""
import importlib
import sys

mods = (
    "autogen_agentchat.teams",
    "autogen_ext.models.openai",
    "autogen_ext.agents.file_surfer",
    "autogen_ext.agents.magentic_one",
    "autogen_ext.code_executors.local",
    "autogen_agentchat.agents",
)
for name in mods:
    try:
        importlib.import_module(name)
        print(f"[INFO][auto] import ok: {name}", flush=True)
    except Exception as e:
        print(f"[ERROR][auto] {name}: {e}", file=sys.stderr)
        raise SystemExit(1)

from autogen_agentchat.teams import MagenticOneGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

print(
    f"[INFO][auto] MagenticOneGroupChat={MagenticOneGroupChat.__module__} "
    f"OpenAIChatCompletionClient={OpenAIChatCompletionClient.__module__}",
    flush=True,
)
PY
info "AutoGen Magentic-One ready"
