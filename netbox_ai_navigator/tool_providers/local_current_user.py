import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.http import QueryDict
from django.urls import NoReverseMatch, resolve, reverse
from django.utils.translation import gettext as _
from netbox.api.exceptions import SerializerNotFound
from netbox.registry import registry
from netbox.search.backends import search_backend
from utilities.api import get_serializer_for_model
from utilities.views import get_action_url

from netbox_ai_navigator.documentation import DocumentationIndex
from netbox_ai_navigator.exceptions import ToolError, ToolNotFoundError, ToolValidationError

from .base import ToolDefinition, ToolProvider
from .context import ToolContext

IDENTITY_FIELDS = ("id", "display", "display_url")
MAX_WRITE_PAYLOAD_CHARS = 20000
CHANGELOG_MESSAGE = "Changed through NetBox AI Navigator after explicit user confirmation."

# These restrictions are deliberately not configurable. Dynamic discovery must
# never turn a newly installed model into a credential-reading side channel.
NEVER_ALLOWED_OBJECT_TYPES = frozenset(
    {
        "extras.webhook",
        "users.token",
    }
)
NEVER_ALLOWED_NAME_FRAGMENTS = (
    "api_key",
    "apikey",
    "auth_key",
    "access_key",
    "community",
    "credential",
    "encryption_key",
    "passphrase",
    "password",
    "preshared_key",
    "private_key",
    "secret",
    "session_key",
    "shared_key",
    "ssh_key",
    "token",
)
NEVER_ALLOWED_FIELDS = frozenset(
    {
        "additional_headers",
        "auth_key",
        "body_template",
        "config_context",
        "config_data",
        "data",
        "key",
        "local_context_data",
        "parameters",
        "password",
        "postchange_data",
        "prechange_data",
        "preshared_key",
        "private_key",
        "secret",
        "template_code",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ObjectTypeCapability:
    model: Any
    filterset_class: Any
    serializer_class: Any


class LocalCurrentUserProvider(ToolProvider):
    """Read-only NetBox tools executed under the current user's object permissions."""

    def __init__(self, config: dict[str, Any]):
        configured_types = config.get("allowed_object_types")
        self.configured_allowed_object_types = (
            None if configured_types is None else frozenset(self._normalize_config_values(configured_types))
        )
        self.excluded_object_types = NEVER_ALLOWED_OBJECT_TYPES | frozenset(
            self._normalize_config_values(config.get("excluded_object_types") or [])
        )
        self.excluded_fields = NEVER_ALLOWED_FIELDS | frozenset(
            self._normalize_config_values(config.get("excluded_fields") or [])
        )
        self.include_custom_fields = config.get("include_custom_fields", False) is True
        self.max_results = max(1, min(int(config.get("max_results", 50)), 50))
        self.timeout = max(0.1, float(config.get("timeout", 30)))
        documentation_config = config.get("documentation") or {}
        self.documentation = (
            DocumentationIndex(documentation_config) if documentation_config.get("enabled", True) else None
        )
        self._capabilities = self._discover_capabilities()
        self.allowed_object_types = tuple(self._capabilities)

    def list_tools(self, context: ToolContext) -> list[ToolDefinition]:
        object_type_schema = {
            "type": "string",
            "pattern": "^[a-z0-9_]+\\.[a-z0-9_]+$",
            "description": "A discovered NetBox model label in app.model form.",
        }
        tools = [
            ToolDefinition(
                name="list_object_types",
                description=(
                    "Discover the core and plugin NetBox object types available to the current-user assistant. "
                    "Use query to narrow the result by model label or translated name."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 100},
                    },
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="describe_object_type",
                description=(
                    "Describe the readable output fields and registered filters for a discovered core or plugin "
                    "object type. Call this before querying an unfamiliar type."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"object_type": object_type_schema},
                    "required": ["object_type"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="query_objects",
                description=(
                    "Query read-only core or plugin NetBox objects. Filters use the registered NetBox FilterSet "
                    "semantics. Discover unknown model labels with list_object_types and their exact fields and "
                    "filters with describe_object_type. "
                    "Use the q filter for free-text searches on the queried object. To find objects by city, site, "
                    "or location, first query dcim.site or dcim.location with q, then filter the target object type "
                    "by the returned site_id or location_id. A target object's q filter does not necessarily search "
                    "related objects. Relationship filters such as site and location accept registered choices, not "
                    "unverified free text. Call describe_object_type first when other filter or field names are "
                    "uncertain."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "object_type": object_type_schema,
                        "filters": {
                            "type": "object",
                            "additionalProperties": {
                                "oneOf": [
                                    {"type": ["string", "number", "integer", "boolean", "null"]},
                                    {
                                        "type": "array",
                                        "items": {"type": ["string", "number", "integer", "boolean"]},
                                    },
                                ]
                            },
                        },
                        "fields": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                        "order_by": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": self.max_results},
                    },
                    "required": ["object_type"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="get_object",
                description="Get one read-only NetBox object by numeric ID if the current user may view it.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "object_type": object_type_schema,
                        "object_id": {"type": "integer", "minimum": 1},
                        "fields": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    },
                    "required": ["object_type", "object_id"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="search_netbox",
                description=(
                    "Search NetBox's global object index across core and installed plugins under current-user "
                    "permissions. Use this only to discover an object's exact model type and ID. Before answering "
                    "with object attributes, follow every relevant match with get_object or a filtered query_objects "
                    "call."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 2, "maxLength": 200},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
        ]
        if self.documentation and self.documentation.available:
            tools.extend(
                [
                    ToolDefinition(
                        name="search_documentation",
                        description=(
                            "Search the locally installed NetBox and plugin documentation. Use this for questions "
                            "about configuration, concepts, workflows, APIs, or plugin behavior rather than guessing."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "minLength": 2, "maxLength": 200},
                                "limit": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": self.documentation.max_results,
                                },
                            },
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    ),
                    ToolDefinition(
                        name="read_documentation",
                        description="Read one exact section returned by search_documentation.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "doc_id": {"type": "string", "minLength": 1, "maxLength": 1000},
                            },
                            "required": ["doc_id"],
                            "additionalProperties": False,
                        },
                    ),
                ]
            )
        tools.extend(
            [
                ToolDefinition(
                    name="navigate_to_object",
                    description=(
                        "Offer a verified browser navigation action for one NetBox object the current user may view. "
                        "Use only when the user explicitly asks to open, show, or navigate to that object."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "object_type": object_type_schema,
                            "object_id": {"type": "integer", "minimum": 1},
                        },
                        "required": ["object_type", "object_id"],
                        "additionalProperties": False,
                    },
                ),
                ToolDefinition(
                    name="navigate_to_object_list",
                    description=(
                        "Offer navigation to the list page of a discovered NetBox core or plugin object type. "
                        "Use only when the user explicitly requests navigation."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"object_type": object_type_schema},
                        "required": ["object_type"],
                        "additionalProperties": False,
                    },
                ),
                ToolDefinition(
                    name="navigate_to_search",
                    description="Offer navigation to NetBox global search for an explicit user-requested query.",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
            ]
        )
        if context.can_write:
            write_data_schema = {"type": "object", "minProperties": 1, "additionalProperties": True}
            tools.extend(
                [
                    ToolDefinition(
                        name="propose_create_object",
                        description=(
                            "Validate and propose creating one NetBox object. This never writes immediately and "
                            "always requires the user to approve the exact preview in the browser."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {"object_type": object_type_schema, "data": write_data_schema},
                            "required": ["object_type", "data"],
                            "additionalProperties": False,
                        },
                    ),
                    ToolDefinition(
                        name="propose_update_object",
                        description=(
                            "Validate and propose a partial update to one NetBox object. This never writes "
                            "immediately and always requires explicit browser approval."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "object_type": object_type_schema,
                                "object_id": {"type": "integer", "minimum": 1},
                                "data": write_data_schema,
                            },
                            "required": ["object_type", "object_id", "data"],
                            "additionalProperties": False,
                        },
                    ),
                    ToolDefinition(
                        name="propose_delete_object",
                        description=(
                            "Propose deleting one NetBox object. This never deletes immediately and always requires "
                            "explicit browser approval. Use only for an unambiguous user request."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "object_type": object_type_schema,
                                "object_id": {"type": "integer", "minimum": 1},
                            },
                            "required": ["object_type", "object_id"],
                            "additionalProperties": False,
                        },
                    ),
                ]
            )
        return tools

    def call_tool(self, context: ToolContext, name: str, arguments: dict[str, Any]) -> Any:
        handlers = {
            "list_object_types": self._list_object_types,
            "describe_object_type": self._describe_object_type,
            "query_objects": self._query_objects,
            "get_object": self._get_object,
            "search_netbox": self._search_netbox,
            "search_documentation": self._search_documentation,
            "read_documentation": self._read_documentation,
            "navigate_to_object": self._navigate_to_object,
            "navigate_to_object_list": self._navigate_to_object_list,
            "navigate_to_search": self._navigate_to_search,
            "propose_create_object": self._propose_create_object,
            "propose_update_object": self._propose_update_object,
            "propose_delete_object": self._propose_delete_object,
        }
        try:
            handler = handlers[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Unknown tool: {name}") from exc
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        database_tools = {
            "query_objects",
            "get_object",
            "search_netbox",
            "navigate_to_object",
            "propose_create_object",
            "propose_update_object",
            "propose_delete_object",
        }
        if name not in database_tools and not (name == "describe_object_type" and self.include_custom_fields):
            return handler(context, arguments)
        try:
            with transaction.atomic():
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            [f"{round(self.timeout * 1000)}ms"],
                        )
                return handler(context, arguments)
        except DatabaseError as exc:
            raise ToolError("The tool database query failed or timed out.") from exc

    def _list_object_types(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_arguments(arguments, {"query"})
        query = arguments.get("query")
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 100):
            raise ToolValidationError("query must be a non-empty string with at most 100 characters.")
        normalized_query = query.strip().casefold() if query else None
        object_types = []
        for label, capability in self._capabilities.items():
            model = capability.model
            item = {
                "object_type": label,
                "name": str(model._meta.verbose_name),
                "name_plural": str(model._meta.verbose_name_plural),
            }
            if normalized_query and normalized_query not in " ".join(item.values()).casefold():
                continue
            object_types.append(item)
        return {"count": len(object_types), "object_types": object_types}

    def _describe_object_type(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"object_type"}, {"object_type"})
        label = self._normalize_label(arguments["object_type"])
        model, filterset_class, serializer_class = self._resolve(label, context)
        output_fields = self._supported_output_fields(serializer_class, context)
        available_filters = filterset_class.base_filters
        if self.include_custom_fields:
            filterset = filterset_class(queryset=self._restricted_queryset(model, context), request=context.request)
            available_filters = filterset.filters
        filters = [
            {
                "name": name,
                "label": str(filter_.label or name),
                "type": filter_.__class__.__name__,
            }
            for name, filter_ in available_filters.items()
            if self._field_is_allowed(name)
        ]
        return {
            "object_type": label,
            "name": str(model._meta.verbose_name),
            "name_plural": str(model._meta.verbose_name_plural),
            "output_fields": output_fields,
            "writable_fields": (self._supported_write_fields(serializer_class, context) if context.can_write else []),
            "filters": filters,
            "max_results": self.max_results,
        }

    def _query_objects(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"object_type", "filters", "fields", "order_by", "limit"}
        self._require_arguments(arguments, {"object_type"}, allowed)
        label = self._normalize_label(arguments["object_type"])
        model, filterset_class, serializer_class = self._resolve(label, context)
        fields = self._select_fields(serializer_class, context, arguments.get("fields"))
        queryset = self._restricted_queryset(model, context)

        filters = arguments.get("filters") or {}
        if not isinstance(filters, dict):
            raise ToolValidationError("filters must be a JSON object.")
        query_data = self._to_query_dict(filters)
        filterset = filterset_class(data=query_data, queryset=queryset, request=context.request)
        supported_filters = {name for name in filterset.filters if self._field_is_allowed(name)}
        unknown_filters = set(filters) - supported_filters
        if unknown_filters:
            raise ToolValidationError(f"Unsupported filters: {', '.join(sorted(unknown_filters))}")

        if not filterset.is_valid():
            raise ToolValidationError(f"Invalid filters: {filterset.errors.get_json_data(escape_html=True)}")
        try:
            queryset = filterset.qs
        except ValidationError as exc:
            raise ToolValidationError(f"Invalid filters: {exc}") from exc

        ordering = arguments.get("order_by") or []
        if ordering:
            queryset = queryset.order_by(*self._validate_ordering(model, serializer_class, context, ordering))

        limit = arguments.get("limit", self.max_results)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.max_results:
            raise ToolValidationError(f"limit must be between 1 and {self.max_results}.")

        objects = list(queryset[:limit])
        serialized = serializer_class(
            objects,
            many=True,
            fields=fields,
            context={"request": context.request},
        ).data
        return {
            "object_type": label,
            "returned": len(objects),
            "limit": limit,
            "objects": [self._sanitize_serialized_value(item) for item in serialized],
        }

    def _get_object(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"object_type", "object_id", "fields"}
        self._require_arguments(arguments, {"object_type", "object_id"}, allowed)
        label = self._normalize_label(arguments["object_type"])
        model, _filterset_class, serializer_class = self._resolve(label, context)
        object_id = arguments["object_id"]
        if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id < 1:
            raise ToolValidationError("object_id must be a positive integer.")
        fields = self._select_fields(serializer_class, context, arguments.get("fields"))

        # Restriction precedes lookup so an unauthorized ID is indistinguishable from a missing ID.
        instance = self._restricted_queryset(model, context).filter(pk=object_id).first()
        if instance is None:
            return {"object_type": label, "found": False, "object": None}

        serialized = serializer_class(instance, fields=fields, context={"request": context.request}).data
        return {"object_type": label, "found": True, "object": self._sanitize_serialized_value(dict(serialized))}

    def _search_netbox(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"query"}, {"query", "limit"})
        query = arguments["query"]
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 200:
            raise ToolValidationError("query must contain between 2 and 200 characters.")
        limit = arguments.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ToolValidationError("limit must be between 1 and 20.")
        results = []
        for match in search_backend.search(query.strip(), user=context.user):
            instance = getattr(match, "object", None)
            label = getattr(getattr(instance, "_meta", None), "label_lower", None)
            if instance is None or label not in self._capabilities:
                continue
            try:
                display_url = self._safe_local_url(instance.get_absolute_url())
            except (AttributeError, ToolValidationError):
                continue
            results.append(
                {
                    "id": instance.pk,
                    "display": str(instance),
                    "display_url": display_url,
                    "object_type": label,
                }
            )
            if len(results) >= limit:
                break
        return {"query": query.strip(), "returned": len(results), "objects": results}

    def _search_documentation(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"query"}, {"query", "limit"})
        if not self.documentation or not self.documentation.available:
            raise ToolValidationError("Local documentation is not available.")
        query = arguments["query"]
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 200:
            raise ToolValidationError("query must contain between 2 and 200 characters.")
        limit = arguments.get("limit")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.documentation.max_results
        ):
            raise ToolValidationError(f"limit must be between 1 and {self.documentation.max_results}.")
        results = self.documentation.search(query.strip(), limit)
        return {"query": query.strip(), "returned": len(results), "results": results}

    def _read_documentation(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"doc_id"}, {"doc_id"})
        if not self.documentation or not self.documentation.available:
            raise ToolValidationError("Local documentation is not available.")
        doc_id = arguments["doc_id"]
        if not isinstance(doc_id, str) or not doc_id or len(doc_id) > 1000:
            raise ToolValidationError("doc_id is invalid.")
        result = self.documentation.read(doc_id)
        if result is None:
            raise ToolValidationError("The requested documentation section does not exist.")
        return result

    def _navigate_to_object(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"object_type", "object_id"}, {"object_type", "object_id"})
        label = self._normalize_label(arguments["object_type"])
        model, _filterset_class, _serializer_class = self._resolve(label, context)
        object_id = self._positive_object_id(arguments["object_id"])
        instance = self._restricted_queryset(model, context).filter(pk=object_id).first()
        if instance is None:
            raise ToolValidationError("The object does not exist or is not visible to the current user.")
        url = instance.get_absolute_url()
        return {
            "object_type": label,
            "object_id": object_id,
            "client_action": {
                "type": "navigate",
                "url": self._safe_local_url(url),
                "label": str(instance),
            },
        }

    def _navigate_to_object_list(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"object_type"}, {"object_type"})
        label = self._normalize_label(arguments["object_type"])
        model, _filterset_class, _serializer_class = self._resolve(label, context)
        permission = f"{model._meta.app_label}.view_{model._meta.model_name}"
        if not context.user.has_perm(permission):
            raise ToolValidationError("The current user may not view this object type.")
        try:
            url = get_action_url(model, action="list")
        except NoReverseMatch as exc:
            raise ToolValidationError("No list page is registered for this object type.") from exc
        return {
            "object_type": label,
            "client_action": {
                "type": "navigate",
                "url": self._safe_local_url(url),
                "label": str(model._meta.verbose_name_plural),
            },
        }

    def _navigate_to_search(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"query"}, {"query"})
        query = arguments["query"]
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ToolValidationError("query must be a non-empty string with at most 200 characters.")
        url = f"{reverse('search')}?{urlencode({'q': query.strip()})}"
        return {
            "query": query.strip(),
            "client_action": {"type": "navigate", "url": url, "label": query.strip()},
        }

    def _propose_create_object(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_write(context)
        self._require_arguments(arguments, {"object_type", "data"}, {"object_type", "data"})
        label = self._normalize_label(arguments["object_type"])
        model, _filterset_class, serializer_class = self._resolve(label, context)
        permission = f"{model._meta.app_label}.add_{model._meta.model_name}"
        if not context.user.has_perm(permission):
            raise ToolValidationError("The current user may not create this object type.")
        payload = self._validated_write_payload(serializer_class, context, arguments["data"])
        endpoint = self._api_endpoint(model, "list")
        self._require_endpoint_method(endpoint, "POST")
        changes = [
            {"field": field, "before": None, "after": self._preview_value(value)}
            for field, value in arguments["data"].items()
        ]
        return self._pending_action(
            operation="create",
            method="POST",
            endpoint=endpoint,
            payload=payload,
            object_type=label,
            object_id=None,
            title=_("Create {object_type}").format(object_type=str(model._meta.verbose_name)),
            target=str(model._meta.verbose_name),
            changes=changes,
        )

    def _propose_update_object(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_write(context)
        allowed = {"object_type", "object_id", "data"}
        self._require_arguments(arguments, allowed, allowed)
        label = self._normalize_label(arguments["object_type"])
        model, _filterset_class, serializer_class = self._resolve(label, context)
        object_id = self._positive_object_id(arguments["object_id"])
        instance = self._restricted_queryset(model, context, "change").filter(pk=object_id).first()
        if instance is None:
            raise ToolValidationError("The object does not exist or may not be changed by the current user.")
        data = arguments["data"]
        target = str(instance)
        etag = self._object_etag(instance)
        current = self._serialize_preview(instance, serializer_class, context, data)
        payload = self._validated_write_payload(serializer_class, context, data, instance=instance)
        endpoint = self._api_endpoint(model, "detail", object_id)
        self._require_endpoint_method(endpoint, "PATCH")
        changes = [
            {
                "field": field,
                "before": self._preview_value(current.get(field)),
                "after": self._preview_value(value),
            }
            for field, value in data.items()
        ]
        return self._pending_action(
            operation="update",
            method="PATCH",
            endpoint=endpoint,
            payload=payload,
            object_type=label,
            object_id=object_id,
            title=_("Update {object}").format(object=target),
            target=target,
            changes=changes,
            etag=etag,
        )

    def _propose_delete_object(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_write(context)
        allowed = {"object_type", "object_id"}
        self._require_arguments(arguments, allowed, allowed)
        label = self._normalize_label(arguments["object_type"])
        model, _filterset_class, _serializer_class = self._resolve(label, context)
        object_id = self._positive_object_id(arguments["object_id"])
        instance = self._restricted_queryset(model, context, "delete").filter(pk=object_id).first()
        if instance is None:
            raise ToolValidationError("The object does not exist or may not be deleted by the current user.")
        endpoint = self._api_endpoint(model, "detail", object_id)
        self._require_endpoint_method(endpoint, "DELETE")
        return self._pending_action(
            operation="delete",
            method="DELETE",
            endpoint=endpoint,
            payload={"changelog_message": CHANGELOG_MESSAGE},
            object_type=label,
            object_id=object_id,
            title=_("Delete {object}").format(object=str(instance)),
            target=str(instance),
            changes=[],
            etag=self._object_etag(instance),
        )

    def _resolve(self, label: str, context: ToolContext):
        capability = self._capabilities.get(label)
        if capability is None:
            raise ToolValidationError(f"Object type is not allowed: {label}")
        return capability.model, capability.filterset_class, capability.serializer_class

    def supports_object_type(self, label: str | None) -> bool:
        """Return whether a model label passed the dynamic read-safety checks."""
        return bool(label and label.lower() in self._capabilities)

    def _discover_capabilities(self) -> dict[str, ObjectTypeCapability]:
        capabilities = {}
        for model in apps.get_models():
            label = model._meta.label_lower
            model_name = label.rsplit(".", 1)[-1]
            if label in self.excluded_object_types or any(term in model_name for term in NEVER_ALLOWED_NAME_FRAGMENTS):
                continue
            if self.configured_allowed_object_types is not None and label not in self.configured_allowed_object_types:
                continue
            if model._meta.abstract or not hasattr(model._default_manager, "restrict"):
                continue
            filterset_class = registry["filtersets"].get(label)
            if filterset_class is None:
                continue
            try:
                serializer_class = get_serializer_for_model(model)
            except SerializerNotFound:
                continue
            capabilities[label] = ObjectTypeCapability(model, filterset_class, serializer_class)
        return dict(sorted(capabilities.items()))

    @staticmethod
    def _restricted_queryset(model, context: ToolContext, action: str = "view"):
        manager = model._default_manager
        if not hasattr(manager, "restrict"):
            raise ToolValidationError("This object type does not support NetBox object permissions.")
        return manager.restrict(context.user, action)

    def _supported_output_fields(self, serializer_class, context: ToolContext) -> list[str]:
        serializer = serializer_class(context={"request": context.request})
        return [name for name, field in serializer.fields.items() if self._field_is_allowed(name, field)]

    def _supported_write_fields(self, serializer_class, context: ToolContext) -> list[dict[str, Any]]:
        serializer = serializer_class(context={"request": context.request})
        return [
            {
                "name": name,
                "label": str(getattr(field, "label", None) or name),
                "type": field.__class__.__name__,
                "required": bool(getattr(field, "required", False)),
                "allow_null": bool(getattr(field, "allow_null", False)),
            }
            for name, field in serializer.fields.items()
            if not getattr(field, "read_only", False)
            and name not in {*IDENTITY_FIELDS, "url"}
            and self._write_field_is_allowed(name, field)
        ]

    def _validated_write_payload(
        self,
        serializer_class,
        context: ToolContext,
        data: Any,
        *,
        instance: Any | None = None,
    ) -> dict[str, Any]:
        if not isinstance(data, dict) or not data or len(data) > 25:
            raise ToolValidationError("data must be a non-empty JSON object with at most 25 fields.")
        try:
            serialized_size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError) as exc:
            raise ToolValidationError("data must contain only JSON values.") from exc
        if serialized_size > MAX_WRITE_PAYLOAD_CHARS:
            raise ToolValidationError(f"data exceeds the {MAX_WRITE_PAYLOAD_CHARS}-character limit.")
        if self._contains_blocked_nested_name(data):
            raise ToolValidationError("data contains a blocked credential-bearing or excessively nested field.")

        supported = {field["name"] for field in self._supported_write_fields(serializer_class, context)}
        unsupported = set(data) - supported
        if unsupported:
            raise ToolValidationError(f"Unsupported writable fields: {', '.join(sorted(unsupported))}")

        payload = {**data, "changelog_message": CHANGELOG_MESSAGE}
        serializer = serializer_class(
            instance,
            data=payload,
            partial=instance is not None,
            context={"request": context.request},
        )
        if not serializer.is_valid():
            raise ToolValidationError(
                "Invalid change data: "
                + json.dumps(serializer.errors, ensure_ascii=False, default=str, separators=(",", ":"))
            )
        return payload

    def _serialize_preview(
        self, instance, serializer_class, context: ToolContext, data: dict[str, Any]
    ) -> dict[str, Any]:
        readable = set(self._supported_output_fields(serializer_class, context))
        fields = [*IDENTITY_FIELDS, *(field for field in data if field in readable)]
        fields = list(dict.fromkeys(field for field in fields if field in readable))
        serialized = dict(serializer_class(instance, fields=fields, context={"request": context.request}).data)
        return self._sanitize_serialized_value(serialized)

    def _write_field_is_allowed(self, name: str, serializer_field: Any) -> bool:
        normalized = name.casefold()
        source = getattr(serializer_field, "source", None)
        normalized_source = source.casefold().split(".", 1)[0] if isinstance(source, str) else None
        is_custom_field = self._is_custom_field_reference(normalized) or (
            normalized_source is not None and self._is_custom_field_reference(normalized_source)
        )
        return (
            (self.include_custom_fields or not is_custom_field)
            and not self._name_is_blocked(normalized)
            and (normalized_source is None or not self._name_is_blocked(normalized_source))
        )

    @staticmethod
    def _pending_action(
        *,
        operation: str,
        method: str,
        endpoint: str,
        payload: dict[str, Any],
        object_type: str,
        object_id: int | None,
        title: str,
        target: str,
        changes: list[dict[str, Any]],
        etag: str | None = None,
    ) -> dict[str, Any]:
        return {
            "requires_confirmation": True,
            "pending_action": {
                "type": "change_approval",
                "operation": operation,
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "object_type": object_type,
                "object_id": object_id,
                "title": title,
                "target": target,
                "changes": changes,
                "etag": etag,
            },
        }

    @staticmethod
    def _api_endpoint(model, action: str, object_id: int | None = None) -> str:
        kwargs = {"pk": object_id} if object_id is not None else None
        try:
            return get_action_url(model, action=action, rest_api=True, kwargs=kwargs)
        except NoReverseMatch as exc:
            raise ToolValidationError(
                f"No writable REST API endpoint is registered for {model._meta.label_lower}."
            ) from exc

    @staticmethod
    def _require_endpoint_method(endpoint: str, method: str) -> None:
        try:
            match = resolve(endpoint)
        except Exception as exc:
            raise ToolValidationError("The REST API endpoint could not be resolved.") from exc
        actions = getattr(match.func, "actions", {})
        if method.lower() not in actions:
            raise ToolValidationError(f"The object type does not support {method} through its REST API.")

    @staticmethod
    def _object_etag(instance) -> str | None:
        timestamp = getattr(instance, "last_updated", None) or getattr(instance, "created", None)
        return f'W/"{timestamp.isoformat()}"' if timestamp else None

    @staticmethod
    def _positive_object_id(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ToolValidationError("object_id must be a positive integer.")
        return value

    @staticmethod
    def _require_write(context: ToolContext) -> None:
        if not context.can_write:
            raise ToolValidationError("Write proposals are not available to the current user.")

    @staticmethod
    def _safe_local_url(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or len(value) > 2048:
            raise ToolValidationError("The navigation target is not a safe local URL.")
        return value

    @classmethod
    def _preview_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, list):
            return [cls._preview_value(item) for item in value[:20]]
        if isinstance(value, dict):
            if "label" in value and "value" in value:
                return cls._preview_value(value["label"])
            if "display" in value and ("id" in value or "url" in value):
                return cls._preview_value(value["display"])
            return {str(key)[:100]: cls._preview_value(item) for key, item in list(value.items())[:20]}
        return str(value)[:500]

    def _select_fields(self, serializer_class, context: ToolContext, requested) -> list[str]:
        supported = self._supported_output_fields(serializer_class, context)
        if requested is None:
            return supported
        if not isinstance(requested, list) or not all(isinstance(field, str) for field in requested):
            raise ToolValidationError("fields must be an array of field names.")
        unsupported = set(requested) - set(supported)
        if unsupported:
            raise ToolValidationError(f"Unsupported output fields: {', '.join(sorted(unsupported))}")
        return list(dict.fromkeys([*(field for field in IDENTITY_FIELDS if field in supported), *requested]))

    def _validate_ordering(self, model, serializer_class, context: ToolContext, ordering) -> list[str]:
        if not isinstance(ordering, list) or not all(isinstance(field, str) for field in ordering):
            raise ToolValidationError("order_by must be an array of field names.")
        concrete_fields = {field.name for field in model._meta.concrete_fields}
        allowed = concrete_fields.intersection(self._supported_output_fields(serializer_class, context)) | {"id"}
        invalid = [field for field in ordering if not field or field.removeprefix("-") not in allowed]
        if invalid:
            raise ToolValidationError(f"Unsupported ordering fields: {', '.join(invalid)}")
        return ordering

    def _field_is_allowed(self, name: str, serializer_field: Any | None = None) -> bool:
        normalized = name.casefold()
        base_name = normalized.split("__", 1)[0]
        if not self.include_custom_fields and (
            self._is_custom_field_reference(normalized) or self._is_custom_field_reference(base_name)
        ):
            return False
        if self._name_is_blocked(normalized) or self._name_is_blocked(base_name):
            return False
        if serializer_field is None:
            return True
        if getattr(serializer_field, "write_only", False):
            return False
        source = getattr(serializer_field, "source", None)
        if not isinstance(source, str):
            return True
        normalized_source = source.casefold()
        source_root = normalized_source.split(".", 1)[0]
        if not self.include_custom_fields and (
            self._is_custom_field_reference(normalized_source) or self._is_custom_field_reference(source_root)
        ):
            return False
        return not self._name_is_blocked(normalized_source) and not self._name_is_blocked(source_root)

    @staticmethod
    def _is_custom_field_reference(value: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        return normalized in {"custom_fields", "custom_field_data"} or normalized.startswith("cf_")

    def _name_is_blocked(self, value: str) -> bool:
        raw = value.casefold()
        normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        return raw in self.excluded_fields or normalized in self.excluded_fields or any(
            term in normalized for term in NEVER_ALLOWED_NAME_FRAGMENTS
        )

    def _sanitize_serialized_value(self, value: Any, *, depth: int = 0) -> Any:
        if depth >= 10:
            return None
        if isinstance(value, dict):
            return {
                key: self._sanitize_serialized_value(item, depth=depth + 1)
                for key, item in value.items()
                if not self._name_is_blocked(str(key))
            }
        if isinstance(value, list):
            return [self._sanitize_serialized_value(item, depth=depth + 1) for item in value]
        return value

    def _contains_blocked_nested_name(self, value: Any, *, depth: int = 0) -> bool:
        if depth >= 10:
            return True
        if isinstance(value, dict):
            return any(
                self._name_is_blocked(str(key))
                or self._contains_blocked_nested_name(item, depth=depth + 1)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(self._contains_blocked_nested_name(item, depth=depth + 1) for item in value)
        return False

    @staticmethod
    def _normalize_config_values(values: Iterable[Any]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(str(value).strip().casefold() for value in values if isinstance(value, str) and value.strip())
        )

    @staticmethod
    def _to_query_dict(filters: dict[str, Any]) -> QueryDict:
        query_data = QueryDict(mutable=True)
        for key, value in filters.items():
            values: Iterable[Any] = value if isinstance(value, list) else [value]
            if any(isinstance(item, (dict, list)) for item in values):
                raise ToolValidationError(f"Filter {key} contains an unsupported value.")
            normalized = [
                "" if item is None else str(item).lower() if isinstance(item, bool) else str(item) for item in values
            ]
            query_data.setlist(key, normalized)
        return query_data

    @staticmethod
    def _normalize_label(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ToolValidationError("object_type must be a non-empty string.")
        return value.strip().lower()

    @staticmethod
    def _reject_extra_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
        extras = set(arguments) - allowed
        if extras:
            raise ToolValidationError(f"Unsupported arguments: {', '.join(sorted(extras))}")

    @classmethod
    def _require_arguments(cls, arguments: dict[str, Any], required: set[str], allowed: set[str]) -> None:
        cls._reject_extra_arguments(arguments, allowed)
        missing = required - set(arguments)
        if missing:
            raise ToolValidationError(f"Missing arguments: {', '.join(sorted(missing))}")
