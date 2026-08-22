from django.test import SimpleTestCase
from django.utils.translation import gettext, override

from netbox_ai_navigator.template_content import get_ui_translations


class UITranslationTest(SimpleTestCase):
    def test_uses_active_netbox_language(self):
        with override("de"):
            translations = get_ui_translations()
            read_permission = gettext("Use AI Navigator in read-only mode")
            write_permission = gettext("Use AI Navigator with write capabilities")

        self.assertEqual(translations["subtitle"], "Nur Lesen · Ihre Berechtigungen")
        self.assertEqual(translations["send"], "Senden")
        self.assertEqual(translations["expand_assistant"], "Assistent vergrößern")
        self.assertEqual(
            read_permission,
            "AI Navigator im Nur-Lese-Modus verwenden",
        )
        self.assertEqual(
            write_permission,
            "AI Navigator mit Schreibberechtigung verwenden",
        )

    def test_falls_back_to_english_source_strings(self):
        with override("en"):
            translations = get_ui_translations()

        self.assertEqual(translations["send"], "Send")
        self.assertEqual(translations["clear_conversation"], "Clear conversation")
