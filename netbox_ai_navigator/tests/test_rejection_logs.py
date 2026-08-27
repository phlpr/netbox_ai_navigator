import json
from unittest.mock import patch

from core.models import ObjectType
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from users.models import ObjectPermission

from netbox_ai_navigator.agent import AgentResult, RejectedResponse
from netbox_ai_navigator.exceptions import UngroundedResponseError
from netbox_ai_navigator.models import RejectedResponseLog
from netbox_ai_navigator.rejection_logging import record_rejected_response
from netbox_ai_navigator.rejections import RejectionReason
from netbox_ai_navigator.views import ChatView


class FakeRejectedRuntime:
    def run(self, context, messages, page_context):
        return AgentResult(
            answer="AI Navigator is limited to NetBox data.",
            tool_calls=0,
            rejection=RejectedResponse(
                reason=RejectionReason.SCOPE_GUARD,
                response="Use list.sort() to sort the Python list.",
            ),
        )


class FakeUngroundedRuntime:
    def run(self, context, messages, page_context):
        raise UngroundedResponseError(
            "The model response could not be verified.",
            rejected_response="Device01 has the invented address 192.0.2.10.",
        )


@override_settings(
    PLUGINS_CONFIG={
        "netbox_ai_navigator": {
            "enabled": True,
            "model": {
                "provider": "openai_compatible",
                "model": "test-model",
            },
            "rejected_response_logs": {"enabled": True, "max_entries": 1000},
        }
    }
)
class RejectedResponseLogTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = get_user_model().objects.create_superuser(
            username="log-admin",
            email="log-admin@example.test",
            password="test-password",
        )
        cls.regular_user = get_user_model().objects.create_user(username="regular-user")

    def setUp(self):
        self.factory = RequestFactory()

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeRejectedRuntime())
    def test_chat_persists_rejected_response_with_request_and_user(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "First request"},
                        {"role": "assistant", "content": "Earlier response"},
                        {"role": "user", "content": "How do I sort a Python list?"},
                    ]
                }
            ),
            content_type="application/json",
        )
        request.user = self.superuser

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        entry = RejectedResponseLog.objects.get()
        self.assertEqual(entry.user, self.superuser)
        self.assertEqual(entry.username, "log-admin")
        self.assertEqual(entry.user_request, "How do I sort a Python list?")
        self.assertEqual(entry.rejected_response, "Use list.sort() to sort the Python list.")
        self.assertEqual(entry.delivered_response, "AI Navigator is limited to NetBox data.")
        self.assertEqual(entry.reason, RejectionReason.SCOPE_GUARD)
        self.assertEqual(entry.provider, "openai_compatible")
        self.assertEqual(entry.model_name, "test-model")

    @patch("netbox_ai_navigator.views.build_agent_runtime", return_value=FakeUngroundedRuntime())
    def test_chat_persists_rejected_response_when_grounding_fails_closed(self, _build_runtime):
        request = self.factory.post(
            "/plugins/ai-navigator/api/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "Show Device01"}]}),
            content_type="application/json",
        )
        request.user = self.superuser

        response = ChatView.as_view()(request)

        self.assertEqual(response.status_code, 502)
        entry = RejectedResponseLog.objects.get()
        self.assertEqual(entry.user_request, "Show Device01")
        self.assertEqual(entry.rejected_response, "Device01 has the invented address 192.0.2.10.")
        self.assertEqual(entry.delivered_response, "The model response could not be verified.")
        self.assertEqual(entry.reason, RejectionReason.GROUNDING_GUARD)

    def test_retention_keeps_only_configured_number_of_entries(self):
        settings = {
            "model": {"provider": "openai_compatible", "model": "test-model", "max_response_chars": 20_000},
            "rejected_response_logs": {"enabled": True, "max_entries": 2},
        }
        for index in range(3):
            record_rejected_response(
                user=self.superuser,
                user_request=f"Request {index}",
                rejection=RejectedResponse(reason=RejectionReason.SCOPE_GUARD, response=f"Rejected {index}"),
                delivered_response=f"Delivered {index}",
                plugin_settings=settings,
            )

        self.assertEqual(RejectedResponseLog.objects.count(), 2)
        self.assertEqual(
            set(RejectedResponseLog.objects.values_list("user_request", flat=True)),
            {"Request 1", "Request 2"},
        )

    def test_logging_can_be_disabled(self):
        result = record_rejected_response(
            user=self.superuser,
            user_request="Request",
            rejection=RejectedResponse(reason=RejectionReason.SCOPE_GUARD, response="Rejected"),
            delivered_response="Delivered",
            plugin_settings={
                "model": {"max_response_chars": 20_000},
                "rejected_response_logs": {"enabled": False, "max_entries": 1000},
            },
        )

        self.assertIsNone(result)
        self.assertFalse(RejectedResponseLog.objects.exists())

    def test_log_views_require_the_dedicated_view_permission(self):
        entry = RejectedResponseLog.objects.create(
            user=self.superuser,
            username=self.superuser.username,
            user_request="Request",
            rejected_response="Rejected",
            delivered_response="Delivered",
            reason=RejectionReason.SCOPE_GUARD,
        )
        list_url = reverse("plugins:netbox_ai_navigator:rejectedresponselog_list")

        self.client.force_login(self.regular_user)
        self.assertEqual(self.client.get(list_url).status_code, 403)

        permission = ObjectPermission.objects.create(name="View rejected AI responses", actions=["view"])
        permission.users.add(self.regular_user)
        permission.object_types.add(ObjectType.objects.get_for_model(RejectedResponseLog))
        self.regular_user = get_user_model().objects.get(pk=self.regular_user.pk)
        self.client.force_login(self.regular_user)

        self.assertEqual(self.client.get(list_url).status_code, 200)
        detail_response = self.client.get(entry.get_absolute_url())
        self.assertEqual(detail_response.status_code, 200)

    def test_rejected_content_is_escaped_in_detail_view(self):
        entry = RejectedResponseLog.objects.create(
            user=self.superuser,
            username=self.superuser.username,
            user_request='<script id="request-xss">alert(1)</script>',
            rejected_response='<img src=x onerror="alert(2)">',
            delivered_response="Safe response",
            reason=RejectionReason.SCOPE_GUARD,
        )
        self.client.force_login(self.superuser)

        response = self.client.get(entry.get_absolute_url())
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script id="request-xss">', content)
        self.assertNotIn('<img src=x onerror="alert(2)">', content)
        self.assertIn("&lt;script", content)
