"""Regresiones de los hallazgos estáticos de Codex; ejecución pendiente por Claude."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tax_calculator as tc
from database import Database, a_num


@pytest.fixture
def caso(tmp_path):
    db = Database(tmp_path / "auditoria.db")
    db.carpeta_clientes = tmp_path / "Clientes"
    cliente = db.crear_cliente("Prueba", "Auditoría", "X0000000T")
    yield db, cliente["id"]
    db.cerrar()


@pytest.mark.parametrize("valor,esperado", [("1.234 €", 1234), ("1.234.567 €", 1234567),
                                            ("1\u00a0234,56 €", 1234.56), ("1234.56", 1234.56),
                                            ("0,00 €", 0), ("50%", 0.5)])
def test_importes_espanoles_no_reducen_miles(valor, esperado):
    assert a_num(valor) == esperado


def test_obligacion_presentada_completamente_congelada(caso):
    db, cid = caso
    codigo = db.upsert("obligaciones", cid, {"ejercicio": 2026, "tipo": "Otra", "estado": "Presentado",
                                             "fecha_limite": "2026-04-20", "notas": "Histórico",
                                             "pagado": 123}, clave="FIJA")
    antes = db.get("obligaciones", cid, codigo)
    assert tc._obligacion(db, cid, 2027, "210 Alquiler", "INM-999", "FIJA", date(2027, 1, 20),
                          "OTRO", "Pendiente", "Sobrescribir", 999) == codigo
    assert db.get("obligaciones", cid, codigo) == antes


def test_cobros_limitados_a_tenencia_y_cobrado_manual_se_conserva(caso):
    db, cid = caso
    inm = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2026-01-15", "fecha_venta": "2026-02-15"})
    ctr = db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2026-01-01",
                                      "renta_inicial": 310, "estado": "Vigente"})
    marzo = db.upsert("cobros", cid, {"inmueble": inm, "contrato": ctr, "ejercicio": 2026,
                                      "importe_contractual": 310, "importe_cobrado": 33, "estado": "Cobrado"},
                       clave=f"COB|{ctr}|2026-03")
    tc.recalcular_cliente(db, cid, 2026)
    assert db.get_por_clave("cobros", cid, f"COB|{ctr}|2026-01")["importe_contractual"] == 170
    assert db.get_por_clave("cobros", cid, f"COB|{ctr}|2026-02")["importe_contractual"] == 155
    assert db.get("cobros", cid, marzo)["importe_contractual"] == 0
    assert db.get("cobros", cid, marzo)["importe_cobrado"] == 33
    assert db.get("cobros", cid, marzo)["estado"] == "Cobrado"
    assert db.get_por_clave("cobros", cid, f"COB|{ctr}|2026-04") is None


def test_total_cero_no_sustituye_por_base(caso):
    ventana = (date(2026, 1, 1), date(2026, 12, 31))
    gasto = {"ejercicio": 2026, "total": 0, "base": 100, "pct_alquiler": 1}
    assert tc.importe_gasto_periodo(gasto, ventana, [ventana], 1) == 0


def test_imputacion_que_deja_de_proceder_borra_importes_calculados(caso):
    db, cid = caso
    db.guardar_residencia(cid, 2026, pais="Francia")
    inm = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2020-01-01"})
    db.upsert("catastro", cid, {"ejercicio": 2026, "inmueble": inm, "valor_catastral": 100000})
    tc.recalcular_cliente(db, cid, 2026)
    assert db.get_por_clave("m210_imputacion", cid, f"210I|2026|{inm}")["cuota"] > 0
    db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2026-01-01",
                                "renta_inicial": 1000, "estado": "Vigente"})
    tc.recalcular_cliente(db, cid, 2026)
    fila = db.get_por_clave("m210_imputacion", cid, f"210I|2026|{inm}")
    assert fila["dias"] == fila["renta_imputada"] == fila["cuota"] == 0
    assert fila["estado"] == "No procede"


def test_bisiesto_contrato_sin_fin_y_titularidad_cero():
    inm = {"codigo": "INM-001", "fecha_adquisicion": "2020-01-01", "pct_titularidad": None}
    res = tc.resolver_residencia({"pais": "Francia"})
    imp = tc.calcular_210_imputacion(inm, [], {"valor_catastral": 100000, "anio_revision": 2020}, res, 2024)
    assert imp.dias == 366
    # Los 366 días naturales no hacen superar la imputación anual completa.
    assert imp.renta_imputada == 1100
    ctr = [{"codigo": "CTR-001", "fecha_inicio": "2024-01-01", "estado": "Vigente", "renta_inicial": 1000}]
    assert tc.calcular_210_imputacion(inm, ctr, {"valor_catastral": 100000}, res, 2024) is None
    inm["pct_titularidad"] = 0
    assert tc.calcular_210_alquiler(inm, ctr, {}, [], None, res, 2024) == []
    assert tc.pct_titularidad(inm) == 0


def test_gasto_fechas_invertidas_se_reparte_sin_crash():
    gasto = {"periodo_desde": "2026-03-31", "periodo_hasta": "2026-01-01", "total": 90,
             "importe_deducible": 90, "ejercicio": 2026}
    assert tc.importe_gasto_periodo(gasto, (date(2026, 1, 1), date(2026, 3, 31)), [], 1) == 90


@pytest.mark.parametrize("pais", ["España", "ES", "Spain"])
def test_ue_27_incluye_espana(pais):
    assert tc.grupo_territorio(pais) == tc.GRUPO_UE


def test_identificadores_sql_validados_y_valores_parametrizados(caso):
    db, cid = caso
    codigo = db.upsert("inmuebles", cid, {"direccion": "x'; DROP TABLE clientes; --",
                                         "direccion=NULL --": "ignorado"})
    assert db.get("inmuebles", cid, codigo)["direccion"].startswith("x'; DROP")
    with pytest.raises(KeyError):
        db.listar("inmuebles; DROP TABLE clientes", cid)
    with pytest.raises(KeyError):
        db.listar("inmuebles", cid, orden="codigo; DROP TABLE clientes")
    with pytest.raises(KeyError):
        db.listar("inmuebles", cid, **{"direccion OR 1=1": "x"})
    with pytest.raises(KeyError):
        db.vaciar_campo("inmuebles", cid, codigo, "direccion=NULL --")
    assert db.get_cliente(cid) is not None


def test_porcentaje_imputacion_manual_cero_prevalece():
    assert tc.pct_imputacion(2020, 2026, manual=0) == 0
    assert tc.pct_imputacion(2020, 2026, manual=None) == 0.011
