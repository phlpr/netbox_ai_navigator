import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .config import get_plugin_settings
from .exceptions import ProviderError, ProviderTimeoutError
from .model_providers import MyGPTApiProvider
from .session_state import (
    BROWSER_STORAGE_TOKEN_SESSION_KEY,
    clear_mygpt_conversation_id,
    clear_pending_actions,
    get_mygpt_conversation_id,
    rotate_browser_storage_token,
)

logger = logging.getLogger("netbox.plugins.netbox_ai_navigator.signals")


def _delete_session_conversation(request) -> None:
    conversation_id = get_mygpt_conversation_id(request)
    if not conversation_id:
        return

    plugin_settings = get_plugin_settings()
    if plugin_settings["model"].get("provider") == "mygpt_api":
        try:
            provider = MyGPTApiProvider(plugin_settings["model"], conversation_id=conversation_id)
            provider.delete_conversation(suppress_errors=True)
        except (ProviderError, ProviderTimeoutError):
            logger.warning("Unable to initialize MyGPT cleanup during authentication")
    clear_mygpt_conversation_id(request)


@receiver(user_logged_out, dispatch_uid="netbox_ai_navigator_cleanup_on_logout")
def cleanup_conversation_on_logout(sender, request, user, **kwargs):
    if request is None:
        return
    _delete_session_conversation(request)
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(BROWSER_STORAGE_TOKEN_SESSION_KEY, None)
    clear_pending_actions(request)


@receiver(user_logged_in, dispatch_uid="netbox_ai_navigator_reset_on_login")
def reset_conversation_on_login(sender, request, user, **kwargs):
    if request is None:
        return
    _delete_session_conversation(request)
    clear_pending_actions(request)
    rotate_browser_storage_token(request)
