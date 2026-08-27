from dataclasses import dataclass

from django.db import models
from django.utils.translation import gettext_lazy as _


class RejectionReason(models.TextChoices):
    SCOPE_GUARD = "scope_guard", _("Outside NetBox scope")
    CHANGE_GUARD = "change_guard", _("Change response was not validated")
    APPROVAL_NORMALIZATION = "approval_normalization", _("Change approval response was replaced")
    PROPOSAL_GUARD = "proposal_guard", _("Unsafe change proposal response")
    GROUNDING_GUARD = "grounding_guard", _("Response could not be grounded in NetBox data")


@dataclass(frozen=True, slots=True)
class RejectedResponse:
    reason: str
    response: str
