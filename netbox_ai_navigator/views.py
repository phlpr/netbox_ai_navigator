import json
import logging
import time
from typing import Any

from django.http import JsonResponse
from django.urls import resolve
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from rest_framework.test import APIRequestFactory, force_authenticate

from .agent import build_agent_runtime
from .config import get_plugin_settings, user_can_read_assistant, user_can_write_assistant
from .exceptions import AgentLimitError, InvalidRequestError, ProviderError, ProviderTimeoutError
from .model_providers import MyGPTApiProvider
from .session_state import (
    clear_mygpt_conversation_id,
    clear_pending_actions,
    discard_pending_action,
    get_mygpt_conversation_id,
    pop_pending_action,
    set_mygpt_conversation_id,
    store_pending_action,
)
from .tool_providers import ToolContext

logger = logging.getLogger("netbox.plugins.netbox_ai_navigator.views")

MAX_REQUEST_BYTES = 256_000


@method_decorator(never_cache, name="dispatch")
class ChatView(View):
    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_active:
            return self._json_error("Authentication is required.", status=401)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        started = time.monotonic()
        status = "error"
        tool_calls = 0
        runtime = None
        plugin_settings = get_plugin_settings()
        model_name = str(plugin_settings["model"].get("model", ""))

        if not user_can_read_assistant(request.user, plugin_settings):
            return self._json_error("The assistant is not available for this user.", status=403)
        if request.content_type != "application/json":
            return self._json_error("Content-Type must be application/json.", status=415)

        try:
            content_length = int(request.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length > MAX_REQUEST_BYTES:
            return self._json_error("The request body is too large.", status=413)

        try:
            body = request.body
            if len(body) > MAX_REQUEST_BYTES:
                return self._json_error("The request body is too large.", status=413)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise InvalidRequestError("The request body must be a JSON object.")
            page_context = self._sanitize_page_context(payload.get("page_context"))
            can_write = user_can_write_assistant(request.user, plugin_settings)
            context = ToolContext(
                request=request,
                user=request.user,
                current_object_type=page_context.get("object_type"),
                current_object_id=page_context.get("object_id"),
                can_write=can_write,
            )
            runtime = build_agent_runtime(
                plugin_settings,
                conversation_id=get_mygpt_conversation_id(request),
            )
            result = runtime.run(context, payload.get("messages"), page_context)
            tool_calls = result.tool_calls
            write_config = plugin_settings["tools"].get("write") or {}
            pending_actions = [
                store_pending_action(
                    request,
                    action,
                    max_pending=int(write_config.get("max_pending", 5)),
                )
                for action in result.pending_actions
            ]
            status = "ok"
            response = JsonResponse(
                {
                    "answer": result.answer,
                    "tool_calls": tool_calls,
                    "client_actions": list(result.client_actions),
                    "pending_actions": pending_actions,
                    "can_write": can_write,
                }
            )
            response["Cache-Control"] = "no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json_error("The request body contains invalid JSON.", status=400)
        except InvalidRequestError as exc:
            return self._json_error(str(exc), status=400)
        except AgentLimitError as exc:
            return self._json_error(str(exc), status=422)
        except ProviderTimeoutError as exc:
            return self._json_error(str(exc), status=504)
        except ProviderError as exc:
            return self._json_error(str(exc), status=502)
        except Exception:
            logger.exception("Unhandled assistant request failure")
            return self._json_error("The assistant failed unexpectedly.", status=500)
        finally:
            self._persist_conversation_id(request, runtime)
            logger.info(
                "Assistant request completed",
                extra={
                    "username": request.user.get_username(),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "model": model_name,
                    "tool_calls": tool_calls,
                    "assistant_status": status,
                },
            )

    @staticmethod
    def _persist_conversation_id(request, runtime) -> None:
        provider = getattr(runtime, "model_provider", None)
        if isinstance(provider, MyGPTApiProvider) and provider.conversation_id:
            set_mygpt_conversation_id(request, provider.conversation_id)

    @staticmethod
    def _sanitize_page_context(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise InvalidRequestError("page_context must be a JSON object.")

        result: dict[str, Any] = {}
        object_type = value.get("object_type")
        if object_type is not None:
            if not isinstance(object_type, str) or not 1 <= len(object_type) <= 100:
                raise InvalidRequestError("page_context.object_type is invalid.")
            result["object_type"] = object_type.lower()

        object_id = value.get("object_id")
        if object_id is not None:
            if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id < 1:
                raise InvalidRequestError("page_context.object_id is invalid.")
            result["object_id"] = object_id

        page_url = value.get("url")
        if page_url is not None:
            if (
                not isinstance(page_url, str)
                or not page_url.startswith("/")
                or page_url.startswith("//")
                or len(page_url) > 2048
            ):
                raise InvalidRequestError("page_context.url is invalid.")
            result["url"] = page_url

        title = value.get("title")
        if title is not None:
            if not isinstance(title, str) or len(title) > 300:
                raise InvalidRequestError("page_context.title is invalid.")
            result["title"] = title
        return result

    @staticmethod
    def _json_error(message: str, *, status: int) -> JsonResponse:
        response = JsonResponse({"error": message}, status=status)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ResetConversationView(ChatView):
    def post(self, request):
        plugin_settings = get_plugin_settings()
        if not user_can_read_assistant(request.user, plugin_settings):
            return self._json_error("The assistant is not available for this user.", status=403)

        conversation_id = get_mygpt_conversation_id(request)
        if conversation_id and plugin_settings["model"].get("provider") == "mygpt_api":
            try:
                provider = MyGPTApiProvider(plugin_settings["model"], conversation_id=conversation_id)
                provider.delete_conversation()
            except ProviderTimeoutError as exc:
                return self._json_error(str(exc), status=504)
            except ProviderError as exc:
                return self._json_error(str(exc), status=502)

        clear_mygpt_conversation_id(request)
        clear_pending_actions(request)
        response = JsonResponse(
            {
                "cleared": True,
                "can_write": user_can_write_assistant(request.user, plugin_settings),
            }
        )
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ChangeApprovalView(ChatView):
    """Execute one short-lived, server-stored proposal through its NetBox REST ViewSet."""

    def post(self, request):
        plugin_settings = get_plugin_settings()
        if not user_can_write_assistant(request.user, plugin_settings):
            return self._json_error("Write access is not available for this user.", status=403)
        if request.content_type != "application/json":
            return self._json_error("Content-Type must be application/json.", status=415)
        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json_error("The request body contains invalid JSON.", status=400)
        if not isinstance(payload, dict):
            return self._json_error("The request body must be a JSON object.", status=400)
        action_id = payload.get("action_id")
        decision = payload.get("decision")
        if not isinstance(action_id, str) or len(action_id) != 32 or decision not in {"confirm", "cancel"}:
            return self._json_error("The approval request is invalid.", status=400)
        if decision == "cancel":
            discarded = discard_pending_action(request, action_id)
            return self._json_response({"cancelled": discarded})

        write_config = plugin_settings["tools"].get("write") or {}
        action = pop_pending_action(
            request,
            action_id,
            ttl=int(write_config.get("approval_ttl", 600)),
        )
        if action is None:
            return self._json_error("The change proposal has expired or was already used.", status=410)

        started = time.monotonic()
        operation = str(action.get("operation") or "unknown")
        try:
            api_response = self._execute_api_action(request, action)
            response_data = self._json_safe(getattr(api_response, "data", None))
            if not 200 <= api_response.status_code < 300:
                logger.warning(
                    "Approved assistant change rejected by NetBox API",
                    extra={
                        "username": request.user.get_username(),
                        "operation": operation,
                        "api_status": api_response.status_code,
                    },
                )
                return self._json_response(
                    {"error": "NetBox rejected the approved change.", "details": response_data},
                    status=api_response.status_code,
                )
            result = {
                "executed": True,
                "operation": operation,
                "object_type": action.get("object_type"),
                "object_id": action.get("object_id"),
            }
            if isinstance(response_data, dict):
                result["object_id"] = response_data.get("id", result["object_id"])
                result["display"] = response_data.get("display") or response_data.get("name")
                display_url = response_data.get("display_url")
                if isinstance(display_url, str):
                    result["display_url"] = display_url
            logger.info(
                "Approved assistant change executed",
                extra={
                    "username": request.user.get_username(),
                    "operation": operation,
                    "object_type": action.get("object_type"),
                    "object_id": result.get("object_id"),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            return self._json_response(result)
        except InvalidRequestError as exc:
            return self._json_error(str(exc), status=400)
        except Exception:
            logger.exception("Approved assistant change failed unexpectedly", extra={"operation": operation})
            return self._json_error("The approved change failed unexpectedly.", status=500)

    @staticmethod
    def _execute_api_action(request, action: dict[str, Any]):
        method = action.get("method")
        endpoint = action.get("endpoint")
        payload = action.get("payload")
        etag = action.get("etag")
        if (
            method not in {"POST", "PATCH", "DELETE"}
            or not isinstance(endpoint, str)
            or not endpoint.startswith("/api/")
            or endpoint.startswith("//")
            or "?" in endpoint
            or not isinstance(payload, dict)
            or (etag is not None and not isinstance(etag, str))
        ):
            raise InvalidRequestError("The stored change proposal is invalid.")
        match = resolve(endpoint)
        if method.lower() not in getattr(match.func, "actions", {}):
            raise InvalidRequestError("The stored REST API action is not supported.")

        factory = APIRequestFactory()
        api_request = factory.generic(
            method,
            endpoint,
            data=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
            HTTP_IF_MATCH=etag or "",
            HTTP_HOST=request.get_host(),
        )
        force_authenticate(api_request, user=request.user)
        return match.func(api_request, *match.args, **match.kwargs)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _json_response(payload: dict[str, Any], *, status: int = 200) -> JsonResponse:
        response = JsonResponse(payload, status=status)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
