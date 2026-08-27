import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, columns

from .models import RejectedResponseLog


class RejectedResponseLogTable(NetBoxTable):
    created = columns.DateTimeColumn(linkify=True, timespec="minutes", verbose_name=_("Created"))
    username = tables.Column(verbose_name=_("User"))
    category = tables.Column(verbose_name=_("Category"))
    reason = tables.Column(verbose_name=_("Reason"))
    request_preview = tables.Column(linkify=True, orderable=False, verbose_name=_("Last request"))
    rejected_response_preview = tables.Column(
        linkify=True,
        orderable=False,
        verbose_name=_("Model response"),
    )
    model_name = tables.Column(verbose_name=_("Model name"))

    class Meta(NetBoxTable.Meta):
        model = RejectedResponseLog
        exclude = ("pk", "id", "actions")
        fields = (
            "created",
            "username",
            "category",
            "reason",
            "request_preview",
            "rejected_response_preview",
            "model_name",
        )
        default_columns = fields

    @staticmethod
    def render_category(record: RejectedResponseLog) -> str:
        return record.get_category_display()

    @staticmethod
    def render_reason(record: RejectedResponseLog) -> str:
        return record.get_reason_display()
