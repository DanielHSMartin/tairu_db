================================================================================
TairuDB Plugin para QGIS — v1.2
================================================================================
Autor  : Daniel Hulshof Saint Martin <danielhsmartin@gmail.com>
GitHub : https://github.com/DanielHSMartin/tairu_db
App    : https://tairumaps.com
================================================================================

DESCRIÇÃO
---------
Plugin para geração de arquivos .tairudb compatíveis com o aplicativo móvel
Tairu Maps (iOS, Android e Web). Permite exportar mapas offline — incluindo tiles
raster e camadas vetoriais — diretamente de projetos QGIS.


O QUE É O TAIRU MAPS?
----------------------
O Tairu Maps é um aplicativo móvel de mapas colaborativos voltado para uso em
campo, com suporte a mapas offline, compartilhamento de localização em tempo
real, registros georreferenciados e integração com dados geográficos produzidos
no QGIS. O arquivo .tairudb é o formato nativo de importação de mapas offline
no Tairu Maps.


O ARQUIVO .tairudb
------------------
Um arquivo .tairudb é um banco de dados SQLite3 com as seguintes tabelas:

  metadata         — Configurações gerais (formato, zoom, centro, versão)
  regions          — Regiões geográficas com bounds e níveis de zoom
  tiles_region_N   — Tiles raster TMS por região (PNG, JPG ou WebP)
  vector_layers    — Camadas vetoriais exportadas (ponto, linha, polígono)
  features         — Feições com geometria, estilo e atributos em JSON

Para detalhes completos do schema, consulte TAIRUDB_SCHEMA.txt.


COMO USAR
---------
1. Abra o QGIS com um projeto contendo as camadas raster visíveis.
2. Crie ou importe uma camada com o(s) polígono(s) da área de interesse.
   Cada feição do polígono gera uma região independente no arquivo de saída.
3. Acesse: Processamento > Caixa de Ferramentas > TairuDB
4. Preencha os parâmetros (veja abaixo) e clique em Executar.
5. Transfira o arquivo .tairudb para o dispositivo e importe no Tairu Maps.


PARÂMETROS
----------
  Área de interesse (polígono)    Camada vetorial de polígonos. Cada feição
                                  gera uma região independente.

  Resolução do mapa               Define o zoom máximo:
                                    Altíssima (0,5 m/px) → zoom 18
                                    Alta       (1 m/px)   → zoom 17
                                    Médio Alta (2 m/px)   → zoom 16
                                    Média      (4 m/px)   → zoom 15
                                    Médio Baixa(8 m/px)   → zoom 14
                                    Baixa      (16 m/px)  → zoom 13
                                    Muito Baixa(32 m/px)  → zoom 12

  Formato da imagem               PNG (sem perda), JPG ou WebP.
                                  JPG e WebP produzem arquivos menores.

  Qualidade                       Compressão para JPG/WebP (1–100). Padrão: 90.

  Camadas vetoriais para exportar (Opcional) Camadas vetoriais do projeto a
                                  incluir, com cor QGIS e atributos em JSON.

  Arquivo de saída                Caminho do arquivo .tairudb a ser gerado.


CONVERSOR GEOPDF
----------------
O arquivo geopdf_converter.py converte arquivos GeoPDF para o formato .tairudb,
extraindo o fundo raster como tiles e as geometrias como camadas vetoriais.

Requer: GDAL, pyproj e bibliotecas Python do QGIS.

Uso:
  python geopdf_converter.py entrada.pdf saida.tairudb [opções]


ATENÇÃO
-------
Resoluções altas (zoom 18) em áreas grandes podem gerar milhares de tiles e
demorar vários minutos. Avalie a relação entre área e resolução antes de
executar.


CHANGELOG
---------
1.2  Suporte a múltiplas regiões; exportação vetorial com atributos JSON;
     retry automático em tiles com falha; suporte a WebP.
1.1  Adição do conversor GeoPDF; melhorias no desempenho de renderização.
1.0  Versão inicial: geração de tiles raster para o Tairu Maps.

================================================================================
