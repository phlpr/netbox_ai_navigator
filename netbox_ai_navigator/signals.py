from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .session_state import (
    BROWSER_STORAGE_TOKEN_SESSION_KEY,
    clear_pending_actions,
    rotate_browser_storage_token,
)


@receiver(user_logged_out, dispatch_uid="netbox_ai_navigator_cleanup_on_logout")
def cleanup_session_on_logout(sender, request, user, **kwargs):
    if request is None:
        return
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(BROWSER_STORAGE_TOKEN_SESSION_KEY, None)
    clear_pending_actions(request)


@receiver(user_logged_in, dispatch_uid="netbox_ai_navigator_reset_on_login")
def reset_session_on_login(sender, request, user, **kwargs):
    if request is None:
        return
    clear_pending_actions(request)
    rotate_browser_storage_token(request)
