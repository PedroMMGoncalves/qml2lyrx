import json
import shutil
import sqlite3
import sys
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI))

import qml2lyrx  # noqa: E402

EXEMPLO = AQUI / "exemplo.qml"
TMP = AQUI / ".tmp"


def _criar_gpkg(caminho):
    """GeoPackage mínimo: uma tabela, uma vista e um estilo com 2 regras."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.unlink(missing_ok=True)
    con = sqlite3.connect(caminho)
    con.execute("create table parcelas (id integer)")
    con.execute("create view v_parcelas as select * from parcelas")
    con.execute("create table layer_styles (styleName text, f_table_name text, styleQML text)")
    con.execute("insert into layer_styles values ('n4', 'v_parcelas', ?)", (QML_REGRAS,))
    con.commit()
    con.close()

SIMPLES = """<?xml version="1.0" encoding="utf-8"?>
<qgis version="3.34.11-Prizren">
 <renderer-v2 type="singleSymbol">
  <symbols>
   <symbol type="fill" name="0" alpha="1">
    <layer class="SimpleFill" enabled="1">
     <Option name="color" value="10,20,30,255"/>
     <Option name="outline_style" value="no"/>
    </layer>
   </symbol>
  </symbols>
 </renderer-v2>
</qgis>
"""

QML_REGRAS = """<?xml version="1.0" encoding="utf-8"?>
<qgis version="3.34.11-Prizren">
 <renderer-v2 type="RuleRenderer">
  <rules>
   <rule symbol="0" filter="&quot;classe&quot; = 'a'" label="A"/>
   <rule symbol="0" filter="&quot;classe&quot; = 'b'" label="B"/>
  </rules>
  <symbols>
   <symbol type="fill" name="0" alpha="1">
    <layer class="SimpleFill" enabled="1">
     <Option name="color" value="200,10,10,255"/>
     <Option name="outline_style" value="no"/>
    </layer>
   </symbol>
  </symbols>
 </renderer-v2>
