#!/usr/bin/env python3
"""TB2 headless AgentFlow wrapper: official construct_solver only."""
from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from pathlib import Path

cfg = json.loads(Path("/run/agent_config.json").read_text(encoding="utf-8"))
prompt = Path("/run/prompt.txt").read_text(encoding="utf-8")
workdir = cfg.get("workdir", "/app")
os.chdir(workdir)

os.environ["OPENAI_API_KEY"] = cfg["api_key"]
os.environ["OPENAI_BASE_URL"] = cfg["base_url"]
os.environ["OPENAI_API_BASE"] = cfg["base_url"]
os.environ["DEEPSEEK_API_KEY"] = cfg["api_key"]

# Never put the trainer tree on sys.path: /opt/agentflow/agentflow/__init__.py
# shadows the installed solver package and its logging.py shadows stdlib logging.
_AF_ROOT = "/opt/agentflow"
_AF_PKG_DIR = "/opt/agentflow/agentflow"


def _norm(p: str) -> str:
    if not p:
        return ""
    return os.path.normpath(p).replace("\\", "/").rstrip("/")


def _scrub_trainer_from_path() -> None:
    bad = {"", _norm(_AF_ROOT), _norm(_AF_PKG_DIR)}
    sys.path[:] = [p for p in sys.path if _norm(p) not in bad]


def _ensure_stdlib_logging() -> None:
    _scrub_trainer_from_path()
    mod = sys.modules.get("logging")
    shadowed = False
    if mod is not None:
        f = str(getattr(mod, "__file__", "") or "").replace("\\", "/")
        if "agentflow" in f:
            shadowed = True
    if not shadowed and mod is not None and getattr(mod, "INFO", None) is not None:
        return
    for key in list(sys.modules):
        if key == "logging" or key.startswith("logging."):
            del sys.modules[key]
    importlib.import_module("logging")


_scrub_trainer_from_path()
_ensure_stdlib_logging()


def _load_construct_solver():
    last = None
    for name in ("agentflow.solver", "agentflow.agentflow.solver"):
        try:
            mod = importlib.import_module(name)
            fn = getattr(mod, "construct_solver")
            print(f"[INFO] construct_solver from {name}", flush=True)
            return fn
        except Exception as e:
            last = e
            print(f"[WARN] import {name}: {e}", flush=True)
    raise RuntimeError(f"AgentFlow solver not importable: {last}") from last


def _run_solver() -> None:
    construct_solver = _load_construct_solver()
    model = cfg.get("model") or "deepseek-chat"
    timeout_sec = int(cfg.get("timeout_sec") or 900)
    # Must name tools explicitly. enabled_tools=["all"] never indexes
    # tool_engine, so each tool's __init__ keeps gpt-4o / gpt-4o-mini and
    # DeepSeek 400s ("you passed gpt-4o-mini"). "self" = llm_engine_name.
    enabled_tools = [
        "Base_Generator_Tool",
        "Python_Coder_Tool",
    ]
    # No web/wiki search: Google needs GOOGLE_API_KEY; Web RAG embeddings
    # hit DeepSeek 404; Wikipedia is an outbound network search.
    tool_engine = ["self", "self"]
    print(
        f"[INFO] construct_solver model={model} base_url={cfg.get('base_url')} "
        f"timeout={timeout_sec}s engines=all-trainable tool_engine={tool_engine}",
        flush=True,
    )
    solver = construct_solver(
        llm_engine_name=model,
        enabled_tools=enabled_tools,
        tool_engine=tool_engine,
        model_engine=["trainable", "trainable", "trainable", "trainable"],
        output_types="final,direct",
        max_steps=10,
        max_time=timeout_sec,
        max_tokens=4000,
        root_cache_dir="/tmp/agentflow_solver_cache",
        verbose=True,
        base_url=cfg.get("base_url"),
        temperature=0.2,
    )
    result = solver.solve(prompt)
    print(f"[INFO] AgentFlow solver result: {result!r}"[:4000], flush=True)


def main() -> int:
    try:
        _run_solver()
    except Exception as e:
        print(f"[ERROR] AgentFlow solver failed: {e}", flush=True)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
