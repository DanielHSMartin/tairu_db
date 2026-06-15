from qgis.core import QgsApplication, QgsVectorLayer
import sys

# Initialize QGIS
QgsApplication.setPrefixPath('/Applications/QGIS-LTR.app/Contents/MacOS', True)
qgs = QgsApplication([], False)
qgs.initQgis()

try:
    pdf_path = 'COP30 - GBE.pdf'
    
    # Open as vector layer
    layer = QgsVectorLayer(pdf_path, 'test', 'ogr')
    sublayers = layer.dataProvider().subLayers()
    
    print(f'Found {len(sublayers)} sublayers:\n')
    
    for sublayer in sublayers:
        parts = sublayer.split('!!::!!')
        if len(parts) >= 2:
            layer_id = parts[0]
            layer_name = parts[1]
            
            uri = f'{pdf_path}|layername={layer_name}'
            vlayer = QgsVectorLayer(uri, layer_name, 'ogr')
            
            if vlayer.isValid():
                print(f'Layer: {layer_name}')
                renderer = vlayer.renderer()
                if renderer:
                    print(f'  Renderer: {renderer.type()}')
                    if hasattr(renderer, 'symbol'):
                        symbol = renderer.symbol()
                        if symbol:
                            color = symbol.color()
                            print(f'  Color: #{color.red():02X}{color.green():02X}{color.blue():02X}')
                print()
finally:
    qgs.exitQgis()
