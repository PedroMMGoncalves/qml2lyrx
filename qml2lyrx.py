#!/usr/bin/env python3
"""Converte um estilo QML do QGIS num layer file (.lyrx) do ArcGIS Pro.

Um .qml é XML, um .lyrx é JSON. O programa faz três coisas:

    ler o QML   ->  [(valor, rótulo, cor), ...]   (a lista de classes)
    traduzir    ->  o mesmo, no molde JSON que o Pro espera
    escrever    ->  o .lyrx

Tudo o resto é o molde fixo da função `camada` — copiado de um .lyrx escrito
pelo próprio Pro, porque o Pro exige aquelas chaves todas.

    python qml2lyrx.py estilo.qml saida.lyrx --source dados.gpkg --dataset camada
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

# o CIM mede espessuras em pontos; o QGIS em mm/pixel/polegada
UNIDADES = {"MM": 2.834645669, "Pixel": 0.75, "Point": 1.0, "Inch": 72.0}

VERSAO = "0.1.0"


# ------------------------------------------------------------------- ler
def cor(texto, alpha=1.0):
    """'230,0,77,255' -> (230, 0, 77, 100). O alfa do CIM vai de 0 a 100."""
    if not texto:
        return None
    r, g, b, a = [float(x) for x in (texto.split(",") + ["255"])[:4]]
    return int(r), int(g), int(b), round(a / 255 * 100 * alpha)


def pontos(valor, unidade):
    return float(valor or 0) * UNIDADES.get(unidade or "MM", 2.834645669)


def simbolo(elem):
    """<symbol> do QML -> {'geometria', 'cor', 'contorno', 'espessura'}."""
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
                print("aviso: linha tracejada desenhada contínua", file=sys.stderr)
            return {"geometria": "line", "cor": cor(op.get("line_color"), alpha),
                    "contorno": None,
                    "espessura": pontos(op.get("line_width"), op.get("line_width_unit"))}
    raise SystemExit(f"símbolo não suportado: {elem.get('type')} (só SimpleFill e SimpleLine)")


def ler_qml(origem):
    """Abre o QML e devolve (campo, [{'valor', 'rotulo', 'simbolo'}, ...]).

    `origem` é um ficheiro .qml, ou 'dados.gpkg#NomeDoEstilo' para os estilos que
    o QGIS guarda dentro do próprio GeoPackage (tabela layer_styles).
    """
    if "#" in str(origem):
        ficheiro, nome = str(origem).rsplit("#", 1)
        raiz = ET.fromstring(estilo_do_gpkg(ficheiro, nome).encode("utf-8"))
    else:
        raiz = ET.parse(origem).getroot()

    renderer = raiz.find("renderer-v2")
    if renderer is None:
        raise SystemExit(f"{origem}: não é um estilo de camada (falta <renderer-v2>)")

    simbolos = {s.get("name"): simbolo(s) for s in renderer.findall("./symbols/symbol")}
    tipo = renderer.get("type")

    if tipo == "singleSymbol":
        chave = renderer.get("symbol")
        if chave not in simbolos:  # o QGIS 3.34 não escreve o atributo `symbol`
            chave = next(iter(simbolos), None)
        if chave is None:
            raise SystemExit(f"{origem}: renderer sem símbolos")
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
                raise SystemExit(f"filtro não suportado: {filtro}")
            campo = campo or achado.group(1)
            valor = achado.group(2).strip("'\"")
            if valor in vistos:
                print(f"aviso: valor {valor} repetido; fica a primeira regra", file=sys.stderr)
                continue
            vistos.add(valor)
            classes.append({"valor": valor, "rotulo": regra.get("label") or valor,
                            "simbolo": simbolos[regra.get("symbol")]})
        return campo, classes

    raise SystemExit(f"renderer '{tipo}' não suportado (singleSymbol, categorizedSymbol, RuleRenderer)")


# --------------------------------------------------------------- escrever
def rgbs(c):
    return {"type": "CIMRGBColor", "values": list(c)}


def traco(c, espessura):
    """Um contorno/linha. Sem cor -> não há traço."""
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
    """Símbolo único se não há campo; senão unique values, uma classe por valor."""
    if campo is None:
        return {"type": "CIMSimpleRenderer", "sampleSize": 10000, "patch": "Default",
                "symbol": referencia(classes[0]["simbolo"])}

    # símbolo cinzento que o Pro guarda para "outros valores" (não é desenhado)
    cinzento = {"geometria": "polygon", "cor": (130, 130, 130, 100),
                "contorno": (110, 110, 110, 100), "espessura": 0.7}

    r = {
        "type": "CIMUniqueValueRenderer",
        "sampleSize": 10000,
        # o Pro guarda sempre uma rampa no renderer; as cores das classes são explícitas
        "colorRamp": {"type": "CIMRandomHSVColorRamp",
                      "colorSpace": {"type": "CIMICCColorSpace", "url": "Default RGB"},
                      "maxH": 360, "minS": 15, "maxS": 30, "minV": 99, "maxV": 100,
                      "minAlpha": 100, "maxAlpha": 100},
        "defaultLabel": "Outros valores",
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
    """O molde de uma camada. Isto é cópia de um .lyrx do Pro, não é lógica."""
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
    """Onde estão os dados. Sem --source fica uma ligação de exemplo."""
    if not source:
        return {"DATABASE": r"DATABASE=C:\dados\exemplo.gdb", "factory": "FileGDB",
                "dataset": dataset or "capa"}
    p = Path(source)
    # uma pasta é, por convenção, uma pasta de shapefiles
    factory = "Shapefile" if p.is_dir() else {".gdb": "FileGDB"}.get(p.suffix.lower(), "GPKG")
    return {"DATABASE": f"DATABASE={p}", "factory": factory,
            "dataset": dataset or p.stem}


def estilo_do_gpkg(caminho, nome):
    """Vai buscar o QML que o QGIS guardou na tabela layer_styles do GeoPackage."""
    con = None
    try:
        con = sqlite3.connect(f"file:{Path(caminho).as_posix()}?mode=ro", uri=True)
        linha = con.execute(
            "select styleQML from layer_styles where styleName = ?", (nome,)
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(f"{caminho}: não consigo ler os estilos ({exc})")
    finally:
        if con is not None:
            con.close()
    if not linha:
        raise SystemExit(f"{caminho}: não encontrei o estilo '{nome}' em layer_styles")
    return linha[0]


def estilos_do_gpkg(caminho):
    """[(estilo, tabela), ...] guardados no GeoPackage; [] se não houver nenhum.

    Um ficheiro com extensão .gpkg que não seja uma base de dados (um .zip
    renomeado, por exemplo) devolve [] em vez de rebentar.
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


