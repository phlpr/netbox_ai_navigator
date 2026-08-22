from core.models import ObjectType
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from users.models import ObjectPermission

from netbox_ai_navigator.config import user_can_read_assistant, user_can_write_assistant
from netbox_ai_navigator.models import AINavigator
from netbox_ai_navigator.template_content import GlobalAssistantExtension
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext


class NavigatorCapabilityPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.read_user = get_user_model().objects.create_user(username="navigator-reader")
        cls.write_user = get_user_model().objects.create_user(username="navigator-writer")
        cls.denied_user = get_user_model().objects.create_user(username="navigator-denied")
        object_type = ObjectType.objects.get_for_model(AINavigator)

        read_permission = ObjectPermission.objects.create(name="Use AI Navigator read-only", actions=["use_read"])
        read_permission.users.add(cls.read_user)
        read_permission.object_types.add(object_type)

        write_permission = ObjectPermission.objects.create(name="Use AI Navigator with writes", actions=["use_write"])
        write_permission.users.add(cls.write_user)
        write_permission.object_types.add(object_type)

    def test_read_permission_allows_read_but_not_write(self):
        self.assertTrue(user_can_read_assistant(self.read_user))
        self.assertFalse(user_can_write_assistant(self.read_user))

    def test_write_permission_implies_read_access(self):
        self.assertTrue(user_can_read_assistant(self.write_user))
        self.assertTrue(user_can_write_assistant(self.write_user))

    def test_user_without_capability_has_no_access(self):
        self.assertFalse(user_can_read_assistant(self.denied_user))
        self.assertFalse(user_can_write_assistant(self.denied_user))

    def test_user_without_capability_does_not_receive_ui(self):
        request = RequestFactory().get("/")
        request.user = self.denied_user

        self.assertEqual(GlobalAssistantExtension({"request": request}).head(), "")

    def test_global_kill_switch_overrides_permissions(self):
        disabled = {"enabled": False}

        self.assertFalse(user_can_read_assistant(self.read_user, disabled))
        self.assertFalse(user_can_write_assistant(self.write_user, disabled))


class CurrentUserRBACIntegrationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.visible_site = Site.objects.create(name="Visible site", slug="visible-site", status="active")
        cls.hidden_site = Site.objects.create(name="Hidden site", slug="hidden-site", status="active")
        cls.limited_user = get_user_model().objects.create_user(username="limited-user")
        cls.other_user = get_user_model().objects.create_user(username="other-user")

        permission = ObjectPermission.objects.create(
            name="View one site",
            actions=["view"],
            constraints={"name": cls.visible_site.name},
        )
        permission.users.add(cls.limited_user)
        permission.object_types.add(ObjectType.objects.get_for_model(Site))

    def setUp(self):
        self.provider = LocalCurrentUserProvider({"allowed_object_types": ["dcim.site"], "max_results": 50})
        self.request = RequestFactory().get("/")

    def context_for(self, user):
        self.request.user = user
        return ToolContext(request=self.request, user=user)

    def test_query_returns_only_objects_visible_to_current_user(self):
        result = self.provider.call_tool(
            self.context_for(self.limited_user),
            "query_objects",
            {"object_type": "dcim.site", "fields": ["name"]},
        )

        self.assertEqual([item["name"] for item in result["objects"]], [self.visible_site.name])

    def test_hidden_object_id_looks_missing(self):
        result = self.provider.call_tool(
            self.context_for(self.limited_user),
            "get_object",
            {"object_type": "dcim.site", "object_id": self.hidden_site.pk, "fields": ["name"]},
        )

        self.assertEqual(result, {"object_type": "dcim.site", "found": False, "object": None})

    def test_user_without_permission_sees_no_objects(self):
        result = self.provider.call_tool(
            self.context_for(self.other_user),
            "query_objects",
            {"object_type": "dcim.site", "fields": ["name"]},
        )

        self.assertEqual(result["objects"], [])
