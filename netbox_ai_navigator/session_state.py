import json
import time
import uuid
from typing import Any

# This is a session dictionary key, not a hardcoded authentication token.
BROWSER_STORAGE_TOKEN_SESSION_KEY = "netbox_ai_navigator_browser_storage_token"  # nosec B105
PENDING_ACTIONS_SESSION_KEY = "netbox_ai_navigator_pending_actions"
NAVIGATION_TARGETS_SESSION_KEY = "netbox_ai_navigator_navigation_targets"


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


def get_navigation_targets(request) -> list[dict[str, Any]]:
    session = getattr(request, "session", None)
    if session is None:
        return []
    targets = session.get(NAVIGATION_TARGETS_SESSION_KEY)
    if not isinstance(targets, list):
        return []
    validated = []
    for target in targets[:20]:
        if not isinstance(target, dict):
            continue
        object_type = target.get("object_type")
        object_id = target.get("object_id")
        label = target.get("label")
        if (
            not isinstance(object_type, str)
            or not 1 <= len(object_type) <= 100
            or isinstance(object_id, bool)
            or not isinstance(object_id, int)
            or object_id < 1
            or not isinstance(label, str)
            or not 1 <= len(label) <= 500
        ):
            continue
        validated.append({"object_type": object_type, "object_id": object_id, "label": label})
    return validated


def store_navigation_targets(request, targets: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
    session = getattr(request, "session", None)
    if session is None:
        return
    serializable = json.loads(json.dumps(list(targets)[:20], ensure_ascii=False, default=str))
    session[NAVIGATION_TARGETS_SESSION_KEY] = [target for target in serializable if isinstance(target, dict)]


def clear_navigation_targets(request) -> None:
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(NAVIGATION_TARGETS_SESSION_KEY, None)
