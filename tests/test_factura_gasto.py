"""Facturas y recibos de gasto: aplicación a 08_GASTOS, deducción en el 210 y tasa de basura del IBI."""
from pathlib import Path

import fitz
import pytest

import pdf_processor as pp
import tax_calculator as tc
from main import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.db.set_config("periodicidad_210_alquiler", "Trimestral")  # estos casos verifican cuotas de 1T
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


def _pdf(ruta: Path, texto: str) -> Path:
    doc = fitz.open()
    doc.new_page().insert_text((50, 72), texto, fontsize=10)
    doc.save(ruta)
    doc.close()
    return ruta


def _cliente_con_piso(ctrl) -> int:
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2026)["id"]
    ctrl.editar(cid, "inmuebles", None, {"direccion": "Calle Mayor 1", "fecha_adquisicion": "01/01/2019",
                                         "pct_titularidad": "100", "estado": "Activo"}, 2026)
    ctrl.editar(cid, "contratos", None, {"inmueble": "INM-001", "fecha_inicio": "01/01/2026",
                                         "renta_inicial": "1000", "estado": "Vigente"}, 2026)
    return cid


def _factura(categoria, total, n="F-1", fecha="2026-02-10", **extra):
    return pp.Extraccion(tipo=pp.TipoDocumento.FACTURA_GASTO, confianza=0.95, nombre_archivo="factura.pdf",
                         motor="ia", campos={"categoria": categoria, "total": total, "n_factura": n,
                                             "proveedor": "Iberdrola", "fecha": fecha, **extra})


@pytest.mark.parametrize("texto,esperada", [
    ("Suministros", "Suministros"), ("luz", "Suministros"), ("Seguro de hogar", "Seguro"),
    ("Intereses/financiación", "Intereses/financiación"), ("cuota comunidad", "Comunidad"),
    ("Tasa de basuras", "Tasa/basura"), ("Reparación fontanería", "Reparación/conservación"), ("raro", "Otros"),
])
def test_categoria_gasto(texto, esperada):
    assert pp.categoria_gasto(texto) == esperada


def test_factura_crea_gasto_y_descuenta_en_el_210(ctrl, tmp_path):
    cid = _cliente_con_piso(ctrl)
    cuota_antes = ctrl.db.get_por_clave("m210_alquiler", cid, "210A|2026|INM-001|1T")["cuota"]
    pdf = _pdf(tmp_path / "luz.pdf", "Factura de luz Iberdrola F-1")
    res = ctrl.procesar_pdf(cid, pdf, 2026, confirmar=lambda ext: _factura("luz", 120.0, concepto="Luz"))
    gasto = ctrl.db.get("gastos", cid, res["resultado"].codigos["gasto"])
    assert gasto["categoria"] == "Suministros" and gasto["total"] == 120 and gasto["origen"] == "pdf"
    assert gasto["inmueble"] == "INM-001"            # un solo inmueble: se asigna solo
    assert gasto["ruta"].startswith("Soportes_PDF/") and gasto["observaciones"] == "Luz"
    cuota_despues = ctrl.db.get_por_clave("m210_alquiler", cid, "210A|2026|INM-001|1T")["cuota"]
    assert cuota_despues == pytest.approx(cuota_antes - 120 * 0.19, abs=0.02)


def test_misma_factura_escaneada_dos_veces_no_duplica(ctrl, tmp_path):
    cid = _cliente_con_piso(ctrl)
    ctrl.procesar_pdf(cid, _pdf(tmp_path / "a.pdf", "Factura A"), 2026,
                      confirmar=lambda ext: _factura("Comunidad", 90.0, n="C-7"))
    ctrl.procesar_pdf(cid, _pdf(tmp_path / "b.pdf", "Factura A (escaneo 2)"), 2026,
                      confirmar=lambda ext: _factura("Comunidad", 95.0, n="C-7"))
    gastos = [g for g in ctrl.db.listar("gastos", cid) if g["categoria"] == "Comunidad"]
    assert len(gastos) == 1 and gastos[0]["total"] == 95


def test_factura_sin_inmueble_con_varios_pisos_avisa(ctrl, tmp_path):
    cid = _cliente_con_piso(ctrl)
    ctrl.editar(cid, "inmuebles", None, {"direccion": "Calle Sol 2", "estado": "Activo"}, 2026)
    res = ctrl.procesar_pdf(cid, _pdf(tmp_path / "s.pdf", "Seguro"), 2026,
                            confirmar=lambda ext: _factura("Seguro", 300.0))
    assert "gasto" not in res["resultado"].codigos
    assert any("sin inmueble" in m for m in res["resultado"].mensajes)


def test_tasa_basura_del_ibi_crea_su_gasto(ctrl, tmp_path):
    cid = _cliente_con_piso(ctrl)

    def ibi(_ext):
        return pp.Extraccion(tipo=pp.TipoDocumento.CATASTRO_IBI, confianza=0.9, nombre_archivo="ibi.pdf",
                             campos={"ejercicio": 2026, "valor_catastral": 90000, "ibi_ejercicio": 450,
                                     "tasa_basura": 85, "inmueble": "INM-001"})
    ctrl.procesar_pdf(cid, _pdf(tmp_path / "ibi.pdf", "Recibo IBI"), 2026, confirmar=ibi, inmueble_destino="INM-001")
    cats = {g["categoria"]: g["total"] for g in ctrl.db.listar("gastos", cid)}
    assert cats["IBI"] == 450 and cats["Tasa/basura"] == 85


def test_hipoteca_solo_intereses_y_extra_ue_no_deduce(ctrl, tmp_path):
    cid = _cliente_con_piso(ctrl)
    ctrl.editar(cid, "residencias", "2026", {"pais": "Estados Unidos"}, 2026)
    ctrl.procesar_pdf(cid, _pdf(tmp_path / "h.pdf", "Recibo préstamo"), 2026,
                      confirmar=lambda ext: _factura("Intereses/financiación", 210.0, n="H-3"))
    fila = ctrl.db.get_por_clave("m210_alquiler", cid, "210A|2026|INM-001|1T")
    assert fila["gastos"] == 0 and fila["tipo"] == tc.TIPO_EXTRA_UE  # Extra-UE: 24 % sobre íntegros


def test_desglose_excel_no_depende_de_q(ctrl, tmp_path):
    """La columna Q de la plantilla (N*O*P) vale 0 con O/P vacíos: el desglose debe usar R o N×O×P con 100 %
    por defecto. Verificado además recalculando en Excel real (suministros 100 + 25, seguro 150 + 123)."""
    import openpyxl
    cid = _cliente_con_piso(ctrl)
    ctrl.alta_gasto(cid, 2026, "INM-001", "Suministros", "2026-03-10", 100)
    ruta = ctrl.sincronizar(cid, 2026)["excel"]
    ws = openpyxl.load_workbook(ruta)["00_RESUMEN"]
    formulas = [ws.cell(f, c).value for f in range(31, 43) for c in (2, 3)]
    assert all(str(v).startswith("=SUMPRODUCT(") for v in formulas)
    assert not any("$Q$" in str(v) for v in formulas)
