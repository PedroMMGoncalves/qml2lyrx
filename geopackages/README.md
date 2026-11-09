# Data drop zone

Put the GeoPackages you want to convert here — `.gpkg` files or `.zip` archives —
and double-click `correr.bat` in the repository root (or drop this folder onto
that file).

```
qml2lyrx/
├── correr.bat
├── geopackages/          <- you are here
│   └── cos2025v1.gpkg
├── out/
│   └── cos2025v1_2026-11-09/
│       ├── COS25v1_n4.lyrx
│       └── ...
└── converter.ini         <- generated inside this folder, editable
```

Nothing in this folder is committed: the `.gitignore` keeps it out of git, and
only this README is tracked.

For very large datasets (the COS2025v1 GeoPackage is 1.8 GB), do not copy the
file here — drag its folder onto `correr.bat` instead. The tool accepts any
folder, and nothing is duplicated.
