"""Propuestas de la auditoría A4 aplicadas por Claude (P01, P02, P04, P05, P06)."""
from datetime import date

import openpyxl
import pytest

import fixtures_pdf as f
import pdf_processor as pp
import tax_calculator as tc
from main import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


@pytest.fixture
def pepito(ctrl, tmp_path):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2026)["id"]
    ctrl.procesar_pdf(cid, f.escritura_compra(tmp_path / "esc.pdf"), 2026)
    return cid


def test_p01_excel_guardado_durante_el_proceso_no_se_pisa(ctrl, pepito, tmp_path):
    ruta = ctrl.db.ruta_excel(ctrl.db.get_cliente(pepito))

    def guardar_excel_mientras_reviso(ext):
        wb = openpyxl.load_workbook(ruta)
        wb["02_INMUEBLES"]["AA2"] = "nota del usuario"
        wb.save(ruta)
        wb.close()
        return ext

    res = ctrl.procesar_pdf(pepito, f.recibo_ibi(tmp_path / "ibi.pdf"), 2026, confirmar=guardar_excel_mientras_reviso)
    assert res["error_excel"] and "PENDIENTE" in res["excel"].name
    wb = openpyxl.load_workbook(ruta)
    assert wb["02_INMUEBLES"]["AA2"].value == "nota del usuario"   # su versión sigue intacta
    wb.close()
    ctrl.sincronizar(pepito, 2026)                                  # al sincronizar se unen
    assert ctrl.db.get("inmuebles", pepito, "INM-001")["observaciones"] == "nota del usuario"
    assert ctrl.db.listar("catastro", pepito)                       # y el IBI no se perdió


def test_p02_fallo_a_mitad_permite_reintentar(ctrl, pepito, tmp_path, monkeypatch):
    pdf = f.contrato_alquiler(tmp_path / "ctr.pdf", fecha_inicio="2026-07-01", fecha_fin_prevista="2027-06-30")
    original = ctrl.db.upsert

    def upsert_que_falla(tabla, *a, **k):
        if tabla == "contratos":
            raise RuntimeError("disco lleno")
        return original(tabla, *a, **k)

    monkeypatch.setattr(ctrl.db, "upsert", upsert_que_falla)
    with pytest.raises(RuntimeError):
        ctrl.procesar_pdf(pepito, pdf, 2026)
    monkeypatch.setattr(ctrl.db, "upsert", original)
    res = ctrl.procesar_pdf(pepito, pdf, 2026)
    assert not res["resultado"].duplicado and ctrl.db.listar("contratos", pepito)


def test_p04_borrar_inmueble_arrastra_dependientes(ctrl, pepito, tmp_path):
    ctrl.procesar_pdf(pepito, f.contrato_alquiler(tmp_path / "ctr.pdf", fecha_inicio="2026-07-01",
                                                  fecha_fin_prevista="2027-06-30"), 2026)
    assert ctrl.db.listar("inquilinos", pepito) and ctrl.db.listar("m210_alquiler", pepito)
    ctrl.borrar_registro(pepito, "inmuebles", "INM-001", 2026)
    for t in ("contratos", "inquilinos", "cobros", "m210_alquiler", "m210_imputacion", "catastro"):
        assert ctrl.db.listar(t, pepito) == [], t


def test_p04_no_borra_inmueble_con_declaracion_presentada(ctrl, pepito):
    inf = ctrl.sincronizar(pepito, 2026)["informe"]
    ctrl.marcar_presentado(pepito, "m210_imputacion", inf.filas_210_imputacion[0], 2026, None, "10")
    with pytest.raises(ValueError, match="presentadas"):
        ctrl.borrar_registro(pepito, "inmuebles", "INM-001", 2026)
    assert ctrl.db.get("inmuebles", pepito, "INM-001")


def test_p05_falta_de_contrato_no_desaparece_al_cambiar_de_anio(ctrl, pepito):
    ctrl.editar(pepito, "contratos", None, {"inmueble": "INM-001", "fecha_inicio": "01/07/2026",
                                            "renta_inicial": "800"}, 2026)
    assert any(p.titulo == "Falta: Contrato de alquiler" for p in tc.revisar_pendientes(ctrl.db, pepito, 2026))
    tc.revisar_pendientes(ctrl.db, pepito, 2025)  # consultar otro año no borra la falta
    faltas = [d for d in ctrl.db.listar("documentos", pepito) if d["clave"] == "FALTA|-|CTR-001|contrato"]
    assert faltas and faltas[0]["estado"] == "Falta"


def test_p06_pdf_no_pisa_estado_corregido_a_mano(ctrl, pepito, tmp_path):
    pdf = f.contrato_alquiler(tmp_path / "ctr.pdf", fecha_inicio="2026-01-01", fecha_fin_prevista="2026-12-31")
    codigo = ctrl.procesar_pdf(pepito, pdf, 2026)["resultado"].codigos["contrato"]
    ctrl.editar(pepito, "contratos", codigo, {"estado": "Rescindido anticipadamente", "fecha_fin_real": "30/06/2026"}, 2026)
    otra_copia = f.contrato_alquiler(tmp_path / "ctr_escaneado.pdf", fecha_inicio="2026-01-01",
                                     fecha_fin_prevista="2026-12-31", renta_inicial=901)
    ctrl.procesar_pdf(pepito, otra_copia, 2026)
    assert ctrl.db.get("contratos", pepito, codigo)["estado"] == "Rescindido anticipadamente"
    ctrl.editar(pepito, "inmuebles", "INM-001", {"estado": "Baja"}, 2026)
    ctrl.procesar_pdf(pepito, f.escritura_compra(tmp_path / "esc_copia.pdf", precio=180_001), 2026)
    assert ctrl.db.get("inmuebles", pepito, "INM-001")["estado"] == "Baja"
