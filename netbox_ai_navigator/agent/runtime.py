import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from django.utils.translation import gettext as _

from netbox_ai_navigator.exceptions import (
    AgentLimitError,
    InvalidRequestError,
    ToolError,
    UngroundedResponseError,
)
from netbox_ai_navigator.model_providers import ModelProvider, MyGPTApiProvider, OpenAICompatibleProvider
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext, ToolProvider

from .prompts import SYSTEM_PROMPT

logger = logging.getLogger("netbox.plugins.netbox_ai_navigator.agent")

DATA_TOOL_NAMES = frozenset({"query_objects", "get_object"})
IDENTITY_KEYS = frozenset({"display", "name", "address", "prefix", "cid", "rd", "vid"})
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)]\(([^\s)]+)(?:\s+[^)]*)?\)")
EMPHASIZED_VALUE_RE = re.compile(r"\*\*([^*\n]+)\*\*|`([^`\n]+)`")
NETBOX_DETAIL_PATH_RE = re.compile(r"^/(?:api/)?(?:dcim|ipam|circuits|virtualization)/.+/\d+/?$")
TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
EMPTY_TABLE_VALUES = frozenset({"", "-", "—", "n/a", "none", "null"})


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    tool_calls: int


@dataclass(frozen=True, slots=True)
class GroundingRecord:
    display: str
    display_url: str | None
    identities: frozenset[str]
    values: frozenset[str]
    object_urls: frozenset[str]
    all_urls: frozenset[str]


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
        data_tool_attempted = False
        grounding_records: list[GroundingRecord] = []

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
                if tool_call.name in DATA_TOOL_NAMES:
                    data_tool_attempted = True
                    grounding_records.extend(self._grounding_records(tool_call.name, result))
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
        try:
            self._validate_grounding(
                answer,
                grounding_records,
                data_tool_attempted=data_tool_attempted,
                forced_final=forced_final,
            )
        except UngroundedResponseError:
            if not grounding_records:
                raise
            logger.info(
                "Returning deterministic fallback for ungrounded model response",
                extra={"grounding_records": len(grounding_records)},
            )
            answer = self._grounded_fallback(grounding_records)
        if len(answer) > self.max_response_chars:
            suffix = "\n\n[Response truncated by NetBox AI Navigator.]"
            answer = answer[: max(0, self.max_response_chars - len(suffix))] + suffix
        return AgentResult(answer=answer, tool_calls=tool_call_count)

    def _validate_grounding(
        self,
        answer: str,
        records: list[GroundingRecord],
        *,
        data_tool_attempted: bool,
        forced_final: bool,
    ) -> None:
        tables = self._markdown_tables(answer)
        allowed_urls = set().union(*(record.all_urls for record in records)) if records else set()

        for _link_text, target in MARKDOWN_LINK_RE.findall(answer):
            url_key = self._url_key(target)
            if NETBOX_DETAIL_PATH_RE.fullmatch(url_key) and url_key not in allowed_urls:
                self._reject_ungrounded("unknown NetBox object link")

        if tables:
            if not records:
                self._reject_ungrounded("object table without successful data result")
            self._validate_tables(tables, records)

        if data_tool_attempted and records:
            self._validate_emphasized_values(answer, records)

        if forced_final and data_tool_attempted:
            if not records:
                raise AgentLimitError(
                    _(
                        "The tool-call limit was reached before the answer could be verified against NetBox data. "
                        "Please refine the request."
                    )
                )
            known_object_urls = set().union(*(record.object_urls for record in records))
            answer_urls = {self._url_key(target) for _link_text, target in MARKDOWN_LINK_RE.findall(answer)}
            if not tables and not known_object_urls.intersection(answer_urls):
                self._reject_ungrounded("forced final answer without a verifiable object reference")

    def _validate_tables(self, tables: list[list[list[str]]], records: list[GroundingRecord]) -> None:
        used_records: set[int] = set()
        for rows in tables:
            if not rows:
                self._reject_ungrounded("object table without data rows")
            for row in rows:
                record_index = self._match_record(row[0], records)
                if record_index is None or record_index in used_records:
                    self._reject_ungrounded("unknown or duplicate table object")
                used_records.add(record_index)
                record = records[record_index]
                for cell in row[1:]:
                    if not self._cell_is_grounded(cell, record):
                        self._reject_ungrounded("table value absent from tool result")

    def _match_record(self, cell: str, records: list[GroundingRecord]) -> int | None:
        links = MARKDOWN_LINK_RE.findall(cell)
        for _, target in links:
            target_key = self._url_key(target)
            for index, record in enumerate(records):
                if target_key in record.object_urls:
                    return index

        visible_value = self._normalize_value(self._strip_markdown(cell))
        for index, record in enumerate(records):
            if visible_value in record.identities:
                return index
        return None

    def _cell_is_grounded(self, cell: str, record: GroundingRecord) -> bool:
        visible_value = self._normalize_value(self._strip_markdown(cell))
        if visible_value in EMPTY_TABLE_VALUES:
            return True
        links = MARKDOWN_LINK_RE.findall(cell)
        if links and any(self._url_key(target) not in record.all_urls for _, target in links):
            return False
        return visible_value in record.values

    def _validate_emphasized_values(self, answer: str, records: list[GroundingRecord]) -> None:
        allowed_values = set().union(*(record.values | record.identities for record in records))
        for match in EMPHASIZED_VALUE_RE.finditer(answer):
            raw_value = match.group(1) or match.group(2) or ""
            if answer[match.end() :].lstrip().startswith(":"):
                continue
            visible_value = self._normalize_value(self._strip_markdown(raw_value))
            if visible_value in allowed_values:
                continue
            if len(visible_value) >= 3 and any(visible_value in allowed for allowed in allowed_values):
                continue
            self._reject_ungrounded("emphasized value absent from tool result")

    @classmethod
    def _grounding_records(cls, tool_name: str, execution_result: dict[str, Any]) -> list[GroundingRecord]:
        if not execution_result.get("ok") or not isinstance(execution_result.get("result"), dict):
            return []
        result = execution_result["result"]
        if tool_name == "query_objects":
            objects = result.get("objects")
            if not isinstance(objects, list):
                return []
        elif tool_name == "get_object":
            value = result.get("object")
            objects = [value] if result.get("found") and isinstance(value, dict) else []
        else:
            return []

        records = []
        for value in objects:
            if not isinstance(value, dict):
                continue
            identities = {
                cls._normalize_value(str(field_value))
                for key, field_value in value.items()
                if key in IDENTITY_KEYS and isinstance(field_value, (str, int))
            }
            scalar_values, all_urls = cls._collect_values(value)
            object_urls: set[str] = set()
            for key, field_value in value.items():
                if key in {"display_url", "url"} and isinstance(field_value, str):
                    object_urls.update(cls._url_variants(field_value))
            if identities:
                display = value.get("name") or value.get("display") or next(iter(identities))
                display_url = value.get("display_url") or value.get("url")
                records.append(
                    GroundingRecord(
                        display=str(display),
                        display_url=str(display_url) if isinstance(display_url, str) else None,
                        identities=frozenset(identities),
                        values=frozenset(scalar_values),
                        object_urls=frozenset(object_urls),
                        all_urls=frozenset(all_urls),
                    )
                )
        return records

    @classmethod
    def _grounded_fallback(cls, records: list[GroundingRecord]) -> str:
        unique_records: list[GroundingRecord] = []
        seen = set()
        for record in records:
            key = (record.display_url, cls._normalize_value(record.display))
            if key in seen:
                continue
            seen.add(key)
            unique_records.append(record)

        lines = [_("Verified NetBox results ({count}):").format(count=len(unique_records)), ""]
        for record in unique_records:
            display = record.display.replace("[", r"\[").replace("]", r"\]")
            if record.display_url:
                lines.append(f"- [{display}]({cls._url_key(record.display_url)})")
            else:
                lines.append(f"- `{record.display.replace('`', '')}`")
        return "\n".join(lines)

    @classmethod
    def _collect_values(cls, value: Any) -> tuple[set[str], set[str]]:
        scalar_values: set[str] = set()
        urls: set[str] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)
            elif isinstance(item, str):
                if item.startswith(("/", "http://", "https://")):
                    urls.update(cls._url_variants(item))
                scalar_values.add(cls._normalize_value(item))
                if re.fullmatch(r"[0-9a-fA-F:.]+/\d+", item):
                    scalar_values.add(cls._normalize_value(item.rsplit("/", 1)[0]))
            elif isinstance(item, bool):
                scalar_values.add(str(item).lower())
            elif isinstance(item, (int, float)):
                scalar_values.add(cls._normalize_value(str(item)))

        visit(value)
        return scalar_values, urls

    @classmethod
    def _markdown_tables(cls, answer: str) -> list[list[list[str]]]:
        lines = answer.splitlines()
        tables: list[list[list[str]]] = []
        index = 0
        while index + 1 < len(lines):
            header = cls._split_table_row(lines[index])
            separator = cls._split_table_row(lines[index + 1])
            if (
                len(header) >= 2
                and len(separator) == len(header)
                and all(TABLE_SEPARATOR_RE.fullmatch(cell.strip()) for cell in separator)
            ):
                rows = []
                index += 2
                while index < len(lines):
                    row = cls._split_table_row(lines[index])
                    if len(row) != len(header):
                        break
                    rows.append(row)
                    index += 1
                tables.append(rows)
                continue
            index += 1
        return tables

    @staticmethod
    def _split_table_row(line: str) -> list[str]:
        stripped = line.strip()
        if "|" not in stripped:
            return []
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|") and not stripped.endswith(r"\|"):
            stripped = stripped[:-1]

        cells = []
        current = []
        escaped = False
        for character in stripped:
            if character == "|" and not escaped:
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(character)
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        cells.append("".join(current).strip())
        return cells

    @staticmethod
    def _strip_markdown(value: str) -> str:
        value = MARKDOWN_LINK_RE.sub(lambda match: match.group(1), value)
        return value.replace(r"\|", "|").replace("`", "").replace("*", "").replace("_", "").strip()

    @staticmethod
    def _normalize_value(value: str) -> str:
        return " ".join(value.split()).casefold()

    @staticmethod
    def _url_key(value: str) -> str:
        path = urlsplit(value).path
        if not path.startswith("/"):
            path = f"/{path}"
        normalized = path.rstrip("/")
        return f"{normalized}/" if normalized else "/"

    @classmethod
    def _url_variants(cls, value: str) -> set[str]:
        url_key = cls._url_key(value)
        if url_key.startswith("/api/"):
            return {url_key, url_key.removeprefix("/api")}
        if NETBOX_DETAIL_PATH_RE.fullmatch(url_key):
            return {url_key, f"/api{url_key}"}
        return {url_key}

    @staticmethod
    def _reject_ungrounded(reason: str) -> None:
        logger.warning("Rejected ungrounded model response", extra={"grounding_reason": reason})
        raise UngroundedResponseError(
            _(
                "The model response could not be verified against NetBox data. "
                "Please retry or refine the request."
            )
        )

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
