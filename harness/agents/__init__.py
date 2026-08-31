"""Agent adapter registry (神衍 2×2 四变体)."""

from .base import AgentAdapter
from .logorythia import LogorythiaAdapter


def get_adapter(agent_id: str) -> AgentAdapter:
    from ..config import LOGORYTHIA_VARIANT_BY_ID

    variant = LOGORYTHIA_VARIANT_BY_ID.get(agent_id)
    if variant is None:
        raise KeyError(f"Unknown agent: {agent_id}")
    return LogorythiaAdapter(variant)


__all__ = ["AgentAdapter", "get_adapter"]
