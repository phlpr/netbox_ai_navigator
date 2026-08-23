from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from netbox_ai_navigator.config import validate_plugin_settings


class DynamicToolConfigurationTest(SimpleTestCase):
    def test_dynamic_discovery_is_valid_default(self):
        validate_plugin_settings({"tools": {"allowed_object_types": None}})

    def test_plugin_model_labels_are_valid_allowlist_entries(self):
        validate_plugin_settings(
            {
                "tools": {
                    "allowed_object_types": ["example_plugin.widget"],
                    "excluded_object_types": ["example_plugin.secretwidget"],
                    "excluded_fields": ["internal_reference"],
                }
            }
        )

    def test_allowed_object_types_rejects_a_string(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_plugin_settings({"tools": {"allowed_object_types": "dcim.device"}})

    def test_exclusions_reject_empty_values(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_plugin_settings({"tools": {"excluded_fields": [""]}})
