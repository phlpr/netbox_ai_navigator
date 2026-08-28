import json

from core.models import ObjectType
from dcim.models import Site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from tenancy.models import Contact, ContactAssignment, ContactRole
from users.models import ObjectPermission
from virtualization.models import VirtualMachine

from netbox_ai_navigator.config import user_can_read_assistant, user_can_write_assistant
from netbox_ai_navigator.exceptions import ToolValidationError
from netbox_ai_navigator.models import AINavigator
from netbox_ai_navigator.session_state import store_pending_action
from netbox_ai_navigator.template_content import GlobalAssistantExtension
from netbox_ai_navigator.tool_providers import LocalCurrentUserProvider, ToolContext
from netbox_ai_navigator.views import ChangeApprovalView


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

    def test_write_kill_switch_preserves_read_but_disables_changes(self):
        write_disabled = {"enabled": True, "tools": {"write": {"enabled": False}}}

        self.assertTrue(user_can_read_assistant(self.write_user, write_disabled))
        self.assertFalse(user_can_write_assistant(self.write_user, write_disabled))


class CurrentUserRBACIntegrationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.visible_site = Site.objects.create(name="Visible site", slug="visible-site", status="active")
        cls.hidden_site = Site.objects.create(name="Hidden site", slug="hidden-site", status="active")
        cls.limited_user = get_user_model().objects.create_user(username="limited-user")
        cls.other_user = get_user_model().objects.create_user(username="other-user")
        cls.superuser = get_user_model().objects.create_superuser(
            username="navigator-superuser",
            email="navigator@example.test",
            password="test-password",
        )
        cls.navigator_only_writer = get_user_model().objects.create_user(username="navigator-only-writer")
        navigator_type = ObjectType.objects.get_for_model(AINavigator)
        navigator_permission = ObjectPermission.objects.create(
            name="Use writable Navigator without Site change permission",
            actions=["use_write"],
        )
        navigator_permission.users.add(cls.navigator_only_writer)
        navigator_permission.object_types.add(navigator_type)

        permission = ObjectPermission.objects.create(
            name="View and change one site",
            actions=["view", "change"],
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

    def write_context_for(self, user):
        self.request.user = user
        return ToolContext(request=self.request, user=user, can_write=True)

    def approve(self, user, action):
        session_request = self.request
        session_request.session = {}
        public = store_pending_action(session_request, action)
        request = RequestFactory().post(
            "/plugins/ai-navigator/api/actions/approve/",
            data=json.dumps({"action_id": public["action_id"], "decision": "confirm"}),
            content_type="application/json",
            HTTP_HOST="testserver",
        )
        request.user = user
        request.session = session_request.session
        return ChangeApprovalView.as_view()(request)

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

    def test_contact_capable_objects_can_be_filtered_by_missing_assignment(self):
        assigned_vm = VirtualMachine.objects.create(name="vm-with-contact", status="active")
        first_unassigned_vm = VirtualMachine.objects.create(name="vm-without-contact-1", status="active")
        second_unassigned_vm = VirtualMachine.objects.create(name="vm-without-contact-2", status="active")
        contact = Contact.objects.create(name="VM owner")
        role = ContactRole.objects.create(name="Owner", slug="owner")
        ContactAssignment.objects.create(object=assigned_vm, contact=contact, role=role)
        provider = LocalCurrentUserProvider(
            {"allowed_object_types": ["virtualization.virtualmachine"], "max_results": 50}
        )

        description = provider.call_tool(
            self.context_for(self.superuser),
            "describe_object_type",
            {"object_type": "virtualization.virtualmachine"},
        )
        result = provider.call_tool(
            self.context_for(self.superuser),
            "query_objects",
            {
                "object_type": "virtualization.virtualmachine",
                "filters": {"has_contact": False},
                "fields": ["name"],
                "order_by": ["name"],
            },
        )

        self.assertIn("has_contact", {item["name"] for item in description["filters"]})
        self.assertEqual(
            [item["name"] for item in result["objects"]],
            [first_unassigned_vm.name, second_unassigned_vm.name],
        )
        self.assertFalse(result["has_more"])

        assigned = provider.call_tool(
            self.context_for(self.superuser),
            "query_objects",
            {
                "object_type": "virtualization.virtualmachine",
                "filters": {"has_contact": True},
                "fields": ["name"],
            },
        )
        self.assertEqual([item["name"] for item in assigned["objects"]], [assigned_vm.name])

        truncated = provider.call_tool(
            self.context_for(self.superuser),
            "query_objects",
            {
                "object_type": "virtualization.virtualmachine",
                "filters": {"has_contact": False},
                "fields": ["name"],
                "limit": 1,
            },
        )
        self.assertTrue(truncated["has_more"])

        with self.assertRaisesMessage(ToolValidationError, "has_contact must be a boolean."):
            provider.call_tool(
                self.context_for(self.superuser),
                "query_objects",
                {
                    "object_type": "virtualization.virtualmachine",
                    "filters": {"has_contact": "false"},
                },
            )

    def test_contact_filter_handles_more_than_one_result_page(self):
        names = ["SPSQLPROD001", "SPSQLPROD002", "SPSQLPROD003"] + [
            f"LAB-VM-{number:03d}" for number in range(4, 61)
        ]
        virtual_machines = VirtualMachine.objects.bulk_create(
            [VirtualMachine(name=name, status="active") for name in names]
        )
        contact = Contact.objects.create(name="Fictional Lab Operations")
        role = ContactRole.objects.create(name="Fictional Lab Owner", slug="fictional-lab-owner")
        mapped_names = {"LAB-VM-015", "LAB-VM-030", "LAB-VM-045", "LAB-VM-060"}
        ContactAssignment.objects.bulk_create(
            [
                ContactAssignment(object=vm, contact=contact, role=role)
                for vm in virtual_machines
                if vm.name in mapped_names
            ]
        )
        provider = LocalCurrentUserProvider(
            {"allowed_object_types": ["virtualization.virtualmachine"], "max_results": 50}
        )

        unmapped = provider.call_tool(
            self.context_for(self.superuser),
            "query_objects",
            {
                "object_type": "virtualization.virtualmachine",
                "filters": {"has_contact": False},
                "fields": ["name"],
                "order_by": ["name"],
            },
        )
        mapped = provider.call_tool(
            self.context_for(self.superuser),
            "query_objects",
            {
                "object_type": "virtualization.virtualmachine",
                "filters": {"has_contact": True},
                "fields": ["name"],
                "order_by": ["name"],
            },
        )
        remaining_unmapped = provider.call_tool(
            self.context_for(self.superuser),
            "query_objects",
            {
                "object_type": "virtualization.virtualmachine",
                "filters": {"has_contact": False},
                "fields": ["name"],
                "order_by": ["name"],
                "offset": unmapped["next_offset"],
            },
        )

        self.assertEqual(unmapped["returned"], 50)
        self.assertTrue(unmapped["has_more"])
        self.assertEqual(unmapped["next_offset"], 50)
        self.assertEqual(remaining_unmapped["returned"], 6)
        self.assertFalse(remaining_unmapped["has_more"])
        self.assertIsNone(remaining_unmapped["next_offset"])
        self.assertEqual(
            len({item["name"] for item in [*unmapped["objects"], *remaining_unmapped["objects"]]}),
            56,
        )
        self.assertEqual([item["name"] for item in mapped["objects"]], sorted(mapped_names))
        self.assertFalse(mapped["has_more"])

        with self.assertRaisesMessage(ToolValidationError, "offset must be between"):
            provider.call_tool(
                self.context_for(self.superuser),
                "query_objects",
                {
                    "object_type": "virtualization.virtualmachine",
                    "filters": {"has_contact": False},
                    "offset": -1,
                },
            )

    def test_navigation_only_targets_visible_object(self):
        result = self.provider.call_tool(
            self.context_for(self.limited_user),
            "navigate_to_object",
            {"object_type": "dcim.site", "object_id": self.visible_site.pk},
        )

        self.assertEqual(result["client_action"]["url"], self.visible_site.get_absolute_url())
        self.assertTrue(result["client_action"]["auto"])
        with self.assertRaises(ToolValidationError):
            self.provider.call_tool(
                self.context_for(self.limited_user),
                "navigate_to_object",
                {"object_type": "dcim.site", "object_id": self.hidden_site.pk},
            )

    def test_update_is_validated_but_not_written_until_approved(self):
        proposed = self.provider.call_tool(
            self.write_context_for(self.superuser),
            "propose_update_object",
            {
                "object_type": "dcim.site",
                "object_id": self.visible_site.pk,
                "data": {"description": "Approved description"},
            },
        )
        self.visible_site.refresh_from_db()
        self.assertNotEqual(self.visible_site.description, "Approved description")

        response = self.approve(self.superuser, proposed["pending_action"])

        self.assertEqual(response.status_code, 200, response.content)
        self.visible_site.refresh_from_db()
        self.assertEqual(self.visible_site.description, "Approved description")

    def test_named_bulk_update_is_validated_atomically_without_writing(self):
        proposed = self.provider.call_tool(
            self.write_context_for(self.superuser),
            "propose_bulk_update_named_objects",
            {
                "object_type": "dcim.site",
                "object_names": [self.visible_site.name, self.hidden_site.name],
                "data": {"status": "planned"},
            },
        )

        self.visible_site.refresh_from_db()
        self.hidden_site.refresh_from_db()
        self.assertEqual(proposed["count"], 2)
        self.assertEqual(len(proposed["pending_actions"]), 2)
        self.assertEqual(self.visible_site.status, "active")
        self.assertEqual(self.hidden_site.status, "active")

    def test_named_bulk_update_rejects_partial_permission_match(self):
        with self.assertRaisesMessage(ToolValidationError, "No partial batch was staged"):
            self.provider.call_tool(
                self.write_context_for(self.limited_user),
                "propose_bulk_update_named_objects",
                {
                    "object_type": "dcim.site",
                    "object_names": [self.visible_site.name, self.hidden_site.name],
                    "data": {"status": "planned"},
                },
            )

    def test_navigator_write_capability_does_not_bypass_object_change_permission(self):
        self.assertTrue(user_can_write_assistant(self.navigator_only_writer))

        with self.assertRaises(ToolValidationError):
            self.provider.call_tool(
                self.write_context_for(self.navigator_only_writer),
                "propose_update_object",
                {
                    "object_type": "dcim.site",
                    "object_id": self.visible_site.pk,
                    "data": {"description": "Must not be written"},
                },
            )

        self.visible_site.refresh_from_db()
        self.assertNotEqual(self.visible_site.description, "Must not be written")

    def test_concurrent_change_invalidates_approved_proposal(self):
        proposed = self.provider.call_tool(
            self.write_context_for(self.superuser),
            "propose_update_object",
            {
                "object_type": "dcim.site",
                "object_id": self.visible_site.pk,
                "data": {"description": "Stale proposed description"},
            },
        )
        self.visible_site.description = "Concurrent description"
        self.visible_site.save()

        response = self.approve(self.superuser, proposed["pending_action"])

        self.assertEqual(response.status_code, 412, response.content)
        self.visible_site.refresh_from_db()
        self.assertEqual(self.visible_site.description, "Concurrent description")

    def test_create_and_delete_use_netbox_api_after_each_confirmation(self):
        create = self.provider.call_tool(
            self.write_context_for(self.superuser),
            "propose_create_object",
            {
                "object_type": "dcim.site",
                "data": {"name": "AI approved site", "slug": "ai-approved-site", "status": "active"},
            },
        )

        create_response = self.approve(self.superuser, create["pending_action"])
        self.assertEqual(create_response.status_code, 200, create_response.content)
        created_id = json.loads(create_response.content)["object_id"]
        self.assertTrue(Site.objects.filter(pk=created_id).exists())

        delete = self.provider.call_tool(
            self.write_context_for(self.superuser),
            "propose_delete_object",
            {"object_type": "dcim.site", "object_id": created_id},
        )
        delete_response = self.approve(self.superuser, delete["pending_action"])

        self.assertEqual(delete_response.status_code, 200, delete_response.content)
        self.assertFalse(Site.objects.filter(pk=created_id).exists())
