"""Extracción de PDFs sintéticos y aplicación al esquema real de Database."""
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures_pdf as f
import pdf_processor as pp
import tax_calculator as tc
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.carpeta_clientes = tmp_path / "Clientes"
    yield d
    d.cerrar()


@pytest.fixture
def cliente(db):
    return db.crear_cliente("Pepito", "Pérez", "X1234567A")


@pytest.mark.parametrize("crear,tipo,campo,valor", [
    (f.escritura_compra, pp.TipoDocumento.ESCRITURA_COMPRA, "precio_escritura", 180000),
    (f.escritura_venta, pp.TipoDocumento.ESCRITURA_VENTA, "precio_venta", 250000),
    (f.recibo_ibi, pp.TipoDocumento.CATASTRO_IBI, "valor_catastral", 90000),
    (f.valor_referencia, pp.TipoDocumento.VALOR_REFERENCIA, "valor_referencia", 210000),
    (f.contrato_alquiler, pp.TipoDocumento.CONTRATO_ALQUILER, "renta_inicial", 1000),
    (f.modelo_210, pp.TipoDocumento.MODELO_210, "tipo_renta", "01"),
    (f.certificado_residencia, pp.TipoDocumento.CERTIFICADO_RESIDENCIA, "pais", "Francia"),
    (f.modelo_036, pp.TipoDocumento.MODELO_036_037, "modelos", "303,130"),
    (f.modelo_presentado, pp.TipoDocumento.MODELO_PRESENTADO, "modelo", "303"),
])
def test_clasificacion_y_extraccion_por_tipo(tmp_path, crear, tipo, campo, valor):
    ruta = crear(tmp_path / "entrada.pdf")  # el nombre no da pistas
    anterior = ruta.read_bytes()
    ext = pp.extraer(ruta)
    assert ext.tipo == tipo and 0.5 <= ext.confianza <= 1
    assert ext.campos[campo] == valor
    assert ext.texto and not ext.necesita_ocr
    assert ext.nombre_archivo == "entrada.pdf"
    assert ruta.read_bytes() == anterior
    if tipo == pp.TipoDocumento.CONTRATO_ALQUILER:
        assert ext.inquilinos[0]["nombre"] == "Ana López"
        assert ext.inquilinos[0]["documento"] == "12345678Z"
        assert ext.campos["fecha_fin_prevista"] == "2026-12-31"
    if tipo == pp.TipoDocumento.CATASTRO_IBI:
        assert ext.campos["valor_suelo"] == 36000
        assert ext.campos["valor_construccion"] == 54000
        assert ext.campos["ibi_ejercicio"] == 450
        assert ext.campos["anio_revision"] == 2019
    if tipo == pp.TipoDocumento.MODELO_210:
        assert ext.campos["ejercicio"] == 2026
        assert ext.campos["periodo"] == "3T"
        assert ext.campos["fecha_presentacion"] == "2026-10-15"


@pytest.mark.parametrize("importe,esperado", [("1.234,56 €", 1234.56), ("1234.56", 1234.56),
                                               ("1.234 €", 1234), ("0,00 euros", 0)])
@pytest.mark.parametrize("fecha", ["15/03/2024", "15-03-2024", "15 de marzo de 2024", "2024-03-15"])
def test_formatos_importes_fechas_y_referencia_con_espacios(tmp_path, importe, esperado, fecha):
    texto = f"""ESCRITURA PÚBLICA DE COMPRAVENTA - NOTARIO
Cliente comprador. Documento sintético para revisar extracción de importes y fechas.
Fecha escritura: {fecha}
Precio de compra: {importe}
Referencia catastral: 1234567 AB 1234 C 0001 DE
CP: 03001
"""
    ruta = f._crear(tmp_path / "formatos.pdf", texto)
    ext = pp.extraer(ruta)
    assert ext.campos["precio_escritura"] == esperado
    assert ext.campos["fecha_adquisicion"] == "2024-03-15"
    assert ext.campos["ref_catastral"] == f.REF
    assert ext.campos["cp"] == "03001"


def test_ocr_fallido_advierte_y_ocr_disponible_recupera(monkeypatch, tmp_path):
    class Pagina:
        disponible = False

        def get_text(self, textpage=None):
            return "Certificado de residencia fiscal. Texto reconocido por OCR para revisión manual." if textpage else ""

        def get_textpage_ocr(self):
            if not self.disponible:
                raise RuntimeError("Tesseract no instalado")
            return object()

    class Documento:
        needs_pass = False

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([pagina])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    pagina = Pagina()
    monkeypatch.setattr(pp.fitz, "open", lambda *args: Documento())
    (tmp_path / "escaneado.pdf").write_bytes(b"%PDF-1.4 fixture OCR")
    ext = pp.extraer(tmp_path / "escaneado.pdf")
    assert ext.necesita_ocr and pp.AVISO_OCR in ext.avisos
    pagina.disponible = True
    texto, pendiente = pp.extraer_texto(tmp_path / "escaneado.pdf")
    assert "reconocido por OCR" in texto and not pendiente


