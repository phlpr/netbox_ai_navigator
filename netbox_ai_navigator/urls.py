from django.urls import path

from .views import ChangeApprovalView, ChatView, ResetConversationView

app_name = "netbox_ai_navigator"

urlpatterns = (
    path("api/chat/", ChatView.as_view(), name="chat"),
    path("api/chat/reset/", ResetConversationView.as_view(), name="reset_conversation"),
    path("api/actions/approve/", ChangeApprovalView.as_view(), name="approve_action"),
)
