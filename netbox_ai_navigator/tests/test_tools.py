from types import SimpleNamespace
from unittest.mock import patch

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

    def test_configured_allowlist_is_dynamic_and_caps_results(self):
        self.assertEqual(self.provider.allowed_object_types, ("dcim.device", "users.user"))
        self.assertEqual(self.provider.max_results, 50)

    def test_preview_value_uses_choice_label(self):
        self.assertEqual(
            self.provider._preview_value({"value": "planned", "label": "Planned"}),
            "Planned",
        )

    def test_discovers_compatible_core_models_and_blocks_credential_models(self):
        provider = LocalCurrentUserProvider({})

        self.assertIn("dcim.device", provider.allowed_object_types)
        self.assertIn("tenancy.tenant", provider.allowed_object_types)
        self.assertIn("wireless.wirelesslan", provider.allowed_object_types)
        self.assertNotIn("users.token", provider.allowed_object_types)
        self.assertNotIn("extras.webhook", provider.allowed_object_types)

    def test_administrator_can_exclude_discovered_types_and_fields(self):
        provider = LocalCurrentUserProvider(
            {
                "excluded_object_types": ["tenancy.tenant"],
                "excluded_fields": ["email"],
            }
        )

        self.assertNotIn("tenancy.tenant", provider.allowed_object_types)
        description = provider.call_tool(self.context, "describe_object_type", {"object_type": "users.user"})
        self.assertNotIn("email", description["output_fields"])
        self.assertNotIn("password", description["output_fields"])

    def test_nested_credential_fields_are_removed_from_serialized_data(self):
        value = {
            "name": "edge-router",
            "custom_fields": {
                "owner": "network-team",
                "router password": "must-not-leave-netbox",
                "nested": [{"api-token": "must-not-leave-netbox", "label": "safe"}],
            },
        }

        sanitized = self.provider._sanitize_serialized_value(value)

        self.assertEqual(sanitized["custom_fields"]["owner"], "network-team")
        self.assertNotIn("router password", sanitized["custom_fields"])
        self.assertNotIn("api-token", sanitized["custom_fields"]["nested"][0])
        self.assertEqual(sanitized["custom_fields"]["nested"][0]["label"], "safe")

    def test_nested_credential_fields_are_rejected_for_writes(self):
        self.assertTrue(
            self.provider._contains_blocked_nested_name({"custom_fields": {"client_secret": "must-not-be-written"}})
        )
        self.assertFalse(self.provider._contains_blocked_nested_name({"custom_fields": {"owner": "network-team"}}))

    def test_custom_fields_are_available_only_with_explicit_opt_in(self):
        field = SimpleNamespace(write_only=False, read_only=False, source="custom_fields")
        enabled = LocalCurrentUserProvider({"include_custom_fields": True})

        self.assertFalse(self.provider._field_is_allowed("custom_fields", field))
        self.assertFalse(self.provider._field_is_allowed("cf_owner"))
        self.assertTrue(enabled._field_is_allowed("custom_fields", field))
        self.assertTrue(enabled._field_is_allowed("cf_owner"))
        self.assertFalse(enabled._field_is_allowed("cf_service_password"))
        self.assertTrue(enabled._write_field_is_allowed("custom_fields", field))

    def test_custom_field_write_preview_keeps_safe_values_and_removes_credentials(self):
        readable = SimpleNamespace(write_only=False, read_only=False, source="custom_fields")

        class PreviewSerializer:
            def __init__(self, *args, **kwargs):
                self.fields = {"custom_fields": readable}
                self.data = {
                    "custom_fields": {
                        "owner": "network-team",
                        "service_token": "must-not-leave-netbox",
                    }
                }

        enabled = LocalCurrentUserProvider({"include_custom_fields": True})

        preview = enabled._serialize_preview(
            object(),
            PreviewSerializer,
            self.context,
            {"custom_fields": {"owner": "network-team"}},
        )

        self.assertEqual(preview, {"custom_fields": {"owner": "network-team"}})

    def test_discovers_plugin_model_from_standard_netbox_registries(self):
        readable = SimpleNamespace(write_only=False, source="name")
        write_only = SimpleNamespace(write_only=True, source="password")

        class PluginSerializer:
            def __init__(self, *args, **kwargs):
                self.fields = {
                    "id": readable,
                    "display": readable,
                    "name": readable,
                    "secret": readable,
                    "password": write_only,
                }

        class PluginFilterSet:
            base_filters = {
                "q": SimpleNamespace(label="Search"),
                "secret": SimpleNamespace(label="Secret"),
            }

            def __init__(self, *args, **kwargs):
                self.filters = self.base_filters

        plugin_model = SimpleNamespace(
            _meta=SimpleNamespace(
                label_lower="example_plugin.widget",
                verbose_name="widget",
                verbose_name_plural="widgets",
                abstract=False,
            ),
            _default_manager=SimpleNamespace(restrict=lambda *args: None),
        )
        credential_model = SimpleNamespace(
            _meta=SimpleNamespace(
                label_lower="example_plugin.apitoken",
                verbose_name="API token",
                verbose_name_plural="API tokens",
                abstract=False,
            ),
            _default_manager=SimpleNamespace(restrict=lambda *args: None),
        )

        with (
            patch(
                "netbox_ai_navigator.tool_providers.local_current_user.apps.get_models",
                return_value=[plugin_model, credential_model],
            ),
            patch(
                "netbox_ai_navigator.tool_providers.local_current_user.registry",
                {
                    "filtersets": {
                        "example_plugin.widget": PluginFilterSet,
                        "example_plugin.apitoken": PluginFilterSet,
                    }
                },
            ),
            patch(
                "netbox_ai_navigator.tool_providers.local_current_user.get_serializer_for_model",
                return_value=PluginSerializer,
            ),
        ):
            provider = LocalCurrentUserProvider({})

        result = provider.call_tool(self.context, "list_object_types", {"query": "widget"})
        description = provider.call_tool(
            self.context,
            "describe_object_type",
            {"object_type": "example_plugin.widget"},
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["object_types"][0]["object_type"], "example_plugin.widget")
        self.assertNotIn("example_plugin.apitoken", provider.allowed_object_types)
        self.assertEqual(description["output_fields"], ["id", "display", "name"])
        self.assertEqual([item["name"] for item in description["filters"]], ["q"])

    def test_read_documentation_and_navigation_tools_are_exposed_without_write_tools(self):
        tools = self.provider.list_tools(self.context)
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "list_object_types",
                "describe_object_type",
                "query_objects",
                "get_object",
                "search_netbox",
                "search_documentation",
                "read_documentation",
                "navigate_to_object",
                "navigate_to_object_list",
                "navigate_to_search",
            },
        )
        query_description = next(tool.description for tool in tools if tool.name == "query_objects")
        query_tool = next(tool for tool in tools if tool.name == "query_objects")
        list_navigation_tool = next(tool for tool in tools if tool.name == "navigate_to_object_list")
        object_type_schema = query_tool.input_schema["properties"]["object_type"]
        self.assertNotIn("enum", object_type_schema)
        self.assertEqual(object_type_schema["pattern"], "^[a-z0-9_]+\\.[a-z0-9_]+$")
        self.assertIn("core or plugin", query_description)
        self.assertIn("q filter for free-text searches on the queried object", query_description)
        self.assertIn("has_contact", query_description)
        self.assertIn("returned site_id or location_id", query_description)
        self.assertIn("site and location accept registered choices", query_description)
        self.assertIn("filters", list_navigation_tool.input_schema["properties"])
        self.assertIn("object_ids", list_navigation_tool.input_schema["properties"])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ToolNotFoundError):
            self.provider.call_tool(self.context, "delete_object", {})

    def test_extra_arguments_are_rejected(self):
        with self.assertRaises(ToolValidationError):
            self.provider.call_tool(self.context, "list_object_types", {"model": "users.user"})

    def test_empty_object_type_search_is_rejected(self):
        with self.assertRaises(ToolValidationError):
            self.provider.call_tool(self.context, "list_object_types", {"query": " "})

    def test_write_tools_require_explicit_write_context(self):
        write_context = ToolContext(request=self.context.request, user=self.context.user, can_write=True)

        read_tools = {tool.name for tool in self.provider.list_tools(self.context)}
        write_tools = {tool.name for tool in self.provider.list_tools(write_context)}

        self.assertFalse(any(name.startswith("propose_") for name in read_tools))
        self.assertTrue(
            {
                "propose_create_object",
                "propose_update_object",
                "propose_bulk_update_named_objects",
                "propose_delete_object",
            }.issubset(write_tools)
        )

    def test_navigation_search_returns_verified_local_action(self):
        result = self.provider.call_tool(self.context, "navigate_to_search", {"query": "edge router"})

        self.assertEqual(result["client_action"]["type"], "navigate")
        self.assertEqual(result["client_action"]["url"], "/search/?q=edge+router")
        self.assertTrue(result["client_action"]["auto"])

    @patch("netbox_ai_navigator.tool_providers.local_current_user.search_backend.search")
    def test_global_search_returns_only_one_discovered_identity_per_object(self, search):
        complex_name = "++ATOBE+NDB.G00-4-B02--PoE01"

        class DeviceResult:
            pk = 42
            _meta = SimpleNamespace(label_lower="dcim.device")

            def __str__(self):
                return complex_name

            def get_absolute_url(self):
                return "/dcim/devices/42/"

        result_match = SimpleNamespace(object=DeviceResult())
        search.return_value = [result_match, result_match]

        result = self.provider._search_netbox(self.context, {"query": complex_name})

        self.assertEqual(
            result["objects"],
            [
                {
                    "id": 42,
                    "display": complex_name,
                    "display_url": "/dcim/devices/42/",
                    "object_type": "dcim.device",
                }
            ],
        )
        search.assert_called_once_with(complex_name, user=self.context.user)
