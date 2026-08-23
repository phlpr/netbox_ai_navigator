import json

import requests
from django.test import SimpleTestCase

from netbox_ai_navigator.exceptions import ProviderError, ProviderTimeoutError
from netbox_ai_navigator.model_providers import MyGPTApiProvider, OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, data=None, status_code=200):
        self.data = data
        self.status_code = status_code
        self.content = b"" if data is None else b"json"

    def json(self):
        return self.data


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


class MyGPTApiProviderTest(SimpleTestCase):
    channel_id = "1f708a24-f8bc-4e5d-ba4b-942b5aae71da"
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
