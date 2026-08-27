import json

import requests
from django.test import SimpleTestCase

from netbox_ai_navigator.exceptions import ProviderError, ProviderTimeoutError
from netbox_ai_navigator.model_providers import MyGPTApiProvider, OpenAICompatibleProvider


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
        self.request_calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return self.response

    def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
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


class MyGPTApiProviderTest(SimpleTestCase):
    channel_id = "00000000-0000-4000-8000-000000000001"
    config = {
        "api_url": "https://api.myg.pt/api/v1/",
        "tenant": "test-tenant",
        "service_user": "service@example.test",
        "service_password": "server-secret",
        "channel_id": channel_id,
        "delete_conversations": True,
        "model": "mygpt-service-channel",
        "timeout": 15,
    }

    @staticmethod
    def successful_session(message_payload):
        return FakeSession(
            responses=[
                FakeResponse({"access_token": "jwt-token"}),
                FakeResponse({"id": "conversation-1", "channel_id": MyGPTApiProviderTest.channel_id}, 201),
                FakeResponse({"message": {"payload": message_payload, "origin": "assistant"}}, 201),
                FakeResponse(None, 204),
            ]
        )

    def test_uses_documented_endpoints_and_deletes_conversation_on_reset(self):
        session = self.successful_session("Hallo aus MyGPT")
        provider = MyGPTApiProvider(self.config, session=session)

        result = provider.complete([{"role": "user", "content": "Hello"}], [])

        self.assertEqual(provider.conversation_id, "conversation-1")
        self.assertEqual(len(session.request_calls), 3)
        provider.delete_conversation()

        self.assertEqual(result.content, "Hallo aus MyGPT")
        self.assertEqual(
            [(method, url) for method, url, _ in session.request_calls],
            [
                ("POST", "https://api.myg.pt/api/v1/login"),
                ("POST", "https://api.myg.pt/api/v1/conversations"),
                ("POST", "https://api.myg.pt/api/v1/messages"),
                ("DELETE", "https://api.myg.pt/api/v1/conversations/conversation-1"),
            ],
        )
        login_request = session.request_calls[0][2]
        self.assertFalse(login_request["allow_redirects"])
        self.assertTrue(login_request["stream"])
        self.assertEqual(
            login_request["json"],
            {
                "tenant_id": "test-tenant",
                "email": "service@example.test",
                "password": "server-secret",
            },
        )
        conversation_request = session.request_calls[1][2]
        self.assertEqual(conversation_request["json"]["channel_id"], self.channel_id)
        message_request = session.request_calls[2][2]
        self.assertEqual(message_request["headers"]["Authorization"], "Bearer jwt-token")
        self.assertEqual(message_request["json"]["channel_id"], self.channel_id)
        self.assertEqual(message_request["json"]["conversation_id"], "conversation-1")
        self.assertNotIn("server-secret", message_request["json"]["payload"])
        self.assertIsNone(provider.conversation_id)

    def test_reuses_an_existing_session_conversation(self):
        session = FakeSession(
            responses=[
                FakeResponse({"access_token": "jwt-token"}),
                FakeResponse({"message": {"payload": "Second answer", "origin": "assistant"}}, 201),
                FakeResponse(None, 204),
            ]
        )
        provider = MyGPTApiProvider(self.config, session=session, conversation_id="conversation-existing")

        result = provider.complete([{"role": "user", "content": "Continue"}], [])
        provider.delete_conversation()

        self.assertEqual(result.content, "Second answer")
        self.assertEqual(
            [(method, url) for method, url, _ in session.request_calls],
            [
                ("POST", "https://api.myg.pt/api/v1/login"),
                ("POST", "https://api.myg.pt/api/v1/messages"),
                ("DELETE", "https://api.myg.pt/api/v1/conversations/conversation-existing"),
            ],
        )
        self.assertEqual(session.request_calls[1][2]["json"]["conversation_id"], "conversation-existing")

    def test_emulates_tool_call_with_strict_json_protocol(self):
        session = self.successful_session(
            json.dumps(
                {
                    "type": "tool_calls",
                    "calls": [{"name": "query_objects", "arguments": {"model": "dcim.site", "limit": 1}}],
                }
            )
        )
        provider = MyGPTApiProvider(self.config, session=session)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "query_objects",
                    "description": "Query NetBox objects.",
                    "parameters": {"type": "object", "properties": {"model": {"type": "string"}}},
                },
            }
        ]

        result = provider.complete([{"role": "user", "content": "List sites"}], tools)

        self.assertIsNone(result.content)
        self.assertEqual(result.tool_calls[0].name, "query_objects")
        self.assertEqual(result.tool_calls[0].arguments, {"model": "dcim.site", "limit": 1})
        prompt = session.request_calls[2][2]["json"]["payload"]
        self.assertIn('"name":"query_objects"', prompt)
        self.assertIn('"role":"user"', prompt)

    def test_parses_wrapped_and_double_encoded_protocol_json(self):
        expected = {
            "type": "tool_calls",
            "calls": [{"name": "query_objects", "arguments": {"limit": 2}}],
        }
        wrapped = f"Reasoning omitted.\n```json\n{json.dumps(expected)}\n```"
        double_encoded = json.dumps(json.dumps(expected))

        self.assertEqual(MyGPTApiProvider._parse_protocol_json(wrapped), expected)
        self.assertEqual(MyGPTApiProvider._parse_protocol_json(double_encoded), expected)

    def test_retries_one_malformed_tool_protocol_response(self):
        valid_protocol = json.dumps(
            {
                "type": "tool_calls",
                "calls": [{"name": "query_objects", "arguments": {"limit": 2}}],
            }
        )
        session = FakeSession(
            responses=[
                FakeResponse({"access_token": "jwt-token"}),
                FakeResponse({"id": "conversation-1", "channel_id": self.channel_id}, 201),
                FakeResponse({"message": {"payload": "I should query NetBox first."}}, 201),
                FakeResponse({"message": {"payload": valid_protocol}}, 201),
            ]
        )
        provider = MyGPTApiProvider(self.config, session=session)
        tools = [{"type": "function", "function": {"name": "query_objects"}}]

        result = provider.complete([{"role": "user", "content": "List devices"}], tools)

        self.assertEqual(result.tool_calls[0].name, "query_objects")
        self.assertEqual(len(session.request_calls), 4)
        repair_payload = session.request_calls[-1][2]["json"]["payload"]
        self.assertIn("previous response could not be parsed", repair_payload)
        self.assertIn("Allowed tool names: query_objects", repair_payload)

    def test_controller_prompt_reflects_available_write_tools(self):
        read_tools = [{"type": "function", "function": {"name": "query_objects"}}]
        write_tools = [
            *read_tools,
            {"type": "function", "function": {"name": "propose_update_object"}},
        ]

        read_prompt = MyGPTApiProvider._build_prompt([{"role": "user", "content": "List sites"}], read_tools)
        write_prompt = MyGPTApiProvider._build_prompt([{"role": "user", "content": "Update a site"}], write_tools)

        self.assertIn("This session is read-only because no proposal tools are listed.", read_prompt)
        self.assertIn("This session supports staged change proposals", write_prompt)
        self.assertNotIn("controller for the read-only NetBox AI Navigator", write_prompt)

    def test_parses_final_protocol_response(self):
        session = self.successful_session('{"type":"final","content":"There is one site."}')
        provider = MyGPTApiProvider(self.config, session=session)
        tools = [{"type": "function", "function": {"name": "query_objects"}}]

        result = provider.complete([{"role": "user", "content": "List sites"}], tools)

        self.assertEqual(result.content, "There is one site.")
        self.assertEqual(result.tool_calls, [])

    def test_deletes_conversation_after_message_failure_without_exposing_body(self):
        session = FakeSession(
            responses=[
                FakeResponse({"access_token": "jwt-token"}),
                FakeResponse({"id": "conversation-1", "channel_id": self.channel_id}, 201),
                FakeResponse({"secret": "NetBox data"}, 500),
                FakeResponse(None, 204),
            ]
        )
        provider = MyGPTApiProvider(self.config, session=session)

        with self.assertRaisesMessage(ProviderError, "HTTP 500") as raised:
            provider.complete([{"role": "user", "content": "Hello"}], [])

        self.assertNotIn("NetBox data", str(raised.exception))
        self.assertEqual(session.request_calls[-1][0], "DELETE")

    def test_cleanup_failure_is_reported(self):
        session = FakeSession(
            responses=[
                FakeResponse({"access_token": "jwt-token"}),
                FakeResponse({"id": "conversation-1", "channel_id": self.channel_id}, 201),
                FakeResponse({"message": {"payload": "Hello"}}, 201),
                FakeResponse(None, 503),
            ]
        )
        provider = MyGPTApiProvider(self.config, session=session)

        provider.complete([{"role": "user", "content": "Hello"}], [])
        with self.assertRaisesMessage(ProviderError, "HTTP 503"):
            provider.delete_conversation()
        self.assertEqual(provider.conversation_id, "conversation-1")

    def test_requires_service_credentials(self):
        with self.assertRaisesMessage(ProviderError, "service user is not configured"):
            MyGPTApiProvider({**self.config, "service_user": None})
        with self.assertRaisesMessage(ProviderError, "service password is not configured"):
            MyGPTApiProvider({**self.config, "service_password": None})

    def test_timeout_is_controlled(self):
        provider = MyGPTApiProvider(self.config, session=FakeSession(error=requests.Timeout()))

        with self.assertRaises(ProviderTimeoutError):
            provider.complete([{"role": "user", "content": "Hello"}], [])
