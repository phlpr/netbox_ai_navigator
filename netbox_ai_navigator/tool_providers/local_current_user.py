from collections.abc import Iterable
from typing import Any

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.http import QueryDict
from netbox.registry import registry
from utilities.api import get_serializer_for_model

from netbox_ai_navigator.exceptions import ToolError, ToolNotFoundError, ToolValidationError

from .base import ToolDefinition, ToolProvider
from .context import ToolContext

IDENTITY_FIELDS = ("id", "display", "display_url")

# Fields outside this code-level allowlist can never be returned to a model, even if
# an administrator accidentally adds a sensitive model to PLUGINS_CONFIG.
SAFE_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "dcim.site": (
        *IDENTITY_FIELDS,
        "name",
        "slug",
        "status",
        "region",
        "group",
        "tenant",
        "facility",
        "physical_address",
        "description",
    ),
    "dcim.location": (
        *IDENTITY_FIELDS,
        "name",
        "slug",
        "site",
        "parent",
        "status",
        "tenant",
        "facility",
        "description",
    ),
    "dcim.rack": (
        *IDENTITY_FIELDS,
        "name",
        "facility_id",
        "site",
        "location",
        "tenant",
        "status",
        "role",
        "type",
        "width",
        "u_height",
        "description",
    ),
    "dcim.device": (
        *IDENTITY_FIELDS,
        "name",
        "device_type",
        "role",
        "tenant",
        "platform",
        "serial",
        "asset_tag",
        "site",
        "location",
        "rack",
        "position",
        "face",
        "status",
        "primary_ip",
        "primary_ip4",
        "primary_ip6",
        "cluster",
        "virtual_chassis",
        "vc_position",
        "description",
    ),
    "dcim.interface": (
        *IDENTITY_FIELDS,
        "device",
        "vdcs",
        "module",
        "name",
        "label",
        "type",
        "enabled",
        "parent",
        "bridge",
        "lag",
        "mtu",
        "mac_address",
        "speed",
        "duplex",
        "mode",
        "rf_role",
        "description",
        "cable",
    ),
    "ipam.vrf": (
        *IDENTITY_FIELDS,
        "name",
        "rd",
        "tenant",
        "enforce_unique",
        "description",
    ),
    "ipam.prefix": (
        *IDENTITY_FIELDS,
        "family",
        "prefix",
        "vrf",
        "scope_type",
        "scope_id",
        "scope",
        "tenant",
        "vlan",
        "status",
        "role",
        "is_pool",
        "mark_utilized",
        "description",
    ),
    "ipam.ipaddress": (
        *IDENTITY_FIELDS,
        "family",
        "address",
        "vrf",
        "tenant",
        "status",
        "role",
        "assigned_object_type",
        "assigned_object_id",
        "assigned_object",
        "nat_inside",
        "dns_name",
        "description",
    ),
    "ipam.vlan": (
        *IDENTITY_FIELDS,
        "site",
        "group",
        "vid",
        "name",
        "tenant",
        "status",
        "role",
        "description",
    ),
    "circuits.provider": (
        *IDENTITY_FIELDS,
        "name",
        "slug",
        "asn",
        "account",
        "portal_url",
        "noc_contact",
        "admin_contact",
        "description",
    ),
    "circuits.circuit": (
        *IDENTITY_FIELDS,
        "cid",
        "provider",
        "provider_account",
        "type",
        "status",
        "tenant",
        "install_date",
        "termination_date",
        "commit_rate",
        "description",
    ),
    "virtualization.cluster": (
        *IDENTITY_FIELDS,
        "name",
        "type",
        "group",
        "status",
        "tenant",
        "scope_type",
        "scope_id",
        "scope",
        "description",
    ),
    "virtualization.virtualmachine": (
        *IDENTITY_FIELDS,
        "name",
        "status",
        "site",
        "cluster",
        "device",
        "role",
        "tenant",
        "platform",
        "primary_ip",
        "primary_ip4",
        "primary_ip6",
        "vcpus",
        "memory",
        "disk",
        "description",
    ),
}


