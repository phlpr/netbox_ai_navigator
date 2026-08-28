from typing import Any

import requests

from netbox_ai_navigator.exceptions import ProviderError, ProviderTimeoutError
from netbox_ai_navigator.provider_http import (
    ProviderResponseTooLargeError,
    close_response,
    normalize_provider_headers,
    normalize_provider_url,
    read_bounded_json,
)

from .base import ModelProvider, ModelResponse, ModelToolCall


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI-compatible Chat Completions or Responses provider with tool-call support."""

    def __init__(self, config: dict[str, Any], session=None):
        try:
            self.base_url = normalize_provider_url(
                config.get("base_url"),
                allow_insecure_http=config.get("allow_insecure_http", False) is True,
            )
        except ValueError as exc:
            raise ProviderError(f"The model base URL is invalid: {exc}") from exc
        self.protocol = str(config.get("protocol", "chat_completions"))
        if self.protocol not in {"chat_completions", "responses"}:
            raise ProviderError("The model protocol must be chat_completions or responses.")
        self.api_key = config.get("api_key")
        try:
            self.extra_headers = normalize_provider_headers(config.get("extra_headers"))
        except ValueError as exc:
            raise ProviderError(f"The model provider headers are invalid: {exc}") from exc
        self.model_name = str(config.get("model", ""))
        self.timeout = float(config.get("timeout", 60))
        self.temperature = config.get("temperature", 0.1)
        self.max_tokens = config.get("max_tokens", 1200)
        self.max_http_response_bytes = max(1, min(int(config.get("max_http_response_bytes", 2_000_000)), 10_000_000))
        self.session = session or requests
        self._responses_input: list[dict[str, Any]] | None = None
        self._processed_message_count = 0

        if not self.model_name:
            raise ProviderError("The model name is not configured.")

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelResponse:
        if self.protocol == "responses":
            return self._complete_responses(messages, tools)
        return self._complete_chat_completions(messages, tools)

    def _complete_chat_completions(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = int(self.max_tokens)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = self._post_json("chat/completions", payload)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError("The model provider returned an invalid response.") from exc

        tool_calls = []
        for raw_call in message.get("tool_calls") or []:
            try:
                function = raw_call["function"]
                tool_calls.append(
                    ModelToolCall(
                        id=str(raw_call["id"]),
                        name=str(function["name"]),
                        arguments=function.get("arguments", "{}"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("The model provider returned an invalid tool call.") from exc

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderError("The model provider returned invalid message content.")
        if not content and not tool_calls:
            raise ProviderError("The model provider returned an empty response.")

        return ModelResponse(content=content, tool_calls=tool_calls)

    def _complete_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        continuing = self._responses_input is not None
        new_messages = messages[self._processed_message_count :] if continuing else messages
        converted_messages = self._as_responses_input(new_messages, continuing=continuing)
        if self._responses_input is None:
            self._responses_input = converted_messages
        else:
            self._responses_input.extend(converted_messages)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": list(self._responses_input),
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_output_tokens"] = int(self.max_tokens)
        if tools:
            payload["tools"] = self._as_responses_tools(tools)
            payload["tool_choice"] = "auto"

        data = self._post_json("responses", payload)
        result = self._parse_responses_result(data)
        self._responses_input.extend(self._as_responses_replay_items(data["output"]))
        self._processed_message_count = len(messages)
        return result

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = self.session.post(
                f"{self.base_url}/{endpoint}",
                headers=headers,
                json=payload,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout as exc:
            raise ProviderTimeoutError("The model provider timed out.") from exc
        except requests.RequestException as exc:
            raise ProviderError("The model provider could not be reached.") from exc

        if not 200 <= response.status_code < 300:
            close_response(response)
            raise ProviderError(f"The model provider returned HTTP {response.status_code}.")

        try:
            return read_bounded_json(response, max_bytes=self.max_http_response_bytes)
        except ProviderResponseTooLargeError as exc:
            raise ProviderError("The model provider returned an oversized response.") from exc
        except (TypeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError("The model provider returned an invalid response.") from exc
        finally:
            close_response(response)

    @staticmethod
    def _as_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            try:
                function = tool["function"]
                item: dict[str, Any] = {
                    "type": "function",
                    "name": function["name"],
                    "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                }
                if function.get("description") is not None:
                    item["description"] = function["description"]
                if function.get("strict") is not None:
                    item["strict"] = function["strict"]
                converted.append(item)
            except (KeyError, TypeError) as exc:
                raise ProviderError("The configured model tool is invalid.") from exc
        return converted

    @staticmethod
    def _as_responses_input(
        messages: list[dict[str, Any]],
        *,
        continuing: bool,
    ) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if continuing and role == "assistant":
                # The provider's original output items are already present in the stateless replay input.
                continue
            if role == "tool":
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id"),
                        "output": message.get("content", ""),
                    }
                )
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                raise ProviderError("The model conversation contains an unsupported message role.")

            content = message.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ProviderError("The model conversation contains invalid message content.")
                converted.append({"role": role, "content": content})

            if role == "assistant":
                for raw_call in message.get("tool_calls") or []:
                    try:
                        function = raw_call["function"]
                        converted.append(
                            {
                                "type": "function_call",
                                "call_id": raw_call["id"],
                                "name": function["name"],
                                "arguments": function.get("arguments", "{}"),
                            }
                        )
                    except (KeyError, TypeError) as exc:
                        raise ProviderError("The model conversation contains an invalid tool call.") from exc
        return converted

    @staticmethod
    def _as_responses_replay_items(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
        replay_items: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError("The model provider returned an invalid response item.")
            replay_item = dict(item)
            # Some compatible proxies reject the response-only lifecycle field when an output item is replayed as
            # input. All semantic fields, including encrypted reasoning content and message phase, remain intact.
            replay_item.pop("status", None)
            replay_items.append(replay_item)
        return replay_items

    @staticmethod
    def _parse_responses_result(data: Any) -> ModelResponse:
        if not isinstance(data, dict) or not isinstance(data.get("output"), list):
            raise ProviderError("The model provider returned an invalid response.")
        if data.get("error"):
            raise ProviderError("The model provider returned a failed response.")
        status = data.get("status")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            if reason == "max_output_tokens":
                raise ProviderError("The model provider reached the configured output token limit.")
            if reason == "content_filter":
                raise ProviderError("The model provider returned an incomplete response due to content filtering.")
            raise ProviderError("The model provider returned an incomplete response.")
        if status in {"cancelled", "failed"}:
            raise ProviderError(f"The model provider returned a {status} response.")

        text_parts: list[str] = []
        tool_calls: list[ModelToolCall] = []
        for item in data["output"]:
            if not isinstance(item, dict):
                raise ProviderError("The model provider returned an invalid response item.")
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments", "{}")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(arguments, (dict, str))
                ):
                    raise ProviderError("The model provider returned an invalid tool call.")
                tool_calls.append(ModelToolCall(id=call_id, name=name, arguments=arguments))
            elif item.get("type") == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    raise ProviderError("The model provider returned invalid message content.")
                for block in content:
                    if not isinstance(block, dict):
                        raise ProviderError("The model provider returned invalid message content.")
                    if block.get("type") == "output_text":
                        text = block.get("text")
                        if not isinstance(text, str):
                            raise ProviderError("The model provider returned invalid message content.")
                        text_parts.append(text)
                    elif block.get("type") == "refusal":
                        refusal = block.get("refusal")
                        if not isinstance(refusal, str):
                            raise ProviderError("The model provider returned invalid message content.")
                        text_parts.append(refusal)

        content = "\n".join(part for part in text_parts if part) or None
        if not content and not tool_calls:
            raise ProviderError("The model provider returned an empty response.")
        return ModelResponse(content=content, tool_calls=tool_calls)
