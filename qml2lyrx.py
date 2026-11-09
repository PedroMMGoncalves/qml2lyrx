#!/usr/bin/env python3
"""Convert a QGIS layer style (.qml) into an ArcGIS Pro layer file (.lyrx).

A .qml is XML, a .lyrx is JSON. The program does three things:

    read the QML  ->  [(value, label, colour), ...]   (the list of classes)
    translate     ->  the same list, in the JSON template Pro expects
    write         ->  the .lyrx

Everything else is the fixed template in `camada()` — copied from a .lyrx
written by Pro itself, because Pro insists on all those keys.

    python qml2lyrx.py style.qml out.lyrx --source data.gpkg --dataset layer
"""
import argparse
import configparser
import json
import os
import re
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

# CIM measures widths in points; QGIS in mm / pixel / inch
UNIDADES = {"MM": 2.834645669, "Pixel": 0.75, "Point": 1.0, "Inch": 72.0}

VERSAO = "0.1.1"


def _n(count, word, suffix="s"):
    """'1 class' / '2 classes' — keeps the console messages readable."""
    return f"{count} {word}{'' if count == 1 else suffix}"


# ------------------------------------------------------------------- read
def cor(texto, alpha=1.0):
    """'230,0,77,255' -> (230, 0, 77, 100). CIM alpha runs from 0 to 100."""
    if not texto:
        return None
    r, g, b, a = [float(x) for x in (texto.split(",") + ["255"])[:4]]
    return int(r), int(g), int(b), round(a / 255 * 100 * alpha)


def pontos(valor, unidade):
    return float(valor or 0) * UNIDADES.get(unidade or "MM", 2.834645669)


def simbolo(elem):
    """<symbol> of the QML -> {'geometria', 'cor', 'contorno', 'espessura'}."""
    alpha = float(elem.get("alpha", 1))
    for camada in elem.findall("layer"):
        op = {o.get("name"): o.get("value") for o in camada.iter("Option") if o.get("name")}
        if camada.get("class") == "SimpleFill":
            contorno, espessura = None, 0.0
            if (op.get("outline_style") or "solid") != "no":
                contorno = cor(op.get("outline_color"), alpha)
                espessura = pontos(op.get("outline_width"), op.get("outline_width_unit"))
            return {"geometria": "polygon", "cor": cor(op.get("color"), alpha),
                    "contorno": contorno, "espessura": espessura}
        if camada.get("class") == "SimpleLine":
            if (op.get("line_style") or "solid") not in ("solid", "no"):
                print("warning: dashed line drawn as solid", file=sys.stderr)
            return {"geometria": "line", "cor": cor(op.get("line_color"), alpha),
                    "contorno": None,
                    "espessura": pontos(op.get("line_width"), op.get("line_width_unit"))}
    raise SystemExit(f"unsupported symbol: {elem.get('type')} (only SimpleFill and SimpleLine)")


