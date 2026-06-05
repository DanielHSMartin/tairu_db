# TairuDB — QGIS Plugin

Generates **`.tairudb`** files for the [Tairu Maps](https://tairumaps.com) mobile app (iOS and Android).
Exports raster tiles and vector layers from QGIS projects into a portable SQLite database.

> Gera arquivos **`.tairudb`** para o aplicativo [Tairu Maps](https://tairumaps.com) (iOS e Android).
> Exporta tiles raster e camadas vetoriais de projetos QGIS para um banco SQLite portátil.

---

## What is Tairu Maps?

Tairu Maps is a mobile app for collaborative field mapping, with support for offline maps, real-time location sharing, georeferenced records, and integration with geographic data produced in QGIS.
The `.tairudb` file is the native offline map import format for the app.

## The .tairudb format

A `.tairudb` file is a SQLite3 database with the following tables:

| Table | Content |
|---|---|
| `metadata` | General settings (format, zoom, centre, version) |
| `regions` | Geographic regions with bounds and zoom levels |
| `tiles_region_N` | Raster tiles in TMS format per region (PNG, JPG or WebP) |
| `vector_layers` | Exported vector layers (point, line, polygon) |
| `features` | Features with geometry, style and JSON attributes |

## How to use

1. Open a QGIS project with the desired raster layers visible.
2. Create or import a vector layer with the polygon(s) defining the area of interest.
3. Go to **Processing › Toolbox** and search for **TairuDB**.
4. Set the parameters and click *Run*.
5. Transfer the `.tairudb` file to your device and import it in Tairu Maps.

## Parameters

| Parameter | Description |
|---|---|
| Area of interest (polygon) | Vector polygon layer. Each feature creates an independent region. |
| Map resolution | Sets the maximum zoom: Very High (0.5 m/px, zoom 18) to Very Low (32 m/px, zoom 12). |
| Image format | PNG (lossless), JPG or WebP. JPG and WebP produce smaller files. |
| Quality | Compression for JPG/WebP (1–100). Default: 90. |
| Vector layers to export | (Optional) Vector layers to include with colour and attributes. |
| Output file | Path to the `.tairudb` file to generate. |
| Dry Run | Estimate tile count, file size and processing time without generating any file. |

## GeoPDF Converter

`geopdf_converter.py` converts **GeoPDF** files to `.tairudb` format, extracting the raster background as tiles and vector geometries as layers.

Requires GDAL, pyproj and QGIS Python bindings.

```bash
python geopdf_converter.py input.pdf output.tairudb [options]
```

## Resolution guide

| Resolution | Zoom | Suggested use |
|---|---|---|
| Very High (0.5 m/px) | 18 | Detail surveys, floor plans |
| High (1 m/px) | 17 | Urban areas, trails |
| Medium-High (2 m/px) | 16 | Neighbourhoods, parks |
| Medium (4 m/px) | 15 | Small towns |
| Medium-Low (8 m/px) | 14 | Municipalities |
| Low (16 m/px) | 13 | Regions |
| Very Low (32 m/px) | 12 | States, overview |

> **Note:** High resolutions over large areas can generate thousands of tiles and take several minutes.
> Use the **Dry Run** option to estimate before committing to a full run.

## Changelog

| Version | Changes |
|---|---|
| 1.2 | Multi-region support; vector export with JSON attributes; tile retry; WebP; Dry Run mode |
| 1.1 | GeoPDF converter; rendering performance improvements |
| 1.0 | Initial release |

## License

GNU General Public License v2 — see [LICENSE](LICENSE).

## Author

Daniel Hulshof Saint Martin — [danielhsmartin@gmail.com](mailto:danielhsmartin@gmail.com)
