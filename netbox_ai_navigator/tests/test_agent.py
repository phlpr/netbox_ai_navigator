import json
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase
from django.utils.translation import override

from netbox_ai_navigator.agent.runtime import AgentRuntime
from netbox_ai_navigator.exceptions import AgentLimitError, ToolNotFoundError, UngroundedResponseError
from netbox_ai_navigator.model_providers import ModelResponse, ModelToolCall
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


class FakeDeviceToolProvider(ToolProvider):
    def __init__(self, objects):
        self.objects = objects
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
            "object_type": "dcim.device",
            "returned": len(self.objects),
            "limit": 50,
            "objects": self.objects,
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
        self.assertIn("GitHub-style Markdown", system_message["content"])
        self.assertIn("For two or more comparable NetBox objects", system_message["content"])
        self.assertIn("no more than five relevant columns", system_message["content"])
        self.assertIn("Never create a separate Link or URL column", system_message["content"])
        self.assertIn("For device tables", system_message["content"])
        self.assertIn("prefer the `q` filter", system_message["content"])
        self.assertIn("returned numeric `site_id` or `location_id`", system_message["content"])
        self.assertIn("common English or", system_message["content"])
        self.assertIn("localized equivalent", system_message["content"])
        self.assertIn("every other cell value", system_message["content"])
        self.assertIn("Do not add a concluding claim", system_message["content"])
        self.assertIn("Do not emit raw JSON", system_message["content"])

    def test_accepts_table_grounded_in_query_result(self):
        answer = """| Device | Site | Role | Status | Primary IPv4 |
| --- | --- | --- | --- | --- |
| [gv-graz-access-01](http://testserver/dcim/devices/4/) | [GeoView Graz Edge](http://testserver/dcim/sites/2/) | GeoView Access Switch | Active | — |
| [gv-graz-fw-01](http://testserver/dcim/devices/3/) | [GeoView Graz Edge](http://testserver/dcim/sites/2/) | GeoView Firewall | Active | — |"""
        model = FakeModelProvider(
            [
                ModelResponse(tool_calls=[ModelToolCall("call-1", "query_objects", {})]),
                ModelResponse(content=answer),
            ]
        )
        runtime = AgentRuntime(model, FakeDeviceToolProvider(GRAZ_DEVICES))

        result = runtime.run(self.context, [{"role": "user", "content": "List Graz devices."}])

        self.assertEqual(result.answer, answer)

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
        runtime = AgentRuntime(FakeModelProvider([ModelResponse(content=answer)]), FakeToolProvider())

        with self.assertRaises(UngroundedResponseError):
            runtime.run(self.context, [{"role": "user", "content": "List devices."}])

    def test_rejects_unknown_netbox_object_link(self):
        answer = "See [Device01](/dcim/devices/999/)."
        runtime = AgentRuntime(FakeModelProvider([ModelResponse(content=answer)]), FakeToolProvider())

        with self.assertRaises(UngroundedResponseError):
            runtime.run(self.context, [{"role": "user", "content": "Show Device01."}])

    def test_unknown_tools_are_rejected_and_reported_to_model(self):
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
        self.assertIn("Unknown tool", tool_result["error"])
        self.assertEqual(result.tool_calls, 1)

    def test_enforces_five_tool_call_limit(self):
        responses = [
            ModelResponse(tool_calls=[ModelToolCall(f"call-{index}", "read_test_data", {})]) for index in range(5)
        ]
        responses.append(ModelResponse(content="Finished at the limit."))
        model = FakeModelProvider(responses)
        tools = FakeToolProvider()
        runtime = AgentRuntime(model, tools, max_tool_calls=99)

        result = runtime.run(self.context, [{"role": "user", "content": "Run several reads."}])

        self.assertEqual(result.tool_calls, 5)
        self.assertEqual(len(tools.calls), 5)
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
