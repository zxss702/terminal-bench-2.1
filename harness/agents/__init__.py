"""Agent adapter registry."""

from .base import AgentAdapter


def get_adapter(agent_id: str) -> AgentAdapter:
    from .agentflow_agent import AgentFlowAdapter
    from .autogen_agent import AutogenAdapter
    from .claude_code import ClaudeCodeAdapter
    from .logorythia import LogorythiaAdapter
    from .swe_agent import SweAgentAdapter

    registry: dict[str, type[AgentAdapter]] = {
        "logorythia": LogorythiaAdapter,
        "claude-code": ClaudeCodeAdapter,
        "swe-agent": SweAgentAdapter,
        "autogen": AutogenAdapter,
        "agentflow": AgentFlowAdapter,
    }
    if agent_id not in registry:
        raise KeyError(f"Unknown agent: {agent_id}")
    return registry[agent_id]()


__all__ = ["AgentAdapter", "get_adapter"]
