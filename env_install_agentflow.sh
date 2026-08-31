#!/bin/sh
# Official AgentFlow inference install.
# Source from host cache (no git clone). pip via Clash + PIP_CACHE_DIR.
#
# Mirrors setup.sh inference half:
#   cd agentflow && pip install -r requirements.txt && pip install --no-deps -e .
# Does NOT install Ray / verl / GPU training extras.
# Also skips vllm / easyocr / transformers: local GPU serve, OCR, HF weights.
# Unused when planner/tools talk to DeepSeek (OpenAI-compatible API).
set -e

info() { printf '%s\n' "[INFO][agentflow] $1"; }
error() { printf '%s\n' "[ERROR][agentflow] $1" >&2; exit 1; }

VENV_DIR="${TB2_VENV_DIR:-/opt/tb2-venv}"
if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
    export PATH="$VENV_DIR/bin:$PATH"
fi

command -v python3 >/dev/null 2>&1 || error "python3 missing; common-env not baked?"

SRC="${AGENTS_CACHE_DIR:-/agent-packages}/agentflow"
DEST="${AGENTFLOW_ROOT:-/opt/agentflow}"

[ -d "$SRC" ] || error "AgentFlow source cache missing ($SRC); run scripts/cache_agent_packages.ps1"

info "copying AgentFlow from $SRC -> $DEST ..."
rm -rf "$DEST"
cp -a "$SRC" "$DEST"
cd "$DEST"

info "patching factory.py DeepSeek branch -> ChatOpenAI..."
python3 - <<'PY'
"""Replace official DeepSeek factory arm with ChatOpenAI (any deepseek-* name)."""
from pathlib import Path

old = '''    # === DeepSeek ===
    elif any(x in model_string for x in ["deepseek-chat", "deepseek-reasoner"]):
        from .deepseek import ChatDeepseek

        # DeepSeek uses repetition_penalty, not frequency/presence
        config = {
            "model_string": model_string,
            "use_cache": use_cache,
            "is_multimodal": is_multimodal,
        }
        return ChatDeepseek(**config)
'''
new = '''    # === DeepSeek (TB2: OpenAI-compatible gateway) ===
    elif "deepseek" in model_string.lower():
        from .openai import ChatOpenAI
        engine = ChatOpenAI(
            model_string=model_string,
            use_cache=use_cache,
            is_multimodal=is_multimodal,
        )
        engine.is_chat_model = True
        engine.support_structured_output = False
        return engine
'''

candidates = [
    Path("agentflow/agentflow/engine/factory.py"),
    Path("agentflow/engine/factory.py"),
]
path = next((p for p in candidates if p.is_file()), None)
if path is None:
    print("[ERROR][agentflow] factory.py missing under /opt/agentflow", flush=True)
    raise SystemExit(1)
text = path.read_text(encoding="utf-8")
if old not in text:
    print(f"[ERROR][agentflow] DeepSeek branch not found in {path}", flush=True)
    raise SystemExit(1)
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print(f"[INFO][agentflow] patched {path}", flush=True)
PY

export PIP_DEFAULT_TIMEOUT=120

info "installing build backends (setuptools hatchling wheel)..."
python3 -m pip install 'setuptools>=77' hatchling wheel \
    || error "pip setuptools/hatchling failed"

REQ=""
if [ -f agentflow/requirements.txt ]; then
    REQ=agentflow/requirements.txt
elif [ -f requirements.txt ]; then
    REQ=requirements.txt
fi
[ -n "$REQ" ] || error "AgentFlow requirements.txt missing under $DEST"

# Same skip list as scripts/cache_agent_packages.ps1. Official -r would pull
# CUDA torch via vllm==0.8.5 even though factory never imports ChatVLLM here.
FILTERED=/tmp/tb2-agentflow-req.txt
grep -E -v '^(vllm|easyocr|transformers)(==|[[:space:]]|$)' "$REQ" \
    | grep -v '^#' \
    | grep -v '^[[:space:]]*$' > "$FILTERED" || true
[ -s "$FILTERED" ] || error "filtered requirements empty (from $REQ)"
SKIPPED=$(grep -E '^(vllm|easyocr|transformers)(==|[[:space:]]|$)' "$REQ" || true)
if [ -n "$SKIPPED" ]; then
    info "skipping local-GPU/OCR extras:"
    printf '%s\n' "$SKIPPED" | while IFS= read -r line; do
        info "  $line"
    done
fi

info "pip install -r $FILTERED (official inference minus vllm/easyocr/transformers, Clash + PIP_CACHE_DIR)..."
python3 -m pip install -r "$FILTERED" \
    || error "pip -r $FILTERED failed"

# Official: editable solver package only. Never pip install the repo root (trainer).
if [ ! -f agentflow/pyproject.toml ]; then
    error "missing agentflow/pyproject.toml (solver package)"
fi
info "pip install --no-deps --no-build-isolation -e ./agentflow"
python3 -m pip install --no-deps --no-build-isolation -e ./agentflow \
    || error "pip -e ./agentflow failed"

# cwd must NOT be /opt/agentflow: '' on sys.path loads trainer (needs agentops).
# Official solver is the nested agentflowkit mapping under site-packages.
info "verifying construct_solver (cwd=/app, not trainer tree)..."
cd /app
python3 - <<'PY'
"""Fail install unless official construct_solver imports."""
import os
import sys

os.chdir("/app")
blocked = {"", "/opt/agentflow", "/opt/agentflow/agentflow"}
sys.path[:] = [
    p for p in sys.path
    if p.rstrip("/").replace("\\", "/") not in blocked
]

err = None
construct_solver = None
for name in ("agentflow.solver", "agentflow.agentflow.solver"):
    try:
        mod = __import__(name, fromlist=["construct_solver"])
        construct_solver = getattr(mod, "construct_solver")
        print(f"[INFO][agentflow] construct_solver ok via {name}", flush=True)
        break
    except Exception as e:
        err = e
        print(f"[WARN][agentflow] import {name}: {e}", flush=True)

if construct_solver is None:
    print(f"[ERROR][agentflow] official solver not importable: {err}", flush=True)
    raise SystemExit(1)
PY

info "AgentFlow solver ready"
