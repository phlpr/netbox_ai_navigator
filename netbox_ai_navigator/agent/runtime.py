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
from netbox_ai_navigator.rejections import RejectedResponse, RejectionReason
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext, ToolProvider

from .prompts import SYSTEM_PROMPT

logger = logging.getLogger("netbox.plugins.netbox_ai_navigator.agent")

DATA_TOOL_NAMES = frozenset({"query_objects", "get_object", "search_netbox"})
DETAIL_TOOL_NAMES = frozenset({"query_objects", "get_object"})
IDENTITY_KEYS = frozenset({"display", "name", "address", "prefix", "cid", "rd", "vid"})
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)]\(([^\s)]+)(?:\s+[^)]*)?\)")
EMPHASIZED_VALUE_RE = re.compile(r"\*\*([^*\n]+)\*\*|`([^`\n]+)`")
NETBOX_DETAIL_PATH_RE = re.compile(r"^/(?:api/)?(?:plugins/)?[a-z0-9_-]+(?:/[a-z0-9_-]+)+/\d+/?$")
TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
CHANGE_INTENT_RE = re.compile(
    r"\b(?:set|setze|setzen|setzt|change|changed|update|updated|aktualisiere|aktualisieren|"
    r"ändere|ändern|aendere|aendern|delete|deleted|lösche|löschen|loesche|loeschen|entferne|entfernen|"
    r"create|erstelle|erstellen)\b",
    re.IGNORECASE,
)
UPDATE_INTENT_RE = re.compile(
    r"\b(?:set|setze|setzen|setzt|change|changed|update|updated|aktualisiere|aktualisieren|"
    r"ändere|ändern|aendere|aendern)\b",
    re.IGNORECASE,
)
NETBOX_CHANGE_SUBJECT_RE = re.compile(
    r"\b(?:status|role|rolle|site|standort|location|tenant|device|devices|gerät|geräte|geraet|geraete|"
    r"virtual\s+machine|virtual\s+machines|vm|vms|rack|vlan|prefix|ip(?:\s+address)?|interface|"
    r"cluster|circuit|contact|kontakt|platform|plattform|description|beschreibung|name)\b",
    re.IGNORECASE,
)
COMPACT_NUMERIC_RANGE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_.-]*\d+\s*(?:-|–|—)\s*\d+\b")
OBJECT_IDENTIFIER_RE = re.compile(r"(?<!\w)[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*(?!\w)")
EMPTY_TABLE_VALUES = frozenset({"", "-", "—", "n/a", "none", "null"})
DISCOVERY_ONLY_RECORD_KEYS = frozenset({"id", "display", "display_url", "object_type"})
MAX_AUTOMATIC_HYDRATION_OBJECTS = 20
FALLBACK_FIELD_PRIORITIES: dict[str, tuple[str, ...]] = {
    "dcim.site": ("region", "group", "status", "facility", "description"),
    "dcim.location": ("site", "parent", "status", "tenant", "description"),
    "dcim.rack": ("site", "location", "role", "status", "u_height"),
    "dcim.device": ("role", "site", "location", "status", "primary_ip4"),
    "dcim.interface": ("device", "type", "enabled", "mtu", "description"),
    "ipam.vrf": ("rd", "tenant", "enforce_unique", "description"),
    "ipam.prefix": ("prefix", "vrf", "tenant", "status", "role"),
    "ipam.ipaddress": ("address", "vrf", "tenant", "status", "dns_name"),
    "ipam.vlan": ("vid", "site", "group", "status", "role"),
    "circuits.provider": ("asn", "account", "noc_contact", "description"),
    "circuits.circuit": ("cid", "provider", "type", "status", "commit_rate"),
    "virtualization.cluster": ("type", "group", "status", "tenant", "scope"),
    "virtualization.virtualmachine": ("role", "site", "cluster", "status", "primary_ip4"),
}
FALLBACK_OBJECT_LABELS = {
    "dcim.site": "Site",
    "dcim.location": "Location",
    "dcim.rack": "Rack",
    "dcim.device": "Device",
    "dcim.interface": "Interface",
    "ipam.vrf": "VRF",
    "ipam.prefix": "Prefix",
    "ipam.ipaddress": "IP address",
    "ipam.vlan": "VLAN",
    "circuits.provider": "Provider",
    "circuits.circuit": "Circuit",
    "virtualization.cluster": "Cluster",
    "virtualization.virtualmachine": "Virtual machine",
}
FALLBACK_FIELD_LABELS = {
    "region": "Region",
    "group": "Group",
    "status": "Status",
    "facility": "Facility",
    "description": "Description",
    "site": "Site",
    "parent": "Parent",
    "tenant": "Tenant",
    "location": "Location",
    "role": "Role",
    "u_height": "Height (U)",
    "primary_ip4": "Primary IPv4",
    "device": "Device",
    "type": "Type",
    "enabled": "Enabled",
    "mtu": "MTU",
    "rd": "RD",
    "enforce_unique": "Enforce unique",
    "prefix": "Prefix",
    "vrf": "VRF",
    "address": "IP address",
    "dns_name": "DNS name",
    "vid": "VLAN ID",
    "asn": "ASN",
    "account": "Account",
    "noc_contact": "NOC contact",
    "cid": "Circuit ID",
    "provider": "Provider",
    "commit_rate": "Commit rate",
    "scope": "Scope",
    "cluster": "Cluster",
}


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    tool_calls: int
    client_actions: tuple[dict[str, Any], ...] = ()
    pending_actions: tuple[dict[str, Any], ...] = ()
    rejection: RejectedResponse | None = None


