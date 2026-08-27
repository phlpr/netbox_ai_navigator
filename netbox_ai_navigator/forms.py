from django import forms
from django.utils.translation import gettext_lazy as _
from utilities.forms import FilterForm
from utilities.forms.rendering import FieldSet

from .models import RejectedResponseLog
from .rejections import RejectionReason, ResponseLogCategory


class ResponseLogFilterForm(FilterForm):
    model = RejectedResponseLog
    fieldsets = (
        FieldSet("q"),
        FieldSet("category", "reason", name=_("Attributes")),
    )
    category = forms.MultipleChoiceField(
        label=_("Category"),
        choices=ResponseLogCategory.choices,
        required=False,
    )
    reason = forms.MultipleChoiceField(
        label=_("Reason"),
        choices=RejectionReason.choices,
        required=False,
    )
