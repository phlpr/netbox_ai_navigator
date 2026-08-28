import json
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase
from django.utils.translation import override

from netbox_ai_navigator.agent.runtime import AgentRuntime
from netbox_ai_navigator.exceptions import AgentLimitError, ToolNotFoundError, UngroundedResponseError
from netbox_ai_navigator.model_providers import ModelResponse, ModelToolCall
from netbox_ai_navigator.rejections import RejectionReason
from netbox_ai_navigator.tool_providers import ToolContext, ToolDefinition, ToolProvider


class FakeModelProvider:
    model_name = "test-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.responses.pop(0)


class FakeToolProvider(ToolProvider):
    def __init__(self):
        self.calls = []

    def list_tools(self, context):
        return [
            ToolDefinition(
                name="read_test_data",
                description="Read test data.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ]

    def call_tool(self, context, name, arguments):
        self.calls.append((name, arguments))
        if name != "read_test_data":
            raise ToolNotFoundError(f"Unknown tool: {name}")
        return {"value": 42}


class FakeWriteToolProvider(FakeToolProvider):
    def list_tools(self, context):
        return [
            *super().list_tools(context),
            ToolDefinition(
                name="propose_update_object",
                description="Propose one test update.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]

    def call_tool(self, context, name, arguments):
        if name == "propose_update_object":
            object_id = arguments.get("object_id", 4)
            return {
                "requires_confirmation": True,
                "pending_action": {
                    "type": "change_approval",
                    "operation": "update",
                    "object_type": "dcim.device",
                    "object_id": object_id,
                    "endpoint": f"/api/dcim/devices/{object_id}/",
                },
            }
        return super().call_tool(context, name, arguments)


class FakeBulkWriteToolProvider(FakeWriteToolProvider):
    def list_tools(self, context):
        return [
            *super().list_tools(context),
            ToolDefinition(
                name="propose_bulk_update_named_objects",
                description="Propose an atomic named-object update batch.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]

    def call_tool(self, context, name, arguments):
        if name == "propose_bulk_update_named_objects":
            return {
                "requires_confirmation": True,
                "pending_actions": [
                    {
                        "type": "change_approval",
                        "operation": "update",
                        "object_type": "virtualization.virtualmachine",
                        "object_id": object_id,
                        "endpoint": f"/api/virtualization/virtual-machines/{object_id}/",
                    }
                    for object_id, _object_name in enumerate(arguments["object_names"], start=1)
                ],
            }
        return super().call_tool(context, name, arguments)


class FakeDeviceToolProvider(ToolProvider):
    def __init__(self, objects, object_type="dcim.device"):
        self.objects = objects
        self.object_type = object_type
        self.calls = []

    def list_tools(self, context):
        return [
            ToolDefinition(
                name="query_objects",
                description="Query test devices.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ]

    def call_tool(self, context, name, arguments):
        self.calls.append((name, arguments))
        if name != "query_objects":
            raise ToolNotFoundError(f"Unknown tool: {name}")
        return {
            "object_type": self.object_type,
            "returned": len(self.objects),
            "limit": 50,
            "objects": self.objects,
        }


class FakeSearchThenDetailToolProvider(ToolProvider):
    def __init__(self, objects, *, include_site=False):
        self.objects = objects
        self.include_site = include_site
        self.calls = []

    def list_tools(self, context):
        return [
            ToolDefinition(
                name="search_netbox",
                description="Discover matching objects.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolDefinition(
                name="get_object",
                description="Get one matching object.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]

    def call_tool(self, context, name, arguments):
        self.calls.append((name, arguments))
        if name == "search_netbox":
            objects = [
                {
                    "id": item["id"],
                    "display": item["display"],
                    "display_url": item["display_url"],
                    "object_type": "dcim.device",
                }
                for item in self.objects
            ]
            if self.include_site:
                objects.insert(
                    0,
                    {
                        "id": 2,
                        "display": "GeoView Graz Edge",
                        "display_url": "http://testserver/dcim/sites/2/",
                        "object_type": "dcim.site",
                    },
                )
            return {
                "query": "graz",
                "returned": len(objects),
                "objects": objects,
            }
        if name == "get_object":
            item = next(value for value in self.objects if value["id"] == arguments["object_id"])
            if arguments.get("fields") == []:
                item = {
                    "id": item["id"],
                    "display": item["display"],
                    "display_url": item["display_url"],
                }
            return {"object_type": "dcim.device", "found": True, "object": item}
        raise ToolNotFoundError(name)


class FakeActionToolProvider(ToolProvider):
    def list_tools(self, context):
        return [
            ToolDefinition(
                name="offer_navigation",
                description="Offer navigation.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolDefinition(
                name="propose_change",
                description="Propose change.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]

    def call_tool(self, context, name, arguments):
        if name == "offer_navigation":
            return {"client_action": {"type": "navigate", "url": "/dcim/devices/4/", "label": "Device"}}
        if name == "propose_change":
            return {
                "requires_confirmation": True,
                "pending_action": {
                    "type": "change_approval",
                    "operation": "update",
                    "endpoint": "/api/dcim/devices/4/",
                },
            }
        raise ToolNotFoundError(name)


class FakeNavigationToolProvider(ToolProvider):
    def __init__(self):
        self.calls = []

    def list_tools(self, context):
        return [
            ToolDefinition(
                name="search_netbox",
                description="Search test objects.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            ),
            ToolDefinition(
                name="navigate_to_object",
                description="Navigate to one test object.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            ),
        ]

    def call_tool(self, context, name, arguments):
        self.calls.append((name, arguments))
        if name == "search_netbox":
            return {
                "query": "Fictional Lab Operations",
                "returned": 1,
                "objects": [
                    {
                        "id": 7,
                        "display": "Fictional Lab Operations",
                        "display_url": "http://testserver/tenancy/contacts/7/",
                        "object_type": "tenancy.contact",
                    }
                ],
            }
        if name == "navigate_to_object":
            return {
                "client_action": {
                    "type": "navigate",
                    "url": "/tenancy/contacts/7/",
                    "label": "Fictional Lab Operations",
                    "auto": True,
                }
            }
        raise ToolNotFoundError(name)


class FakePagedVMToolProvider(ToolProvider):
    def __init__(self):
        self.calls = []

    def list_tools(self, context):
        return [
            ToolDefinition(
                name="query_objects",
                description="Query paginated test VMs.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            )
        ]

    def call_tool(self, context, name, arguments):
        if name != "query_objects":
            raise ToolNotFoundError(name)
        self.calls.append(arguments)
        offset = arguments.get("offset", 0)
        names = ["SPSQLPROD001", "SPSQLPROD002"] if offset == 0 else ["LAB-VM-059"]
        return {
            "object_type": "virtualization.virtualmachine",
            "returned": len(names),
            "limit": 2,
            "offset": offset,
            "has_more": offset == 0,
            "next_offset": 2 if offset == 0 else None,
            "objects": [
                {
                    "id": index,
                    "display": value,
                    "name": value,
                    "display_url": f"http://testserver/virtualization/virtual-machines/{index}/",
                }
                for index, value in enumerate(names, start=offset + 1)
            ],
        }


GRAZ_DEVICES = [
    {
        "id": 4,
        "display": "gv-graz-access-01 (GV-ASSET-0004)",
        "display_url": "http://testserver/dcim/devices/4/",
        "name": "gv-graz-access-01",
        "site": {
            "id": 2,
            "url": "http://testserver/api/dcim/sites/2/",
            "display": "GeoView Graz Edge",
            "name": "GeoView Graz Edge",
        },
        "role": {"id": 1, "display": "GeoView Access Switch", "name": "GeoView Access Switch"},
        "location": {"id": 1, "display": "Network Room 201", "name": "Network Room 201"},
        "status": {"value": "active", "label": "Active"},
        "primary_ip4": None,
    },
    {
        "id": 3,
        "display": "gv-graz-fw-01 (GV-ASSET-0003)",
        "display_url": "http://testserver/dcim/devices/3/",
        "name": "gv-graz-fw-01",
        "site": {
            "id": 2,
            "url": "http://testserver/api/dcim/sites/2/",
            "display": "GeoView Graz Edge",
            "name": "GeoView Graz Edge",
        },
        "role": {"id": 2, "display": "GeoView Firewall", "name": "GeoView Firewall"},
        "location": {"id": 1, "display": "Network Room 201", "name": "Network Room 201"},
        "status": {"value": "active", "label": "Active"},
        "primary_ip4": None,
    },
]


class AgentRuntimeTest(SimpleTestCase):
    def setUp(self):
        request = RequestFactory().get("/")
        user = SimpleNamespace(username="test-user")
        request.user = user
        self.context = ToolContext(request=request, user=user)

    def test_executes_tool_and_returns_final_answer(self):
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "read_test_data", "{}")]),
                ModelResponse(content="The answer is 42."),
            ]
        )
        tools = FakeToolProvider()
        runtime = AgentRuntime(model, tools)

        result = runtime.run(self.context, [{"role": "user", "content": "What is the value?"}])

        self.assertEqual(result.answer, "The answer is 42.")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(tools.calls, [("read_test_data", {})])
        tool_message = model.calls[1][0][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(json.loads(tool_message["content"])["result"]["value"], 42)

    def test_sends_shared_markdown_formatting_contract(self):
        model = FakeModelProvider([ModelResponse(content="The answer is 42.")])
        runtime = AgentRuntime(model, FakeToolProvider())

        runtime.run(self.context, [{"role": "user", "content": "What is the value?"}])

        system_message = model.calls[0][0][0]
        self.assertEqual(system_message["role"], "system")
        self.assertIn("Formatting is part of correctness", system_message["content"])
        self.assertIn("scope is limited to NetBox data", system_message["content"])
        self.assertIn("Do not answer unrelated general-knowledge", system_message["content"])
        self.assertIn("GitHub-style Markdown", system_message["content"])
        self.assertIn("For two or more comparable NetBox objects", system_message["content"])
        self.assertIn("no more than five relevant columns", system_message["content"])
        self.assertIn("Never create a separate Link or URL column", system_message["content"])
        self.assertIn("For device tables", system_message["content"])
        self.assertIn("core applications and installed plugins are discovered dynamically", system_message["content"])
        self.assertIn("use list_object_types", system_message["content"])
        self.assertIn("Treat plugin object types exactly like core object types", system_message["content"])
        self.assertIn("Use search_netbox", system_message["content"])
        self.assertIn("Use search_documentation", system_message["content"])
        self.assertIn("Use navigation tools only", system_message["content"])
        self.assertIn("Write-proposal tools are available only", system_message["content"])
        self.assertIn("each is awaiting", system_message["content"])
        self.assertIn("prefer the `q` filter", system_message["content"])
        self.assertIn("returned numeric `site_id` or `location_id`", system_message["content"])
        self.assertIn("common English or", system_message["content"])
        self.assertIn("localized equivalent", system_message["content"])
        self.assertIn("every other cell value", system_message["content"])
        self.assertIn("Do not add a concluding claim", system_message["content"])
        self.assertIn("Do not emit raw JSON", system_message["content"])
        self.assertIn("exact text intended to be copied", system_message["content"])
        self.assertIn("CSV template", system_message["content"])

    def test_replaces_out_of_scope_answer_without_tool_call(self):
        model = FakeModelProvider(
            [ModelResponse(content="Use list.sort() or sorted() to sort a Python list.")]
        )

        result = AgentRuntime(model, FakeToolProvider()).run(
            self.context,
            [{"role": "user", "content": "How do I sort a Python list?"}],
        )

        self.assertEqual(
            result.answer,
            "AI Navigator is limited to NetBox data, configuration, and workflows. "
            "Please ask a NetBox-related question.",
        )
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.rejection.reason, RejectionReason.SCOPE_GUARD)
        self.assertEqual(result.rejection.response, "Use list.sort() or sorted() to sort a Python list.")

    def test_translates_out_of_scope_answer(self):
        model = FakeModelProvider([ModelResponse(content="Verwende list.sort().")])

        with override("de"):
            result = AgentRuntime(model, FakeToolProvider()).run(
                self.context,
                [{"role": "user", "content": "Wie sortiere ich eine Python-Liste?"}],
            )

        self.assertEqual(
            result.answer,
            "AI Navigator ist auf NetBox-Daten, -Konfiguration und -Arbeitsabläufe beschränkt. "
            "Bitte stellen Sie eine NetBox-bezogene Frage.",
        )

    def test_sends_explicit_session_capability_from_available_tools(self):
        write_model = FakeModelProvider([ModelResponse(content="A write-capable answer.")])
        AgentRuntime(write_model, FakeWriteToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Update one object."}],
        )
        write_capability = write_model.calls[0][0][1]

        read_model = FakeModelProvider([ModelResponse(content="A read-only answer.")])
        AgentRuntime(read_model, FakeToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Inspect one object."}],
        )
        read_capability = read_model.calls[0][0][1]

        self.assertIn("WRITE PROPOSALS ENABLED", write_capability["content"])
        self.assertIn("Never describe this session as read-only", write_capability["content"])
        self.assertIn("at most 5", write_capability["content"])
        self.assertIn("call the matching proposal tool", write_capability["content"])
        self.assertIn("READ-ONLY", read_capability["content"])
        self.assertIn("No propose_* tools are available", read_capability["content"])

    def test_pending_change_uses_deterministic_confirmation_answer(self):
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[ModelToolCall("change", "propose_update_object", {"status": "active"})]
                ),
                ModelResponse(content="The object was already updated."),
            ]
        )

        result = AgentRuntime(model, FakeWriteToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Set the status to active."}],
        )

        self.assertEqual(
            result.answer,
            "The requested change was validated and is awaiting manual confirmation.",
        )
        self.assertEqual(len(result.pending_actions), 1)
        self.assertEqual(result.rejection.reason, RejectionReason.APPROVAL_NORMALIZATION)
        self.assertEqual(result.rejection.response, "The object was already updated.")

    def test_pending_change_is_logged_even_when_model_matches_confirmation_answer(self):
        confirmation = "The requested change was validated and is awaiting manual confirmation."
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[ModelToolCall("change", "propose_update_object", {"status": "active"})]
                ),
                ModelResponse(content=confirmation),
            ]
        )

        result = AgentRuntime(model, FakeWriteToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Set the status to active."}],
        )

        self.assertEqual(result.answer, confirmation)
        self.assertEqual(result.rejection.reason, RejectionReason.APPROVAL_NORMALIZATION)
        self.assertEqual(result.rejection.response, confirmation)

    def test_rejects_non_atomic_series_of_single_object_proposals(self):
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            f"change-{object_id}",
                            "propose_update_object",
                            {"object_id": object_id, "status": "deleted"},
                        )
                        for object_id in (1, 2, 3)
                    ]
                ),
                ModelResponse(content="The devices were already deleted."),
            ]
        )

        result = AgentRuntime(model, FakeWriteToolProvider(), max_pending_actions=3).run(
            self.context,
            [{"role": "user", "content": "Set the status of SPSQLPROD001 - 003 to deleted."}],
        )

        self.assertEqual(
            result.answer,
            "No change proposals were created because multiple updates must be validated as one atomic batch. "
            "Please retry the complete named-object request.",
        )
        self.assertEqual(result.pending_actions, ())
        self.assertEqual(result.rejection.reason, RejectionReason.PROPOSAL_GUARD)

    def test_requires_atomic_bulk_tool_for_compact_numeric_update_range(self):
        names = ["SPSQLPROD001", "SPSQLPROD002", "SPSQLPROD003"]
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "bulk-change",
                            "propose_bulk_update_named_objects",
                            {
                                "object_type": "virtualization.virtualmachine",
                                "object_names": names,
                                "data": {"status": "deleted"},
                            },
                        )
                    ]
                ),
                ModelResponse(content="The objects were already changed."),
            ]
        )

        result = AgentRuntime(model, FakeBulkWriteToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Setze den Status von SPSQLPROD001 - 003 auf deleted"}],
        )

        exposed_tool_names = {tool["function"]["name"] for tool in model.calls[0][1]}
        capability_message = model.calls[0][0][1]["content"]
        self.assertNotIn("propose_update_object", exposed_tool_names)
        self.assertIn("propose_bulk_update_named_objects", exposed_tool_names)
        self.assertIn("single-object update tool is intentionally unavailable", capability_message)
        self.assertEqual(len(result.pending_actions), 3)
        self.assertEqual(
            result.answer,
            "3 requested changes were validated. Each change is awaiting separate manual confirmation.",
        )

    def test_plural_followup_reference_requires_atomic_bulk_tool(self):
        model = FakeModelProvider(
            [
                ModelResponse(content="No proposal was created."),
                ModelResponse(content="The exact object names are required."),
            ]
        )

        AgentRuntime(model, FakeBulkWriteToolProvider()).run(
            self.context,
            [
                {"role": "user", "content": "Show the matching devices."},
                {"role": "assistant", "content": "Device A and Device B"},
                {"role": "user", "content": "Setze beide auf planned"},
            ],
        )

        exposed_tool_names = {tool["function"]["name"] for tool in model.calls[0][1]}
        self.assertNotIn("propose_update_object", exposed_tool_names)
        self.assertIn("propose_bulk_update_named_objects", exposed_tool_names)

    def test_rejects_single_update_tool_when_bulk_request_requires_atomic_tool(self):
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "hidden-single-change",
                            "propose_update_object",
                            {"object_id": 1, "status": "deleted"},
                        )
                    ]
                ),
                ModelResponse(content="One object is ready."),
            ]
        )

        result = AgentRuntime(model, FakeBulkWriteToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Setze den Status von SPSQLPROD001 - 003 auf deleted"}],
        )

        self.assertEqual(
            result.answer,
            "No validated change proposal could be created. Please verify the exact NetBox object names and "
            "requested value, then try again.",
        )
        self.assertEqual(result.pending_actions, ())

    def test_discards_all_proposals_when_multi_object_limit_is_exceeded(self):
        names = ["SPSQLPROD001", "SPSQLPROD002", "SPSQLPROD003"]
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "bulk-change",
                            "propose_bulk_update_named_objects",
                            {
                                "object_type": "virtualization.virtualmachine",
                                "object_names": names,
                                "data": {"status": "deleted"},
                            },
                        )
                    ]
                ),
                ModelResponse(content="Two of three changes are ready."),
            ]
        )

        result = AgentRuntime(model, FakeBulkWriteToolProvider(), max_pending_actions=2).run(
            self.context,
            [{"role": "user", "content": "Set three device statuses to deleted."}],
        )

        self.assertEqual(
            result.answer,
            "No change proposals were created because the request exceeded the limit of 2 objects. "
            "Please narrow the request.",
        )
        self.assertEqual(result.pending_actions, ())

    def test_returns_change_specific_failure_when_write_model_does_not_use_tools(self):
        model = FakeModelProvider(
            [
                ModelResponse(content="This is not a NetBox request."),
                ModelResponse(content="I still cannot create a proposal."),
            ]
        )

        result = AgentRuntime(model, FakeWriteToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Setze den Status von SPSQLPROD001 - 003 auf deleted"}],
        )

        self.assertEqual(
            result.answer,
            "No validated change proposal could be created. Please verify the exact NetBox object names and "
            "requested value, then try again.",
        )

    def test_reprompts_once_when_change_request_stops_after_lookup(self):
        model = FakeModelProvider(
            [
                ModelResponse(content="The device was found."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "change-1",
                            "propose_update_object",
                            {"object_id": 4, "data": {"status": "planned"}},
                        )
                    ]
                ),
                ModelResponse(content="The change is ready."),
            ]
        )

        result = AgentRuntime(model, FakeWriteToolProvider()).run(
            self.context,
            [
                {
                    "role": "user",
                    "content": "Setze ++ATOBE+NDB.G00-4-B02--PoE01 auf planned",
                }
            ],
        )

        refinement_messages = [
            message
            for message in model.calls[1][0]
            if message["role"] == "system" and message["content"].startswith("The user explicitly requested")
        ]
        self.assertEqual(len(refinement_messages), 1)
        self.assertEqual(len(result.pending_actions), 1)
        self.assertEqual(result.answer, "The requested change was validated and is awaiting manual confirmation.")

    def test_fetches_remaining_pages_for_complete_list_request(self):
        final_answer = "\n".join(
            (
                "| VM | ID |",
                "| --- | --- |",
                "| [SPSQLPROD001](http://testserver/virtualization/virtual-machines/1/) | 1 |",
                "| [SPSQLPROD002](http://testserver/virtualization/virtual-machines/2/) | 2 |",
                "| [LAB-VM-059](http://testserver/virtualization/virtual-machines/3/) | 3 |",
            )
        )
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "page-1",
                            "query_objects",
                            {
                                "object_type": "virtualization.virtualmachine",
                                "filters": {"has_contact": False},
                                "fields": ["name"],
                                "limit": 2,
                            },
                        )
                    ]
                ),
                ModelResponse(content="Here are the first two VMs."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "page-2",
                            "query_objects",
                            {
                                "object_type": "virtualization.virtualmachine",
                                "filters": {"has_contact": False},
                                "fields": ["name"],
                                "limit": 2,
                                "offset": 2,
                            },
                        )
                    ]
                ),
                ModelResponse(content=final_answer),
            ]
        )
        tools = FakePagedVMToolProvider()

        result = AgentRuntime(model, tools).run(
            self.context,
            [{"role": "user", "content": "Suche mir alle VMs ohne Kontakt-Mapping"}],
        )

        self.assertEqual(result.answer, final_answer)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual([call.get("offset", 0) for call in tools.calls], [0, 2])
        pagination_messages = [
            message
            for message in model.calls[2][0]
            if message["role"] == "system" and message["content"].startswith("The user requested the complete")
        ]
        self.assertEqual(len(pagination_messages), 1)

    def test_returns_read_only_failure_for_recognized_change_request(self):
        model = FakeModelProvider([ModelResponse(content="This is not a NetBox request.")])

        result = AgentRuntime(model, FakeToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Setze den Status von SPSQLPROD001 auf deleted"}],
        )

        self.assertEqual(
            result.answer,
            "The current session is read-only. No NetBox change can be proposed or performed.",
        )

    def test_accepts_table_grounded_in_query_result(self):
        answer = "\n".join(
            (
                "| Device | Site | Role | Status | Primary IPv4 |",
                "| --- | --- | --- | --- | --- |",
                "| [gv-graz-access-01](http://testserver/dcim/devices/4/) | "
                "[GeoView Graz Edge](http://testserver/dcim/sites/2/) | GeoView Access Switch | Active | — |",
                "| [gv-graz-fw-01](http://testserver/dcim/devices/3/) | "
                "[GeoView Graz Edge](http://testserver/dcim/sites/2/) | GeoView Firewall | Active | — |",
            )
        )
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        result = runtime.run(self.context, [{"role": "user", "content": "List Graz devices."}])

        self.assertEqual(result.answer, answer)

    def test_global_search_is_refined_before_returning_object_attributes(self):
        answer = "\n".join(
            (
                "| Device | Site | Role | Status |",
                "| --- | --- | --- | --- |",
                "| [gv-graz-access-01](http://testserver/dcim/devices/4/) | GeoView Graz Edge | "
                "GeoView Access Switch | Active |",
                "| [gv-graz-fw-01](http://testserver/dcim/devices/3/) | GeoView Graz Edge | "
                "GeoView Firewall | Active |",
            )
        )
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "search_netbox", {"query": "graz"})]),
                ModelResponse(content="I found two matching devices."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "call-2",
                            "get_object",
                            {"object_type": "dcim.device", "object_id": 4, "fields": ["site", "role", "status"]},
                        ),
                        ModelToolCall(
                            "call-3",
                            "get_object",
                            {"object_type": "dcim.device", "object_id": 3, "fields": ["site", "role", "status"]},
                        ),
                    ]
                ),
                ModelResponse(content=answer),
            ]
        )
        tools = FakeSearchThenDetailToolProvider(GRAZ_DEVICES)
        runtime = AgentRuntime(model, tools)

        result = runtime.run(self.context, [{"role": "user", "content": "Show the matching device status."}])

        self.assertEqual(result.answer, answer)
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual([name for name, _arguments in tools.calls], ["search_netbox", "get_object", "get_object"])
        refinement_messages = [
            message
            for message in model.calls[2][0]
            if message["role"] == "system" and message["content"].startswith("The successful search_netbox result")
        ]
        self.assertEqual(len(refinement_messages), 1)

    def test_repeated_global_search_is_automatically_hydrated_into_a_table(self):
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "search_netbox", {"query": "graz"})]),
                ModelResponse(content="I found three matching objects."),
                ModelResponse(tool_calls=[ModelToolCall("call-2", "search_netbox", {"query": "graz"})]),
                ModelResponse(content="I found the same three matching objects."),
            ]
        )
        tools = FakeSearchThenDetailToolProvider(GRAZ_DEVICES, include_site=True)
        runtime = AgentRuntime(model, tools)

        result = runtime.run(self.context, [{"role": "user", "content": "Show all devices in Graz."}])

        self.assertEqual(result.tool_calls, 4)
        self.assertEqual(
            [name for name, _arguments in tools.calls],
            ["search_netbox", "search_netbox", "get_object", "get_object"],
        )
        self.assertIn("Verified NetBox results (2):", result.answer)
        self.assertIn("| Device | Role | Site | Location | Status |", result.answer)
        self.assertNotIn("[GeoView Graz Edge](/dcim/sites/2/)", result.answer)

    def test_exhausted_model_tool_budget_still_hydrates_discovery_results(self):
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "search_netbox", {"query": "graz"})]),
                ModelResponse(content="I found three matching objects."),
            ]
        )
        tools = FakeSearchThenDetailToolProvider(GRAZ_DEVICES, include_site=True)
        runtime = AgentRuntime(model, tools, max_tool_calls=1)

        result = runtime.run(self.context, [{"role": "user", "content": "Show all devices in Graz."}])

        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(
            [name for name, _arguments in tools.calls],
            ["search_netbox", "get_object", "get_object"],
        )
        self.assertIn("Verified NetBox results (2):", result.answer)
        self.assertIn("| Device | Role | Site | Location | Status |", result.answer)

    def test_identity_only_detail_calls_are_rehydrated_with_useful_fields(self):
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "search_netbox", {"query": "graz"})]),
                ModelResponse(content="I found three matching objects."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "call-2",
                            "get_object",
                            {"object_type": "dcim.device", "object_id": 4, "fields": []},
                        ),
                        ModelToolCall(
                            "call-3",
                            "get_object",
                            {"object_type": "dcim.device", "object_id": 3, "fields": []},
                        ),
                    ]
                ),
                ModelResponse(content="I found the two devices."),
            ]
        )
        tools = FakeSearchThenDetailToolProvider(GRAZ_DEVICES, include_site=True)
        runtime = AgentRuntime(model, tools)

        result = runtime.run(self.context, [{"role": "user", "content": "Show all devices in Graz."}])

        self.assertEqual(result.tool_calls, 5)
        self.assertEqual(
            [name for name, _arguments in tools.calls],
            ["search_netbox", "get_object", "get_object", "get_object", "get_object"],
        )
        self.assertIn("Verified NetBox results (2):", result.answer)
        self.assertIn("| Device | Role | Site | Location | Status |", result.answer)

    def test_followup_table_is_refreshed_with_current_netbox_data(self):
        answer = "\n".join(
            (
                "| Device | Status |",
                "| --- | --- |",
                "| [gv-graz-access-01](http://testserver/dcim/devices/4/) | Active |",
                "| [gv-graz-fw-01](http://testserver/dcim/devices/3/) | Active |",
            )
        )
        model = FakeModelProvider(
            [
                ModelResponse(content=answer),
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        tools = FakeDeviceToolProvider(GRAZ_DEVICES)
        runtime = AgentRuntime(model, tools)

        result = runtime.run(
            self.context,
            [
                {"role": "user", "content": "List the Graz devices."},
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "Show their status too."},
            ],
        )

        self.assertEqual(result.answer, answer)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(tools.calls, [("query_objects", {})])
        refresh_messages = [
            message
            for message in model.calls[1][0]
            if message["role"] == "system" and message["content"].startswith("This turn has no successful")
        ]
        self.assertEqual(len(refresh_messages), 1)

    def test_accepts_ui_variant_of_returned_nested_api_link(self):
        answer = (
            "[gv-graz-access-01](http://testserver/dcim/devices/4/) is at "
            "[GeoView Graz Edge](http://testserver/dcim/sites/2/)."
        )
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        result = runtime.run(self.context, [{"role": "user", "content": "Show the Graz device site."}])

        self.assertEqual(result.answer, answer)

    def test_replaces_fabricated_objects_with_grounded_fallback(self):
        answer = """| Device | Site | Role | Status | Primary IPv4 |
| --- | --- | --- | --- | --- |
| Device01 | Graz | Server | Active | 192.168.1.10 |
| Device02 | Graz | Firewall | Inactive | — |"""
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        result = runtime.run(self.context, [{"role": "user", "content": "List Graz devices."}])

        self.assertIn("[gv-graz-access-01](/dcim/devices/4/)", result.answer)
        self.assertIn("[gv-graz-fw-01](/dcim/devices/3/)", result.answer)
        self.assertIn("| Device | Role | Site | Location | Status |", result.answer)
        self.assertIn("| GeoView Access Switch | GeoView Graz Edge | Network Room 201 | Active |", result.answer)
        self.assertNotIn("Device01", result.answer)
        self.assertNotIn("192.168.1.10", result.answer)

    def test_fallback_ignores_discovery_only_matches_when_details_exist(self):
        search_objects = [
            {
                "id": 2,
                "display": "GeoView Graz Edge",
                "display_url": "http://testserver/dcim/sites/2/",
                "object_type": "dcim.site",
            },
            *(
                {
                    "id": item["id"],
                    "display": item["display"],
                    "display_url": item["display_url"],
                    "object_type": "dcim.device",
                }
                for item in GRAZ_DEVICES
            ),
        ]
        records = AgentRuntime._grounding_records(
            "search_netbox",
            {"ok": True, "result": {"objects": search_objects}},
        )
        for item in GRAZ_DEVICES:
            records.extend(
                AgentRuntime._grounding_records(
                    "get_object",
                    {
                        "ok": True,
                        "result": {"object_type": "dcim.device", "found": True, "object": item},
                    },
                )
            )

        answer = AgentRuntime._grounded_fallback(records)

        self.assertIn("Verified NetBox results (2):", answer)
        self.assertIn("| Device | Role | Site | Location | Status |", answer)
        self.assertNotIn("[GeoView Graz Edge](/dcim/sites/2/)", answer)

    def test_fallback_tables_unique_largest_detailed_object_group(self):
        site = {
            "id": 2,
            "display": "GeoView Graz Edge",
            "display_url": "http://testserver/dcim/sites/2/",
            "name": "GeoView Graz Edge",
            "slug": "geoview-graz-edge",
        }
        records = AgentRuntime._grounding_records(
            "query_objects",
            {"ok": True, "result": {"object_type": "dcim.site", "objects": [site]}},
        )
        records.extend(
            AgentRuntime._grounding_records(
                "query_objects",
                {"ok": True, "result": {"object_type": "dcim.device", "objects": GRAZ_DEVICES}},
            )
        )

        answer = AgentRuntime._grounded_fallback(records)

        self.assertIn("Verified NetBox results (2):", answer)
        self.assertIn("| Device | Role | Site | Location | Status |", answer)
        self.assertIn("[gv-graz-access-01](/dcim/devices/4/)", answer)
        self.assertNotIn("GeoView Graz Edge](/dcim/sites/2/)", answer)

    def test_builds_generic_fallback_table_for_plugin_objects(self):
        objects = [
            {
                "id": 1,
                "display": "Widget A",
                "display_url": "http://testserver/plugins/example/widgets/1/",
                "name": "Widget A",
                "status": {"value": "active", "label": "Active"},
                "category": "Edge",
                "owner": {"id": 7, "display": "Operations"},
            },
            {
                "id": 2,
                "display": "Widget B",
                "display_url": "http://testserver/plugins/example/widgets/2/",
                "name": "Widget B",
                "status": {"value": "planned", "label": "Planned"},
                "category": "Core",
                "owner": {"id": 8, "display": "Engineering"},
            },
        ]
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content="| Widget | Status |\n| --- | --- |\n| Invented | Active |"),
            ]
        )
        tools = FakeDeviceToolProvider(objects, object_type="example_plugin.widget")
        runtime = AgentRuntime(model, tools)

        result = runtime.run(self.context, [{"role": "user", "content": "List widgets."}])

        self.assertIn("| Widget | Status | Category | Owner |", result.answer)
        self.assertIn("[Widget A](/plugins/example/widgets/1/)", result.answer)
        self.assertIn("| Active | Edge | Operations |", result.answer)
        self.assertNotIn("Invented", result.answer)

    def test_replaces_fabricated_value_with_grounded_fallback(self):
        answer = """| Device | Primary IPv4 |
| --- | --- |
| [gv-graz-access-01](http://testserver/dcim/devices/4/) | 192.168.1.10 |"""
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        result = runtime.run(self.context, [{"role": "user", "content": "Show the primary IP."}])

        self.assertIn("[gv-graz-access-01](/dcim/devices/4/)", result.answer)
        self.assertIn("| Device | Role | Site | Location | Status |", result.answer)
        self.assertNotIn("192.168.1.10", result.answer)
        self.assertEqual(result.rejection.reason, RejectionReason.GROUNDING_GUARD)
        self.assertEqual(result.rejection.response, answer)

    def test_translates_grounded_fallback_table(self):
        answer = """| Device | Status |
| --- | --- |
| Device01 | Inactive |"""
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        with override("de"):
            result = runtime.run(self.context, [{"role": "user", "content": "Zeige Geräte in Graz."}])

        self.assertIn("| Gerät | Rolle | Standort | Lokation | Status |", result.answer)
        self.assertIn("Verifizierte NetBox-Ergebnisse (2):", result.answer)

    def test_replaces_unsupported_emphasized_claim_with_grounded_fallback(self):
        answer = """1. **[gv-graz-access-01](http://testserver/dcim/devices/4/)**
2. **[gv-graz-fw-01](http://testserver/dcim/devices/3/)**

Both devices are used in the **GeoView Test Lab** environment."""
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        result = runtime.run(self.context, [{"role": "user", "content": "List Graz devices."}])

        self.assertIn("[gv-graz-access-01](/dcim/devices/4/)", result.answer)
        self.assertIn("[gv-graz-fw-01](/dcim/devices/3/)", result.answer)
        self.assertNotIn("GeoView Test Lab", result.answer)

    def test_rejects_object_table_without_data_result(self):
        answer = """| Device | Status |
| --- | --- |
| Device01 | Active |"""
        runtime = AgentRuntime(
            FakeModelProvider([ModelResponse(content=answer), ModelResponse(content=answer)]),
            FakeToolProvider(),
        )

        with self.assertRaises(UngroundedResponseError):
            runtime.run(self.context, [{"role": "user", "content": "List devices."}])

    def test_rejects_unknown_netbox_object_link(self):
        answer = "See [Device01](/dcim/devices/999/)."
        runtime = AgentRuntime(
            FakeModelProvider([ModelResponse(content=answer), ModelResponse(content=answer)]),
            FakeToolProvider(),
        )

        with self.assertRaises(UngroundedResponseError):
            runtime.run(self.context, [{"role": "user", "content": "Show Device01."}])

    def test_unavailable_tools_are_rejected_and_reported_to_model(self):
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "delete_everything", {})]),
                ModelResponse(content="I cannot perform that operation."),
            ]
        )
        runtime = AgentRuntime(model, FakeToolProvider())

        result = runtime.run(self.context, [{"role": "user", "content": "Delete everything."}])

        tool_result = json.loads(model.calls[1][0][-1]["content"])
        self.assertFalse(tool_result["ok"])
        self.assertIn("not available for the current request", tool_result["error"])
        self.assertEqual(result.tool_calls, 1)

    def test_caps_tool_call_limit_at_ten(self):
        responses = [
            ModelResponse(tool_calls=[ModelToolCall(f"call-{index}", "read_test_data", {})]) for index in range(10)
        ]
        responses.append(ModelResponse(content="Finished at the limit."))
        model = FakeModelProvider(responses)
        tools = FakeToolProvider()
        runtime = AgentRuntime(model, tools, max_tool_calls=99)

        result = runtime.run(self.context, [{"role": "user", "content": "Run several reads."}])

        self.assertEqual(result.tool_calls, 10)
        self.assertEqual(len(tools.calls), 10)
        self.assertEqual(model.calls[-1][1], [])

    def test_rejects_tool_call_after_forced_final_request(self):
        responses = [
            ModelResponse(tool_calls=[ModelToolCall(f"call-{index}", "read_test_data", {})]) for index in range(6)
        ]
        model = FakeModelProvider(responses)
        runtime = AgentRuntime(model, FakeToolProvider(), max_tool_calls=5)

        with self.assertRaises(AgentLimitError):
            runtime.run(self.context, [{"role": "user", "content": "Never stop."}])

    def test_fails_closed_at_limit_when_data_queries_return_no_records(self):
        responses = [
            ModelResponse(tool_calls=[ModelToolCall(f"call-{index}", "query_objects", {})]) for index in range(5)
        ]
        responses.append(ModelResponse(content="There are five devices in Graz."))
        runtime = AgentRuntime(FakeModelProvider(responses), FakeDeviceToolProvider([]), max_tool_calls=5)

        with self.assertRaises(AgentLimitError):
            runtime.run(self.context, [{"role": "user", "content": "List Graz devices."}])

    def test_bounded_tool_output_remains_valid_json(self):
        runtime = AgentRuntime(FakeModelProvider([]), FakeToolProvider(), max_tool_output_chars=512)

        value = runtime._bounded_json({"data": "x" * 5000})

        parsed = json.loads(value)
        self.assertTrue(parsed["truncated"])
        self.assertLessEqual(len(value), 512)

    def test_returns_verified_client_and_pending_actions_separately(self):
        model = FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ModelToolCall("nav", "offer_navigation", {}),
                        ModelToolCall("change", "propose_change", {}),
                    ]
                ),
                ModelResponse(content="The navigation and change preview are ready."),
            ]
        )
        runtime = AgentRuntime(model, FakeActionToolProvider())

        result = runtime.run(self.context, [{"role": "user", "content": "Open and change the device."}])

        self.assertEqual(result.client_actions[0]["url"], "/dcim/devices/4/")
        self.assertEqual(result.pending_actions[0]["operation"], "update")

    def test_reprompts_named_navigation_until_verified_action_is_created(self):
        model = FakeModelProvider(
            [
                ModelResponse(content="I found the requested contact."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "search",
                            "search_netbox",
                            {"query": "Fictional Lab Operations"},
                        )
                    ]
                ),
                ModelResponse(content="The contact was found."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "navigate",
                            "navigate_to_object",
                            {"object_type": "tenancy.contact", "object_id": 7},
                        )
                    ]
                ),
                ModelResponse(content="Opening Fictional Lab Operations."),
            ]
        )
        tools = FakeNavigationToolProvider()

        result = AgentRuntime(model, tools).run(
            self.context,
            [{"role": "user", "content": "Navigiere zu Kontakt Fictional Lab Operations"}],
        )

        self.assertEqual([name for name, _arguments in tools.calls], ["search_netbox", "navigate_to_object"])
        self.assertEqual(result.client_actions[0]["url"], "/tenancy/contacts/7/")
        self.assertTrue(result.client_actions[0]["auto"])
        self.assertEqual(result.tool_calls, 2)

    def test_contextual_navigation_uses_single_previous_target(self):
        model = FakeModelProvider(
            [
                ModelResponse(content="I will open it."),
                ModelResponse(
                    tool_calls=[
                        ModelToolCall(
                            "navigate",
                            "navigate_to_object",
                            {"object_type": "tenancy.contact", "object_id": 7},
                        )
                    ]
                ),
                ModelResponse(content="Opening Fictional Lab Operations."),
            ]
        )
        tools = FakeNavigationToolProvider()

        result = AgentRuntime(model, tools).run(
            self.context,
            [{"role": "user", "content": "Navigiere dahin"}],
            {
                "previous_navigation_targets": [
                    {
                        "object_type": "tenancy.contact",
                        "object_id": 7,
                        "label": "Fictional Lab Operations",
                    }
                ]
            },
        )

        self.assertEqual(tools.calls, [("navigate_to_object", {"object_type": "tenancy.contact", "object_id": 7})])
        self.assertEqual(result.client_actions[0]["label"], "Fictional Lab Operations")

    def test_contextual_navigation_rejects_ambiguous_previous_targets(self):
        model = FakeModelProvider([ModelResponse(content="Which object should I open?")])

        result = AgentRuntime(model, FakeNavigationToolProvider()).run(
            self.context,
            [{"role": "user", "content": "Navigiere dahin"}],
            {
                "previous_navigation_targets": [
                    {"object_type": "dcim.device", "object_id": 1, "label": "device-01"},
                    {"object_type": "dcim.device", "object_id": 2, "label": "device-02"},
                ]
            },
        )

        self.assertEqual(
            result.answer,
            "No unique visible navigation target could be resolved. Please specify the exact NetBox object "
            "and try again.",
        )
        self.assertEqual(result.client_actions, ())

    def test_exposes_answered_object_as_contextual_navigation_target(self):
        device = GRAZ_DEVICES[0]
        answer = f"Found [{device['display']}]({device['display_url']})."
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("query", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )

        result = AgentRuntime(model, FakeDeviceToolProvider([device])).run(
            self.context,
            [{"role": "user", "content": "Find gv-graz-access-01"}],
        )

        self.assertEqual(
            result.navigation_targets,
            (
                {
                    "object_type": "dcim.device",
                    "object_id": device["id"],
                    "label": device["display"],
                },
            ),
        )