def test_no_confunde_vendedor_generico_con_cliente_vendedor(tmp_path):
    ruta = f.escritura_compra(tmp_path / "compra.pdf", titulares=["Pepito Pérez"], gananciales=True)
    ext = pp.extraer(ruta)
    assert ext.tipo == pp.TipoDocumento.ESCRITURA_COMPRA
    assert ext.campos["pct_titularidad"] == 0.5 and ext.campos["gananciales_forzado"] == 1
    assert any("compra o vende" in aviso for aviso in ext.avisos)
    assert pp.clasificar("", "modelo_210.pdf")[0] == pp.TipoDocumento.MODELO_210
    assert pp.clasificar("Texto sin indicios tributarios.")[0] == pp.TipoDocumento.DESCONOCIDO


def test_dos_titulares_y_duracion_de_contrato(tmp_path):
    compra = pp.extraer(f.escritura_compra(tmp_path / "compra.pdf", titulares=["Pepito Pérez", "María López"]))
    assert len(compra.titulares) == 2 and compra.campos["pct_titularidad"] == 0.5
    contrato = pp.extraer(f.contrato_alquiler(tmp_path / "contrato.pdf", fecha_inicio="2025-07-01",
                                             fecha_fin_prevista=None, duracion="1 año"))
    assert contrato.campos["fecha_fin_prevista"] == "2026-06-30"


def test_flujo_pepito_aplica_calcula_y_congela_presentado(db, cliente, tmp_path):
    cid = cliente["id"]
    entradas = [
        f.certificado_residencia(tmp_path / "residencia.pdf"),
        f.escritura_compra(tmp_path / "compra.pdf", gananciales=True),
        f.recibo_ibi(tmp_path / "ibi.pdf"),
        f.contrato_alquiler(tmp_path / "contrato.pdf"),
        f.modelo_210(tmp_path / "210.pdf"),
    ]
    resultados = [pp.procesar_pdf(db, cid, ruta, ejercicio=2026) for ruta in entradas]
    inm = resultados[1].codigos["inmueble"]
    assert resultados[1].ejercicios_afectados == {2019}
    assert resultados[3].ejercicios_afectados == {2026}
    assert db.get("inmuebles", cid, inm)["pct_titularidad"] == 0.5
    assert len(db.listar("inquilinos", cid)) == 1
    assert len(db.listar("gastos", cid)) == 1
    assert len(db.listar("documentos", cid)) == 5
    for r in resultados:
        assert r.ruta_relativa.startswith("Soportes_PDF/")
        assert "\\" not in r.ruta_relativa
        assert (Path(cliente["carpeta"]) / r.ruta_relativa).is_file()
    documento_contrato = db.get("documentos", cid, resultados[3].documento)
    assert documento_contrato["contrato"] == resultados[3].codigos["contrato"]
    inf = tc.recalcular_cliente(db, cid, 2026)
    modelos = {r["periodo"]: r for r in db.listar("m210_alquiler", cid)}
    assert set(modelos) == {"3T"}
    assert modelos["3T"]["estado"] == "Presentado" and modelos["3T"]["pagado"] == 285
    assert modelos["3T"]["cuota"] == 285
    assert inm in inf.alquileres_bloqueados  # no inventar otro modelo junto al histórico trimestral presentado
    assert db.listar("m210_imputacion", cid)[0]["dias"] == 181
    assert "Tiene 1 piso," in inf.mensaje_patrimonio
    assert inf.patrimonio.valor_bienes == 90000


def test_ibi_y_referencia_se_fusionan_y_ibi_no_duplica(db, cliente, tmp_path):
    cid = cliente["id"]
    a = pp.procesar_pdf(db, cid, f.recibo_ibi(tmp_path / "ibi.pdf"))
    assert db.get("inmuebles", cid, a.codigos["inmueble"])["estado"] == "En revisión"
    pp.procesar_pdf(db, cid, f.valor_referencia(tmp_path / "valor.pdf"))
    pp.procesar_pdf(db, cid, f.recibo_ibi(tmp_path / "ibi-corregido.pdf", ibi=500))
    catastros, gastos = db.listar("catastro", cid), db.listar("gastos", cid)
    assert len(catastros) == len(gastos) == 1
    assert catastros[0]["valor_referencia"] == 210000
    assert catastros[0]["valor_catastral"] == 90000
    assert gastos[0]["total"] == 500 and gastos[0]["origen"] == "pdf"
    assert gastos[0]["periodo_desde"] == "2026-01-01"


