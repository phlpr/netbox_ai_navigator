import json
from unittest import skipUnless
from unittest.mock import patch

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site, SiteGroup
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import RequestFactory, TestCase
from extras.choices import CustomFieldTypeChoices
from extras.models import CustomField, CustomFieldChoiceSet
from ipam.models import ServiceTemplate
from netbox.search.backends import search_backend
from packaging.version import Version
from virtualization.models import VirtualMachine

from netbox_ai_navigator.exceptions import ToolValidationError
from netbox_ai_navigator.session_state import store_pending_action
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext
from netbox_ai_navigator.views import ChangeApprovalView


@skipUnless(Version(settings.VERSION) >= Version("4.7"), "Requires NetBox 4.7 or newer.")
class NetBox47CompatibilityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="review-admin", email="review@example.test", password="test-only"
        )
        cls.site = Site.objects.create(name="Compatibility Lab", slug="compatibility-lab")
        cls.vm = VirtualMachine.objects.create(name="REVIEW-VM-001", site=cls.site)

    def setUp(self):
        self.request = RequestFactory().get("/")
        self.request.user = self.user
        self.request.session = {}
        self.context = ToolContext(request=self.request, user=self.user, can_write=True)
        self.provider = LocalCurrentUserProvider({"include_custom_fields": True})

    def tool(self, tool, **arguments):
        return self.provider.call_tool(self.context, tool, arguments)

    def approve(self, action):
        public = store_pending_action(self.request, action)
        request = RequestFactory().post(
            "/plugins/ai-navigator/api/actions/approve/",
            data=json.dumps({"action_id": public["action_id"], "decision": "confirm"}),
            content_type="application/json",
        )
        request.user = self.user
        request.session = self.request.session
        response = ChangeApprovalView.as_view()(request)
        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(json.loads(response.content)["executed"])

    def make_custom_fields(self):
        choices = CustomFieldChoiceSet.objects.create(
            name="Review lifecycle",
            extra_choices=[["stage", "Staging"], ["live", "Live service"]],
        )
        for name, kind in [
            ("lifecycle", CustomFieldTypeChoices.TYPE_SELECT),
            ("purposes", CustomFieldTypeChoices.TYPE_MULTISELECT),
        ]:
            field = CustomField.objects.create(name=name, type=kind, choice_set=choices)
            field.object_types.add(ObjectType.objects.get_for_model(VirtualMachine))
        field = CustomField.objects.create(name="api_token", type=CustomFieldTypeChoices.TYPE_TEXT)
        field.object_types.add(ObjectType.objects.get_for_model(VirtualMachine))
        self.vm.custom_field_data = {
            "lifecycle": "stage",
            "purposes": ["stage", "live"],
            "api_token": "TEST-ONLY-DO-NOT-EXPOSE",
        }
        self.vm.save()

    def test_discovery_and_query_all_available_models(self):
        discovered = self.tool("list_object_types")["object_types"]
        labels = {item["object_type"] for item in discovered}
        for label in (
            "dcim.coolingsource",
            "dcim.coolingfeed",
            "dcim.coolingintake",
            "dcim.coolingoutflow",
            "dcim.modulebaytype",
        ):
            self.assertIn(label, labels)
        for label in sorted(labels):
            with self.subTest(object_type=label):
                description = self.tool("describe_object_type", object_type=label)
                self.assertIn("output_fields", description)
                result = self.tool("query_objects", object_type=label, limit=1)
                self.assertIn("objects", result)

    def test_selection_fields_read_filter_preview_and_confirm(self):
        self.make_custom_fields()
        result = self.tool(
            "query_objects",
            object_type="virtualization.virtualmachine",
            filters={"cf_lifecycle": "stage"},
            fields=["name", "custom_fields"],
        )
        self.assertEqual(len(result["objects"]), 1)
        fields = result["objects"][0]["custom_fields"]
        self.assertEqual(fields["lifecycle"], {"value": "stage", "label": "Staging"})
        self.assertEqual(fields["purposes"][1], {"value": "live", "label": "Live service"})
        self.assertNotIn("api_token", fields)
        proposal = self.tool(
            "propose_update_object",
            object_type="virtualization.virtualmachine",
            object_id=self.vm.pk,
            data={"custom_fields": {"lifecycle": "live"}},
        )
        self.approve(proposal["pending_action"])
        self.vm.refresh_from_db()
        self.assertEqual(self.vm.custom_field_data["lifecycle"], "live")
        self.assertEqual(self.vm.custom_field_data["purposes"], ["stage", "live"])

    def test_selection_field_disabled_is_hidden(self):
        self.make_custom_fields()
        provider = LocalCurrentUserProvider({"include_custom_fields": False})
        result = provider.call_tool(
            self.context,
            "get_object",
            {
                "object_type": "virtualization.virtualmachine",
                "object_id": self.vm.pk,
            },
        )
        self.assertNotIn("custom_fields", result["object"])
        self.assertNotIn("TEST-ONLY-DO-NOT-EXPOSE", json.dumps(result, default=str))

    def test_config_context_excluded_from_device_and_vm(self):
        manufacturer = Manufacturer.objects.create(name="Review maker", slug="review-maker")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="Review type", slug="review-type")
        role = DeviceRole.objects.create(name="Review role", slug="review-role")
        device = Device.objects.create(
            name="REVIEW-DEVICE-001",
            site=self.site,
            device_type=device_type,
            role=role,
            local_context_data={"sensitive_marker": "REVIEW-CONTEXT-DO-NOT-EXPOSE"},
        )
        self.vm.local_context_data = {"sensitive_marker": "REVIEW-CONTEXT-DO-NOT-EXPOSE"}
        self.vm.save()
        for label, instance in [("dcim.device", device), ("virtualization.virtualmachine", self.vm)]:
            with self.subTest(object_type=label):
                result = self.tool("get_object", object_type=label, object_id=instance.pk)
                self.assertNotIn("config_context", result["object"])
                self.assertNotIn("REVIEW-CONTEXT-DO-NOT-EXPOSE", json.dumps(result, default=str))
                with self.assertRaises(ToolValidationError):
                    self.tool("get_object", object_type=label, object_id=instance.pk, fields=["config_context"])

    def test_service_new_and_legacy_port_mappings(self):
        service = ServiceTemplate.objects.create(name="Review DNS", port_mappings=["tcp/53"])
        description = self.tool("describe_object_type", object_type="ipam.servicetemplate")
        self.assertIn("port_mappings", description["output_fields"])
        self.assertIn("port_mappings", {field["name"] for field in description["writable_fields"]})
        result = self.tool("query_objects", object_type="ipam.servicetemplate", filters={"port": 53})
        self.assertEqual(result["objects"][0]["port_mappings"], ["tcp/53"])
        for payload in [{"ports": [5353]}, {"port_mappings": ["tcp/53", "udp/53"]}]:
            with self.subTest(payload=payload):
                proposal = self.tool(
                    "propose_update_object", object_type="ipam.servicetemplate", object_id=service.pk, data=payload
                )
                self.approve(proposal["pending_action"])
        service.refresh_from_db()
        self.assertEqual(service.port_mappings, ["tcp/53", "udp/53"])
        with self.assertRaises(ToolValidationError):
            self.tool("query_objects", object_type="ipam.servicetemplate", filters={"protocol__ic": "tcp"})

    def test_hierarchical_group_filter_and_navigation(self):
        group = SiteGroup.objects.create(name="Review root", slug="review-root")
        child = SiteGroup.objects.create(name="Review child", slug="review-child", parent=group)
        self.site.group = child
        self.site.save()
        result = self.tool("query_objects", object_type="dcim.site", filters={"group_id": group.pk})
        self.assertEqual([item["id"] for item in result["objects"]], [self.site.pk])
        target = self.tool("navigate_to_object_list", object_type="dcim.site", filters={"group_id": group.pk})
        self.assertEqual(target["filter_mode"], "native")
        self.assertIn(f"group_id={group.pk}", target["client_action"]["url"])

    def test_new_cooling_model_contact_filter_and_change(self):
        source = apps.get_model("dcim", "CoolingSource").objects.create(
            site=self.site, name="Review chiller", type="chiller"
        )
        result = self.tool("query_objects", object_type="dcim.coolingsource", filters={"has_contact": False})
        self.assertEqual([item["id"] for item in result["objects"]], [source.pk])
        proposal = self.tool(
            "propose_update_object",
            object_type="dcim.coolingsource",
            object_id=source.pk,
            data={"description": "Verified through Navigator"},
        )
        self.approve(proposal["pending_action"])
        source.refresh_from_db()
        self.assertEqual(source.description, "Verified through Navigator")

    def test_search_finds_indexed_objects(self):
        search_backend.cache(self.vm)
        result = self.tool("search_netbox", query=self.vm.name)
        self.assertEqual([item["id"] for item in result["objects"]], [self.vm.pk])

    def test_search_after_commit_without_worker(self):
        with (
            patch("netbox.search.deferred.any_workers_for_queue", return_value=False),
            self.captureOnCommitCallbacks(execute=True),
        ):
            vm = VirtualMachine.objects.create(name="REVIEW-NEWLY-CREATED")
        result = self.tool("search_netbox", query=vm.name)
        self.assertEqual([item["id"] for item in result["objects"]], [vm.pk])

    def test_core_page_contains_navigator_assets(self):
        self.client.force_login(self.user)
        for url in (self.site.get_absolute_url(), self.vm.get_absolute_url(), "/dcim/devices/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "netbox_ai_navigator/assistant.js")
        self.assertTrue(finders.find("netbox_ai_navigator/assistant.js"))
        self.assertTrue(finders.find("netbox_ai_navigator/assistant.css"))
