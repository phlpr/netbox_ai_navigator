from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from utilities.querysets import RestrictedQuerySet

from .rejections import RejectionReason, ResponseLogCategory


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


class RejectedResponseLog(models.Model):
    objects = RestrictedQuerySet.as_manager()

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
        verbose_name=_("User"),
    )
    username = models.CharField(max_length=255, verbose_name=_("Username"))
    category = models.CharField(
        max_length=16,
        choices=ResponseLogCategory.choices,
        default=ResponseLogCategory.REJECTED,
        db_index=True,
        verbose_name=_("Category"),
    )
    user_request = models.TextField(verbose_name=_("Last request"))
    rejected_response = models.TextField(verbose_name=_("Model response"))
    delivered_response = models.TextField(verbose_name=_("Delivered response"))
    reason = models.CharField(max_length=32, choices=RejectionReason.choices, verbose_name=_("Reason"))
    provider = models.CharField(max_length=50, blank=True, verbose_name=_("Model provider"))
    model_name = models.CharField(max_length=255, blank=True, verbose_name=_("Model name"))
    created = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_("Created"))

    class Meta:
        ordering = ("-created", "-pk")
        default_permissions = ("view",)
        verbose_name = _("AI response log")
        verbose_name_plural = _("AI response logs")

    def __str__(self) -> str:
        return f"{self.username} · {self.get_category_display()} · {self.created:%Y-%m-%d %H:%M:%S}"

    def get_absolute_url(self) -> str:
        return reverse("plugins:netbox_ai_navigator:rejectedresponselog", args=[self.pk])

    @property
    def request_preview(self) -> str:
        return self._preview(self.user_request)

    @property
    def rejected_response_preview(self) -> str:
        return self._preview(self.rejected_response)

    @staticmethod
    def _preview(value: str, length: int = 120) -> str:
        compact = " ".join(value.split())
        return compact if len(compact) <= length else f"{compact[: length - 1]}…"
