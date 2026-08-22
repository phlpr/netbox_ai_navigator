import json
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase

from netbox_ai_navigator.agent.runtime import AgentRuntime
from netbox_ai_navigator.exceptions import AgentLimitError, ToolNotFoundError
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
        self.assertIn("no more than five relevant columns", system_message["content"])
        self.assertIn("Never create a separate Link or URL column", system_message["content"])
        self.assertIn("For device tables", system_message["content"])
        self.assertIn("Do not emit raw JSON", system_message["content"])

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

    def test_bounded_tool_output_remains_valid_json(self):
        runtime = AgentRuntime(FakeModelProvider([]), FakeToolProvider(), max_tool_output_chars=512)

        value = runtime._bounded_json({"data": "x" * 5000})

        parsed = json.loads(value)
        self.assertTrue(parsed["truncated"])
        self.assertLessEqual(len(value), 512)