# ---------------------------------------------------------------- estilo
# O .stylx é uma base de dados SQLite. Estas definições foram copiadas de um
# .stylx instalado pelo ArcGIS Pro (app6b.stylx): o conteúdo dos símbolos é CIM
# em JSON, na tabela ITEMS.
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
    """Escreve o .stylx a partir das classes já lidas do QML."""
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
                    5 if poligono else 4,          # 5=Polygon Symbol, 4=Line Symbol
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

    print(f"  estilo: {len(classes)} símbolos -> {destino}")
    return len(classes)


def escrever_stylx(origem, destino, categoria=None):
    """Lê um estilo e escreve só o .stylx (modo linha de comando)."""
    campo, classes = ler_qml(origem)
    return gravar_stylx(destino, classes, categoria or Path(destino).stem)


def converter(origem, saida, dados=None, camada=None, nome=None, campo=None):
    """Converte uma coisa só e escreve o .lyrx.

    `campo` serve para quando o campo no destino tem outro nome — é o caso das
    shapefiles, que cortam os nomes aos 10 caracteres (cos25v1_n4_c -> cos25v1_n4).
    """
    campo_qml, classes = ler_qml(origem)
    campo = campo or campo_qml
    doc = documento(nome or Path(saida).stem, campo, renderer(campo, classes),
                    ligacao(dados, camada))
    Path(saida).parent.mkdir(parents=True, exist_ok=True)
    Path(saida).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(f"{origem}: {len(classes)} classes sobre '{campo or '—'}' -> {saida}")

    # a cada .lyrx sai também o .stylx com os mesmos símbolos
    gravar_stylx(Path(saida).with_suffix(".stylx"), classes,
                 nome or Path(saida).stem)

    # variante para shapefiles: o DBF corta os nomes dos campos aos 10 caracteres
    if campo and len(campo) > 10:
        curto = campo[:10]
        gemeo = Path(saida).with_name(Path(saida).stem + "_shapefile.lyrx")
        doc_curto = documento(nome or Path(saida).stem, curto,
                              renderer(curto, classes), ligacao(dados, camada))
        gemeo.write_text(json.dumps(doc_curto, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        print(f"  shapefile: campo '{curto}' -> {gemeo}")


def lote(config):
    """Corre as conversões de um ficheiro .ini (uma secção por camada).

        [COS25v1_N4]                    <- nome da camada no ArcGIS Pro
        estilo = dados.gpkg#COS25v1_n4  <- .qml, ou 'gpkg#Estilo'
        dados  = dados.gpkg             <- opcional: por omissão, o do estilo
        camada = cos2025v1              <- tabela que o Pro vai abrir
        saida  = saida/COS25v1_N4.lyrx  <- opcional: por omissão, <secção>.lyrx
    """
    base = Path(config).parent
    cfg = configparser.ConfigParser()
    try:
        if not cfg.read(config, encoding="utf-8"):
            raise SystemExit(f"não consigo ler {config}")
    except configparser.Error as exc:
        raise SystemExit(f"{config}: {exc}")

    ok = falhas = 0
    for nome in cfg.sections():
        secao = cfg[nome]
        estilo = secao.get("estilo", "").strip()
        if not estilo:
            print(f"{config} [{nome}]: falta 'estilo ='", file=sys.stderr)
            falhas += 1
            continue

        dados = secao.get("dados", "").strip() or None
        camada = secao.get("camada", "").strip() or None
        saida = secao.get("saida", "").strip() or f"{nome}.lyrx"

        if "#" in estilo:
            ficheiro, nome_estilo = estilo.rsplit("#", 1)
            origem = f"{base / ficheiro}#{nome_estilo}"
            dados = dados or str(base / ficheiro)
        else:
            origem = str(base / estilo)

        try:
            converter(origem, (base / saida).resolve(),
                      str((base / dados).resolve()) if dados else None, camada, nome)
            ok += 1
        except SystemExit as exc:
            print(f"{config} [{nome}]: {exc}", file=sys.stderr)
            falhas += 1

    print(f"{Path(config).name}: {ok} convertidos, {falhas} falhas")
    return 1 if falhas else 0


def descobrir(pasta):
    """Imprime uma configuração .ini a partir dos .gpkg e .qml de uma pasta."""
    pasta = Path(pasta)
    print(f"# gerado por: qml2lyrx --descobrir {pasta}")
    print("# uma secção por camada; o nome da secção é o nome no ArcGIS Pro")
    for gpkg in sorted(pasta.rglob("*.gpkg")):
        for nome, tabela in estilos_do_gpkg(gpkg):
            print(f"\n[{nome}]")
            print(f"estilo = {gpkg}#{nome}")
            print(f"camada = {tabela}")
            print(f"saida  = {nome}.lyrx")
    for qml in sorted(pasta.rglob("*.qml")):
        print(f"\n[{qml.stem}]")
        print(f"estilo = {qml}")
    return 0


def tabela_ou_origem(gpkg, tabela):
    """(tabela, nome_da_vista): se `tabela` for uma vista, devolve a tabela de baixo.

    O GeoPackage do COS guarda os estilos em vistas (v_cos2025v1_n4). O ArcGIS
    Pro pode não listar vistas, por isso a configuração aponta para a tabela
    real e deixa a vista como alternativa comentada.
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
    """A pasta onde está o programa (a raiz do repo) — funciona também no .exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def extrair_gpkg(caminho):
    """Extrai os .gpkg de um zip para uma pasta com o nome do zip (só uma vez).

    O ArcGIS Pro não lê dentro de zips: para o .lyrx apontar para dados que
    existam, o GeoPackage tem de ficar em disco.
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
                print(f"a extrair {Path(caminho).name} -> {alvo.name} (pode demorar)",
                      file=sys.stderr)
                with z.open(membro) as origem, open(alvo, "wb") as saida:
                    shutil.copyfileobj(origem, saida, 1024 * 1024)
            achados.append(alvo)
    return achados


def descobrir_entradas(pasta):
    """Percorre a pasta (e os zips) e devolve uma entrada por estilo encontrado."""
    pasta = Path(pasta)
    entradas, usados = [], set()

    gpkgs = sorted(pasta.rglob("*.gpkg"))
    for z in sorted(pasta.rglob("*.zip")):
        try:
            gpkgs += extrair_gpkg(z)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"aviso: {z.name}: não consigo abrir ({exc})", file=sys.stderr)

    vistos_gpkg = set()
    for gpkg in gpkgs:
        gpkg = Path(gpkg).resolve()
        if gpkg in vistos_gpkg:      # o mesmo gpkg pode vir do zip e do rglob
            continue
        vistos_gpkg.add(gpkg)

        for estilo, tabela in estilos_do_gpkg(gpkg):
            try:
                qml = estilo_do_gpkg(gpkg, estilo)
            except SystemExit as exc:
                print(f"aviso: {exc}", file=sys.stderr)
                continue
            if "<renderer-v2" not in qml:
                print(f"aviso: '{estilo}' ({gpkg.name}) é um estilo de símbolo, "
                      "não de camada — ignorado", file=sys.stderr)
                continue

            camada, vista = tabela_ou_origem(gpkg, tabela)

            secao = re.sub(r"\W+", "_", estilo)
            while secao in usados:
                secao = f"{secao}_2"
            usados.add(secao)

            # os .lyrx vão para out/<nome-do-gpkg>_<data>/ na raiz do repo
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
    """Grava o converter.ini, com caminhos relativos à pasta."""
    pasta = Path(pasta)
    linhas = [
        "# qml2lyrx - gerado automaticamente a partir dos .gpkg desta pasta.",
        "# Podes editar: enquanto este ficheiro existir, o programa usa-o tal como",
        "# está (--forcar refaz a descoberta e substitui).",
        "#",
        "# Uma secção por camada; o nome da secção é o nome da camada no Pro.",
        "",
    ]
    for e in entradas:
        gpkg, estilo = e["estilo"]
        linhas.append(f"[{e['secao']}]")
        linhas.append(f"estilo = {gpkg.relative_to(pasta)}#{estilo}")
        if e["alternativa"]:
            linhas.append(f"# vista {e['alternativa']} (mais leve, se o Pro a abrir):")
            linhas.append(f"# camada = {e['alternativa']}")
        linhas.append(f"camada = {e['camada']}")
        linhas.append(f"saida = {os.path.relpath(e['saida'], pasta)}")
        linhas.append("")

    Path(caminho).write_text("\n".join(linhas), encoding="utf-8")


def modo_pasta(pasta, forcar=False):
    """Varre uma pasta: descobre estilos, escreve o converter.ini e converte."""
    pasta = Path(pasta).expanduser().resolve()
    if not pasta.is_dir():
        raise SystemExit(f"não encontro a pasta {pasta}")

    config = pasta / "converter.ini"
    if config.exists() and not forcar:
        print(f"{config.name} já existe — a usar esse (--forcar para refazer)")
    else:
        entradas = descobrir_entradas(pasta)
        if not entradas:
            raise SystemExit(f"não encontrei estilos em GeoPackages dentro de {pasta}")
        escrever_config(config, entradas, pasta)
        print(f"escrevi {config.name}: {len(entradas)} camadas encontradas")

    return lote(config)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Converte um estilo QML do QGIS num .lyrx do ArcGIS Pro.")
    p.add_argument("entrada", nargs="?",
                   help=".qml, 'dados.gpkg#Estilo', ou um .ini de configuração")
    p.add_argument("saida", nargs="?", help=".lyrx a escrever (só no modo de um ficheiro)")
    p.add_argument("-n", "--name", help="nome da camada (por omissão: nome do .lyrx)")
    p.add_argument("-s", "--source", help="GeoPackage ou FileGDB de origem")
    p.add_argument("-d", "--dataset", help="tabela / feature class")
    p.add_argument("-c", "--campo",
                   help="nome do campo no destino, se diferir do QML "
                        "(shapefiles cortam aos 10 caracteres)")
    p.add_argument("--descobrir", metavar="PASTA",
                   help="lista estilos e dados de uma pasta e imprime uma configuração")
    p.add_argument("--pasta", metavar="PASTA",
                   help="varre a pasta, escreve o converter.ini e converte tudo")
    p.add_argument("--forcar", action="store_true",
                   help="com --pasta, refaz o converter.ini mesmo que já exista")
    p.add_argument("--version", action="version", version=f"qml2lyrx {VERSAO}")
    a = p.parse_args(argv)

    if a.descobrir:
        return descobrir(a.descobrir)
    if a.pasta:
        return modo_pasta(a.pasta, a.forcar)
    if not a.entrada:
        p.error("indica um .qml, um 'gpkg#Estilo' ou um .txt de configuração")
    if Path(a.entrada).suffix.lower() in (".txt", ".cfg", ".ini"):
        return lote(a.entrada)
    if not a.saida:
        p.error("falta o .lyrx de saída")

    if Path(a.saida).suffix.lower() == ".stylx":
        escrever_stylx(a.entrada, a.saida, a.name)
        return 0

    converter(a.entrada, a.saida, a.source, a.dataset, a.name, a.campo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