def test_duplicado_sha256_aunque_cambie_el_nombre(db, cliente, tmp_path):
    cid = cliente["id"]
    ruta = f.escritura_compra(tmp_path / "original.pdf")
    a = pp.procesar_pdf(db, cid, ruta)
    copia = tmp_path / "otro_nombre.pdf"
    shutil.copyfile(ruta, copia)
    ext = pp.extraer(copia)
    ext.campos["precio_escritura"] = 1  # Un duplicado no reaplica ni estas correcciones.
    b = pp.aplicar(db, cid, ext, copia)
    assert b.duplicado and b.documento == a.documento and b.ruta_relativa == a.ruta_relativa
    assert b.mensajes == [f"Ya archivado como {a.documento}"]
    assert len(db.listar("documentos", cid)) == len(db.listar("inmuebles", cid)) == 1
    assert db.get("inmuebles", cid, a.codigos["inmueble"])["precio_escritura"] == 180000
    assert len(list((Path(cliente["carpeta"]) / "Soportes_PDF").glob("*.pdf"))) == 1


def test_archivado_sin_colisiones_y_fuente_intacta(tmp_path):
    carpeta = tmp_path / "Cliente"
    uno = f.escritura_compra(tmp_path / "uno" / "escritura.pdf", precio=100)
    dos = f.escritura_compra(tmp_path / "dos" / "escritura.pdf", precio=200)
    a, ha = pp.archivar_pdf(uno, carpeta, pp.TipoDocumento.ESCRITURA_COMPRA, date(2019, 5, 10))
    b, hb = pp.archivar_pdf(dos, carpeta, pp.TipoDocumento.ESCRITURA_COMPRA, date(2019, 5, 10))
    assert a != b and ha != hb  # La huella del contenido evita colisiones sin sufijo numérico.
    assert a.startswith("Soportes_PDF/2019-05-10_ESCRITURA_COMPRA_")
    assert (carpeta / a).read_bytes() == uno.read_bytes()
    assert (carpeta / b).read_bytes() == dos.read_bytes()


def test_venta_sin_inmueble_no_crea_y_destino_permite_aplicar(db, cliente, tmp_path):
    cid = cliente["id"]
    ruta = f.escritura_venta(tmp_path / "venta.pdf")
    r = pp.procesar_pdf(db, cid, ruta)
    assert not db.listar("inmuebles", cid)
    assert any("Venta sin inmueble" in m for m in r.mensajes)
    inm = db.upsert("inmuebles", cid, {"ref_catastral": "otra", "estado": "Activo"})
    # Otro documento: el primero ya está registrado y no se reaplica.
    ruta = f.escritura_venta(tmp_path / "venta-corregida.pdf", precio=260000)
    r = pp.procesar_pdf(db, cid, ruta, inmueble_destino=inm)
    assert r.codigos["inmueble"] == inm
    assert db.get("inmuebles", cid, inm)["estado"] == "Vendido"
    assert db.get("inmuebles", cid, inm)["fecha_venta"] == "2026-08-15"
    assert r.ejercicios_afectados == {2026}


def test_contrato_indefinido_por_direccion_o_destino_y_multiples_inquilinos(db, cliente, tmp_path):
    cid = cliente["id"]
    inm = db.upsert("inmuebles", cid, {"direccion": f.DIRECCION})
    ruta = f.contrato_alquiler(tmp_path / "contrato.pdf", ref="", fecha_inicio="2025-07-01",
                               fecha_fin_prevista=None, direccion="C/ MAYOR 1 - 2 A", inquilinos=[
                                   {"nombre": "Ana López", "documento": "12345678Z", "pais": "España"},
                                   {"nombre": "Luis Núñez", "documento": "87654321X", "pais": "España"},
                               ])
    r = pp.procesar_pdf(db, cid, ruta, ejercicio=2026)
    assert r.codigos["inmueble"] == inm
    assert r.ejercicios_afectados == {2025, 2026}
    assert len(db.listar("inquilinos", cid)) == 2
    sin_destino = f.contrato_alquiler(tmp_path / "otro.pdf", ref="", direccion="Otra dirección 90")
    r = pp.procesar_pdf(db, cid, sin_destino)
    assert "contrato" not in r.codigos
    assert len(db.listar("contratos", cid)) == 1


