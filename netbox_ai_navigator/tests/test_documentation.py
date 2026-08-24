from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings

from netbox_ai_navigator.documentation import DocumentationIndex, _build_sections


class DocumentationIndexTest(SimpleTestCase):
    def setUp(self):
        self.temp_directory = TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        root = Path(self.temp_directory.name)
        (root / "configuration").mkdir()
        (root / "configuration" / "plugins.md").write_text(
            """# Plugins

## Installation

Install a plugin in the same Python environment as NetBox and add it to `PLUGINS`.

## Configuration

Plugin settings are defined in `PLUGINS_CONFIG`.
""",
            encoding="utf-8",
        )
        _build_sections.cache_clear()

    @override_settings(STATIC_URL="/static/")
    def test_searches_and_reads_local_documentation(self):
        with override_settings(
            DOCS_ROOT=self.temp_directory.name,
            STATICFILES_DIRS=(("docs", self.temp_directory.name),),
        ):
            index = DocumentationIndex({"max_results": 5, "max_section_chars": 12000})

            results = index.search("PLUGINS_CONFIG")
            section = index.read(results[0]["doc_id"])

        self.assertEqual(results[0]["title"], "Plugins")
        self.assertEqual(results[0]["section"], "Configuration")
        self.assertEqual(results[0]["url"], "/static/docs/configuration/plugins/")
        self.assertIn("PLUGINS_CONFIG", section["content"])

    def test_unknown_document_id_is_not_read(self):
        with override_settings(DOCS_ROOT=self.temp_directory.name):
            index = DocumentationIndex()

        self.assertIsNone(index.read("../../configuration.py"))

    def test_does_not_follow_document_symlink_outside_indexed_root(self):
        base = Path(self.temp_directory.name)
        docs_root = base / "safe-docs"
        docs_root.mkdir()
        outside = base / "outside.md"
        outside.write_text("# Private\n\nDO_NOT_INDEX_THIS_VALUE", encoding="utf-8")
        (docs_root / "linked.md").symlink_to(outside)
        _build_sections.cache_clear()

        with override_settings(DOCS_ROOT=str(docs_root)):
            index = DocumentationIndex()

        self.assertEqual(index.search("DO_NOT_INDEX_THIS_VALUE"), [])
