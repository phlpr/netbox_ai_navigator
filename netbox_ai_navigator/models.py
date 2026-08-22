from django.db import models
from django.utils.translation import gettext_lazy as _


class AINavigator(models.Model):
    """Permission anchor for AI Navigator capabilities; no database rows are stored."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("use_read", _("Use AI Navigator in read-only mode")),
            ("use_write", _("Use AI Navigator with write capabilities")),
        )
        verbose_name = _("AI Navigator")
        verbose_name_plural = _("AI Navigator")
