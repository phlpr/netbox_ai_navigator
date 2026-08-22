from copy import deepcopy
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PLUGIN_NAME = "netbox_ai_navigator"

DEFAULT_ALLOWED_OBJECT_TYPES = (
    "dcim.site",
    "dcim.location",
    "dcim.rack",
    "dcim.device",
    "dcim.interface",
    "ipam.vrf",
    "ipam.prefix",
    "ipam.ipaddress",
    "ipam.vlan",
    "circuits.provider",
    "circuits.circuit",
    "virtualization.cluster",
    "virtualization.virtualmachine",
)

DEFAULT_SETTINGS = {
    "enabled": True,
    "allowed_groups": [],
    "model": {
        "provider": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "api_url": "https://api.myg.pt/api/v1/",
        "api_key": None,
        "model": "qwen3",
        "tenant": None,
        "service_user": None,
        "service_password": None,
        "channel_id": None,
        "delete_conversations": True,
        "timeout": 60,
        "temperature": 0.1,
        "max_tokens": 1200,
        "max_response_chars": 20000,
    },
    "tools": {
        "provider": "local_current_user",
        "max_results": 50,
        "max_output_chars": 50000,
        "timeout": 30,
        "allowed_object_types": list(DEFAULT_ALLOWED_OBJECT_TYPES),
    },
    "agent": {
        "max_tool_calls": 5,
        "max_history_messages": 20,
        "max_message_chars": 12000,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def get_plugin_settings() -> dict[str, Any]:
    configured = settings.PLUGINS_CONFIG.get(PLUGIN_NAME, {})
    return _deep_merge(DEFAULT_SETTINGS, configured)


def validate_plugin_settings(configured: dict[str, Any]) -> None:
    config = _deep_merge(DEFAULT_SETTINGS, configured)
    if not isinstance(config["enabled"], bool):
        raise ImproperlyConfigured("netbox_ai_navigator.enabled must be a boolean.")
    if not isinstance(config["allowed_groups"], list) or not all(
        isinstance(group, str) and group for group in config["allowed_groups"]
    ):
        raise ImproperlyConfigured("netbox_ai_navigator.allowed_groups must be an array of group names.")

    model = config["model"]
    model_provider = model.get("provider")
    if model_provider not in {"openai_compatible", "mygpt_api"}:
        raise ImproperlyConfigured("netbox_ai_navigator.model.provider must be openai_compatible or mygpt_api.")
    if model_provider == "openai_compatible" and (
        not isinstance(model.get("base_url"), str) or not model["base_url"].strip()
    ):
        raise ImproperlyConfigured("netbox_ai_navigator.model.base_url must be configured.")
    if model_provider == "mygpt_api":
        for key in ("api_url", "tenant", "channel_id"):
            if not isinstance(model.get(key), str) or not model[key].strip():
                raise ImproperlyConfigured(f"netbox_ai_navigator.model.{key} must be configured.")
        for key in ("service_user", "service_password"):
            if model.get(key) is not None and (not isinstance(model[key], str) or not model[key]):
                raise ImproperlyConfigured(f"netbox_ai_navigator.model.{key} must be a non-empty string or null.")
        if model.get("delete_conversations") is not True:
            raise ImproperlyConfigured("netbox_ai_navigator.model.delete_conversations must enable session cleanup.")
    if model.get("api_key") is not None and not isinstance(model["api_key"], str):
        raise ImproperlyConfigured("netbox_ai_navigator.model.api_key must be a string or null.")
    if not isinstance(model.get("model"), str) or not model["model"].strip():
        raise ImproperlyConfigured("netbox_ai_navigator.model.model must be configured.")
    _require_positive_number(model, "timeout", "model.timeout")
    _require_positive_number(model, "max_tokens", "model.max_tokens", integer=True)
    _require_positive_number(model, "max_response_chars", "model.max_response_chars", integer=True)
    temperature = model.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2
    ):
        raise ImproperlyConfigured("netbox_ai_navigator.model.temperature must be between 0 and 2.")

    tools = config["tools"]
    if tools.get("provider") != "local_current_user":
        raise ImproperlyConfigured("Only the local_current_user tool provider is supported in version 0.1.")
    _require_positive_number(tools, "max_results", "tools.max_results", integer=True, maximum=50)
    _require_positive_number(tools, "max_output_chars", "tools.max_output_chars", integer=True)
    _require_positive_number(tools, "timeout", "tools.timeout")
    allowed_types = tools.get("allowed_object_types")
    if (
        not isinstance(allowed_types, list)
        or not allowed_types
        or not all(isinstance(value, str) for value in allowed_types)
    ):
        raise ImproperlyConfigured("netbox_ai_navigator.tools.allowed_object_types must be a non-empty array.")
    unsupported_types = set(allowed_types) - set(DEFAULT_ALLOWED_OBJECT_TYPES)
    if unsupported_types:
        raise ImproperlyConfigured(
            "Unsupported netbox_ai_navigator object types: " + ", ".join(sorted(unsupported_types))
        )

    agent = config["agent"]
    _require_positive_number(agent, "max_tool_calls", "agent.max_tool_calls", integer=True, maximum=5)
    _require_positive_number(agent, "max_history_messages", "agent.max_history_messages", integer=True)
    _require_positive_number(agent, "max_message_chars", "agent.max_message_chars", integer=True)


def _require_positive_number(
    config: dict[str, Any],
    key: str,
    path: str,
    *,
    integer: bool = False,
    maximum: int | None = None,
) -> None:
    value = config.get(key)
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected) or value <= 0 or (maximum and value > maximum):
        limit = f" and at most {maximum}" if maximum else ""
        kind = "integer" if integer else "number"
        raise ImproperlyConfigured(f"netbox_ai_navigator.{path} must be a positive {kind}{limit}.")


def user_can_use_assistant(user, plugin_settings: dict[str, Any] | None = None) -> bool:
    config = plugin_settings or get_plugin_settings()
    if not config.get("enabled", True) or not user.is_authenticated or not user.is_active:
        return False

    allowed_groups = config.get("allowed_groups") or []
    return not allowed_groups or user.groups.filter(name__in=allowed_groups).exists()
