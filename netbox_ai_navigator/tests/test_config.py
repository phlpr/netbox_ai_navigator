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

    def test_custom_fields_require_a_boolean_opt_in(self):
        validate_plugin_settings({"tools": {"include_custom_fields": True}})
        with self.assertRaises(ImproperlyConfigured):
            validate_plugin_settings({"tools": {"include_custom_fields": "yes"}})

    def test_remote_provider_requires_https_by_default(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "Plain HTTP"):
            validate_plugin_settings({"model": {"base_url": "http://model.internal/v1"}})

    def test_trusted_internal_http_provider_requires_explicit_opt_in(self):
        validate_plugin_settings(
            {
                "model": {
                    "base_url": "http://model.internal/v1",
                    "allow_insecure_http": True,
                }
            }
        )

    def test_provider_url_rejects_embedded_credentials(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "embedded credentials"):
            validate_plugin_settings({"model": {"base_url": "https://user:password@model.example/v1"}})

    def test_openai_compatible_protocol_is_validated(self):
        validate_plugin_settings({"model": {"protocol": "responses"}})
        with self.assertRaisesMessage(ImproperlyConfigured, "model.protocol"):
            validate_plugin_settings({"model": {"protocol": "completions"}})

    def test_extra_provider_headers_are_validated(self):
        validate_plugin_settings({"model": {"extra_headers": {"deployment-id": "test-model"}}})
        with self.assertRaisesMessage(ImproperlyConfigured, "reserved"):
            validate_plugin_settings({"model": {"extra_headers": {"Content-Type": "text/plain"}}})
        with self.assertRaisesMessage(ImproperlyConfigured, "line breaks"):
            validate_plugin_settings({"model": {"extra_headers": {"deployment-id": "model\nInjected"}}})

    def test_rate_limit_is_bounded(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_plugin_settings({"agent": {"requests_per_minute": 601}})

    def test_rejected_response_log_settings_are_validated(self):
        validate_plugin_settings({"rejected_response_logs": {"enabled": True, "max_entries": 250}})
        with self.assertRaises(ImproperlyConfigured):
            validate_plugin_settings({"rejected_response_logs": {"enabled": "yes"}})
        with self.assertRaises(ImproperlyConfigured):
            validate_plugin_settings({"rejected_response_logs": {"max_entries": 100_001}})
