# -*- coding: utf-8 -*-

"""Visibilidade de camada no painel de camadas do QGIS.

Uma camada desmarcada no painel não é desenhada no mapa, então o plugin também a
ignora: ela não entra no .tairudb nem é oferecida para envio ao Tairu Maps.

CUIDADO com a API. Em QGIS 3, `QgsLayerTreeNode.isVisible()` JÁ é a visibilidade
efetiva — sobe por todos os grupos pai, aninhados inclusive. O nome tentador
`isItemVisibilityCheckedRecursive()` faz outra coisa: olha para BAIXO, para os filhos,
e devolve True para uma camada marcada dentro de um grupo desmarcado. Verificado no
QGIS 3.40 LTR:

    camada marcada, grupo desmarcado -> isVisible()=False   recursive()=True
    camada desmarcada, grupo marcado -> isVisible()=False   recursive()=False

Trocar uma pela outra faria camadas ocultas voltarem a ser assadas no .tairudb sem
nenhum erro visível. Use sempre esta função.
"""


def layer_is_visible(layer, project=None):
    """True quando a camada está efetivamente visível no mapa.

    Uma camada fora da árvore de camadas (não adicionada ao painel) conta como NÃO
    visível — é o caso das camadas internas que o plugin cria com addMapLayer(l, False).
    """
    if layer is None:
        return False
    if project is None:
        from qgis.core import QgsProject
        project = QgsProject.instance()
    try:
        node = project.layerTreeRoot().findLayer(layer.id())
    except Exception:
        return False
    return node is not None and node.isVisible()


# Camada que o plugin abriu a partir de um .tairudb (tairu_ui/open_tairudb.py). Ela e o
# RESULTADO de uma geracao, nao fonte: fica fora de tudo que alimenta a proxima. Visivel,
# a imagem entraria marcada na primeira tela e o mapa anterior seria assado por cima da
# imagem de origem; os vetores voltariam com uuid novo e duplicariam no app a feicao ja
# incorporada. O valor e o caminho do arquivo de origem.
TAIRUDB_VIEW_PROPERTY = 'tairu/tairudbAberto'


def is_tairudb_view(layer):
    """True para camada aberta de um .tairudb pelo plugin."""
    try:
        return bool(layer.customProperty(TAIRUDB_VIEW_PROPERTY, ''))
    except Exception:
        return False


def visible_layers(layers, project=None):
    """Só as camadas visíveis, preservando a ordem recebida."""
    if project is None:
        from qgis.core import QgsProject
        project = QgsProject.instance()
    return [layer for layer in layers if layer_is_visible(layer, project)]
