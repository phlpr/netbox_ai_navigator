import logging
from typing import Any

from django.db.models import Q

from .models import RejectedResponseLog
from .rejections import RejectedResponse, RejectionReason, ResponseLogCategory

logger = logging.getLogger("netbox.plugins.netbox_ai_navigator.rejection_logging")

TRUNCATION_NOTICE = "\n\n[Truncated by NetBox AI Navigator.]"


def record_rejected_response(
    *,
    user,
    user_request: str,
    rejection: RejectedResponse,
    delivered_response: str,
    plugin_settings: dict[str, Any],
) -> RejectedResponseLog | None:
    log_config = plugin_settings.get("rejected_response_logs") or {}
    if not log_config.get("enabled", True):
        return None

    model_config = plugin_settings.get("model") or {}
    max_chars = int(model_config.get("max_response_chars", 20_000))
    username = str(user.get_username())[:255]
    user_id = getattr(user, "pk", None)

    try:
        entry = RejectedResponseLog.objects.create(
            user_id=user_id,
            username=username,
            category=(
                ResponseLogCategory.WRITE
                if rejection.reason == RejectionReason.APPROVAL_NORMALIZATION
                else ResponseLogCategory.REJECTED
            ),
            user_request=_bounded_text(user_request, max_chars),
            rejected_response=_bounded_text(rejection.response, max_chars),
            delivered_response=_bounded_text(delivered_response, max_chars),
            reason=rejection.reason,
            provider=str(model_config.get("provider", ""))[:50],
            model_name=str(model_config.get("model", ""))[:255],
        )
        _prune_rejected_responses(int(log_config.get("max_entries", 1000)))
        return entry
    except Exception:
        # The safety response must still reach the user if audit persistence is temporarily unavailable.
        logger.exception("Unable to persist rejected model response metadata")
        return None


def _prune_rejected_responses(max_entries: int) -> None:
    cutoff = (
        RejectedResponseLog.objects.order_by("-created", "-pk")
        .values_list("created", "pk")[max_entries : max_entries + 1]
        .first()
    )
    if cutoff is None:
        return
    created, pk = cutoff
    RejectedResponseLog.objects.filter(Q(created__lt=created) | Q(created=created, pk__lte=pk)).delete()


def _bounded_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - len(TRUNCATION_NOTICE))] + TRUNCATION_NOTICE
