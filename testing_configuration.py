from netbox.configuration_testing import *  # noqa: F403

PLUGINS = [*PLUGINS, "netbox_ai_navigator"]  # noqa: F405
PLUGINS_CONFIG = {
    "netbox_ai_navigator": {
        "model": {
            "provider": "openai_compatible",
            "base_url": "http://model.invalid/v1",
            "api_key": "test-key",
            "model": "test-model",
        }
    }
}
