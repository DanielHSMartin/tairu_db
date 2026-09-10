# -*- coding: utf-8 -*-

"""Grupos de registros: a MESMA arvore que o usuario ve na aba Registros do app.

Porte 1:1 das regras que o app usa para decidir onde cada grupo aparece
(`lib/common/record_group_model.dart` e `lib/utils/text_fold.dart`). Elas sao
portadas, e nao reinventadas, porque a promessa da funcionalidade e "no QGIS igual
ao app": qualquer divergencia aqui aparece como um grupo em lugar diferente nos dois
lados, que e exatamente a confusao que a feature existe para acabar.

Modulo sem dependencia de QGIS de proposito — a ordenacao e a hierarquia sao
testaveis sem abrir o QGIS, que e onde os casos chatos moram (acento, ciclo, orfao).
"""

import unicodedata


# Bloco Combining Diacritical Marks, onde o NFD deposita os acentos do alfabeto
# latino. Aritmetica de code point em vez de classe de regex pelo mesmo motivo do
# app: escrever o intervalo literalmente poria caracteres invisiveis neste arquivo.
_COMBINING_START = 0x0300
_COMBINING_END = 0x036F


def fold_for_matching(text):
    """Chave de COMPARACAO de um nome (nunca de exibicao). Porte de foldForMatching.

    Dobra as quatro coisas que fazem nomes visualmente iguais compararem diferente:
    forma Unicode (NFC x NFD), acento, caixa e sequencia de espacos. Nunca grave o
    resultado por cima do texto do usuario.
    """
    lowered = unicodedata.normalize('NFD', str(text or '').lower())
    stripped = ''.join(
        ch for ch in lowered
        if not (_COMBINING_START <= ord(ch) <= _COMBINING_END)
    )
    return ' '.join(stripped.split())


def sort_key(name):
    """Chave de ordenacao equivalente ao String.compareTo do Dart.

    O Dart compara UNIDADES DE CODIGO UTF-16; o Python compara code points. Os dois so
    divergem acima do BMP (emoji no nome do grupo), e a diferenca e uma pasta fora de
    ordem em relacao ao app. Comparar os bytes UTF-16BE reproduz o Dart exatamente.
    """
    return fold_for_matching(name).encode('utf-16-be', 'surrogatepass')


def effective_parent_id(group, by_id):
    """O pai sob o qual `group` REALMENTE aparece, ou '' (nivel superior).

    Porte 1:1 de effectiveParentId (record_group_model.dart). Uma regra so, usada pela
    arvore inteira, para um grupo nunca ser mostrado num lugar e tratado como raiz em
    outro. Um vinculo e inutilizavel quando o pai nao existe mais, aponta para o proprio
    grupo, ou fecha um ciclo — e todos degradam para o nivel superior: grupo NUNCA
    desaparece por causa de um vinculo podre.

    Ciclos sao simetricos de proposito: em A->B->A os dois lados falham e os dois vao
    para o nivel superior, o que e estavel independentemente de quem avalia. E so o
    ciclo que passa por ESTE grupo invalida o vinculo dele — um ciclo mais acima
    pertence aos grupos de la, e nao pode arrastar o galho de baixo junto.
    """
    parent_id = (getattr(group, 'parent_group_id', '') or '')
    group_id = getattr(group, 'group_id', '')
    if not parent_id or parent_id == group_id:
        return ''
    cursor = by_id.get(parent_id)
    if cursor is None:
        return ''
    seen = set()
    while cursor is not None:
        cursor_id = getattr(cursor, 'group_id', '')
        if cursor_id == group_id:
            return ''
        if cursor_id in seen:
            break
        seen.add(cursor_id)
        nxt = (getattr(cursor, 'parent_group_id', '') or '')
        if not nxt:
            break
        cursor = by_id.get(nxt)
    return parent_id


def live_groups(groups):
    """{group_id: grupo} so com os vivos, que e como o app monta `map.recordGroups`.

    A lapide fica FORA: e assim que o app decide, e por isso um registro cujo groupId
    aponta para um grupo apagado cai em "Sem grupo" em vez de sumir.
    """
    result = {}
    for group in groups or ():
        group_id = getattr(group, 'group_id', '')
        if group_id and not getattr(group, 'is_deleted', False):
            result[group_id] = group
    return result


def group_children(by_id):
    """{parent_id: [grupos filhos, ja ordenados]} — '' e o nivel superior."""
    children = {}
    for group in by_id.values():
        parent = effective_parent_id(group, by_id)
        children.setdefault(parent, []).append(group)
    for siblings in children.values():
        # Desempate por group_id: o sort do Dart e INSTAVEL, entao dois grupos de mesma
        # chave dobrada ("Ção"/"Çao") trocam de lugar entre reconstrucoes no proprio app.
        # Aqui a ordem tem de ser estavel, senao a arvore do painel se remonta a cada pull.
        siblings.sort(key=lambda g: (sort_key(getattr(g, 'name', '')),
                                     getattr(g, 'group_id', '')))
    return children


def walk_tree(by_id):
    """[(grupo, profundidade, pai_id)] na ordem em que o app desenha, do topo para baixo."""
    children = group_children(by_id)
    ordered = []

    def _descend(parent_id, depth):
        for group in children.get(parent_id, ()):
            ordered.append((group, depth, parent_id))
            _descend(getattr(group, 'group_id', ''), depth + 1)

    _descend('', 0)
    return ordered


def sql_quote(value):
    """Literal de texto para o subset do provedor OGR/GPKG.

    Um apostrofo no valor sem dobrar nao levanta erro nenhum: setSubsetString devolve
    True, isValid() devolve True e featureCount() devolve -1 com a pasta vazia. Todo id
    interpolado passa por aqui.
    """
    return "'" + str(value or '').replace("'", "''") + "'"


def folder_filter(live_ids, group_id=None):
    """Subset da pasta do painel. `group_id=None` monta a pasta "Sem grupo".

    "Sem grupo" e por EXCLUSAO, e nao `groupId = ''`, porque e assim que o app decide:
    todo groupId que nao resolve — vazio, nulo, ou apontando para um grupo apagado —
    cai ali. Filtrar por igualdade perderia justamente os orfaos, que sao o caso em que
    o usuario mais precisa achar o registro.

    Lista de grupos vazia NAO pode virar `NOT IN ()`: o SQLite aceita por extensao
    propria e devolve tudo, mas o parser de expressao do QGIS rejeita e a mesma string
    devolve ZERO feicoes noutro caminho. Sem grupo nenhum, todo registro esta sem grupo,
    e o subset correto e nenhum subset.
    """
    if group_id is not None:
        return '"groupId" = %s' % sql_quote(group_id)
    # sorted(): a expressao entra em layer.source(). Sem ordem estavel a string muda a
    # cada processo e a camada seria recriada a cada reabertura do QGIS.
    ids = sorted({str(g) for g in (live_ids or ()) if g})
    if not ids:
        return ''
    return 'coalesce("groupId", \'\') NOT IN (%s)' % ', '.join(sql_quote(g) for g in ids)
