"""Contraejemplos independientes a la auditoría Opus del 05/11/2026."""
from datetime import date

import openpyxl
import pytest

import database
import excel_generator as eg
import tax_calculator as tc
from controlador import Controlador


def contrato(**cambios):
    return {"codigo": "CTR-001", "fecha_inicio": "2025-01-01",
            "fecha_fin_prevista": "2027-12-31", "estado": "Vigente",
            "periodicidad": "Mensual", "renta_inicial": 1000, **cambios}


@pytest.mark.parametrize("pais", ["Belgique", "België", "Belgien", "Frankreich", "Italien", "Česko", "Republic of Ireland"])
def test_alias_de_residencia_no_se_convierte_en_extra_ue(pais):
    r = tc.resolver_residencia({"pais": pais})
    assert (r.grupo, r.tipo, r.deduce_gastos) == ("UE/EEE", .19, True)


@pytest.mark.parametrize("estado", ["Rescindido anticipadamente", "Finalizado"])
def test_terminado_sin_fecha_real_exige_aclaracion(estado):
    c = contrato(estado=estado)
    avisos = tc.avisos_contratos([c], {}, tc.anio(2025))
    assert any("fecha de fin real" in a for a in avisos)
    c["fecha_fin_real"] = "2025-05-31"
    assert tc.avisos_contratos([c], {}, tc.anio(2025)) == []


def test_finalizacion_natural_acreditada_no_exige_otra_fecha():
    c = contrato(estado="Finalizado", fecha_fin_prevista="2025-05-31")
    assert not tc.avisos_contratos([c], {}, tc.anio(2025))


def test_cero_no_se_confunde_con_cesion_gratuita_y_override_completo_manda():
    c = contrato(renta_inicial=0)
    assert "cesión gratuita" in " ".join(tc.avisos_contratos([c], {}, tc.anio(2025)))
    rentas = {c["codigo"]: [{"desde": "2025-01-01", "hasta": "2025-12-31", "renta": 900}]}
    assert not tc.avisos_contratos([c], rentas, tc.anio(2025))
    rentas[c["codigo"]][0]["hasta"] = "2025-06-30"
    assert tc.avisos_contratos([c], rentas, tc.anio(2025))


def test_otro_no_multiplica_por_doce_y_renta_anual_acreditada_si():
    c = contrato(periodicidad="Otro", renta_inicial=6000)
    assert tc.renta_devengada(c, [], *tc.anio(2025)) == 0
    assert "periodicidad" in " ".join(tc.avisos_contratos([c], {}, tc.anio(2025)))
    rs = [{"desde": "2025-01-01", "hasta": "2025-12-31", "renta": 6000, "periodicidad": "Anual"}]
    assert tc.renta_devengada(c, rs, *tc.anio(2025)) == 6000
    assert not tc.avisos_contratos([c], {c["codigo"]: rs}, tc.anio(2025))


def test_carencia_solo_confirmada_y_acotada_no_bloquea_el_alquiler():
    c = contrato(renta_inicial=0)
    cero = {"desde": "2025-01-01", "hasta": "2025-03-31", "renta": 0, "motivo": "Carencia confirmada"}
    positiva = {"desde": "2025-04-01", "hasta": "2025-12-31", "renta": 1000}
    rentas = {c["codigo"]: [cero, positiva]}
    assert not tc.avisos_contratos([c], rentas, tc.anio(2025))
    assert tc.renta_devengada(c, rentas[c["codigo"]], *tc.anio(2025)) == 9000
    cero["motivo"] = "Quizás carencia"
    assert tc.avisos_contratos([c], rentas, tc.anio(2025))
    cero["motivo"] = "Carencia confirmada"
    cero.pop("hasta")
    assert tc.avisos_contratos([c], rentas, tc.anio(2025))


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "PROYECTO_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH_DEFECTO", tmp_path / "qa.sqlite")
    with database.Database(tmp_path / "qa.sqlite") as db:
        db.carpeta_base = tmp_path / "Clientes"
    c = Controlador(tmp_path / "qa.sqlite")
    yield c
    c.cerrar()


def expediente(c):
    cid = c.crear_cliente("QA", "Ambigüedad", "X9876554Z", pais="Rusia", ejercicio=2025)["id"]
    inm = c.db.upsert("inmuebles", cid, {"direccion": "QA", "fecha_adquisicion": "2010-01-01"})
    cod = c.db.upsert("contratos", cid, {**contrato(), "inmueble": inm})
    c.db.upsert("catastro", cid, {"inmueble": inm, "ejercicio": 2025, "valor_catastral": 90000,
                               "anio_revision": 2000})
    return cid, inm, cod


