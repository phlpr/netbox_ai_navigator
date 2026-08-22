from core.models import ObjectType
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from users.models import ObjectPermission

from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext


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
