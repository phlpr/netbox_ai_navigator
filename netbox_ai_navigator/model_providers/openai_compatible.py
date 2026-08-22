from typing import Any

import requests

from netbox_ai_navigator.exceptions import ProviderError, ProviderTimeoutError

from .base import ModelProvider, ModelResponse, ModelToolCall


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI Chat Completions compatible model provider with tool-call support."""

    def __init__(self, config: dict[str, Any], session=None):
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.api_key = config.get("api_key")
        self.model_name = str(config.get("model", ""))
        self.timeout = float(config.get("timeout", 60))
        self.temperature = config.get("temperature", 0.1)
        self.max_tokens = config.get("max_tokens", 1200)
        self.session = session or requests

        if not self.base_url:
            raise ProviderError("The model base URL is not configured.")
        if not self.model_name:
            raise ProviderError("The model name is not configured.")

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelResponse:
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

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise ProviderTimeoutError("The model provider timed out.") from exc
        except requests.RequestException as exc:
            raise ProviderError("The model provider could not be reached.") from exc

        if not 200 <= response.status_code < 300:
            raise ProviderError(f"The model provider returned HTTP {response.status_code}.")

        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
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
