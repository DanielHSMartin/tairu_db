# -*- coding: utf-8 -*-

"""Tradução da interface — a string pt-BR É a chave, como no app.

`tr('Salvar')` devolve o texto no idioma do QGIS; os dicionários ficam em
`l10n/<idioma>.json`. QGIS em pt → texto original; es* → espanhol; qualquer
outro idioma → inglês. Texto com valores usa placeholders nomeados:
`tr('{n} registros').format(n=n)`.

Valor que é gravado E exibido (situação do registro, tipos) continua pt no dado
e só passa por `tr()` no ponto em que é mostrado. Chave ausente devolve o pt —
`tools/i18n_audit.py` (rodado pelo build_release.sh) impede que isso seja
publicado.
"""

import json
import os

_L10N_DIR = os.path.join(os.path.dirname(__file__), 'l10n')
_table = None


def language():
    """'pt', 'es' ou 'en', conforme o idioma da interface do QGIS."""
    try:
        from qgis.core import QgsApplication
    except ImportError:  # fora do QGIS (testes puros): sem tradução
        return 'pt'
    locale = getattr(QgsApplication, 'locale', lambda: None)()
    if not isinstance(locale, str) or not locale:  # qgis stubado nos testes
        return 'pt'
    code = locale[:2].lower()
    if code == 'pt':
        return 'pt'
    return 'es' if code == 'es' else 'en'


def _load():
    global _table
    lang = language()
    table = {}
    if lang != 'pt':
        with open(os.path.join(_L10N_DIR, lang + '.json'), encoding='utf-8') as fh:
            table = json.load(fh)
    _table = table
    return table


def tr(text):
    """Texto `text` (pt-BR) no idioma do QGIS."""
    table = _table if _table is not None else _load()
    return table.get(text, text)


def decimal(text):
    """Número já formatado com ponto → vírgula em pt/es, ponto em en."""
    return text if language() == 'en' else text.replace('.', ',')


def thousands(text):
    """Número já formatado com `{n:,}` → ponto de milhar em pt/es, vírgula em en."""
    return text if language() == 'en' else text.replace(',', '.')
