import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from netbox.filtersets import BaseFilterSet

from .models import RejectedResponseLog
from .rejections import RejectionReason, ResponseLogCategory


class ResponseLogFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    category = django_filters.MultipleChoiceFilter(choices=ResponseLogCategory.choices)
    reason = django_filters.MultipleChoiceFilter(choices=RejectionReason.choices)

    class Meta:
        model = RejectedResponseLog
        fields = ("id", "category", "reason", "username", "model_name", "created")

    @staticmethod
    def search(queryset, _name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(username__icontains=value)
            | Q(user_request__icontains=value)
            | Q(rejected_response__icontains=value)
            | Q(delivered_response__icontains=value)
            | Q(model_name__icontains=value)
        )
