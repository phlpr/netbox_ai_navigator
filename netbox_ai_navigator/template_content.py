from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils.translation import gettext as _
from netbox.plugins import PluginTemplateExtension

from .config import get_plugin_settings, user_can_use_assistant
from .session_state import get_or_create_browser_storage_token
from .tool_providers.local_current_user import SAFE_OUTPUT_FIELDS


def get_ui_translations() -> dict[str, str]:
    return {
        "open_assistant": _("Open NetBox AI Navigator"),
        "dialog_label": _("NetBox AI Navigator"),
        "subtitle": _("Read-only · your permissions"),
        "expand_assistant": _("Expand assistant"),
        "restore_assistant": _("Restore assistant size"),
        "clear_conversation": _("Clear conversation"),
        "close_assistant": _("Close assistant"),
        "welcome": _("Ask me about the NetBox data you are permitted to view on this page."),
        "question": _("Question"),
        "placeholder": _("Ask about your NetBox…"),
        "send_question": _("Send question"),
        "send": _("Send"),
        "notice": _("Answers may be inaccurate. Verify important data in NetBox."),
        "thinking": _("Thinking…"),
        "request_failed": _("Request failed with HTTP {status}."),
        "assistant_unavailable": _("The assistant is currently unavailable."),
        "reset_failed": _("Reset failed with HTTP {status}."),
        "conversation_cleared": _("Conversation cleared. What would you like to explore?"),
        "conversation_clear_failed": _("The conversation could not be cleared."),
    }


class GlobalAssistantExtension(PluginTemplateExtension):
    def head(self):
        request = self.context["request"]
        plugin_settings = get_plugin_settings()
        if not user_can_use_assistant(request.user, plugin_settings):
            return ""

        allowed_types = set(plugin_settings["tools"].get("allowed_object_types") or []).intersection(SAFE_OUTPUT_FIELDS)
        current = self.context.get("object")
        model = current.__class__ if current is not None else self.context.get("model")
        object_type = model._meta.label_lower if model is not None and hasattr(model, "_meta") else None
        if object_type not in allowed_types:
            object_type = None

        bootstrap = {
            "endpoint": reverse("plugins:netbox_ai_navigator:chat"),
            "reset_endpoint": reverse("plugins:netbox_ai_navigator:reset_conversation"),
            "csrf_token": get_token(request),
            "storage_token": get_or_create_browser_storage_token(request),
            "translations": get_ui_translations(),
            "page_context": {
                "object_type": object_type,
                "object_id": current.pk if current is not None and object_type else None,
                "url": request.get_full_path(),
            },
        }
        return self.render(
            "netbox_ai_navigator/head.html",
            extra_context={"assistant_bootstrap": bootstrap},
        )


template_extensions = (GlobalAssistantExtension,)
