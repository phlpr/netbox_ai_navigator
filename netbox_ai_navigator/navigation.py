from django.utils.translation import gettext_lazy as _
from netbox.plugins.navigation import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label=_("AI Navigator"),
    icon_class="mdi mdi-robot-outline",
    groups=(
        (
            _("Administration"),
            (
                PluginMenuItem(
                    link="plugins:netbox_ai_navigator:rejectedresponselog_list",
                    link_text=_("Rejected AI responses"),
                    permissions=("netbox_ai_navigator.view_rejectedresponselog",),
                ),
            ),
        ),
    ),
)