class LocalCurrentUserProvider(ToolProvider):
    """Read-only NetBox tools executed under the current user's object permissions."""

    def __init__(self, config: dict[str, Any]):
        configured_types = config.get("allowed_object_types") or []
        self.allowed_object_types = tuple(
            label
            for label in dict.fromkeys(str(value).lower() for value in configured_types)
            if label in SAFE_OUTPUT_FIELDS
        )
        self.max_results = max(1, min(int(config.get("max_results", 50)), 50))
        self.timeout = max(0.1, float(config.get("timeout", 30)))

    def list_tools(self, context: ToolContext) -> list[ToolDefinition]:
        object_type_schema = {"type": "string", "enum": list(self.allowed_object_types)}
        return [
            ToolDefinition(
                name="list_object_types",
                description="List the NetBox object types available to the read-only assistant.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolDefinition(
                name="describe_object_type",
                description="Describe the safe output fields and registered NetBox filters for one object type.",
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
                    "Query read-only NetBox objects. Filters use the registered NetBox FilterSet semantics. "
                    "Use the q filter for free-text searches on the queried object. To find objects by city, site, "
                    "or location, first query dcim.site or dcim.location with q, then filter the target object type "
                    "by the returned site_id or location_id. A target object's q filter does not necessarily search "
                    "related objects. Relationship filters such as site and location accept registered choices, not "
                    "unverified free text. Call describe_object_type first when other filter or field names are uncertain."
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
        ]

    def call_tool(self, context: ToolContext, name: str, arguments: dict[str, Any]) -> Any:
        handlers = {
            "list_object_types": self._list_object_types,
            "describe_object_type": self._describe_object_type,
            "query_objects": self._query_objects,
            "get_object": self._get_object,
        }
        try:
            handler = handlers[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Unknown tool: {name}") from exc
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        if name not in {"query_objects", "get_object"}:
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
        self._reject_extra_arguments(arguments, set())
        object_types = []
        for label in self.allowed_object_types:
            model, _, _ = self._resolve(label, context)
            object_types.append(
                {
                    "object_type": label,
                    "name": str(model._meta.verbose_name),
                    "name_plural": str(model._meta.verbose_name_plural),
                }
            )
        return {"object_types": object_types}

    def _describe_object_type(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_arguments(arguments, {"object_type"}, {"object_type"})
        label = self._normalize_label(arguments["object_type"])
        model, filterset_class, serializer_class = self._resolve(label, context)
        output_fields = self._supported_output_fields(label, serializer_class, context)
        filters = [
            {
                "name": name,
                "label": str(filter_.label or name),
                "type": filter_.__class__.__name__,
            }
            for name, filter_ in filterset_class.base_filters.items()
        ]
        return {
            "object_type": label,
            "name": str(model._meta.verbose_name),
            "name_plural": str(model._meta.verbose_name_plural),
            "output_fields": output_fields,
            "filters": filters,
            "max_results": self.max_results,
        }

    def _query_objects(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"object_type", "filters", "fields", "order_by", "limit"}
        self._require_arguments(arguments, {"object_type"}, allowed)
        label = self._normalize_label(arguments["object_type"])
        model, filterset_class, serializer_class = self._resolve(label, context)
        fields = self._select_fields(label, serializer_class, context, arguments.get("fields"))
        queryset = self._restricted_queryset(model, context)

        filters = arguments.get("filters") or {}
        if not isinstance(filters, dict):
            raise ToolValidationError("filters must be a JSON object.")
        unknown_filters = set(filters) - set(filterset_class.base_filters)
        if unknown_filters:
            raise ToolValidationError(f"Unsupported filters: {', '.join(sorted(unknown_filters))}")

        query_data = self._to_query_dict(filters)
        filterset = filterset_class(data=query_data, queryset=queryset, request=context.request)
        if not filterset.is_valid():
            raise ToolValidationError(f"Invalid filters: {filterset.errors.get_json_data(escape_html=True)}")
        try:
            queryset = filterset.qs
        except ValidationError as exc:
            raise ToolValidationError(f"Invalid filters: {exc}") from exc

        ordering = arguments.get("order_by") or []
        if ordering:
            queryset = queryset.order_by(*self._validate_ordering(model, label, ordering))

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
            "objects": list(serialized),
        }

    def _get_object(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"object_type", "object_id", "fields"}
        self._require_arguments(arguments, {"object_type", "object_id"}, allowed)
        label = self._normalize_label(arguments["object_type"])
        model, _, serializer_class = self._resolve(label, context)
        object_id = arguments["object_id"]
        if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id < 1:
            raise ToolValidationError("object_id must be a positive integer.")
        fields = self._select_fields(label, serializer_class, context, arguments.get("fields"))

        # Restriction precedes lookup so an unauthorized ID is indistinguishable from a missing ID.
        instance = self._restricted_queryset(model, context).filter(pk=object_id).first()
        if instance is None:
            return {"object_type": label, "found": False, "object": None}

        serialized = serializer_class(instance, fields=fields, context={"request": context.request}).data
        return {"object_type": label, "found": True, "object": dict(serialized)}

    def _resolve(self, label: str, context: ToolContext):
        if label not in self.allowed_object_types:
            raise ToolValidationError(f"Object type is not allowed: {label}")
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError) as exc:
            raise ToolValidationError(f"Unknown object type: {label}") from exc
        filterset_class = registry["filtersets"].get(label)
        if filterset_class is None:
            raise ToolValidationError(f"No registered FilterSet is available for {label}.")
        try:
            serializer_class = get_serializer_for_model(model)
        except Exception as exc:
            raise ToolValidationError(f"No serializer is available for {label}.") from exc
        return model, filterset_class, serializer_class

    @staticmethod
    def _restricted_queryset(model, context: ToolContext):
        manager = model.objects
        if not hasattr(manager, "restrict"):
            raise ToolValidationError("This object type does not support NetBox object permissions.")
        return manager.restrict(context.user, "view")

    def _supported_output_fields(self, label, serializer_class, context: ToolContext) -> list[str]:
        serializer = serializer_class(context={"request": context.request})
        serializer_fields = set(serializer.fields)
        return [field for field in SAFE_OUTPUT_FIELDS[label] if field in serializer_fields]

    def _select_fields(self, label, serializer_class, context: ToolContext, requested) -> list[str]:
        supported = self._supported_output_fields(label, serializer_class, context)
        if requested is None:
            return supported
        if not isinstance(requested, list) or not all(isinstance(field, str) for field in requested):
            raise ToolValidationError("fields must be an array of field names.")
        unsupported = set(requested) - set(supported)
        if unsupported:
            raise ToolValidationError(f"Unsupported output fields: {', '.join(sorted(unsupported))}")
        return list(dict.fromkeys([*(field for field in IDENTITY_FIELDS if field in supported), *requested]))

    @staticmethod
    def _validate_ordering(model, label: str, ordering) -> list[str]:
        if not isinstance(ordering, list) or not all(isinstance(field, str) for field in ordering):
            raise ToolValidationError("order_by must be an array of field names.")
        concrete_fields = {field.name for field in model._meta.concrete_fields}
        allowed = concrete_fields.intersection(SAFE_OUTPUT_FIELDS[label]) | {"id"}
        invalid = [field for field in ordering if not field or field.removeprefix("-") not in allowed]
        if invalid:
            raise ToolValidationError(f"Unsupported ordering fields: {', '.join(invalid)}")
        return ordering

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
