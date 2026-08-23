import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils.translation import gettext_lazy

from netbox_ai_navigator.session_state import (
    BROWSER_STORAGE_TOKEN_SESSION_KEY,
    MYGPT_CONVERSATION_SESSION_KEY,
    PENDING_ACTIONS_SESSION_KEY,
    discard_pending_action,
    pop_pending_action,
    store_pending_action,
)
from netbox_ai_navigator.signals import cleanup_conversation_on_logout, reset_conversation_on_login


class AuthenticationLifecycleTest(SimpleTestCase):
    @patch("netbox_ai_navigator.signals._delete_session_conversation")
    def test_login_cleans_server_conversation_and_rotates_browser_storage(self, cleanup):
        request = SimpleNamespace(session={BROWSER_STORAGE_TOKEN_SESSION_KEY: "old-token"})
        request.session[PENDING_ACTIONS_SESSION_KEY] = {"pending": {}}

        reset_conversation_on_login(sender=None, request=request, user=SimpleNamespace())

        cleanup.assert_called_once_with(request)
        self.assertNotEqual(request.session[BROWSER_STORAGE_TOKEN_SESSION_KEY], "old-token")
        self.assertNotIn(PENDING_ACTIONS_SESSION_KEY, request.session)

    @patch("netbox_ai_navigator.signals._delete_session_conversation")
    def test_logout_cleans_server_conversation_and_discards_browser_token(self, cleanup):
        request = SimpleNamespace(
            session={
                BROWSER_STORAGE_TOKEN_SESSION_KEY: "active-token",
                MYGPT_CONVERSATION_SESSION_KEY: "conversation-1",
                PENDING_ACTIONS_SESSION_KEY: {"pending": {}},
            }
        )

        cleanup_conversation_on_logout(sender=None, request=request, user=SimpleNamespace())

        cleanup.assert_called_once_with(request)
        self.assertNotIn(BROWSER_STORAGE_TOKEN_SESSION_KEY, request.session)
        self.assertNotIn(PENDING_ACTIONS_SESSION_KEY, request.session)


class PendingActionSessionTest(SimpleTestCase):
    def test_pending_action_is_private_and_single_use(self):
        request = SimpleNamespace(session={})
        action = {
            "operation": "update",
            "endpoint": "/api/dcim/sites/1/",
            "payload": {"description": "private payload"},
            "title": "Update site",
            "target": "Site A",
            "changes": [{"field": "description", "before": "", "after": "new"}],
        }

        public = store_pending_action(request, action)
        restored = pop_pending_action(request, public["action_id"])

        self.assertNotIn("endpoint", public)
        self.assertNotIn("payload", public)
        self.assertEqual(restored, action)
        self.assertIsNone(pop_pending_action(request, public["action_id"]))

    def test_pending_action_can_be_cancelled(self):
        request = SimpleNamespace(session={})
        public = store_pending_action(request, {"operation": "delete"})

        self.assertTrue(discard_pending_action(request, public["action_id"]))
        self.assertFalse(discard_pending_action(request, public["action_id"]))

    def test_pending_action_converts_lazy_preview_values_for_json_sessions(self):
        request = SimpleNamespace(session={})
        public = store_pending_action(
            request,
            {
                "operation": "update",
                "changes": [
                    {
                        "field": "status",
                        "before": {"value": "active", "label": gettext_lazy("Active")},
                        "after": "planned",
                    }
                ],
            },
        )

        json.dumps(request.session)
        self.assertEqual(public["changes"][0]["before"]["label"], "Active")
