import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from netbox_ai_navigator.agent import AgentResult
from netbox_ai_navigator.config import READ_PERMISSION, WRITE_PERMISSION
from netbox_ai_navigator.model_providers import MyGPTApiProvider
from netbox_ai_navigator.session_state import MYGPT_CONVERSATION_SESSION_KEY, store_pending_action
from netbox_ai_navigator.views import ChangeApprovalView, ChatView, ResetConversationView


class FakeUser:
    is_authenticated = True
    is_active = True

    def __init__(self, permissions=None):
        self.permissions = set(permissions if permissions is not None else [READ_PERMISSION])

    def has_perm(self, permission):
        return permission in self.permissions

    def get_username(self):
        return "test-user"


class FakeRuntime:
    def run(self, context, messages, page_context):
        return AgentResult(answer="See [Device](/dcim/devices/1/).", tool_calls=1)


class FakeMyGPTRuntime(FakeRuntime):
    def __init__(self):
        self.model_provider = MyGPTApiProvider(
            {
                "api_url": "https://api.myg.pt/api/v1/",
                "tenant": "test-tenant",
                "service_user": "service@example.test",
                "service_password": "server-secret",
                "channel_id": "channel-1",
                "delete_conversations": True,
            },
            conversation_id="conversation-1",
        )


class FakeActionRuntime(FakeRuntime):
    def run(self, context, messages, page_context):
        return AgentResult(
            answer="The validated change is awaiting confirmation.",
            tool_calls=2,
            client_actions=({"type": "navigate", "url": "/dcim/sites/", "label": "Sites"},),
            pending_actions=(
                {
                    "type": "change_approval",
                    "operation": "update",
                    "method": "PATCH",
                    "endpoint": "/api/dcim/sites/1/",
                    "payload": {"description": "private"},
                    "object_type": "dcim.site",
                    "object_id": 1,
                    "title": "Update Site A",
                    "target": "Site A",
                    "changes": [{"field": "description", "before": "", "after": "private"}],
                    "etag": 'W/"timestamp"',
                },
            ),
        )


@override_settings(
    PLUGINS_CONFIG={
        "netbox_ai_navigator": {
            "enabled": True,
            "model": {"api_key": "never-return-this", "model": "test-model"},
        }
    }
)
class ChatViewTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeRuntime())
    def test_returns_answer_without_configuration_secrets(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Show the device"}]}),
            content_type="application/json",
        )
        request.user = FakeUser()

        response = ChatView.as_view()(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["tool_calls"], 1)
        self.assertEqual(payload["client_actions"], [])
        self.assertEqual(payload["pending_actions"], [])
        self.assertFalse(payload["can_write"])
        self.assertNotIn("never-return-this", response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])

    def test_requires_authentication(self):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data="{}",
            content_type="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=False, is_active=False)

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 401)

    @patch("netbox_ai_navigator.views.build_agent_runtime")
    def test_denies_user_without_navigator_permission(self, build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Show the device"}]}),
            content_type="application/json",
        )
        request.user = FakeUser(permissions=[])

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        build_runtime.assert_not_called()

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeRuntime())
    def test_write_permission_implies_read_only_chat_access(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Show the device"}]}),
            content_type="application/json",
        )
        request.user = FakeUser(permissions=[WRITE_PERMISSION])

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 200)

    def test_rejects_non_json_request(self):
        request = self.factory.post("/plugins/ai-navigator/api/chat/", data="plain text", content_type="text/plain")
        request.user = FakeUser()

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 415)

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeMyGPTRuntime())
    def test_persists_mygpt_conversation_in_netbox_session(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Continue"}]}),
            content_type="application/json",
        )
        request.user = FakeUser()
        request.session = {}

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.session[MYGPT_CONVERSATION_SESSION_KEY], "conversation-1")

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeActionRuntime())
    def test_returns_public_actions_but_keeps_execution_payload_in_session(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Change Site A"}]}),
            content_type="application/json",
        )
        request.user = FakeUser(permissions=[WRITE_PERMISSION])
        request.session = {}

        response = ChatView.as_view()(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["can_write"])
        self.assertEqual(payload["client_actions"][0]["url"], "/dcim/sites/")
        self.assertNotIn("endpoint", payload["pending_actions"][0])
        self.assertNotIn("payload", payload["pending_actions"][0])
        self.assertNotIn("/api/dcim/sites/1/", response.content.decode())

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "navigator-rate"}},
        PLUGINS_CONFIG={
            "netbox_ai_navigator": {
                "enabled": True,
                "model": {"model": "test-model"},
                "agent": {"requests_per_minute": 2},
            }
        },
    )
    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeRuntime())
    def test_rate_limits_chat_requests_per_authenticated_user(self, build_runtime):
        cache.clear()
        responses = []
        for _index in range(3):
            request = self.factory.post(
                "/plugins/ai-navigator/api/chat/",
                data=json.dumps({"messages": [{"role": "user", "content": "Show the device"}]}),
                content_type="application/json",
            )
            request.user = FakeUser()
            responses.append(ChatView.as_view()(request))

        self.assertEqual([response.status_code for response in responses], [200, 200, 429])
        self.assertIn("Retry-After", responses[-1])
        self.assertEqual(build_runtime.call_count, 2)
        cache.clear()


@override_settings(
    PLUGINS_CONFIG={
        "netbox_ai_navigator": {
            "enabled": True,
            "model": {
                "provider": "mygpt_api",
                "api_url": "https://api.myg.pt/api/v1/",
                "tenant": "test-tenant",
                "service_user": "service@example.test",
                "service_password": "server-secret",
                "channel_id": "channel-1",
                "delete_conversations": True,
                "model": "mygpt-service-channel",
            },
        }
    }
)
class ResetConversationViewTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("netbox_ai_navigator.views.MyGPTApiProvider")
    def test_manual_reset_deletes_mygpt_and_clears_session(self, provider_class):
        request = self.factory.post("/plugins/ai-navigator/api/chat/reset/", data="{}", content_type="application/json")
        request.user = FakeUser()
        request.session = {MYGPT_CONVERSATION_SESSION_KEY: "conversation-1"}

        response = ResetConversationView.as_view()(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["can_write"])
        self.assertNotIn(MYGPT_CONVERSATION_SESSION_KEY, request.session)
        provider_class.assert_called_once()
        provider_class.return_value.delete_conversation.assert_called_once_with()


@override_settings(
    PLUGINS_CONFIG={
        "netbox_ai_navigator": {
            "enabled": True,
            "model": {"api_key": "unused", "model": "test-model"},
        }
    }
)
class ChangeApprovalViewTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_cancel_discards_server_stored_action(self):
        request = self.factory.post(
            "/plugins/ai-navigator/api/actions/approve/",
            data="{}",
            content_type="application/json",
        )
        request.user = FakeUser(permissions=[WRITE_PERMISSION])
        request.session = {}
        public = store_pending_action(request, {"operation": "delete"})
        session = request.session
        request = self.factory.post(
            "/plugins/ai-navigator/api/actions/approve/",
            data=json.dumps({"action_id": public["action_id"], "decision": "cancel"}),
            content_type="application/json",
        )
        request.user = FakeUser(permissions=[WRITE_PERMISSION])
        request.session = session

        response = ChangeApprovalView.as_view()(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["cancelled"])
