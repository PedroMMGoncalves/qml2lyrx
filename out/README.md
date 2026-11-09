# Outputs

Everything the converter writes lands here, in one folder per GeoPackage and per
day:

```
out/
└── cos2025v1_2026-11-09/
    ├── COS25v1_n4.lyrx            layer with the symbology
    ├── COS25v1_n4.stylx           the same symbols as an ArcGIS Pro style
    ├── COS25v1_n4_shapefile.lyrx  same layer, field name truncated to 10 chars
    └── ...
```

Generated files are ignored by git; only this README is tracked. Delete the
folder whenever you like — it is rebuilt on the next run.
