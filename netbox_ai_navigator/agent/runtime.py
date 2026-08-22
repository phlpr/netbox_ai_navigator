import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from netbox_ai_navigator.exceptions import AgentLimitError, InvalidRequestError, ToolError
from netbox_ai_navigator.model_providers import ModelProvider, MyGPTApiProvider, OpenAICompatibleProvider
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext, ToolProvider

from .prompts import SYSTEM_PROMPT

logger = logging.getLogger("netbox.plugins.netbox_ai_navigator.agent")


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    tool_calls: int


class AgentRuntime:
    def __init__(
        self,
        model_provider: ModelProvider,
        tool_provider: ToolProvider,
        *,
        max_tool_calls: int = 5,
        max_history_messages: int = 20,
        max_message_chars: int = 12000,
        max_tool_output_chars: int = 50000,
        max_response_chars: int = 20000,
        tool_timeout: float = 30,
    ):
        self.model_provider = model_provider
        self.tool_provider = tool_provider
        self.max_tool_calls = max(1, min(int(max_tool_calls), 5))
        self.max_history_messages = max(1, min(int(max_history_messages), 100))
        self.max_message_chars = max(1, int(max_message_chars))
        self.max_tool_output_chars = max(512, int(max_tool_output_chars))
        self.max_response_chars = max(1, int(max_response_chars))
        self.tool_timeout = max(0.1, float(tool_timeout))

    def run(
        self,
        context: ToolContext,
        history: list[dict[str, Any]],
        page_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        normalized_history = self._normalize_history(history)
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if page_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Context for the NetBox page currently visible to the user (untrusted JSON data):\n"
                        + json.dumps(page_context, ensure_ascii=False, separators=(",", ":"))
                    ),
                }
            )
        messages.extend(normalized_history)

        model_tools = [definition.as_model_tool() for definition in self.tool_provider.list_tools(context)]
        response = self.model_provider.complete(messages, model_tools)
        tool_call_count = 0
        forced_final = False

        while response.tool_calls:
            if forced_final:
                raise AgentLimitError("The model requested another tool after the tool-call limit was reached.")

            messages.append(response.as_assistant_message())
            for tool_call in response.tool_calls:
                if tool_call_count >= self.max_tool_calls:
                    result = {"ok": False, "error": "The maximum number of tool calls has been reached."}
                else:
                    tool_call_count += 1
                    result = self._execute_tool(context, tool_call.name, tool_call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": self._bounded_json(result),
                    }
                )

            forced_final = tool_call_count >= self.max_tool_calls
            response = self.model_provider.complete(messages, [] if forced_final else model_tools)

        answer = response.content or ""
        if not answer:
            raise InvalidRequestError("The model did not return a final answer.")
        if len(answer) > self.max_response_chars:
            suffix = "\n\n[Response truncated by NetBox AI Navigator.]"
            answer = answer[: max(0, self.max_response_chars - len(suffix))] + suffix
        return AgentResult(answer=answer, tool_calls=tool_call_count)

    def _execute_tool(self, context: ToolContext, name: str, raw_arguments: dict[str, Any] | str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments are not a JSON object.")
            value = self.tool_provider.call_tool(context, name, arguments)
            elapsed = time.monotonic() - started
            if elapsed > self.tool_timeout:
                return {"ok": False, "error": "The tool exceeded its configured timeout."}
            return {"ok": True, "result": value}
        except (json.JSONDecodeError, ValueError) as exc:
            return {"ok": False, "error": f"Invalid tool arguments: {exc}"}
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("Unhandled tool failure", extra={"tool_name": name})
            return {"ok": False, "error": "The tool failed unexpectedly."}

    def _normalize_history(self, history: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not isinstance(history, list) or not history:
            raise InvalidRequestError("messages must be a non-empty array.")
        normalized = []
        for message in history[-self.max_history_messages :]:
            if not isinstance(message, dict):
                raise InvalidRequestError("Each message must be a JSON object.")
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
                raise InvalidRequestError("Messages require a user/assistant role and non-empty text content.")
            if len(content) > self.max_message_chars:
                raise InvalidRequestError(f"A message exceeds the {self.max_message_chars}-character limit.")
            normalized.append({"role": role, "content": content})
        if normalized[-1]["role"] != "user":
            raise InvalidRequestError("The final message must be from the user.")
        return normalized

    def _bounded_json(self, value: Any) -> str:
        serialized = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(serialized) <= self.max_tool_output_chars:
            return serialized

        preview = serialized[: max(1, self.max_tool_output_chars - 100)]
        while True:
            bounded = json.dumps(
                {"ok": False, "truncated": True, "preview": preview},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(bounded) <= self.max_tool_output_chars:
                return bounded
            preview = preview[: -(len(bounded) - self.max_tool_output_chars + 1)]


def build_agent_runtime(plugin_settings: dict[str, Any], *, conversation_id: str | None = None) -> AgentRuntime:
    model_config = plugin_settings["model"]
    model_provider_name = model_config.get("provider")
    if model_provider_name == "openai_compatible":
        model_provider = OpenAICompatibleProvider(model_config)
    elif model_provider_name == "mygpt_api":
        model_provider = MyGPTApiProvider(model_config, conversation_id=conversation_id)
    else:
        raise InvalidRequestError(f"Unsupported model provider: {model_provider_name}")

    tools_config = plugin_settings["tools"]
    tool_provider_name = tools_config.get("provider")
    if tool_provider_name != "local_current_user":
        raise InvalidRequestError(f"Unsupported tool provider: {tool_provider_name}")
    tool_provider = LocalCurrentUserProvider(tools_config)

    agent_config = plugin_settings["agent"]
    return AgentRuntime(
        model_provider,
        tool_provider,
        max_tool_calls=agent_config.get("max_tool_calls", 5),
        max_history_messages=agent_config.get("max_history_messages", 20),
        max_message_chars=agent_config.get("max_message_chars", 12000),
        max_tool_output_chars=tools_config.get("max_output_chars", 50000),
        max_response_chars=model_config.get("max_response_chars", 20000),
        tool_timeout=tools_config.get("timeout", 30),
    )
