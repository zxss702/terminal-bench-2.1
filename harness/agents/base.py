"""Agent adapter base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import TaskInfo


class AgentAdapter(ABC):
    id: str
    display_name: str

    @abstractmethod
    def prepare_run_dir(self, run_dir: str, task: "TaskInfo") -> None:
        ...

    @abstractmethod
    def install_mounts(self) -> list[tuple[str, str, str]]:
        ...

    @abstractmethod
    def build_entryscript(self, task: "TaskInfo") -> str:
        ...
