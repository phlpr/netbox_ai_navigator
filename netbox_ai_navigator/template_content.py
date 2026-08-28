from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils.translation import gettext as _
from netbox.plugins import PluginTemplateExtension

from .config import get_plugin_settings, user_can_read_assistant, user_can_write_assistant
from .session_state import get_or_create_browser_storage_token
from .tool_providers.local_current_user import LocalCurrentUserProvider


def get_ui_translations() -> dict[str, str]:
    return {
        "open_assistant": _("Open NetBox AI Navigator"),
        "dialog_label": _("NetBox AI Navigator"),
        "subtitle": _("Read-only · your permissions"),
        "subtitle_write": _("Read and write · your permissions"),
        "expand_assistant": _("Expand assistant"),
        "restore_assistant": _("Restore assistant size"),
        "clear_conversation": _("Clear conversation"),
        "minimize_assistant": _("Minimize assistant"),
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
        "open_navigation": _("Open {label}"),
        "change_requires_approval": _("This change requires your confirmation."),
        "field": _("Field"),
        "before": _("Before"),
        "after": _("After"),
        "confirm_change": _("Confirm change"),
        "cancel_change": _("Cancel"),
        "change_cancelled": _("The proposed change was cancelled."),
        "change_completed": _("The approved change was completed successfully."),
        "change_failed": _("The approved change could not be completed."),
        "approval_failed": _("Approval failed with HTTP {status}."),
        "copy": _("Copy"),
        "copied": _("Copied"),
        "copy_failed": _("Copy failed"),
    }


class GlobalAssistantExtension(PluginTemplateExtension):
    def head(self):
        request = self.context["request"]
        plugin_settings = get_plugin_settings()
        if not user_can_read_assistant(request.user, plugin_settings):
            return ""

        current = self.context.get("object")
        model = current.__class__ if current is not None else self.context.get("model")
        object_type = model._meta.label_lower if model is not None and hasattr(model, "_meta") else None
        tool_provider = LocalCurrentUserProvider(plugin_settings["tools"])
        if not tool_provider.supports_object_type(object_type):
            object_type = None

        bootstrap = {
            "endpoint": reverse("plugins:netbox_ai_navigator:chat"),
            "reset_endpoint": reverse("plugins:netbox_ai_navigator:reset_conversation"),
            "approval_endpoint": reverse("plugins:netbox_ai_navigator:approve_action"),
            "csrf_token": get_token(request),
            "storage_token": get_or_create_browser_storage_token(request),
            "can_write": user_can_write_assistant(request.user, plugin_settings),
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