@pytest.mark.parametrize("renta,tabla,clave", [
    ("01", "m210_alquiler", "210A|2026|{inm}|Anual"),
    ("02", "m210_imputacion", "210I|2026|{inm}"),
    ("28", "obligaciones", "VENTA|{inm}"),
])
def test_modelos_210_comparten_claves_de_calculadora(db, cliente, tmp_path, renta, tabla, clave):
    cid = cliente["id"]
    inm = db.upsert("inmuebles", cid, {"ref_catastral": f.REF})
    r = pp.procesar_pdf(db, cid, f.modelo_210(tmp_path / "210.pdf", tipo_renta=renta, periodo="0A"))
    fila = db.get_por_clave(tabla, cid, clave.format(inm=inm))
    assert fila["estado"] == "Presentado" and fila["pagado"] == 285
    assert r.ejercicios_afectados == {2026}
    if renta == "02":
        assert fila["renta_imputada"] == 1500


def test_residencia_y_autonomos_aplicacion_y_confirmacion(db, cliente, tmp_path):
    cid = cliente["id"]
    pp.procesar_pdf(db, cid, f.certificado_residencia(tmp_path / "res.pdf"))
    assert db.residencia(cid, 2026)["pais"] == "Francia"
    assert db.residencia(cid, 2026)["certificado"] == "Sí"
    assert db.residencia(cid, 2026)["grupo"] == "UE/EEE"
    llamadas = []

    def confirmar(ext):
        llamadas.append(ext.tipo)
        ext.campos["epigrafe_iae"] = "765"
        return ext

    pp.procesar_pdf(db, cid, f.modelo_036(tmp_path / "036.pdf"), confirmar=confirmar)
    assert llamadas == [pp.TipoDocumento.MODELO_036_037]
    # Desde la separación de carteras un 036 no saca al cliente de No residentes: solo avisa
    assert db.get_cliente(cid)["tipo_cliente"] == "No residente"
    assert db.listar("actividades", cid)[0]["epigrafe_iae"] == "765"
    db.actualizar_cliente(cid, tipo_cliente="Autónomo")
    pp.procesar_pdf(db, cid, f.modelo_presentado(tmp_path / "303.pdf"))
    modelo = db.get_por_clave("modelos_presentados", cid, "MOD|303|2026|1T")
    assert modelo["estado"] == "Presentado" and modelo["importe"] == 200
    tc.recalcular_autonomo(db, cid, 2026)
    obligacion = db.get_por_clave("obligaciones", cid, "AUT|303|2026|1T")
    assert obligacion["estado"] == "Presentado"


def test_desconocido_solo_archiva_y_confianza_validada(db, cliente, tmp_path):
    ruta = f._crear(tmp_path / "nota.pdf", "Carta informativa particular sin datos tributarios ni identificación de inmuebles.")
    ext = pp.extraer(ruta)
    assert ext.tipo == pp.TipoDocumento.DESCONOCIDO and ext.avisos
    r = pp.aplicar(db, cliente["id"], ext, ruta)
    assert r.codigos == {} and not r.ejercicios_afectados
    assert len(db.listar("documentos", cliente["id"])) == 1
    with pytest.raises(ValueError):
        pp.Extraccion(tipo=pp.TipoDocumento.DESCONOCIDO, confianza=2, nombre_archivo="x.pdf")


def test_revision_usuario_puede_desactivar_gananciales(db, cliente, tmp_path):
    ruta = f.escritura_compra(tmp_path / "gananciales.pdf", gananciales=True)

    def confirmar(ext):
        ext.campos.update(gananciales_forzado=0, pct_titularidad=1)
        return ext

    r = pp.procesar_pdf(db, cliente["id"], ruta, confirmar=confirmar)
    inmueble = db.get("inmuebles", cliente["id"], r.codigos["inmueble"])
    assert inmueble["pct_titularidad"] == 1 and inmueble["gananciales_forzado"] == 0


def test_dato_unicode_nfd_e_importe_tras_dos_puntos():
    texto_nfd = "Direccio\u0301n: Calle Nu\u0301n\u0303ez de Balboa 15, 2º B\nCuota IBI (ejercicio 2026): 450,75 €"
    assert pp._dato(texto_nfd, "dirección") == "Calle Núñez de Balboa 15, 2º B"
    assert pp._importe(pp._dato(texto_nfd, "cuota ibi")) == 450.75

