from netbox.plugins import PluginConfig

from .config import DEFAULT_SETTINGS, validate_plugin_settings

__version__ = "0.1.0"


class NetBoxAINavigatorConfig(PluginConfig):
    name = "netbox_ai_navigator"
    verbose_name = "NetBox AI Navigator"
    description = "Explore NetBox data through a read-only, RBAC-aware AI assistant."
    version = __version__
    author = "phlpr"
    base_url = "ai-navigator"
    min_version = "4.6.0"
    max_version = "4.6.99"
    default_settings = DEFAULT_SETTINGS

    @classmethod
    def validate(cls, user_config, netbox_version):
        super().validate(user_config, netbox_version)
        validate_plugin_settings(user_config)

    def ready(self):
        super().ready()
        from . import signals  # noqa: F401


config = NetBoxAINavigatorConfig
