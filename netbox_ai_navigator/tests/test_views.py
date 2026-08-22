import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from netbox_ai_navigator.agent import AgentResult
from netbox_ai_navigator.model_providers import MyGPTApiProvider
from netbox_ai_navigator.session_state import MYGPT_CONVERSATION_SESSION_KEY
from netbox_ai_navigator.views import ChatView, ResetConversationView


class FakeUser:
    is_authenticated = True
    is_active = True

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


@override_settings(
    PLUGINS_CONFIG={
        "netbox_ai_navigator": {
            "enabled": True,
            "allowed_groups": [],
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


@override_settings(
    PLUGINS_CONFIG={
        "netbox_ai_navigator": {
            "enabled": True,
            "allowed_groups": [],
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

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(MYGPT_CONVERSATION_SESSION_KEY, request.session)
        provider_class.assert_called_once()
        provider_class.return_value.delete_conversation.assert_called_once_with()
