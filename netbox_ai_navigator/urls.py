from django.urls import include, path
from utilities.urls import get_model_urls

from .views import ChangeApprovalView, ChatView, ResetConversationView

app_name = "netbox_ai_navigator"

urlpatterns = (
    path(
        "rejected-responses/",
        include(get_model_urls("netbox_ai_navigator", "rejectedresponselog", detail=False)),
    ),
    path(
        "rejected-responses/<int:pk>/",
        include(get_model_urls("netbox_ai_navigator", "rejectedresponselog")),
    ),
    path("api/chat/", ChatView.as_view(), name="chat"),
    path("api/chat/reset/", ResetConversationView.as_view(), name="reset_conversation"),
    path("api/actions/approve/", ChangeApprovalView.as_view(), name="approve_action"),
)
