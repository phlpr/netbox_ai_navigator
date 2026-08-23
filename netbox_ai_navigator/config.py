from copy import deepcopy
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PLUGIN_NAME = "netbox_ai_navigator"
READ_PERMISSION = f"{PLUGIN_NAME}.use_read_ainavigator"
WRITE_PERMISSION = f"{PLUGIN_NAME}.use_write_ainavigator"

DEFAULT_SETTINGS = {
    "enabled": True,
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
        "allowed_object_types": None,
        "excluded_object_types": [],
        "excluded_fields": [],
        "documentation": {
            "enabled": True,
            "max_results": 5,
            "max_section_chars": 12000,
            "additional_roots": [],
        },
        "write": {
            "enabled": True,
            "approval_ttl": 600,
            "max_pending": 5,
        },
    },
    "agent": {
        "max_tool_calls": 10,
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
    _require_optional_string_list(tools, "allowed_object_types", allow_none=True)
    _require_optional_string_list(tools, "excluded_object_types")
    _require_optional_string_list(tools, "excluded_fields")
    documentation = tools.get("documentation")
    if not isinstance(documentation, dict) or not isinstance(documentation.get("enabled"), bool):
        raise ImproperlyConfigured("netbox_ai_navigator.tools.documentation.enabled must be a boolean.")
    _require_positive_number(documentation, "max_results", "tools.documentation.max_results", integer=True, maximum=10)
    _require_positive_number(
        documentation,
        "max_section_chars",
        "tools.documentation.max_section_chars",
        integer=True,
        maximum=30000,
    )
    _require_optional_string_list(
        documentation,
        "additional_roots",
        path="tools.documentation.additional_roots",
    )
    write = tools.get("write")
    if not isinstance(write, dict) or not isinstance(write.get("enabled"), bool):
        raise ImproperlyConfigured("netbox_ai_navigator.tools.write.enabled must be a boolean.")
    _require_positive_number(write, "approval_ttl", "tools.write.approval_ttl", integer=True, maximum=3600)
    _require_positive_number(write, "max_pending", "tools.write.max_pending", integer=True, maximum=10)

    agent = config["agent"]
    _require_positive_number(agent, "max_tool_calls", "agent.max_tool_calls", integer=True, maximum=10)
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


def _require_optional_string_list(
    config: dict[str, Any],
    key: str,
    *,
    allow_none: bool = False,
    path: str | None = None,
) -> None:
    value = config.get(key)
    if allow_none and value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        suffix = " or null" if allow_none else ""
        setting_path = path or f"tools.{key}"
        raise ImproperlyConfigured(f"netbox_ai_navigator.{setting_path} must be an array of non-empty strings{suffix}.")


def _user_is_eligible(user, plugin_settings: dict[str, Any] | None = None) -> bool:
    config = plugin_settings or get_plugin_settings()
    return bool(config.get("enabled", True) and user.is_authenticated and user.is_active)


def user_can_write_assistant(user, plugin_settings: dict[str, Any] | None = None) -> bool:
    config = plugin_settings or get_plugin_settings()
    write_enabled = config.get("tools", {}).get("write", {}).get("enabled", True)
    return bool(write_enabled and _user_is_eligible(user, config) and user.has_perm(WRITE_PERMISSION))


def user_can_read_assistant(user, plugin_settings: dict[str, Any] | None = None) -> bool:
    if not _user_is_eligible(user, plugin_settings):
        return False
    return user.has_perm(READ_PERMISSION) or user.has_perm(WRITE_PERMISSION)


def user_can_use_assistant(user, plugin_settings: dict[str, Any] | None = None) -> bool:
    """Compatibility alias for the current read-only assistant access check."""
    return user_can_read_assistant(user, plugin_settings)
