from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .context import ToolContext


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_model_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolProvider(ABC):
    @abstractmethod
    def list_tools(self, context: ToolContext) -> list[ToolDefinition]:
        raise NotImplementedError

    @abstractmethod
    def call_tool(self, context: ToolContext, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError
