#!/usr/bin/env python3
"""Gate de tradução do plugin — o equivalente do `Flutter/tool/i18n_audit.py`.

    python3 tools/i18n_audit.py

Falha (exit 1) quando:
  1. um `tr('literal')` não tem chave em TODOS os `tairu_core/l10n/*.json`;
  2. os dicionários não têm exatamente o mesmo conjunto de chaves;
  3. uma tradução não tem os mesmos placeholders `{nome}` da chave pt;
  4. um `tr()` recebe f-string ou expressão montada (a chave precisa ser fixa);
  5. um texto literal chega sem `tr()` a um ponto que o usuário vê (setText,
     QLabel, pushMessage, QMessageBox, push_info, QgsProcessingException, ...).

O item 5 olha só esses pontos de saída: texto guardado numa constante e exibido
depois passa despercebido — por isso a constante leva `tr()` onde é definida
(ou, se o valor é gravado, onde é exibido, com a chave posta à mão no JSON).
Logs (QgsMessageLog) ficam em pt de propósito e não são checados.
"""

import ast
import json
import pathlib
import re
import string
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
L10N = ROOT / 'tairu_core' / 'l10n'
# Mesmo conjunto que o build_release.sh empacota.
SOURCES = ['__init__.py', 'tairu_db.py', 'tairu_db_algorithm.py', 'tairu_db_provider.py',
           'geopdf_converter.py', 'compat.py', 'qgis_proxy.py',
           'tairu_core', 'tairu_ui', 'tairu_firebase', 'tairu_sync']

# Chamadas cujo argumento de texto aparece para o usuário.
SINKS = {
    'setText', 'setToolTip', 'setWindowTitle', 'setPlaceholderText', 'setTitle',
    'setSubTitle', 'setStatusTip', 'setWhatsThis', 'setLabelText', 'setFormat',
    'addItem', 'addItems', 'insertItem', 'setItemText', 'setTabText', 'addTab',
    'setHeaderLabels', 'setHorizontalHeaderLabels', 'setVerticalHeaderLabels',
    'setButtonText', 'setSpecialValueText', 'setSuffix', 'setPrefix',
    'QLabel', 'QPushButton', 'QCheckBox', 'QRadioButton', 'QGroupBox', 'QAction',
    'QToolButton', 'QTableWidgetItem', 'QListWidgetItem', 'QTreeWidgetItem',
    'pushMessage', 'pushInfo', 'pushWarning', 'pushCritical', 'pushSuccess',
    'pushFormattedMessage', 'reportError', 'setProgressText',
    'information', 'warning', 'critical', 'question', 'about',
    'getText', 'getItem', 'getInt', 'getDouble',
    'getOpenFileName', 'getOpenFileNames', 'getSaveFileName', 'getExistingDirectory',
    'push_info', 'report_error', 'set_progress_text', 'heartbeat',
    'QgsProcessingException',
}
# O 1º argumento destes é o NOME (id) do parâmetro, não texto.
_PARAM_PREFIX = 'QgsProcessingParameter'
_WORD = re.compile(r'[A-Za-zÀ-ÿ]{2,}')


def _files():
    for entry in SOURCES:
        path = ROOT / entry
        if path.is_dir():
            yield from sorted(p for p in path.rglob('*.py') if '__pycache__' not in p.parts)
        else:
            yield path


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _raw_texts(node):
    """Literais de texto em `node` que NÃO passaram por tr()."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _WORD.search(node.value):
            yield node.value
    elif isinstance(node, ast.JoinedStr):
        text = ''.join(v.value for v in node.values if isinstance(v, ast.Constant))
        if _WORD.search(text):
            yield 'f' + repr(text)
    elif isinstance(node, ast.BinOp):
        yield from _raw_texts(node.left)
        yield from _raw_texts(node.right)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            yield from _raw_texts(elt)
    elif isinstance(node, ast.IfExp):
        yield from _raw_texts(node.body)
        yield from _raw_texts(node.orelse)


def _placeholders(text):
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name is not None}


def main():
    errors = []
    keys = {}  # chave -> primeiro lugar onde aparece
    for path in _files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding='utf-8'), str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            where = f'{rel}:{node.lineno}'
            if name == 'tr':
                if len(node.args) != 1:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.setdefault(arg.value, where)
                elif isinstance(arg, (ast.JoinedStr, ast.BinOp)):
                    errors.append(f'{where}: tr() com texto montado — use tr("... {{x}}").format(x=...)')
                continue
            if name not in SINKS and not (name or '').startswith(_PARAM_PREFIX):
                continue
            args = list(node.args)
            if (name or '').startswith(_PARAM_PREFIX):
                args = args[1:]
            args += [kw.value for kw in node.keywords if kw.arg not in ('name', 'objectName')]
            for arg in args:
                for raw in _raw_texts(arg):
                    errors.append(f'{where}: texto sem tr() em {name}(): {raw!r}')

    dicts = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in sorted(L10N.glob('*.json'))}
    if not dicts:
        errors.append(f'nenhum dicionário em {L10N}')
    for lang, table in dicts.items():
        for key, where in sorted(keys.items(), key=lambda kv: kv[1]):
            if key not in table:
                errors.append(f'{where}: chave ausente em {lang}.json: {key!r}')
        for key, value in table.items():
            if not isinstance(value, str) or not value.strip():
                errors.append(f'{lang}.json: tradução vazia para {key!r}')
            elif _placeholders(key) != _placeholders(value):
                errors.append(f'{lang}.json: placeholders diferem em {key!r} -> {value!r}')
    langs = list(dicts)
    for other in langs[1:]:
        only = set(dicts[langs[0]]) ^ set(dicts[other])
        for key in sorted(only):
            errors.append(f'chave só em um de {langs[0]}.json/{other}.json: {key!r}')

    for line in errors:
        print(line)
    print(f'i18n: {len(keys)} chaves em tr(), {len(errors)} problema(s)')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
