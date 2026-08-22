import uuid

MYGPT_CONVERSATION_SESSION_KEY = "netbox_ai_navigator_mygpt_conversation_id"
BROWSER_STORAGE_TOKEN_SESSION_KEY = "netbox_ai_navigator_browser_storage_token"


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
