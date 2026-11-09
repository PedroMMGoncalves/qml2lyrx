# Changelog

## 0.1.2 - 2026-11-09

### Changed

- GitHub Actions moved to `checkout@v5`, `setup-python@v6` and
  `upload-artifact@v5`; the previous majors run on the deprecated Node 20.

### Validated

- The `.stylx` output was opened in ArcGIS Pro 3.x: the style is listed under
  **Catalog → Styles** and its symbols are applied from the Symbology pane. The
  caveat that said otherwise is gone from the README.

### Documentation

- New README section on opening a `.stylx` in ArcGIS Pro.

## 0.1.1 - 2026-11-09

### Changed

- Command-line messages, `--help`, warnings and errors are now in English.
- `converter.ini` keys renamed to `style`, `data`, `layer` and `output`.
- Flags renamed to `--folder`, `--force`, `--discover` and `--field`; the
  Portuguese spellings (`--pasta`, `--forcar`, `--descobrir`, `--campo`) still
  work as aliases.
- `correr.bat` and `construir-exe.bat` messages in English.

### Fixed

- The two-column example in the README collapsed on GitHub; it is now two
  separate code blocks.

## 0.1.0 - 2026-11-09

### Added

- `qml2lyrx.py`: converts QGIS `singleSymbol`, `categorizedSymbol` and
  `RuleRenderer` (equality rules) into ArcGIS Pro layer files.
- `--pasta` mode: scans a folder, reads the styles stored in the GeoPackages
  (`layer_styles`), writes a `converter.ini` and converts everything.
- Zipped GeoPackages: a `.zip` is extracted once, next to the file.
- `.stylx` output: the same symbols written as an ArcGIS Pro style, one per
  class, under a category named after the layer.
- `*_shapefile.lyrx` twin: every layer whose field name exceeds 10 characters
  also gets a variant with the truncated name, for shapefile targets.
- `--campo` to override the field name explicitly.
- `correr.bat` and `construir-exe.bat` for double-click use and for building a
  standalone `.exe` (also built by `.github/workflows/exe.yml`).

### Notes

- `.lyrx` structure validated against 107 layer files shipped with ArcGIS Pro
  3.7; the class table was diffed against the QGIS style stored in the COS25v1
  GeoPackage.
- The `.stylx` layout was copied from a style installed by ArcGIS Pro but has
  not been opened in Pro during development.
- Point symbols, graduated renderers and non-equality filters are refused with
  an explicit message.
