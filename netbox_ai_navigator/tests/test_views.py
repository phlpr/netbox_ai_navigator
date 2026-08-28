import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from netbox_ai_navigator.agent import AgentResult
from netbox_ai_navigator.config import READ_PERMISSION, WRITE_PERMISSION
from netbox_ai_navigator.session_state import store_pending_action
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


class FakeBatchActionRuntime(FakeRuntime):
    def run(self, context, messages, page_context):
        actions = tuple(
            {
                "type": "change_approval",
                "operation": "update",
                "method": "PATCH",
                "endpoint": f"/api/virtualization/virtual-machines/{object_id}/",
                "payload": {"status": "deleted"},
                "object_type": "virtualization.virtualmachine",
                "object_id": object_id,
                "title": f"Update SPSQLPROD00{object_id}",
                "target": f"SPSQLPROD00{object_id}",
                "changes": [{"field": "status", "before": "active", "after": "deleted"}],
                "etag": f'W/"timestamp-{object_id}"',
            }
            for object_id in (1, 2, 3)
        )
        return AgentResult(
            answer="Three validated changes are awaiting confirmation.",
            tool_calls=5,
            pending_actions=actions,
        )


class NavigationContextRuntime(FakeRuntime):
    def __init__(self):
        self.page_contexts = []

    def run(self, context, messages, page_context):
        self.page_contexts.append(page_context)
        if len(self.page_contexts) == 1:
            return AgentResult(
                answer="Found the contact.",
                tool_calls=1,
                navigation_targets=(
                    {
                        "object_type": "tenancy.contact",
                        "object_id": 7,
                        "label": "Fictional Lab Operations",
                    },
                ),
            )
        return AgentResult(answer="Opening the contact.", tool_calls=1)


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

    def test_passes_previous_verified_navigation_targets_to_next_request(self):
        runtime = NavigationContextRuntime()
        session = {}
        with patch("netbox_ai_navigator.views.build_agent_runtime", return_value=runtime):
            first_request = self.factory.post(
                "/plugins/ai-navigator/api/chat/",
                data=json.dumps({"messages": [{"role": "user", "content": "Find the contact"}]}),
                content_type="application/json",
            )
            first_request.user = FakeUser()
            first_request.session = session
            first_response = ChatView.as_view()(first_request)

            second_request = self.factory.post(
                "/plugins/ai-navigator/api/chat/",
                data=json.dumps({"messages": [{"role": "user", "content": "Navigate there"}]}),
                content_type="application/json",
            )
            second_request.user = FakeUser()
            second_request.session = session
            second_response = ChatView.as_view()(second_request)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(
            runtime.page_contexts[1]["previous_navigation_targets"],
            [
                {
                    "object_type": "tenancy.contact",
                    "object_id": 7,
                    "label": "Fictional Lab Operations",
                }
            ],
        )

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

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeBatchActionRuntime())
    def test_returns_separate_approval_cards_for_multi_object_change(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Update three VMs"}]}),
            content_type="application/json",
        )
        request.user = FakeUser(permissions=[WRITE_PERMISSION])
        request.session = {}

        response = ChatView.as_view()(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(payload["pending_actions"]), 3)
        self.assertEqual(
            [action["target"] for action in payload["pending_actions"]],
            ["SPSQLPROD001", "SPSQLPROD002", "SPSQLPROD003"],
        )
        self.assertEqual(len({action["action_id"] for action in payload["pending_actions"]}), 3)
        self.assertNotIn("/api/virtualization/virtual-machines/", response.content.decode())

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


class ResetConversationViewTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_manual_reset_clears_pending_actions(self):
        request = self.factory.post("/plugins/ai-navigator/api/chat/reset/", data="{}", content_type="application/json")
        request.user = FakeUser()
        request.session = {
            "netbox_ai_navigator_pending_actions": {"action": {}},
            "netbox_ai_navigator_navigation_targets": [
                {"object_type": "dcim.device", "object_id": 1, "label": "device-01"}
            ],
        }

        response = ResetConversationView.as_view()(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["can_write"])
        self.assertNotIn("netbox_ai_navigator_pending_actions", request.session)
        self.assertNotIn("netbox_ai_navigator_navigation_targets", request.session)


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