def ler_qml(origem):
    """Open the QML and return (field, [{'valor', 'rotulo', 'simbolo'}, ...]).

    `origem` is a .qml file, or 'data.gpkg#StyleName' for the styles QGIS keeps
    inside the GeoPackage itself (the layer_styles table).
    """
    if "#" in str(origem):
        ficheiro, nome = str(origem).rsplit("#", 1)
        raiz = ET.fromstring(estilo_do_gpkg(ficheiro, nome).encode("utf-8"))
    else:
        raiz = ET.parse(origem).getroot()

    renderer = raiz.find("renderer-v2")
    if renderer is None:
        raise SystemExit(f"{origem}: not a layer style (no <renderer-v2>)")

    simbolos = {s.get("name"): simbolo(s) for s in renderer.findall("./symbols/symbol")}
    tipo = renderer.get("type")

    if tipo == "singleSymbol":
        chave = renderer.get("symbol")
        if chave not in simbolos:  # QGIS 3.34 does not write the `symbol` attribute
            chave = next(iter(simbolos), None)
        if chave is None:
            raise SystemExit(f"{origem}: renderer without symbols")
        return None, [{"valor": None, "rotulo": "", "simbolo": simbolos[chave]}]

    if tipo == "categorizedSymbol":
        return renderer.get("attr"), [
            {"valor": c.get("value"), "rotulo": c.get("label") or c.get("value"),
             "simbolo": simbolos[c.get("symbol")]}
            for c in renderer.findall("./categories/category")
            if c.get("render") != "false"
        ]

    if tipo == "RuleRenderer":
        campo, classes, vistos = None, [], set()
        for regra in renderer.findall("./rules/rule"):
            filtro = (regra.get("filter") or "").strip()
            if regra.get("checkstate") == "0" or not filtro:
                continue
            achado = re.match(r'^\s*"?(\w+)"?\s*=\s*(.+?)\s*$', filtro)
            if not achado:
                raise SystemExit(f"unsupported filter: {filtro}")
            campo = campo or achado.group(1)
            valor = achado.group(2).strip("'\"")
            if valor in vistos:
                print(f"warning: duplicate value {valor}; the first rule wins", file=sys.stderr)
                continue
            vistos.add(valor)
            classes.append({"valor": valor, "rotulo": regra.get("label") or valor,
                            "simbolo": simbolos[regra.get("symbol")]})
        return campo, classes

    raise SystemExit(f"renderer '{tipo}' is not supported (singleSymbol, categorizedSymbol, RuleRenderer)")


# ---------------------------------------------------------------- write
def rgbs(c):
    return {"type": "CIMRGBColor", "values": list(c)}


def traco(c, espessura):
    """One outline / line. No colour means no stroke."""
    return {"type": "CIMSolidStroke", "enable": True, "capStyle": "Round",
            "joinStyle": "Round", "lineStyle3D": "Strip", "miterLimit": 10,
            "width": round(espessura or 0.1, 2), "color": rgbs(c)}


def simbolo_cim(s):
    if s["geometria"] == "polygon":
        camadas = [traco(s["contorno"], s["espessura"])] if s["contorno"] else []
        if s["cor"]:
            camadas.append({"type": "CIMSolidFill", "enable": True, "color": rgbs(s["cor"])})
        return {"type": "CIMPolygonSymbol", "symbolLayers": camadas, "angleAlignment": "Map"}
    return {"type": "CIMLineSymbol", "symbolLayers": [traco(s["cor"], s["espessura"])]}


def referencia(s):
    return {"type": "CIMSymbolReference", "symbol": simbolo_cim(s)}


def renderer(campo, classes):
    """Single symbol when there is no field; otherwise unique values, one class per value."""
    if campo is None:
        return {"type": "CIMSimpleRenderer", "sampleSize": 10000, "patch": "Default",
                "symbol": referencia(classes[0]["simbolo"])}

    # grey symbol Pro keeps for "all other values" (it is not drawn)
    cinzento = {"geometria": "polygon", "cor": (130, 130, 130, 100),
                "contorno": (110, 110, 110, 100), "espessura": 0.7}

    r = {
        "type": "CIMUniqueValueRenderer",
        "sampleSize": 10000,
        # Pro always keeps a colour ramp on the renderer; class colours are explicit
        "colorRamp": {"type": "CIMRandomHSVColorRamp",
                      "colorSpace": {"type": "CIMICCColorSpace", "url": "Default RGB"},
                      "maxH": 360, "minS": 15, "maxS": 30, "minV": 99, "maxV": 100,
                      "minAlpha": 100, "maxAlpha": 100},
        "defaultLabel": "Other values",
        "defaultSymbol": referencia(cinzento),
        "defaultSymbolPatch": "Default",
        "useDefaultSymbol": False,
        "fields": [campo],
        "groups": [{
            "type": "CIMUniqueValueGroup",
            "heading": "",
            "classes": [
                {"type": "CIMUniqueValueClass",
                 "label": c["rotulo"],
                 "patch": "Default",
                 "symbol": referencia(c["simbolo"]),
                 "values": [{"type": "CIMUniqueValue", "fieldValues": [c["valor"]]}],
                 "visible": True}
                for c in classes
            ],
        }],
    }
    if classes[0]["simbolo"]["geometria"] == "polygon":
        r["polygonSymbolColorTarget"] = "Fill"
    return r


