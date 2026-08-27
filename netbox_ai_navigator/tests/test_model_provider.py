import requests
from django.test import SimpleTestCase

from netbox_ai_navigator.exceptions import ProviderError, ProviderTimeoutError
from netbox_ai_navigator.model_providers import OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, data=None, status_code=200, *, content=None, headers=None):
        self.data = data
        self.status_code = status_code
        self.content = (b"" if data is None else b"json") if content is None else content
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self.data

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, *, responses=None, error=None):
        self.response = response
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return self.response

class OpenAICompatibleProviderTest(SimpleTestCase):
    config = {
        "base_url": "https://model.example/v1/",
        "api_key": "server-secret",
        "model": "test-model",
        "timeout": 12,
        "temperature": 0.2,
        "max_tokens": 321,
    }

    def test_sends_chat_completion_and_parses_tool_call(self):
        session = FakeSession(
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "query_objects", "arguments": '{"limit":1}'},
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        )
        provider = OpenAICompatibleProvider(self.config, session=session)
        tools = [{"type": "function", "function": {"name": "query_objects"}}]

        result = provider.complete([{"role": "user", "content": "Hello"}], tools)

        url, request = session.calls[0]
        self.assertEqual(url, "https://model.example/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer server-secret")
        self.assertEqual(request["timeout"], 12)
        self.assertFalse(request["allow_redirects"])
        self.assertTrue(request["stream"])
        self.assertEqual(request["json"]["tools"], tools)
        self.assertEqual(result.tool_calls[0].name, "query_objects")

    def test_provider_error_does_not_expose_response_body(self):
        session = FakeSession(FakeResponse({"secret": "NetBox data"}, status_code=500))
        provider = OpenAICompatibleProvider(self.config, session=session)

        with self.assertRaisesMessage(ProviderError, "HTTP 500") as raised:
            provider.complete([{"role": "user", "content": "Hello"}], [])
        self.assertNotIn("NetBox data", str(raised.exception))

    def test_timeout_is_controlled(self):
        provider = OpenAICompatibleProvider(self.config, session=FakeSession(error=requests.Timeout()))

        with self.assertRaises(ProviderTimeoutError):
            provider.complete([{"role": "user", "content": "Hello"}], [])

    def test_rejects_oversized_provider_response(self):
        session = FakeSession(FakeResponse({"choices": []}, content=b"x" * 101))
        provider = OpenAICompatibleProvider({**self.config, "max_http_response_bytes": 100}, session=session)

        with self.assertRaisesMessage(ProviderError, "oversized response"):
            provider.complete([{"role": "user", "content": "Hello"}], [])
        self.assertTrue(session.response.closed)

    def test_rejects_plain_http_for_remote_provider_without_opt_in(self):
        with self.assertRaisesMessage(ProviderError, "Plain HTTP"):
            OpenAICompatibleProvider({**self.config, "base_url": "http://model.internal/v1"})

    def test_sends_responses_request_with_custom_header_and_parses_text(self):
        session = FakeSession(
            FakeResponse(
                {
                    "id": "resp-1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Hallo aus Responses"}],
                        }
                    ],
                }
            )
        )
        provider = OpenAICompatibleProvider(
            {
                **self.config,
                "protocol": "responses",
                "extra_headers": {"deployment-id": "test-deployment"},
                "temperature": None,
            },
            session=session,
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "query_objects",
                    "description": "Query objects",
                    "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
                    "strict": True,
                },
            }
        ]

        result = provider.complete(
            [
                {"role": "system", "content": "Use NetBox tools."},
                {"role": "user", "content": "List devices"},
            ],
            tools,
        )

        url, request = session.calls[0]
        self.assertEqual(url, "https://model.example/v1/responses")
        self.assertEqual(request["headers"]["deployment-id"], "test-deployment")
        self.assertEqual(request["headers"]["Authorization"], "Bearer server-secret")
        self.assertEqual(
            request["json"]["input"],
            [
                {"role": "system", "content": "Use NetBox tools."},
                {"role": "user", "content": "List devices"},
            ],
        )
        self.assertEqual(
            request["json"]["tools"],
            [
                {
                    "type": "function",
                    "name": "query_objects",
                    "description": "Query objects",
                    "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
                    "strict": True,
                }
            ],
        )
        self.assertEqual(request["json"]["max_output_tokens"], 321)
        self.assertFalse(request["json"]["store"])
        self.assertEqual(request["json"]["include"], ["reasoning.encrypted_content"])
        self.assertNotIn("temperature", request["json"])
        self.assertEqual(result.content, "Hallo aus Responses")

    def test_responses_tool_result_replays_opaque_output_without_provider_storage(self):
        session = FakeSession(
            responses=[
                FakeResponse(
                    {
                        "id": "resp-tool",
                        "status": "completed",
                        "output": [
                            {
                                "id": "reasoning-1",
                                "type": "reasoning",
                                "status": "completed",
                                "encrypted_content": "opaque-reasoning",
                                "summary": [],
                            },
                            {
                                "id": "fc-1",
                                "type": "function_call",
                                "status": "completed",
                                "call_id": "call-1",
                                "name": "query_objects",
                                "arguments": '{"limit":1}',
                            }
                        ],
                    }
                ),
                FakeResponse(
                    {
                        "id": "resp-final",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "Ein Gerät gefunden."}],
                            }
                        ],
                    }
                ),
            ]
        )
        provider = OpenAICompatibleProvider({**self.config, "protocol": "responses"}, session=session)
        messages = [{"role": "user", "content": "List devices"}]

        first = provider.complete(messages, [])
        messages.append(first.as_assistant_message())
        messages.append(
            {
                "role": "tool",
                "tool_call_id": first.tool_calls[0].id,
                "content": '{"ok":true,"result":{"name":"router-1"}}',
            }
        )
        second = provider.complete(messages, [])

        self.assertEqual(first.tool_calls[0].id, "call-1")
        self.assertEqual(first.tool_calls[0].arguments, '{"limit":1}')
        follow_up = session.calls[1][1]["json"]
        self.assertEqual(
            follow_up["input"],
            [
                {"role": "user", "content": "List devices"},
                {
                    "id": "reasoning-1",
                    "type": "reasoning",
                    "encrypted_content": "opaque-reasoning",
                    "summary": [],
                },
                {
                    "id": "fc-1",
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "query_objects",
                    "arguments": '{"limit":1}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": '{"ok":true,"result":{"name":"router-1"}}',
                }
            ],
        )
        self.assertFalse(follow_up["store"])
        self.assertEqual(follow_up["include"], ["reasoning.encrypted_content"])
        self.assertEqual(second.content, "Ein Gerät gefunden.")

    def test_responses_follow_up_replays_output_and_appends_new_system_instruction(self):
        session = FakeSession(
            responses=[
                FakeResponse(
                    {
                        "id": "resp-1",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "Unverified answer"}],
                            }
                        ],
                    }
                ),
                FakeResponse(
                    {
                        "id": "resp-2",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "Refined answer"}],
                            }
                        ],
                    }
                ),
            ]
        )
        provider = OpenAICompatibleProvider({**self.config, "protocol": "responses"}, session=session)
        messages = [{"role": "user", "content": "List devices"}]

        provider.complete(messages, [])
        messages.append({"role": "system", "content": "Use a data tool now."})
        result = provider.complete(messages, [])

        self.assertEqual(
            session.calls[1][1]["json"]["input"],
            [
                {"role": "user", "content": "List devices"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Unverified answer"}],
                },
                {"role": "system", "content": "Use a data tool now."},
            ],
        )
        self.assertNotIn("previous_response_id", session.calls[1][1]["json"])
        self.assertEqual(result.content, "Refined answer")

    def test_rejects_reserved_or_injected_extra_headers(self):
        with self.assertRaisesMessage(ProviderError, "reserved"):
            OpenAICompatibleProvider({**self.config, "extra_headers": {"Authorization": "replacement"}})
        with self.assertRaisesMessage(ProviderError, "line breaks"):
            OpenAICompatibleProvider({**self.config, "extra_headers": {"deployment-id": "safe\r\ninjected"}})
