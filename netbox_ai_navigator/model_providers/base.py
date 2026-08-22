from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | str

    def as_message_tool_call(self) -> dict[str, Any]:
        arguments = self.arguments
        if not isinstance(arguments, str):
            import json

            arguments = json.dumps(arguments, separators=(",", ":"))
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": arguments},
        }


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str | None = None
    tool_calls: list[ModelToolCall] = field(default_factory=list)

    def as_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [tool_call.as_message_tool_call() for tool_call in self.tool_calls]
        return message


class ModelProvider(ABC):
    model_name: str

    @abstractmethod
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelResponse:
        raise NotImplementedError