def camada(nome, campo, rend, ligacao):
    """The layer template. This is copied from a Pro .lyrx; it is not logic."""
    return {
        "type": "CIMFeatureLayer",
        "name": nome,
        "uRI": "CIMPATH=map/layer.json",
        "sourceModifiedTime": {"type": "TimeInstant"},
        "useSourceMetadata": True,
        "description": nome,
        "layerElevation": {"type": "CIMLayerElevationSurface",
                           "elevationSurfaceLayerURI": "CIMPATH=ground/ground.json"},
        "expanded": True,
        "layerType": "Operational",
        "showLegends": True,
        "visibility": True,
        "displayCacheType": "Permanent",
        "maxDisplayCacheAge": 5,
        "showPopups": True,
        "serviceLayerID": -1,
        "refreshRate": -1,
        "refreshRateUnit": "esriTimeUnitsSeconds",
        "blendingMode": "Alpha",
        "allowDrapingOnIntegratedMesh": True,
        "autoGenerateFeatureTemplates": True,
        "featureElevationExpression": "0",
        "featureTable": {
            "type": "CIMFeatureTable",
            "displayField": campo or "",
            "editable": True,
            "dataConnection": {"type": "CIMStandardDataConnection",
                               "workspaceConnectionString": ligacao["DATABASE"],
                               "workspaceFactory": ligacao["factory"],
                               "dataset": ligacao["dataset"],
                               "datasetType": "esriDTFeatureClass"},
            "studyAreaSpatialRel": "esriSpatialRelUndefined",
            "searchOrder": "esriSearchOrderSpatial",
        },
        "featureTemplates": [],
        "htmlPopupEnabled": True,
        "selectable": True,
        "featureCacheType": "Session",
        "displayFiltersType": "ByScale",
        "featureBlendingMode": "Alpha",
        "scaleSymbols": True,
        "snappable": True,
        "labelClasses": [],
        "renderer": rend,
        "symbolLayerDrawing": {"type": "CIMSymbolLayerDrawing"},
    }


def documento(nome, campo, rend, ligacao):
    return {
        "type": "CIMLayerDocument",
        "version": "3.3.0",
        "build": 51169,
        "layers": ["CIMPATH=map/layer.json"],
        "layerDefinitions": [camada(nome, campo, rend, ligacao)],
        "binaryReferences": [],
        "elevationSurfaceLayerDefinitions": [{
            "type": "CIMElevationSurfaceLayer",
            "name": "Ground",
            "uRI": "CIMPATH=ground/ground.json",
            "useSourceMetadata": True,
            "description": "Ground",
            "expanded": True,
            "layerType": "Operational",
            "showLegends": False,
            "visibility": True,
            "displayCacheType": "Permanent",
            "maxDisplayCacheAge": 5,
            "showPopups": True,
            "serviceLayerID": -1,
            "refreshRate": -1,
            "refreshRateUnit": "esriTimeUnitsSeconds",
            "blendingMode": "Alpha",
            "allowDrapingOnIntegratedMesh": True,
            "elevationMode": "BaseGlobeSurface",
            "verticalExaggeration": 1,
            "color": {"type": "CIMRGBColor", "values": [255, 255, 255, 100]},
            "surfaceTINShadingMode": "Smooth",
        }],
        "rGBColorProfile": "sRGB IEC61966-2.1",
        "cMYKColorProfile": "U.S. Web Coated (SWOP) v2",
    }


def ligacao(source, dataset):
    """Where the data lives. Without --source a placeholder connection is written."""
    if not source:
        return {"DATABASE": r"DATABASE=C:\data\example.gdb", "factory": "FileGDB",
                "dataset": dataset or "layer"}
    p = Path(source)
    # a folder is, by convention, a shapefile folder
    factory = "Shapefile" if p.is_dir() else {".gdb": "FileGDB"}.get(p.suffix.lower(), "GPKG")
    return {"DATABASE": f"DATABASE={p}", "factory": factory,
            "dataset": dataset or p.stem}


