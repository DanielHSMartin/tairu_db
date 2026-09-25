import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_audit():
    spec = importlib.util.spec_from_file_location('i18n_audit', ROOT / 'tools' / 'i18n_audit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TranslationsTest(unittest.TestCase):
    def test_audit_passes(self):
        """Todo tr() tem chave em en/es, com os mesmos placeholders."""
        self.assertEqual(_load_audit().main(), 0)

    def test_dictionaries_translate(self):
        for lang in ('en', 'es'):
            table = json.loads((ROOT / 'tairu_core' / 'l10n' / f'{lang}.json').read_text(encoding='utf-8'))
            self.assertEqual(table.get('Cancelar'), {'en': 'Cancel', 'es': 'Cancelar'}[lang])


if __name__ == '__main__':
    unittest.main()
