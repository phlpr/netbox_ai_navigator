import json
import logging
import time
from typing import Any

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from .agent import build_agent_runtime
from .config import get_plugin_settings, user_can_read_assistant
from .exceptions import AgentLimitError, InvalidRequestError, ProviderError, ProviderTimeoutError
from .model_providers import MyGPTApiProvider
from .session_state import (
    clear_mygpt_conversation_id,
    get_mygpt_conversation_id,
    set_mygpt_conversation_id,
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
            context = ToolContext(
                request=request,
                user=request.user,
                current_object_type=page_context.get("object_type"),
                current_object_id=page_context.get("object_id"),
            )
            runtime = build_agent_runtime(
                plugin_settings,
                conversation_id=get_mygpt_conversation_id(request),
            )
            result = runtime.run(context, payload.get("messages"), page_context)
            tool_calls = result.tool_calls
            status = "ok"
            response = JsonResponse({"answer": result.answer, "tool_calls": tool_calls})
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
        response = JsonResponse({"cleared": True})
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