@pytest.mark.parametrize("tabla,campo", [("contratos", "renta_inicial"), ("rentas", "renta")])
def test_renta_negativa_no_entra_por_bd_ni_controlador(ctrl, tabla, campo):
    cid, inm, cod = expediente(ctrl)
    antes = ctrl.db.get("contratos", cid, cod)
    assert campo in ctrl.validar(tabla, {campo: -500})
    with pytest.raises(ValueError, match="negativa"):
        ctrl.db.upsert(tabla, cid, {"codigo": cod if tabla == "contratos" else "REN-001", campo: -500})
    assert ctrl.db.get("contratos", cid, cod) == antes


def test_ambiguo_no_borra_imputacion_anterior_y_no_permite_presentar(ctrl):
    cid, inm, cod = expediente(ctrl)
    ctrl.db.upsert("contratos", cid, {"codigo": cod, "fecha_fin_real": "2025-05-31"}, respetar_manuales=False)
    tc.recalcular_cliente(ctrl.db, cid, 2025)
    imp = ctrl.db.listar("m210_imputacion", cid)[0]
    alquiler = ctrl.db.listar("m210_alquiler", cid)[0]
    ctrl.db.upsert("contratos", cid, {"codigo": cod, "estado": "Rescindido anticipadamente"}, respetar_manuales=False)
    ctrl.db.vaciar_campo("contratos", cid, cod, "fecha_fin_real")
    informe = tc.recalcular_cliente(ctrl.db, cid, 2025)
    assert any(a.nivel == "grave" and "fin real" in a.mensaje for a in informe.alertas)
    assert ctrl.db.get("m210_imputacion", cid, imp["codigo"])["cuota"] == imp["cuota"]
    for tabla, codigo in [("m210_alquiler", alquiler["codigo"]), ("m210_imputacion", imp["codigo"])]:
        with pytest.raises(ValueError, match="Confirma los datos del contrato"):
            ctrl._validar_imputacion_presentable(cid, tabla, codigo)
    ruta = eg.exportar_cliente(ctrl.db, cid, 2025)
    wb = openpyxl.load_workbook(ruta)
    resumen = wb["00_RESUMEN"]
    assert any(cell.comment and "PROVISIONAL" in cell.comment.text
               for row in resumen for cell in row)
    ws = wb["09_210_ALQUILER"]
    columnas = {col.campo: col.letra for col in database.TABLAS["m210_alquiler"].columnas_bd}
    ws[f'{columnas["estado"]}2'] = "Presentado"
    wb.save(ruta)
    wb.close()
    with pytest.raises(ValueError, match="datos del contrato"):
        eg.import_excel_to_db(ctrl.db, cid)
    assert ctrl.db.get("m210_alquiler", cid, alquiler["codigo"])["estado"] != "Presentado"


def test_correccion_del_asesor_se_propaga_y_conserva_presentado(ctrl):
    cid, inm, cod = expediente(ctrl)
    ctrl.db.upsert("contratos", cid, {"codigo": cod, "fecha_fin_real": "2025-05-31"}, respetar_manuales=False)
    tc.recalcular_cliente(ctrl.db, cid, 2025)
    fila = ctrl.db.listar("m210_alquiler", cid)[0]
    ctrl.db.upsert("m210_alquiler", cid, {"codigo": fila["codigo"], "estado": "Presentado"}, respetar_manuales=False)
    ctrl.db.upsert("contratos", cid, {"codigo": cod, "periodicidad": "Otro"}, respetar_manuales=False)
    tc.recalcular_cliente(ctrl.db, cid, 2025)
    assert ctrl.db.get("m210_alquiler", cid, fila["codigo"])["cuota"] == fila["cuota"]
    assert ctrl.db.get("m210_alquiler", cid, fila["codigo"])["estado"] == "Presentado"
    resultado = ctrl.editar(cid, "contratos", cod,
        {"periodicidad": "Mensual", "fecha_fin_real": "2025-06-30", "estado": "Finalizado"}, 2025)
    ruta = ctrl.db.ruta_excel(ctrl.db.get_cliente(cid))
    wb = openpyxl.load_workbook(ruta)
    assert wb["04_CONTRATOS"]["E2"].value.date() == date(2025, 6, 30)
    wb.close()
