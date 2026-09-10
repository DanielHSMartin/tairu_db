#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Gera tairu_core/record_icons.py a partir do codigo Dart do aplicativo.

O QGIS desenha o mesmo icone do aplicativo usando a FONTE Material Icons — a mesma que
o Flutter embarca — num QgsFontMarkerSymbolLayer. Para isso e preciso saber, em Python,
tres coisas que so existem no Dart:

  IconTypes.<nome>      -> ponto de codigo do glifo   (via Icons.<x> do SDK do Flutter)
  RecordTypes.<tipo>    -> IconTypes.<nome>
  RecordSubTypes.<sub>  -> IconTypes.<nome>

Rodar de novo depois de mexer nos catalogos do aplicativo:

    python3 QGIS/tairu_db/tools/gen_record_icons.py

Icones que vem do pacote `material_symbols_icons` (Symbols.*) NAO entram: sao de outra
fonte, que pesa dezenas de megabytes. Quem cair neles usa o icone do tipo do registro,
que e a mesma cadeia de reserva do aplicativo.
"""

import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.abspath(os.path.join(AQUI, '..', '..', '..'))
MODELS = os.path.join(RAIZ, 'Flutter', 'lib', 'common', 'models.dart')
RECORD_MODEL = os.path.join(RAIZ, 'Flutter', 'lib', 'common', 'record_model.dart')
SAIDA = os.path.join(AQUI, '..', 'tairu_core', 'record_icons.py')
FLUTTER_ICONS = os.path.join(
    os.path.expanduser('~'), 'development', 'flutter',
    'packages', 'flutter', 'lib', 'src', 'material', 'icons.dart')


def corpo_do_enum(texto, nome):
    achado = re.search(r'enum %s\s*\{(.*?)\n\}' % nome, texto, re.S)
    if not achado:
        raise SystemExit('enum %s nao encontrado' % nome)
    return achado.group(1)


def pontos_de_codigo_do_flutter(caminho):
    """{nome_no_Icons: ponto_de_codigo} do icons.dart do SDK."""
    texto = open(caminho, encoding='utf-8').read()
    padrao = re.compile(
        r'static const IconData ([A-Za-z0-9_]+) = IconData\(\s*(0x[0-9a-fA-F]+)')
    return {nome: int(cp, 16) for nome, cp in padrao.findall(texto)}


def main():
    if not os.path.exists(FLUTTER_ICONS):
        raise SystemExit('icons.dart do Flutter nao encontrado em %s' % FLUTTER_ICONS)
    codigos = pontos_de_codigo_do_flutter(FLUTTER_ICONS)
    models = open(MODELS, encoding='utf-8').read()
    record_model = open(RECORD_MODEL, encoding='utf-8').read()

    corpo = corpo_do_enum(models, 'IconTypes')
    icones = {}
    sem_fonte = []
    for nome, arg in re.findall(r'^\s*([A-Za-z0-9_]+)\s*\(\s*([^,\n]+),', corpo, re.M):
        arg = arg.strip()
        if arg.startswith('Icons.'):
            chave = arg[len('Icons.'):]
            if chave in codigos:
                icones[nome] = codigos[chave]
            else:
                sem_fonte.append((nome, arg))
        elif arg.startswith('IconData(0x'):
            icones[nome] = int(re.search(r'0x[0-9a-fA-F]+', arg).group(0), 16)
        elif arg.startswith('Symbols.'):
            sem_fonte.append((nome, arg))

    def mapa_de_icone(texto, enum):
        saida = {}
        for nome, args in re.findall(r'^\s*([A-Za-z0-9_]+)\s*\((.*?)\)\s*,\s*$',
                                     corpo_do_enum(texto, enum), re.M | re.S):
            achado = re.search(r'IconTypes\.([A-Za-z0-9_]+)', args)
            if achado:
                saida[nome] = achado.group(1)
        return saida

    tipos = mapa_de_icone(record_model, 'RecordTypes')
    subtipos = mapa_de_icone(record_model, 'RecordSubTypes')

    linhas = [
        '# -*- coding: utf-8 -*-',
        '',
        '"""GERADO por tools/gen_record_icons.py — nao editar a mao.',
        '',
        'Ponte entre o catalogo de icones do aplicativo e o glifo da fonte Material Icons',
        'que o QGIS desenha. Regerar com:',
        '',
        '    python3 QGIS/tairu_db/tools/gen_record_icons.py',
        '"""',
        '',
        '# IconTypes.<nome> -> ponto de codigo na fonte Material Icons',
        'ICON_CODEPOINTS = {',
    ]
    for nome in sorted(icones):
        linhas.append("    '%s': 0x%04x," % (nome, icones[nome]))
    linhas += ['}', '',
               '# RecordTypes.<tipo> -> IconTypes.<nome>',
               'TYPE_ICON = {']
    for nome in sorted(tipos):
        linhas.append("    '%s': '%s'," % (nome, tipos[nome]))
    linhas += ['}', '',
               '# RecordSubTypes.<subtipo> -> IconTypes.<nome>',
               'SUBTYPE_ICON = {']
    for nome in sorted(subtipos):
        linhas.append("    '%s': '%s'," % (nome, subtipos[nome]))
    linhas += ['}', '',
               '# Ultima reserva, igual a do aplicativo (Icons.location_on).',
               "FALLBACK_ICON = 'locationOn'", '']
    open(os.path.abspath(SAIDA), 'w', encoding='utf-8').write('\n'.join(linhas))

    print('icones com glifo: %d' % len(icones))
    print('tipos: %d | subtipos: %d' % (len(tipos), len(subtipos)))
    if sem_fonte:
        print('sem glifo nesta fonte (usarao o icone do tipo): %d' % len(sem_fonte))
        for nome, arg in sem_fonte:
            print('   %s -> %s' % (nome, arg))
    print('escrito: %s' % os.path.abspath(SAIDA))
    return 0


if __name__ == '__main__':
    sys.exit(main())
