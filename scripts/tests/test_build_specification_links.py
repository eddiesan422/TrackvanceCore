"""PDF evidence links stay usable without allowing local/escaping destinations."""
import builtins
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LinksTests(unittest.TestCase):
    def setUp(self):
        real_import = builtins.__import__
        def without_authoring(name, *args, **kwargs):
            if name.partition('.')[0] in {'reportlab', 'pypdf'}:
                raise ModuleNotFoundError('Separate authoring runtime', name=name)
            return real_import(name, *args, **kwargs)
        generator = Path(__file__).resolve().parents[1] / 'docs/build_specification.py'
        spec = importlib.util.spec_from_file_location('pdf_links', generator)
        self.module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', without_authoring):
            spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name) / 'docs'
        (root / 'specification').mkdir(parents=True)
        (root / 'development/evidence').mkdir(parents=True)
        self.source = root / 'specification/source.md'
        self.source.write_text('# Source', encoding='utf-8')
        (root / 'development/evidence/proof & detail.json').write_text('{}', encoding='utf-8')
        self.module.DOCS_ROOT, self.module.SOURCE = root, self.source

    def test_existing_relative_evidence_is_a_published_https_link(self):
        rendered = self.module.escaped('[recibo](../development/evidence/proof%20%26%20detail.json)')
        self.assertIn('href="https://github.com/eddiesan422/TrackvanceCore-docs/blob/main/docs/development/evidence/proof%20%26%20detail.json"', rendered)
        self.assertTrue(rendered.endswith('>recibo</link>'))

    def test_external_https_query_preserves_xml_escaping(self):
        rendered = self.module.escaped('[A & B](https://example.test/a?x=1&y=2)')
        self.assertIn('href="https://example.test/a?x=1&amp;y=2"', rendered)
        self.assertIn('>A &amp; B</link>', rendered)

    def test_local_external_missing_and_encoded_traversal_stay_plain(self):
        outside = Path(self.temporary.name) / 'private.json'
        outside.write_text('{}', encoding='utf-8')
        for target in ['file:///private.json', 'javascript:alert', '//example.test/x',
                       '../../private.json', '%2e%2e/%2e%2e/private.json',
                       '../missing.json', '../development/evidence', '../x.json?token=private']:
            with self.subTest(target=target):
                self.assertNotIn('<link', self.module.escaped(f'[proof]({target})'))

    def test_unbound_authoring_context_does_not_read_working_directory(self):
        del self.module.DOCS_ROOT
        self.assertIsNone(self.module.document_link('../development/evidence/proof%20%26%20detail.json'))

    def test_fragment_on_existing_document_is_encoded(self):
        url = self.module.document_link('source.md#review%20record')
        self.assertTrue(url.endswith('/specification/source.md#review%20record'))


if __name__ == '__main__':
    unittest.main()
