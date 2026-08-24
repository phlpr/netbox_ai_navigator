import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit


class ProviderResponseTooLargeError(ValueError):
    pass


def normalize_provider_url(value: Any, *, allow_insecure_http: bool = False) -> str:
    """Validate and normalize a configured model-provider base URL."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A provider URL is required.")
    normalized = value.strip().rstrip("/")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Provider URLs may not contain control characters.")

    try:
        parsed = urlsplit(normalized)
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("The provider URL is invalid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider URLs must use HTTP or HTTPS and include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Provider URLs may not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Provider URLs may not contain a query string or fragment.")
    if parsed.scheme == "http" and not allow_insecure_http and not _is_loopback_host(parsed.hostname):
        raise ValueError("Plain HTTP is allowed only for loopback providers unless explicitly enabled.")
    return normalized


def read_bounded_json(response: Any, *, max_bytes: int) -> Any:
    """Read a JSON response without accepting an unbounded response body."""
    headers = getattr(response, "headers", {}) or {}
    content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise ProviderResponseTooLargeError("The provider response is too large.")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        size = 0
        for chunk in iter_content(chunk_size=min(65536, max_bytes + 1)):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ProviderResponseTooLargeError("The provider response is too large.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            return None
        return json.loads(raw)

    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)) and len(content) > max_bytes:
        raise ProviderResponseTooLargeError("The provider response is too large.")
    return response.json()


def close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
