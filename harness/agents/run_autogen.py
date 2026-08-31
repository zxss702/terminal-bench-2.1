#!/usr/bin/env python3
"""TB2 runner: Microsoft AutoGen AgentChat Magentic-One (not AG2 Classic / ag2 1.0)."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import traceback
from pathlib import Path

cfg = json.loads(Path("/run/agent_config.json").read_text(encoding="utf-8"))
prompt = Path("/run/prompt.txt").read_text(encoding="utf-8")
workdir = cfg.get("workdir", "/app")
os.chdir(workdir)

os.environ["OPENAI_API_KEY"] = cfg["api_key"]
os.environ["OPENAI_BASE_URL"] = cfg["base_url"]
os.environ["OPENAI_API_BASE"] = cfg["base_url"]


def _empty_object_params() -> dict:
    # OpenAI / JSON Schema: no-arg functions are type=object, not type=null.
    return {"type": "object", "properties": {}}


def _normalize_tools(tools):
    if not tools:
        return tools
    fixed = []
    for tool in tools:
        if not isinstance(tool, dict):
            fixed.append(tool)
            continue
        tool = dict(tool)
        fn = tool.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            params = fn.get("parameters")
            if not isinstance(params, dict) or params.get("type") in (None, "null"):
                fn["parameters"] = _empty_object_params()
            else:
                params = dict(params)
                params.setdefault("type", "object")
                params.setdefault("properties", {})
                fn["parameters"] = params
            tool["function"] = fn
        fixed.append(tool)
    return fixed


def _patch_openai_thinking() -> None:
    """DeepSeek V4 thinking via extra_body; drop top-level reasoning_effort.
    Also fill empty tool parameters (FileSurfer page_down etc.)."""
    try:
        from openai.resources.chat.completions import AsyncCompletions, Completions
    except ImportError:
        print("[WARN] openai not importable; cannot patch thinking", flush=True)
        return

    def _inject(kwargs):
        kwargs.pop("reasoning_effort", None)
        extra = kwargs.get("extra_body")
        extra = dict(extra) if isinstance(extra, dict) else {}
        extra["thinking"] = {"type": "enabled"}
        extra["reasoning_effort"] = "low"
        kwargs["extra_body"] = extra
        if "tools" in kwargs:
            kwargs["tools"] = _normalize_tools(kwargs["tools"])
        return kwargs

    _sync = Completions.create

    def _sync_create(self, *args, **kwargs):
        return _sync(self, *args, **_inject(kwargs))

    Completions.create = _sync_create

    _async = AsyncCompletions.create

    async def _async_create(self, *args, **kwargs):
        return await _async(self, *args, **_inject(kwargs))

    AsyncCompletions.create = _async_create
    print("[INFO] AutoGen openai patched: thinking=enabled effort=low", flush=True)


def _model_family():
    try:
        from autogen_core.models import ModelFamily

        return ModelFamily.UNKNOWN
    except Exception:
        return "unknown"


def _make_local_executor(work_dir: str):
    from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor

    params = inspect.signature(LocalCommandLineCodeExecutor.__init__).parameters
    kwargs = {}
    if "work_dir" in params:
        kwargs["work_dir"] = work_dir
    if "timeout" in params:
        kwargs["timeout"] = 600
    elif "timeout_seconds" in params:
        kwargs["timeout_seconds"] = 600
    return LocalCommandLineCodeExecutor(**kwargs)


async def _maybe_enter(ex):
    enter = getattr(ex, "__aenter__", None)
    if enter is not None:
        return await enter()
    enter_sync = getattr(ex, "__enter__", None)
    if enter_sync is not None:
        return enter_sync()
    start = getattr(ex, "start", None)
    if callable(start):
        maybe = start()
        if inspect.isawaitable(maybe):
            await maybe
    return ex


async def _maybe_exit(ex) -> None:
    aexit = getattr(ex, "__aexit__", None)
    if aexit is not None:
        await aexit(None, None, None)
        return
    exit_sync = getattr(ex, "__exit__", None)
    if exit_sync is not None:
        exit_sync(None, None, None)
        return
    stop = getattr(ex, "stop", None)
    if callable(stop):
        maybe = stop()
        if inspect.isawaitable(maybe):
            await maybe


async def _run() -> None:
    from autogen_agentchat.agents import CodeExecutorAgent
    from autogen_agentchat.teams import MagenticOneGroupChat
    from autogen_agentchat.ui import Console
    from autogen_ext.agents.file_surfer import FileSurfer
    from autogen_ext.agents.magentic_one import MagenticOneCoderAgent
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    _patch_openai_thinking()
    model = cfg.get("model") or "deepseek-chat"
    base_url = cfg.get("base_url")
    timeout_sec = int(cfg.get("timeout_sec") or 900)
    max_turns = max(20, min(60, timeout_sec // 30))

    client_kwargs = {
        "model": model,
        "api_key": cfg["api_key"],
        "base_url": base_url,
        "model_info": {
            "vision": True,
            "function_calling": True,
            "json_output": True,
            "family": _model_family(),
            "structured_output": False,
        },
        "extra_create_args": {
            "extra_body": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
            }
        },
    }
    try:
        client = OpenAIChatCompletionClient(**client_kwargs)
    except TypeError:
        client_kwargs.pop("extra_create_args", None)
        client = OpenAIChatCompletionClient(**client_kwargs)

    executor_cm = _make_local_executor(workdir)
    code_executor = await _maybe_enter(executor_cm)
    print(
        f"[INFO] Magentic-One AgentChat model={model} base_url={base_url} "
        f"max_turns={max_turns} workdir={workdir}",
        flush=True,
    )
    print(
        "[INFO] team=FileSurfer+Coder+ComputerTerminal "
        "(no WebSurfer: TB2 local sandbox, browser disabled)",
        flush=True,
    )

    file_surfer = FileSurfer("FileSurfer", model_client=client)
    coder = MagenticOneCoderAgent("Coder", model_client=client)
    terminal = CodeExecutorAgent("ComputerTerminal", code_executor=code_executor)
    team_kwargs = {
        "model_client": client,
        "max_turns": max_turns,
    }
    try:
        team = MagenticOneGroupChat(
            [file_surfer, coder, terminal],
            **team_kwargs,
        )
    except TypeError:
        team_kwargs.pop("max_turns", None)
        team = MagenticOneGroupChat(
            [file_surfer, coder, terminal],
            model_client=client,
        )
    task = (
        "You are Magentic-One solving a Terminal-Bench task.\n"
        f"Working directory is {workdir}. Write files there. "
        "Use the code executor for shell/python. Do not ask a human.\n\n"
        f"{prompt}"
    )
    try:
        print("[INFO] running MagenticOneGroupChat.run_stream", flush=True)
        await Console(team.run_stream(task=task))
    finally:
        await _maybe_exit(executor_cm)
        close = getattr(client, "close", None)
        if callable(close):
            maybe = close()
            if inspect.isawaitable(maybe):
                await maybe


def main() -> int:
    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"[ERROR] AutoGen Magentic-One failed: {e}", flush=True)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