@dataclass(frozen=True, slots=True)
class GroundingRecord:
    object_type: str
    data: dict[str, Any]
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
        max_tool_calls: int = 10,
        max_history_messages: int = 20,
        max_message_chars: int = 12000,
        max_tool_output_chars: int = 50000,
        max_response_chars: int = 20000,
        tool_timeout: float = 30,
        max_pending_actions: int = 5,
    ):
        self.model_provider = model_provider
        self.tool_provider = tool_provider
        self.max_tool_calls = max(1, min(int(max_tool_calls), 10))
        self.max_history_messages = max(1, min(int(max_history_messages), 100))
        self.max_message_chars = max(1, int(max_message_chars))
        self.max_tool_output_chars = max(512, int(max_tool_output_chars))
        self.max_response_chars = max(1, int(max_response_chars))
        self.tool_timeout = max(0.1, float(tool_timeout))
        self.max_pending_actions = max(1, min(int(max_pending_actions), 10))

    def run(
        self,
        context: ToolContext,
        history: list[dict[str, Any]],
        page_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        normalized_history = self._normalize_history(history)
        model_tools = [definition.as_model_tool() for definition in self.tool_provider.list_tools(context)]
        bulk_update_requested = self._looks_like_multi_object_update_request(normalized_history[-1]["content"])
        bulk_update_available = any(
            tool.get("function", {}).get("name") == "propose_bulk_update_named_objects" for tool in model_tools
        )
        if bulk_update_requested and bulk_update_available:
            model_tools = [
                tool
                for tool in model_tools
                if tool.get("function", {}).get("name") != "propose_update_object"
            ]
        available_tool_names = {
            str(tool.get("function", {}).get("name", "")) for tool in model_tools
        }
        write_proposals_available = any(
            str(tool.get("function", {}).get("name", "")).startswith("propose_") for tool in model_tools
        )
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
        if write_proposals_available:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Session capability for this request: WRITE PROPOSALS ENABLED. The current user has the "
                        "Navigator write capability and the listed propose_* tools are available. Never describe "
                        "this session as read-only or claim that the user lacks write access. For an explicit, "
                        "unambiguous request, create or delete exactly one object, or update one or more exact "
                        f"objects (at most {self.max_pending_actions}). First resolve the target type and call "
                        "describe_object_type, then call the matching proposal tool without asking for separate "
                        "preliminary confirmation. For multiple exact named update targets of "
                        "one type, call propose_bulk_update_named_objects once with every name; never call or emulate "
                        "a series of single-object proposals. Every returned preview requires its own manual browser "
                        "confirmation. Do not stage a partial batch if a target is unresolved or the configured "
                        "limit would be exceeded."
                        + (
                            " This request contains multiple update targets. The single-object update tool is "
                            "intentionally unavailable; use propose_bulk_update_named_objects for the complete set."
                            if bulk_update_requested and bulk_update_available
                            else ""
                        )
                    ),
                }
            )
        else:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Session capability for this request: READ-ONLY. No propose_* tools are available. Do not "
                        "offer or imply that a change preview can be staged in this session."
                    ),
                }
            )
        messages.extend(normalized_history)

        response = self.model_provider.complete(messages, model_tools)
        tool_call_count = 0
        forced_final = False
        data_tool_attempted = False
        successful_tool_calls: set[str] = set()
        successful_data_tools: set[str] = set()
        grounding_records: list[GroundingRecord] = []
        client_actions: list[dict[str, Any]] = []
        pending_actions: list[dict[str, Any]] = []
        pending_action_identities: set[tuple[Any, ...]] = set()
        proposal_limit_exceeded = False
        non_atomic_batch_rejected = False
        search_refinement_requested = False
        data_refresh_requested = False
        force_grounded_fallback = False

        while True:
            while response.tool_calls:
                if forced_final:
                    raise AgentLimitError("The model requested another tool after the tool-call limit was reached.")

                messages.append(response.as_assistant_message())
                for tool_call in response.tool_calls:
                    if tool_call_count >= self.max_tool_calls:
                        result = {"ok": False, "error": "The maximum number of tool calls has been reached."}
                    else:
                        tool_call_count += 1
                        if tool_call.name not in available_tool_names:
                            result = {"ok": False, "error": "This tool is not available for the current request."}
                        else:
                            result = self._execute_tool(context, tool_call.name, tool_call.arguments)
                        tool_result = result.get("result") if result.get("ok") else None
                        candidate_pending_actions: list[dict[str, Any]] = []
                        if isinstance(tool_result, dict):
                            single_pending_action = tool_result.get("pending_action")
                            multiple_pending_actions = tool_result.get("pending_actions")
                            if isinstance(single_pending_action, dict) and multiple_pending_actions is None:
                                candidate_pending_actions = [single_pending_action]
                            elif isinstance(multiple_pending_actions, list) and single_pending_action is None:
                                if multiple_pending_actions and all(
                                    isinstance(action, dict) for action in multiple_pending_actions
                                ):
                                    candidate_pending_actions = multiple_pending_actions
                                else:
                                    result = {"ok": False, "error": "The proposal tool returned an invalid batch."}
                            elif single_pending_action is not None or multiple_pending_actions is not None:
                                result = {"ok": False, "error": "The proposal tool returned an invalid preview."}
                        if candidate_pending_actions and result.get("ok"):
                            identities = [
                                self._pending_action_identity(action) for action in candidate_pending_actions
                            ]
                            if non_atomic_batch_rejected or (
                                len(candidate_pending_actions) == 1 and pending_actions
                            ):
                                non_atomic_batch_rejected = True
                                pending_actions.clear()
                                pending_action_identities.clear()
                                result = {
                                    "ok": False,
                                    "error": (
                                        "Multiple single-object proposals are not allowed. Use one atomic bulk "
                                        "proposal so no partial batch can be staged."
                                    ),
                                }
                            elif (
                                proposal_limit_exceeded
                                or len(pending_actions) + len(candidate_pending_actions) > self.max_pending_actions
                            ):
                                proposal_limit_exceeded = True
                                pending_actions.clear()
                                pending_action_identities.clear()
                                result = {
                                    "ok": False,
                                    "error": (
                                        "The maximum number of change proposals per assistant request "
                                        f"is {self.max_pending_actions}. No partial batch was staged."
                                    ),
                                }
                            elif len(set(identities)) != len(identities) or any(
                                identity in pending_action_identities for identity in identities
                            ):
                                result = {
                                    "ok": False,
                                    "error": "A change proposal for an object was duplicated in this request.",
                                }
                            else:
                                pending_actions.extend(candidate_pending_actions)
                                pending_action_identities.update(identities)
                        if (
                            result.get("ok")
                            and isinstance(tool_result, dict)
                            and isinstance(tool_result.get("client_action"), dict)
                        ):
                            action = tool_result["client_action"]
                            if (
                                self._valid_client_action(action)
                                and action not in client_actions
                                and len(client_actions) < 3
                            ):
                                client_actions.append(action)
                        if result.get("ok"):
                            successful_tool_calls.add(tool_call.name)
                    if tool_call.name in DATA_TOOL_NAMES:
                        data_tool_attempted = True
                        new_records = self._grounding_records(tool_call.name, result)
                        grounding_records.extend(new_records)
                        has_detail_fields = any(
                            set(record.data).difference(DISCOVERY_ONLY_RECORD_KEYS) for record in new_records
                        )
                        if new_records and (tool_call.name not in DETAIL_TOOL_NAMES or has_detail_fields):
                            successful_data_tools.add(tool_call.name)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": self._bounded_json(result),
                        }
                    )

                forced_final = tool_call_count >= self.max_tool_calls
                response = self.model_provider.complete(messages, [] if forced_final else model_tools)

            needs_search_refinement = (
                not forced_final
                and not search_refinement_requested
                and "search_netbox" in successful_data_tools
                and not DETAIL_TOOL_NAMES.intersection(successful_data_tools)
                and not client_actions
                and not pending_actions
            )
            if not needs_search_refinement:
                needs_automatic_hydration = (
                    "search_netbox" in successful_data_tools
                    and not DETAIL_TOOL_NAMES.intersection(successful_data_tools)
                    and not client_actions
                    and not pending_actions
                    and (search_refinement_requested or forced_final)
                )
                if needs_automatic_hydration:
                    hydrated_records, hydration_calls = self._hydrate_discovery_records(
                        context,
                        grounding_records,
                        limit=MAX_AUTOMATIC_HYDRATION_OBJECTS,
                    )
                    tool_call_count = min(self.max_tool_calls, tool_call_count + hydration_calls)
                    grounding_records.extend(hydrated_records)
                    if hydrated_records:
                        successful_data_tools.add("get_object")
                        force_grounded_fallback = True
                    forced_final = tool_call_count >= self.max_tool_calls

                answer_needs_data_refresh = (
                    not forced_final
                    and not data_refresh_requested
                    and not grounding_records
                    and not client_actions
                    and not pending_actions
                    and self._answer_references_netbox_data(response.content or "")
                )
                if not answer_needs_data_refresh:
                    break

                data_refresh_requested = True
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "This turn has no successful NetBox data result, so facts and links from earlier chat "
                            "messages cannot be verified. Do not answer from conversation history alone. Call the "
                            "appropriate current-user data tools now and then answer only from their fresh results."
                        ),
                    }
                )
                response = self.model_provider.complete(messages, model_tools)
                continue

            search_refinement_requested = True
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The successful search_netbox result is discovery-only. Do not answer from it yet. Use its "
                        "exact object_type and ID values with get_object to retrieve the requested fields, or use "
                        "describe_object_type and query_objects for a complete filtered list. Ignore search matches "
                        "whose object type does not match the user's request."
                    ),
                }
            )
            response = self.model_provider.complete(messages, model_tools)

        answer = response.content or ""
        rejected_response = answer
        rejection_reason: str | None = None
        if non_atomic_batch_rejected:
            rejection_reason = RejectionReason.PROPOSAL_GUARD
            answer = _(
                "No change proposals were created because multiple updates must be validated as one atomic batch. "
                "Please retry the complete named-object request."
            )
        elif proposal_limit_exceeded:
            rejection_reason = RejectionReason.PROPOSAL_GUARD
            answer = _(
                "No change proposals were created because the request exceeded the limit of %(limit)s objects. "
                "Please narrow the request."
            ) % {"limit": self.max_pending_actions}
        elif pending_actions:
            rejection_reason = RejectionReason.APPROVAL_NORMALIZATION
            if len(pending_actions) == 1:
                answer = _("The requested change was validated and is awaiting manual confirmation.")
            else:
                answer = _(
                    "%(count)s requested changes were validated. Each change is awaiting separate manual "
                    "confirmation."
                ) % {"count": len(pending_actions)}
        elif not answer:
            raise InvalidRequestError("The model did not return a final answer.")
        elif not successful_tool_calls and not self._answer_references_netbox_data(answer):
            if self._looks_like_change_request(normalized_history[-1]["content"]):
                rejection_reason = RejectionReason.CHANGE_GUARD
                if write_proposals_available:
                    answer = _(
                        "No validated change proposal could be created. Please verify the exact NetBox object "
                        "names and requested value, then try again."
                    )
                else:
                    answer = _("The current session is read-only. No NetBox change can be proposed or performed.")
            else:
                rejection_reason = RejectionReason.SCOPE_GUARD
                answer = _(
                    "AI Navigator is limited to NetBox data, configuration, and workflows. "
                    "Please ask a NetBox-related question."
                )
        elif force_grounded_fallback:
            rejection_reason = RejectionReason.GROUNDING_GUARD
            logger.info(
                "Returning deterministic fallback after automatic search hydration",
                extra={"grounding_records": len(grounding_records)},
            )
            answer = self._grounded_fallback(grounding_records)
        else:
            try:
                self._validate_grounding(
                    answer,
                    grounding_records,
                    data_tool_attempted=data_tool_attempted,
                    forced_final=forced_final,
                )
            except UngroundedResponseError:
                if not grounding_records:
                    raise UngroundedResponseError(
                        _(
                            "The model response could not be verified against NetBox data. "
                            "Please retry or refine the request."
                        ),
                        rejected_response=answer,
                    ) from None
                logger.info(
                    "Returning deterministic fallback for ungrounded model response",
                    extra={"grounding_records": len(grounding_records)},
                )
                rejection_reason = RejectionReason.GROUNDING_GUARD
                answer = self._grounded_fallback(grounding_records)
        if len(answer) > self.max_response_chars:
            suffix = "\n\n[Response truncated by NetBox AI Navigator.]"
            answer = answer[: max(0, self.max_response_chars - len(suffix))] + suffix
        rejection = None
        if (
            rejection_reason
            and rejected_response.strip()
            and (
                rejection_reason == RejectionReason.APPROVAL_NORMALIZATION
                or rejected_response != answer
            )
        ):
            rejection = RejectedResponse(reason=rejection_reason, response=rejected_response)
        return AgentResult(
            answer=answer,
            tool_calls=tool_call_count,
            client_actions=tuple(client_actions),
            pending_actions=tuple(pending_actions),
            rejection=rejection,
        )

    @classmethod
    def _answer_references_netbox_data(cls, answer: str) -> bool:
        if cls._markdown_tables(answer):
            return True
        return any(
            NETBOX_DETAIL_PATH_RE.fullmatch(cls._url_key(target))
            for _link_text, target in MARKDOWN_LINK_RE.findall(answer)
        )

    @staticmethod
    def _looks_like_change_request(user_message: str) -> bool:
        return bool(CHANGE_INTENT_RE.search(user_message) and NETBOX_CHANGE_SUBJECT_RE.search(user_message))

    @classmethod
    def _looks_like_multi_object_update_request(cls, user_message: str) -> bool:
        if not UPDATE_INTENT_RE.search(user_message) or not NETBOX_CHANGE_SUBJECT_RE.search(user_message):
            return False
        if COMPACT_NUMERIC_RANGE_RE.search(user_message):
            return True
        identifiers = {match.casefold() for match in OBJECT_IDENTIFIER_RE.findall(user_message)}
        return len(identifiers) >= 2

    @staticmethod
    def _pending_action_identity(action: dict[str, Any]) -> tuple[Any, ...]:
        return (
            action.get("operation"),
            action.get("object_type"),
            action.get("object_id"),
            action.get("endpoint"),
        )

    def _hydrate_discovery_records(
        self,
        context: ToolContext,
        records: list[GroundingRecord],
        *,
        limit: int,
    ) -> tuple[list[GroundingRecord], int]:
        if limit <= 0:
            return [], 0

        groups: dict[str, list[GroundingRecord]] = {}
        seen: set[tuple[str, int]] = set()
        for record in records:
            object_id = record.data.get("id")
            if (
                not record.object_type
                or isinstance(object_id, bool)
                or not isinstance(object_id, int)
                or object_id < 1
                or set(record.data).difference(DISCOVERY_ONLY_RECORD_KEYS)
            ):
                continue
            key = (record.object_type, object_id)
            if key in seen:
                continue
            seen.add(key)
            groups.setdefault(record.object_type, []).append(record)
        if not groups:
            return [], 0

        selected = max(groups.values(), key=len)
        hydrated: list[GroundingRecord] = []
        calls = 0
        for record in selected[:limit]:
            result = self._execute_tool(
                context,
                "get_object",
                {
                    "object_type": record.object_type,
                    "object_id": record.data["id"],
                },
            )
            calls += 1
            hydrated.extend(self._grounding_records("get_object", result))
        return hydrated, calls

    @staticmethod
    def _valid_client_action(action: dict[str, Any]) -> bool:
        url = action.get("url")
        label = action.get("label")
        return bool(
            action.get("type") == "navigate"
            and isinstance(url, str)
            and url.startswith("/")
            and not url.startswith("//")
            and len(url) <= 2048
            and isinstance(label, str)
            and 0 < len(label) <= 500
        )

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
        for _link_text, target in links:
            target_key = self._url_key(target)
            matching = [index for index, record in enumerate(records) if target_key in record.object_urls]
            if matching:
                return max(matching, key=lambda index: len(records[index].values))

        visible_value = self._normalize_value(self._strip_markdown(cell))
        matching = [index for index, record in enumerate(records) if visible_value in record.identities]
        if matching:
            return max(matching, key=lambda index: len(records[index].values))
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
        if tool_name in {"query_objects", "search_netbox"}:
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
            object_type = str(value.get("object_type") or result.get("object_type") or "")
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
                        object_type=object_type,
                        data=dict(value),
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
        positions: dict[tuple[str | None, str], int] = {}
        for record in records:
            key = (record.display_url, cls._normalize_value(record.display))
            position = positions.get(key)
            if position is None:
                positions[key] = len(unique_records)
                unique_records.append(record)
            elif len(record.values) > len(unique_records[position].values):
                unique_records[position] = record

        detailed_records = [
            record for record in unique_records if set(record.data).difference(DISCOVERY_ONLY_RECORD_KEYS)
        ]
        if detailed_records:
            unique_records = detailed_records

        # Discovery commonly resolves a container (for example a site) before
        # querying the requested child objects. If grounding validation later
        # requires the deterministic fallback, keep that supporting container
        # from preventing an otherwise useful homogeneous result table. Only
        # select a group when it is uniquely largest; genuinely mixed results
        # with equally sized groups remain a list.
        groups: dict[str, list[GroundingRecord]] = {}
        for record in unique_records:
            groups.setdefault(record.object_type, []).append(record)
        if len(groups) > 1:
            ranked_groups = sorted(groups.values(), key=len, reverse=True)
            if len(ranked_groups[0]) > len(ranked_groups[1]) and cls._fallback_table(ranked_groups[0]):
                unique_records = ranked_groups[0]

        lines = [_("Verified NetBox results ({count}):").format(count=len(unique_records)), ""]
        table = cls._fallback_table(unique_records)
        if table:
            lines.extend(table)
            return "\n".join(lines)

        for record in unique_records:
            display = cls._escape_markdown(record.display, link_text=True)
            if record.display_url:
                lines.append(f"- [{display}]({cls._url_key(record.display_url)})")
            else:
                lines.append(f"- `{record.display.replace('`', '')}`")
        return "\n".join(lines)

    @classmethod
    def _fallback_table(cls, records: list[GroundingRecord]) -> list[str]:
        if len(records) < 2:
            return []
        object_types = {record.object_type for record in records}
        if len(object_types) != 1:
            return []
        object_type = next(iter(object_types))
        priorities = FALLBACK_FIELD_PRIORITIES.get(object_type) or cls._generic_fallback_priorities(records)

        fields = [
            field for field in priorities if any(cls._fallback_value(record.data.get(field)) for record in records)
        ][:4]
        if len(fields) < 2:
            return []

        default_object_label = object_type.rsplit(".", 1)[-1].replace("_", " ").title() or "Object"
        object_label = _(FALLBACK_OBJECT_LABELS.get(object_type, default_object_label))
        headers = [
            object_label,
            *(_(FALLBACK_FIELD_LABELS.get(field, field.replace("_", " ").title())) for field in fields),
        ]
        lines = [
            f"| {' | '.join(headers)} |",
            f"| {' | '.join('---' for _header in headers)} |",
        ]
        for record in records:
            display = cls._escape_markdown(record.display, link_text=True)
            if record.display_url:
                object_cell = f"[{display}]({cls._url_key(record.display_url)})"
            else:
                object_cell = cls._escape_markdown(record.display)
            values = [cls._escape_markdown(cls._fallback_value(record.data.get(field)) or "—") for field in fields]
            lines.append(f"| {' | '.join([object_cell, *values])} |")
        return lines

    @classmethod
    def _generic_fallback_priorities(cls, records: list[GroundingRecord]) -> tuple[str, ...]:
        excluded = IDENTITY_KEYS | {"id", "display_url", "url", "custom_fields", "tags"}
        candidates = dict.fromkeys(field for record in records for field in record.data if field not in excluded)
        useful = []
        for field in candidates:
            values = [cls._fallback_value(record.data.get(field)) for record in records]
            nonempty = [value for value in values if value]
            if nonempty and max(map(len, nonempty)) <= 100:
                useful.append(field)
        return tuple(useful)

    @classmethod
    def _fallback_value(cls, value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, dict):
            for key in ("display", "label", "name", "value"):
                nested = value.get(key)
                if nested is not None and nested != "":
                    return cls._fallback_value(nested)
            return ""
        if isinstance(value, list):
            values = [cls._fallback_value(item) for item in value]
            return ", ".join(item for item in values if item)
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        return " ".join(str(value).split())

    @staticmethod
    def _escape_markdown(value: str, *, link_text: bool = False) -> str:
        escaped = value.replace("\\", r"\\").replace("|", r"\|").replace("\n", " ")
        if link_text:
            escaped = escaped.replace("[", r"\[").replace("]", r"\]")
        return escaped

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
            _("The model response could not be verified against NetBox data. Please retry or refine the request.")
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
        max_tool_calls=agent_config.get("max_tool_calls", 10),
        max_history_messages=agent_config.get("max_history_messages", 20),
        max_message_chars=agent_config.get("max_message_chars", 12000),
        max_tool_output_chars=tools_config.get("max_output_chars", 50000),
        max_response_chars=model_config.get("max_response_chars", 20000),
        tool_timeout=tools_config.get("timeout", 30),
        max_pending_actions=(tools_config.get("write") or {}).get("max_pending", 5),
    )