def estilo_do_gpkg(caminho, nome):
    """Fetch the QML that QGIS stored in the GeoPackage layer_styles table."""
    con = None
    try:
        con = sqlite3.connect(f"file:{Path(caminho).as_posix()}?mode=ro", uri=True)
        linha = con.execute(
            "select styleQML from layer_styles where styleName = ?", (nome,)
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(f"{caminho}: cannot read the styles ({exc})")
    finally:
        if con is not None:
            con.close()
    if not linha:
        raise SystemExit(f"{caminho}: no style named '{nome}' in layer_styles")
    return linha[0]


def estilos_do_gpkg(caminho):
    """[(style, table), ...] stored in the GeoPackage; [] when there are none.

    A file with a .gpkg extension that is not a database (a renamed .zip, for
    instance) returns [] instead of crashing.
    """
    con = None
    try:
        con = sqlite3.connect(f"file:{Path(caminho).as_posix()}?mode=ro", uri=True)
        return con.execute("select styleName, f_table_name from layer_styles").fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        if con is not None:
            con.close()


# ----------------------------------------------------------------- style
# A .stylx is a SQLite database. These definitions were copied from a .stylx
# installed by ArcGIS Pro (app6b.stylx): symbol content is CIM JSON, in ITEMS.
_CLASSES = [
    (1, "Color"), (2, "Color Scheme"), (3, "Point Symbol"), (4, "Line Symbol"),
    (5, "Polygon Symbol"), (6, "Text Symbol"), (7, "North Arrow"),
    (8, "Scale Bar"), (9, "Standard Label Placement"), (10, "Maplex Label Placement"),
    (11, "Grid"), (12, "Mesh Symbol"), (13, "Legend"), (14, "Table Frame"),
    (15, "Map Surround"), (17, "Legend Item"), (18, "Table Frame Field"),
    (19, "Area Legend Patch"), (20, "Line Legend Patch"),
]

_TABELAS = """
CREATE TABLE BINARIES (ID INTEGER PRIMARY KEY, MD5 STRING UNIQUE, CLASS INTEGER, CONTENT BLOB);
CREATE TABLE BINARY_CLASSES (ID INTEGER PRIMARY KEY, NAME TEXT);
CREATE TABLE CLASSES (ID INTEGER PRIMARY KEY, NAME TEXT);
CREATE TABLE ITEMS (ID INTEGER PRIMARY KEY, CLASS INTEGER, CATEGORY TEXT, NAME TEXT,
                    TAGS TEXT, CONTENT TEXT, KEY TEXT UNIQUE);
CREATE TABLE STYLE_ITEM_BINARY_REFERENCES (ID INTEGER PRIMARY KEY, ITEMS_ID INTEGER NOT NULL,
                    BINARIES_ID INTEGER NOT NULL,
                    FOREIGN KEY(ITEMS_ID) REFERENCES ITEMS(ID),
                    FOREIGN KEY(BINARIES_ID) REFERENCES BINARIES(ID),
                    UNIQUE(ITEMS_ID, BINARIES_ID));
CREATE TABLE meta (key TEXT, value TEXT);
"""

_META = [
    ("version", "1.0"),
    ("cim_version", "3.3.0"),
    ("build", "527"),
    ("content", "json"),
    ("forceRGB", "true"),
    ("vectorizeCM", "true"),
    ("densifyCurves", "true"),
    ("colorModel", "RGB"),
    ("RGBColorProfile", "sRGB IEC61966-2-1 noBPC"),
    ("CMYKColorProfile", "U.S. Web Coated (SWOP) v2"),
    ("revision_number", "1"),
]


def gravar_stylx(destino, classes, categoria):
    """Write the .stylx from classes already read from the QML."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.unlink(missing_ok=True)

    con = sqlite3.connect(destino)
    try:
        con.executescript(_TABELAS)
        con.executemany("insert into CLASSES values (?, ?)", _CLASSES)
        con.executemany("insert into meta values (?, ?)", _META)

        for numero, classe in enumerate(classes, 1):
            poligono = classe["simbolo"]["geometria"] == "polygon"
            con.execute(
                "insert into ITEMS (CLASS, CATEGORY, NAME, TAGS, CONTENT, KEY) "
                "values (?, ?, ?, ?, ?, ?)",
                (
                    5 if poligono else 4,          # 5 = Polygon Symbol, 4 = Line Symbol
                    categoria,
                    classe["rotulo"],
                    f"{categoria};{classe['valor'] or numero}",
                    json.dumps(simbolo_cim(classe["simbolo"]), ensure_ascii=False),
                    str(classe["valor"] or f"item-{numero}"),
                ),
            )
        con.commit()
    finally:
        con.close()

    print(f"  style: {_n(len(classes), 'symbol')} -> {destino}")
    return len(classes)


def escrever_stylx(origem, destino, categoria=None):
    """Read a style and write only the .stylx (command-line mode)."""
    campo, classes = ler_qml(origem)
    return gravar_stylx(destino, classes, categoria or Path(destino).stem)


def converter(origem, saida, dados=None, camada=None, nome=None, campo=None):
    """Convert one style and write the .lyrx.

    `campo` handles destinations whose field name differs — shapefiles truncate
    names to 10 characters (cos25v1_n4_c -> cos25v1_n4).
    """
    campo_qml, classes = ler_qml(origem)
    campo = campo or campo_qml
    doc = documento(nome or Path(saida).stem, campo, renderer(campo, classes),
                    ligacao(dados, camada))
    Path(saida).parent.mkdir(parents=True, exist_ok=True)
    Path(saida).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(f"{origem}: {_n(len(classes), 'class', 'es')} on '{campo or '—'}' -> {saida}")

    # every .lyrx also gets the .stylx with the same symbols
    gravar_stylx(Path(saida).with_suffix(".stylx"), classes,
                 nome or Path(saida).stem)

    # shapefile variant: the DBF truncates field names to 10 characters
    if campo and len(campo) > 10:
        curto = campo[:10]
        gemeo = Path(saida).with_name(Path(saida).stem + "_shapefile.lyrx")
        doc_curto = documento(nome or Path(saida).stem, curto,
                              renderer(curto, classes), ligacao(dados, camada))
        gemeo.write_text(json.dumps(doc_curto, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        print(f"  shapefile: field '{curto}' -> {gemeo}")


def lote(config):
    """Run the conversions listed in an .ini file (one section per layer).

        [COS25v1_N4]                     <- layer name in ArcGIS Pro
        style  = data.gpkg#COS25v1_n4    <- .qml, or 'gpkg#StyleName'
        data   = data.gpkg               <- optional: defaults to the style's gpkg
        layer  = cos2025v1               <- table Pro will open
        output = out/COS25v1_N4.lyrx     <- optional: defaults to <section>.lyrx
    """
    base = Path(config).parent
    cfg = configparser.ConfigParser()
    try:
        if not cfg.read(config, encoding="utf-8"):
            raise SystemExit(f"cannot read {config}")
    except configparser.Error as exc:
        raise SystemExit(f"{config}: {exc}")

    ok = falhas = 0
    for nome in cfg.sections():
        secao = cfg[nome]
        style = secao.get("style", "").strip()
        if not style:
            print(f"{config} [{nome}]: missing 'style ='", file=sys.stderr)
            falhas += 1
            continue

        data = secao.get("data", "").strip() or None
        layer = secao.get("layer", "").strip() or None
        output = secao.get("output", "").strip() or f"{nome}.lyrx"

        if "#" in style:
            ficheiro, nome_estilo = style.rsplit("#", 1)
            origem = f"{base / ficheiro}#{nome_estilo}"
            data = data or str(base / ficheiro)
        else:
            origem = str(base / style)

        try:
            converter(origem, (base / output).resolve(),
                      str((base / data).resolve()) if data else None, layer, nome)
            ok += 1
        except SystemExit as exc:
            print(f"{config} [{nome}]: {exc}", file=sys.stderr)
            falhas += 1

    print(f"{Path(config).name}: {ok} converted, {falhas} failed")
    return 1 if falhas else 0


def descobrir(pasta):
    """Print an .ini configuration from the .gpkg and .qml files in a folder."""
    pasta = Path(pasta)
    print(f"# generated by: qml2lyrx --descobrir {pasta}")
    print("# one section per layer; the section name is the layer name in ArcGIS Pro")
    for gpkg in sorted(pasta.rglob("*.gpkg")):
        for nome, tabela in estilos_do_gpkg(gpkg):
            print(f"\n[{nome}]")
            print(f"style = {gpkg}#{nome}")
            print(f"layer = {tabela}")
            print(f"output = {nome}.lyrx")
    for qml in sorted(pasta.rglob("*.qml")):
        print(f"\n[{qml.stem}]")
        print(f"style = {qml}")
    return 0


def tabela_ou_origem(gpkg, tabela):
    """(table, view_name): when `tabela` is a view, return the table underneath.

    The COS GeoPackage stores its styles on views (v_cos2025v1_n4). ArcGIS Pro
    may not list views, so the configuration points at the real table and keeps
    the view as a commented-out alternative.
    """
    con = sqlite3.connect(f"file:{Path(gpkg).as_posix()}?mode=ro", uri=True)
    try:
        linha = con.execute("select type, sql from sqlite_master where name = ?",
                            (tabela,)).fetchone()
        if not linha or linha[0] != "view":
            return tabela, None
        achado = re.search(r"\bFROM\s+[\"'\[]?(\w+)", linha[1] or "", re.I)
        if achado:
            origem = achado.group(1)
            if con.execute("select 1 from sqlite_master where name = ? and type = 'table'",
                           (origem,)).fetchone():
                return origem, tabela
        return tabela, None
    finally:
        con.close()


def pasta_ferramenta():
    """The folder holding the program (the repo root) — also works in the .exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def extrair_gpkg(caminho):
    """Extract the .gpkg files from a zip into a folder named after the zip (once).

    ArcGIS Pro cannot read inside a zip: for the .lyrx to point at data that
    exists, the GeoPackage has to live on disk.
    """
    destino = Path(caminho).with_suffix("")
    achados = []

    with zipfile.ZipFile(caminho) as z:
        for membro in z.namelist():
            if not membro.lower().endswith(".gpkg"):
                continue
            alvo = destino / Path(membro).name
            if not alvo.exists():
                alvo.parent.mkdir(parents=True, exist_ok=True)
                print(f"extracting {Path(caminho).name} -> {alvo.name} (may take a while)",
                      file=sys.stderr)
                with z.open(membro) as origem, open(alvo, "wb") as saida:
                    shutil.copyfileobj(origem, saida, 1024 * 1024)
            achados.append(alvo)
    return achados


def descobrir_entradas(pasta):
    """Walk the folder (and its zips) and return one entry per style found."""
    pasta = Path(pasta)
    entradas, usados = [], set()

    gpkgs = sorted(pasta.rglob("*.gpkg"))
    for z in sorted(pasta.rglob("*.zip")):
        try:
            gpkgs += extrair_gpkg(z)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"warning: {z.name}: cannot open ({exc})", file=sys.stderr)

    vistos_gpkg = set()
    for gpkg in gpkgs:
        gpkg = Path(gpkg).resolve()
        if gpkg in vistos_gpkg:      # the same gpkg can come from the zip and from rglob
            continue
        vistos_gpkg.add(gpkg)

        for estilo, tabela in estilos_do_gpkg(gpkg):
            try:
                qml = estilo_do_gpkg(gpkg, estilo)
            except SystemExit as exc:
                print(f"warning: {exc}", file=sys.stderr)
                continue
            if "<renderer-v2" not in qml:
                print(f"warning: '{estilo}' ({gpkg.name}) is a symbol style, not a "
                      "layer style — skipped", file=sys.stderr)
                continue

            camada, vista = tabela_ou_origem(gpkg, tabela)

            secao = re.sub(r"\W+", "_", estilo)
            while secao in usados:
                secao = f"{secao}_2"
            usados.add(secao)

            # .lyrx files go to out/<gpkg-name>_<date>/ at the repo root
            saida = (pasta_ferramenta() / "out" /
                     f"{gpkg.stem}_{date.today().isoformat()}" / f"{estilo}.lyrx")

            entradas.append({
                "secao": secao,
                "estilo": (gpkg, estilo),
                "camada": camada,
                "alternativa": vista,
                "saida": saida,
            })
    return entradas


