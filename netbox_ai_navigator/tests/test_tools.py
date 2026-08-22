from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase

from netbox_ai_navigator.exceptions import ToolNotFoundError, ToolValidationError
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext


class LocalCurrentUserProviderTest(SimpleTestCase):
    def setUp(self):
        self.provider = LocalCurrentUserProvider(
            {
                "allowed_object_types": ["dcim.device", "users.user", "dcim.device"],
                "max_results": 500,
            }
        )
        request = RequestFactory().get("/")
        request.user = SimpleNamespace()
        self.context = ToolContext(request=request, user=request.user)

    def test_code_allowlist_excludes_sensitive_model_and_caps_results(self):
        self.assertEqual(self.provider.allowed_object_types, ("dcim.device",))
        self.assertEqual(self.provider.max_results, 50)

    def test_only_four_read_tools_are_exposed(self):
        tools = self.provider.list_tools(self.context)
        self.assertEqual(
            {tool.name for tool in tools},
            {"list_object_types", "describe_object_type", "query_objects", "get_object"},
        )
        query_description = next(tool.description for tool in tools if tool.name == "query_objects")
        self.assertIn("q filter for free-text searches", query_description)
        self.assertIn("site and location accept registered choices", query_description)

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ToolNotFoundError):
            self.provider.call_tool(self.context, "delete_object", {})

    def test_extra_arguments_are_rejected(self):
        with self.assertRaises(ToolValidationError):
            self.provider.call_tool(self.context, "list_object_types", {"model": "users.user"})
