from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from netbox_ai_navigator.session_state import (
    BROWSER_STORAGE_TOKEN_SESSION_KEY,
    MYGPT_CONVERSATION_SESSION_KEY,
)
from netbox_ai_navigator.signals import cleanup_conversation_on_logout, reset_conversation_on_login


class AuthenticationLifecycleTest(SimpleTestCase):
    @patch("netbox_ai_navigator.signals._delete_session_conversation")
    def test_login_cleans_server_conversation_and_rotates_browser_storage(self, cleanup):
        request = SimpleNamespace(session={BROWSER_STORAGE_TOKEN_SESSION_KEY: "old-token"})

        reset_conversation_on_login(sender=None, request=request, user=SimpleNamespace())

        cleanup.assert_called_once_with(request)
        self.assertNotEqual(request.session[BROWSER_STORAGE_TOKEN_SESSION_KEY], "old-token")

    @patch("netbox_ai_navigator.signals._delete_session_conversation")
    def test_logout_cleans_server_conversation_and_discards_browser_token(self, cleanup):
        request = SimpleNamespace(
            session={
                BROWSER_STORAGE_TOKEN_SESSION_KEY: "active-token",
                MYGPT_CONVERSATION_SESSION_KEY: "conversation-1",
            }
        )

        cleanup_conversation_on_logout(sender=None, request=request, user=SimpleNamespace())

        cleanup.assert_called_once_with(request)
        self.assertNotIn(BROWSER_STORAGE_TOKEN_SESSION_KEY, request.session)
