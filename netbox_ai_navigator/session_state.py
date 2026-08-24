import json
import time
import uuid
from typing import Any

MYGPT_CONVERSATION_SESSION_KEY = "netbox_ai_navigator_mygpt_conversation_id"
# This is a session dictionary key, not a hardcoded authentication token.
BROWSER_STORAGE_TOKEN_SESSION_KEY = "netbox_ai_navigator_browser_storage_token"  # nosec B105
PENDING_ACTIONS_SESSION_KEY = "netbox_ai_navigator_pending_actions"


def get_mygpt_conversation_id(request) -> str | None:
    session = getattr(request, "session", None)
    if session is None:
        return None
    value = session.get(MYGPT_CONVERSATION_SESSION_KEY)
    return value if isinstance(value, str) and value else None


def set_mygpt_conversation_id(request, conversation_id: str) -> None:
    session = getattr(request, "session", None)
    if session is not None:
        session[MYGPT_CONVERSATION_SESSION_KEY] = conversation_id


def clear_mygpt_conversation_id(request) -> None:
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(MYGPT_CONVERSATION_SESSION_KEY, None)


def get_or_create_browser_storage_token(request) -> str:
    session = getattr(request, "session", None)
    if session is None:
        return uuid.uuid4().hex
    value = session.get(BROWSER_STORAGE_TOKEN_SESSION_KEY)
    if not isinstance(value, str) or not value:
        value = uuid.uuid4().hex
        session[BROWSER_STORAGE_TOKEN_SESSION_KEY] = value
    return value


def rotate_browser_storage_token(request) -> str:
    value = uuid.uuid4().hex
    session = getattr(request, "session", None)
    if session is not None:
        session[BROWSER_STORAGE_TOKEN_SESSION_KEY] = value
    return value


def store_pending_action(request, action: dict[str, Any], *, max_pending: int = 5) -> dict[str, Any]:
    session = getattr(request, "session", None)
    if session is None:
        raise RuntimeError("A session is required for change approval.")
    # Django's JSON session serializer cannot encode lazy translation proxies or
    # serializer-specific scalar types. Persist only a plain JSON representation.
    serializable_action = json.loads(json.dumps(action, ensure_ascii=False, default=str))
    pending = session.get(PENDING_ACTIONS_SESSION_KEY)
    if not isinstance(pending, dict):
        pending = {}
    pending = {key: value for key, value in pending.items() if isinstance(key, str) and isinstance(value, dict)}
    action_id = uuid.uuid4().hex
    pending[action_id] = {"created_at": time.time(), "action": serializable_action}
    ordered = sorted(pending.items(), key=lambda item: float(item[1].get("created_at", 0)), reverse=True)
    session[PENDING_ACTIONS_SESSION_KEY] = dict(ordered[: max(1, min(max_pending, 10))])
    return {
        "action_id": action_id,
        "type": "change_approval",
        "operation": serializable_action.get("operation"),
        "object_type": serializable_action.get("object_type"),
        "object_id": serializable_action.get("object_id"),
        "title": serializable_action.get("title"),
        "target": serializable_action.get("target"),
        "changes": (serializable_action.get("changes") if isinstance(serializable_action.get("changes"), list) else []),
    }


def pop_pending_action(request, action_id: str, *, ttl: int = 600) -> dict[str, Any] | None:
    session = getattr(request, "session", None)
    if session is None:
        return None
    pending = session.get(PENDING_ACTIONS_SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    value = pending.pop(action_id, None)
    session[PENDING_ACTIONS_SESSION_KEY] = pending
    try:
        created_at = float(value.get("created_at", 0)) if isinstance(value, dict) else 0
    except (TypeError, ValueError):
        created_at = 0
    if not isinstance(value, dict) or time.time() - created_at > ttl:
        return None
    action = value.get("action")
    return action if isinstance(action, dict) else None


def discard_pending_action(request, action_id: str) -> bool:
    session = getattr(request, "session", None)
    if session is None:
        return False
    pending = session.get(PENDING_ACTIONS_SESSION_KEY)
    if not isinstance(pending, dict) or action_id not in pending:
        return False
    pending.pop(action_id, None)
    session[PENDING_ACTIONS_SESSION_KEY] = pending
    return True


def clear_pending_actions(request) -> None:
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(PENDING_ACTIONS_SESSION_KEY, None)