</qgis>
"""


class TestExemploReal(unittest.TestCase):
    """O COS25v1: 102 regras, mas só 101 valores distintos."""

    def test_le_as_101_classes(self):
        campo, classes = qml2lyrx.ler_qml(EXEMPLO)
        self.assertEqual(campo, "cos25v1_n4_c")
        self.assertEqual(len(classes), 101)
        self.assertEqual(classes[0]["simbolo"]["cor"], (230, 0, 77, 100))
        self.assertIsNone(classes[0]["simbolo"]["contorno"])  # outline_style = no

    def test_renderer(self):
        campo, classes = qml2lyrx.ler_qml(EXEMPLO)
        r = qml2lyrx.renderer(campo, classes)
        self.assertEqual(r["type"], "CIMUniqueValueRenderer")
        self.assertEqual(r["fields"], ["cos25v1_n4_c"])
        self.assertEqual(len(r["groups"][0]["classes"]), 101)
        self.assertEqual(r["groups"][0]["classes"][0]["values"][0]["fieldValues"], ["1.1.1.1"])

    def test_escreve_o_ficheiro(self):
        TMP.mkdir(exist_ok=True)
        saida = TMP / "teste.lyrx"
        qml2lyrx.main([str(EXEMPLO), str(saida), "-d", "cos2025v1"])
        doc = json.loads(saida.read_text(encoding="utf-8"))
        self.assertEqual(doc["type"], "CIMLayerDocument")
        self.assertEqual(len(doc["layerDefinitions"][0]["renderer"]["groups"][0]["classes"]), 101)

        # o campo tem 13 caracteres: tem de sair a variante para shapefiles
        gemeo = TMP / "teste_shapefile.lyrx"
        self.assertTrue(gemeo.exists())
        r = json.loads(gemeo.read_text(encoding="utf-8"))["layerDefinitions"][0]["renderer"]
        self.assertEqual(r["fields"], ["cos25v1_n4"])


class TestSimboloUnico(unittest.TestCase):
    def test_single_symbol(self):
        TMP.mkdir(exist_ok=True)
        qml = TMP / "simples.qml"
        qml.write_text(SIMPLES, encoding="utf-8")

        campo, classes = qml2lyrx.ler_qml(qml)
        self.assertIsNone(campo)
        r = qml2lyrx.renderer(campo, classes)
        self.assertEqual(r["type"], "CIMSimpleRenderer")
        self.assertEqual(r["symbol"]["symbol"]["symbolLayers"][0]["type"], "CIMSolidFill")


class TestModoPasta(unittest.TestCase):
    """--pasta: descobre os estilos, escreve o converter.ini e converte."""

    def setUp(self):
        self.pasta = TMP / "pasta"
        shutil.rmtree(self.pasta, ignore_errors=True)
        _criar_gpkg(self.pasta / "dados" / "x.gpkg")

        self.ferramenta = TMP / "ferramenta"
        shutil.rmtree(self.ferramenta, ignore_errors=True)
        patcher = mock.patch.object(qml2lyrx, "pasta_ferramenta", lambda: self.ferramenta)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _saida(self, gpkg_nome, estilo):
        return self.ferramenta / "out" / f"{gpkg_nome}_{date.today().isoformat()}" / f"{estilo}.lyrx"

    def test_gera_config_e_converte(self):
        codigo = qml2lyrx.main(["--pasta", str(self.pasta)])
        self.assertEqual(codigo, 0)

        config = (self.pasta / "converter.ini").read_text(encoding="utf-8")
        self.assertIn("camada = parcelas", config)          # a vista foi resolvida
        self.assertIn("# camada = v_parcelas", config)      # e a alternativa ficou em comentário
        self.assertTrue(self._saida("x", "n4").exists())
        # a cada .lyrx sai também o .stylx
        self.assertTrue(self._saida("x", "n4").with_suffix(".stylx").exists())

    def test_nao_refaz_o_config_existente(self):
        (self.pasta / "converter.ini").write_text(
            "[n4]\nestilo = dados/x.gpkg#n4\ncamada = parcelas\nsaida = out/n4.lyrx\n",
            encoding="utf-8",
        )
        codigo = qml2lyrx.main(["--pasta", str(self.pasta)])
        self.assertEqual(codigo, 0)
        self.assertNotIn("# gerado", (self.pasta / "converter.ini").read_text(encoding="utf-8"))


class TestZip(unittest.TestCase):
    """Um .gpkg dentro de um zip é extraído e convertido."""

    def setUp(self):
        self.pasta = TMP / "pasta_zip"
        shutil.rmtree(self.pasta, ignore_errors=True)
        self.pasta.mkdir(parents=True)

        origem = TMP / "origem.gpkg"
        _criar_gpkg(origem)
        with zipfile.ZipFile(self.pasta / "lote.zip", "w") as z:
            z.write(origem, "dados/origem.gpkg")

        self.ferramenta = TMP / "ferramenta_zip"
        shutil.rmtree(self.ferramenta, ignore_errors=True)
        patcher = mock.patch.object(qml2lyrx, "pasta_ferramenta", lambda: self.ferramenta)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_extrai_e_converte(self):
        codigo = qml2lyrx.main(["--pasta", str(self.pasta)])
        self.assertEqual(codigo, 0)

        # extraiu para uma pasta com o nome do zip
        self.assertTrue((self.pasta / "lote" / "origem.gpkg").exists())
        alvo = (self.ferramenta / "out" /
                f"origem_{date.today().isoformat()}" / "n4.lyrx")
        self.assertTrue(alvo.exists())
        doc = json.loads(alvo.read_text(encoding="utf-8"))
        conexao = doc["layerDefinitions"][0]["featureTable"]["dataConnection"]
        self.assertIn("lote", conexao["workspaceConnectionString"])  # aponta para o extraído


class TestStylx(unittest.TestCase):
    """.stylx: um estilo do Pro com um símbolo por classe."""

    def test_gera_estilo(self):
        TMP.mkdir(exist_ok=True)
        alvo = TMP / "teste.stylx"
        self.assertEqual(qml2lyrx.main([str(EXEMPLO), str(alvo)]), 0)

        con = sqlite3.connect(alvo)
        try:
            self.assertEqual(con.execute("select count(*) from ITEMS").fetchone()[0], 101)
            self.assertEqual(con.execute("select distinct CLASS from ITEMS").fetchone()[0], 5)
            tabelas = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
            self.assertEqual(len(tabelas), 6)
            conteudo = con.execute("select CONTENT from ITEMS order by ID limit 1").fetchone()[0]
        finally:
            con.close()

        simbolo = json.loads(conteudo)
        self.assertEqual(simbolo["type"], "CIMPolygonSymbol")
        self.assertEqual(simbolo["symbolLayers"][0]["color"]["values"], [230, 0, 77, 100])


if __name__ == "__main__":
    unittest.main()
