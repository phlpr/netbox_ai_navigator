from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest


@dataclass(frozen=True, slots=True)
class ToolContext:
    request: HttpRequest
    user: Any
    current_object_type: str | None = None
    current_object_id: int | None = None
    can_write: bool = False