def escrever_config(caminho, entradas, pasta):
    """Write converter.ini, with paths relative to the folder."""
    pasta = Path(pasta)
    linhas = [
        "# qml2lyrx - generated from the .gpkg files in this folder.",
        "# You can edit this file: while it exists the program uses it as it is",
        "# (--force rebuilds the discovery and replaces it).",
        "#",
        "# One section per layer; the section name is the layer name in Pro.",
        "",
    ]
    for e in entradas:
        gpkg, estilo = e["estilo"]
        linhas.append(f"[{e['secao']}]")
        linhas.append(f"style = {gpkg.relative_to(pasta)}#{estilo}")
        if e["alternativa"]:
            linhas.append(f"# view {e['alternativa']} (lighter, if Pro can open it):")
            linhas.append(f"# layer = {e['alternativa']}")
        linhas.append(f"layer = {e['camada']}")
        linhas.append(f"output = {os.path.relpath(e['saida'], pasta)}")
        linhas.append("")

    Path(caminho).write_text("\n".join(linhas), encoding="utf-8")


def modo_pasta(pasta, forcar=False):
    """Scan a folder: discover styles, write converter.ini and convert."""
    pasta = Path(pasta).expanduser().resolve()
    if not pasta.is_dir():
        raise SystemExit(f"folder not found: {pasta}")

    config = pasta / "converter.ini"
    if config.exists() and not forcar:
        print(f"{config.name} already exists — using it (--force to rebuild)")
    else:
        entradas = descobrir_entradas(pasta)
        if not entradas:
            raise SystemExit(f"no styles found in the GeoPackages inside {pasta}")
        escrever_config(config, entradas, pasta)
        print(f"wrote {config.name}: {_n(len(entradas), 'layer')} found")

    return lote(config)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert a QGIS layer style (QML) into an ArcGIS Pro layer file (LYRX) or style (STYLX).")
    p.add_argument("input", nargs="?",
                   help=".qml, 'data.gpkg#StyleName', or an .ini configuration file")
    p.add_argument("output", nargs="?", help=".lyrx or .stylx to write (single-file mode)")
    p.add_argument("-n", "--name", help="layer name (default: the output file name)")
    p.add_argument("-s", "--source", help="source GeoPackage or FileGDB")
    p.add_argument("-d", "--dataset", help="table / feature class")
    p.add_argument("-c", "--field", "--campo", dest="field",
                   help="field name in the destination when it differs from the QML "
                        "(shapefiles truncate to 10 characters)")
    p.add_argument("--discover", "--descobrir", dest="descobrir", metavar="FOLDER",
                   help="list the styles and data in a folder and print a configuration")
    p.add_argument("--folder", "--pasta", dest="pasta", metavar="FOLDER",
                   help="scan the folder, write converter.ini and convert everything")
    p.add_argument("--force", "--forcar", dest="forcar", action="store_true",
                   help="with --folder, rebuild converter.ini even if it exists")
    p.add_argument("--version", action="version", version=f"qml2lyrx {VERSAO}")
    a = p.parse_args(argv)

    if a.descobrir:
        return descobrir(a.descobrir)
    if a.pasta:
        return modo_pasta(a.pasta, a.forcar)
    if not a.input:
        p.error("give a .qml, a 'gpkg#StyleName' or an .ini configuration file")
    if Path(a.input).suffix.lower() in (".txt", ".cfg", ".ini"):
        return lote(a.input)
    if not a.output:
        p.error("missing the output .lyrx")

    if Path(a.output).suffix.lower() == ".stylx":
        escrever_stylx(a.input, a.output, a.name)
        return 0

    converter(a.input, a.output, a.source, a.dataset, a.name, a.field)
    return 0


if __name__ == "__main__":
    sys.exit(main())
