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
        self.assertEqual(translations["subtitle_write"], "Lesen und Schreiben · Ihre Berechtigungen")
        self.assertEqual(translations["send"], "Senden")
        self.assertEqual(translations["confirm_change"], "Änderung bestätigen")
        self.assertEqual(translations["expand_assistant"], "Assistent vergrößern")
        self.assertEqual(translations["minimize_assistant"], "Assistent minimieren")
        self.assertEqual(translations["copy"], "Kopieren")
        self.assertEqual(translations["copied"], "Kopiert")
        self.assertEqual(translations["copy_failed"], "Kopieren fehlgeschlagen")
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

    def test_translates_grounding_error(self):
        with override("de"):
            message = gettext(
                "The model response could not be verified against NetBox data. Please retry or refine the request."
            )

        self.assertEqual(
            message,
            "Die Modellantwort konnte nicht anhand der NetBox-Daten verifiziert werden. "
            "Bitte versuchen Sie es erneut oder präzisieren Sie die Anfrage.",
        )

    def test_translates_grounded_fallback_heading(self):
        with override("de"):
            message = gettext("Verified NetBox results ({count}):").format(count=2)

        self.assertEqual(message, "Verifizierte NetBox-Ergebnisse (2):")

    def test_translates_grounded_fallback_device_columns(self):
        with override("de"):
            labels = [gettext(value) for value in ("Device", "Role", "Site", "Location", "Status")]

        self.assertEqual(labels, ["Gerät", "Rolle", "Standort", "Lokation", "Status"])

    def test_translates_ambiguous_navigation_failure(self):
        with override("de"):
            message = gettext(
                "No unique visible navigation target could be resolved. Please specify the exact NetBox object "
                "and try again."
            )

        self.assertEqual(
            message,
            "Es konnte kein eindeutiges sichtbares Navigationsziel ermittelt werden. Bitte geben Sie das genaue "
            "NetBox-Objekt an und versuchen Sie es erneut.",
        )
