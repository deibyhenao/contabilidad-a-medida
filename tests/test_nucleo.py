"""Tests del núcleo (database.py + tax_calculator.py). Autor: Claude. Auditor: Codex."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tax_calculator as tc  # noqa: E402
from database import Database, a_num, slug  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.carpeta_clientes = tmp_path / "Clientes"
    yield d
    d.cerrar()


@pytest.fixture
def pepito(db):
    return db.crear_cliente("Pepito", "Pérez", "X1234567A", nacionalidad="Francesa")


# ---------------------------------------------------------------- database
def test_crear_cliente_carpetas_y_excel(db, pepito, tmp_path):
    carpeta = Path(pepito["carpeta"])
    assert carpeta.name == "Pepito_Perez_X1234567A"
    assert (carpeta / "Soportes_PDF").is_dir() and (carpeta / "_ARRASTRA_AQUI").is_dir()
    assert db.nombre_excel(pepito) == "NR_PEREZ_PEPITO_X1234567A.xlsx"
    assert db.crear_cliente("Pepito", "Pérez", "x1234567a")["id"] == pepito["id"]  # idempotente


def test_upsert_codigos_y_none_no_borra(db, pepito):
    cid = pepito["id"]
    c1 = db.upsert("inmuebles", cid, {"direccion": "Calle Mayor 1", "precio_escritura": "150.000,00"})
    c2 = db.upsert("inmuebles", cid, {"direccion": "Calle Sol 2"})
    assert (c1, c2) == ("INM-001", "INM-002")
    db.upsert("inmuebles", cid, {"codigo": c1, "direccion": None, "pct_titularidad": 50})
    fila = db.get("inmuebles", cid, c1)
    assert fila["direccion"] == "Calle Mayor 1" and fila["precio_escritura"] == 150000.0
    assert fila["pct_titularidad"] == 0.5  # 50 -> 0.5


def test_upsert_por_clave_idempotente(db, pepito):
    cid = pepito["id"]
    a = db.upsert("notas", cid, {"asunto": "x"}, clave="K1")
    b = db.upsert("notas", cid, {"asunto": "y"}, clave="K1")
    assert a == b and db.get("notas", cid, a)["asunto"] == "y"


def test_a_num_y_slug():
    assert a_num("1.234,56 €") == 1234.56 and a_num("50%") == 0.5 and a_num(7) == 7.0
    assert slug("José Núñez") == "Jose_Nunez"


# ---------------------------------------------------------------- reglas puras
def test_grupo_territorio():
    assert tc.grupo_territorio("Francia") == tc.GRUPO_UE
    assert tc.grupo_territorio("Noruega") == tc.GRUPO_UE
    assert tc.grupo_territorio("Reino Unido") == tc.GRUPO_EXTRA
    assert tc.grupo_territorio(None) == tc.GRUPO_EXTRA


def test_tres_valores_coge_el_mayor():
    # AEAT: la referencia solo cuenta en compras desde 2022 en las que fue la base del ITP (mayor que el precio)
    inm = {"precio_escritura": 200_000, "valor_referencia": 230_000, "fecha_adquisicion": "2023-03-01"}
    vp = tc.valor_patrimonial(inm, {"valor_catastral": 90_000})
    assert vp.valor == 230_000 and vp.fuente == "Valor referencia"
    antes_2022 = tc.valor_patrimonial({**inm, "fecha_adquisicion": "2021-05-01"}, {"valor_catastral": 90_000})
    assert antes_2022.valor == 200_000 and antes_2022.fuente == "Valor escritura"
    ref_del_anio = tc.valor_patrimonial({**inm, "valor_referencia": None},
                                        {"valor_catastral": 90_000, "valor_referencia": 300_000})
    assert ref_del_anio.valor == 200_000          # la referencia del año consultado no es la de la adquisición


def test_gananciales_forzado():
    assert tc.pct_titularidad({"pct_titularidad": 1.0, "gananciales_forzado": 1}) == 0.5
    assert tc.pct_titularidad({}) == 1.0


def test_pct_imputacion():
    assert tc.pct_imputacion(2018, 2026) == 0.011
    assert tc.pct_imputacion(2015, 2026) == 0.02
    assert tc.pct_imputacion(None, 2026) == 0.02
    assert tc.pct_imputacion(2015, 2026, manual=0.011) == 0.011


def test_tarifa_ip():
    assert tc.aplicar_tarifa(167_129.45) == 334.26
    assert tc.aplicar_tarifa(1_000_000) == pytest.approx(2_506.86 + (1_000_000 - 668_499.75) * 0.009, abs=0.01)


def test_imputacion_anio_vacio_completo():
    inm = {"codigo": "INM-001", "fecha_adquisicion": "2020-01-01", "pct_titularidad": 1}
    res = tc.resolver_residencia({"pais": "Reino Unido"})
    r = tc.calcular_210_imputacion(inm, [], {"valor_catastral": 100_000, "anio_revision": 2010}, res, 2026)
    assert r.dias == 365 and r.pct_imputacion == 0.02
    assert r.renta_imputada == 2000.0 and r.cuota == 480.0  # 24 %


def test_imputacion_parcial_y_50pct():
    inm = {"codigo": "INM-001", "fecha_adquisicion": "2020-01-01", "pct_titularidad": 1, "gananciales_forzado": 1}
    ctr = [{"codigo": "CTR-001", "fecha_inicio": "2026-07-01", "fecha_fin_prevista": "2027-06-30", "estado": "Vigente"}]
    res = tc.resolver_residencia({"pais": "Francia"})
    r = tc.calcular_210_imputacion(inm, ctr, {"valor_catastral": 100_000, "anio_revision": 2020}, res, 2026)
    assert r.dias == 181  # 1 ene - 30 jun
    assert r.renta_imputada == round(100_000 * 0.011 * 0.5 * 181 / 365, 2)
    assert r.cuota == round(r.renta_imputada * 0.19, 2)


def test_210_alquiler_ue_vs_extra():
    inm = {"codigo": "INM-001", "fecha_adquisicion": "2020-01-01", "pct_titularidad": 1}
    ctr = [{"codigo": "CTR-001", "fecha_inicio": "2026-01-01", "renta_inicial": 1000, "periodicidad": "Mensual",
            "estado": "Vigente"}]
    gastos = [{"categoria": "Comunidad", "total": 300, "fecha": "2026-02-10", "ejercicio": 2026}]
    ue = tc.calcular_210_alquiler(inm, ctr, {}, gastos, None, tc.resolver_residencia({"pais": "Francia"}), 2026)
    ex = tc.calcular_210_alquiler(inm, ctr, {}, gastos, None, tc.resolver_residencia({"pais": "EEUU"}), 2026)
    assert [r.periodo for r in ue] == ["1T", "2T", "3T", "4T"]
    assert ue[0].ingresos == 3000 and ue[0].gastos == 300 and ue[0].cuota == round(2700 * 0.19, 2)
    assert ex[0].gastos == 0 and ex[0].cuota == 720.0


def test_venta_corta_tenencia():
    inm = {"codigo": "INM-001", "fecha_adquisicion": "2020-01-01", "fecha_venta": "2026-07-01",
           "precio_escritura": 100_000, "precio_venta": 150_000, "pct_titularidad": 1}
    assert tc.periodo_tenencia(inm, 2026) == (date(2026, 1, 1), date(2026, 6, 30))
    assert not tc.titular_a_31_diciembre(inm, 2026)
    v = tc.calcular_venta(inm)
    assert v.ganancia == 50_000 and v.cuota == 9_500 and v.retencion == 4_500 and v.resultado == 5_000


# ---------------------------------------------------------------- orquestador
def test_recalcular_cliente_pepito(db, pepito):
    db.set_config("periodicidad_210_alquiler", "Trimestral")  # caso histórico de dos trimestres
    cid = pepito["id"]
    db.guardar_residencia(cid, 2025, pais="Francia")
    db.guardar_residencia(cid, 2026, pais="Francia")
    inm = db.upsert("inmuebles", cid, {"direccion": "C/ Mayor 1", "fecha_adquisicion": "2019-05-10",
                                       "precio_escritura": 180_000, "pct_titularidad": 1, "gananciales_forzado": 1,
                                       "estado": "Activo"})
    db.upsert("catastro", cid, {"ejercicio": 2026, "inmueble": inm, "valor_catastral": 90_000,
                                "valor_suelo": 36_000, "valor_construccion": 54_000, "anio_revision": 2019})
    ctr = db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2025-01-01", "fecha_fin_real": "2026-06-30",
                                       "renta_inicial": 800, "periodicidad": "Mensual", "estado": "Finalizado"})
    db.upsert("inquilinos", cid, {"contrato": ctr, "inmueble": inm, "nombre": "Ana López"})
    inf = tc.recalcular_cliente(db, cid, 2026)
    assert "Tiene 1 piso," in inf.mensaje_patrimonio and "NO está obligado" in inf.mensaje_patrimonio
    assert inf.patrimonio.valor_bienes == 90_000  # 180.000 × 50 %
    assert len(inf.filas_210_alquiler) == 2      # 1T y 2T
    imp = db.get("m210_imputacion", cid, inf.filas_210_imputacion[0])
    assert imp["dias"] == 184 and imp["estado"] == "Pendiente"
    assert any("FALTA por presentar 210 Imputación" in e for e in inf.estados)
    # idempotente: segunda pasada no duplica
    tc.recalcular_cliente(db, cid, 2026)
    assert len(db.listar("m210_alquiler", cid)) == 2 and len(db.listar("m210_imputacion", cid)) == 1


def test_presentado_no_se_recalcula(db, pepito):
    cid = pepito["id"]
    db.guardar_residencia(cid, 2026, pais="Francia")
    inm = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2019-01-01", "pct_titularidad": 1})
    db.upsert("catastro", cid, {"ejercicio": 2026, "inmueble": inm, "valor_catastral": 100_000})
    inf = tc.recalcular_cliente(db, cid, 2026)
    cod = inf.filas_210_imputacion[0]
    db.upsert("m210_imputacion", cid, {"codigo": cod, "estado": "Presentado", "pagado": 123.45})
    db.upsert("catastro", cid, {"codigo": "CAT-001", "valor_catastral": 200_000})
    tc.recalcular_cliente(db, cid, 2026)
    assert db.get("m210_imputacion", cid, cod)["valor_catastral"] == 100_000


def test_alerta_interanual_contrato(db, pepito):
    cid = pepito["id"]
    db.guardar_residencia(cid, 2026, pais="Francia")
    inm = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2019-01-01"})
    db.upsert("catastro", cid, {"ejercicio": 2025, "inmueble": inm, "valor_catastral": 100_000})
    c1 = db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2025-01-01", "fecha_fin_real": "2025-12-31",
                                      "renta_inicial": 700, "estado": "Finalizado"})
    db.upsert("inquilinos", cid, {"contrato": c1, "nombre": "Juan"})
    db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2026-01-01", "renta_inicial": 900,
                                 "estado": "Vigente"})
    alertas = tc.comparar_interanual(db, cid, 2026)
    tipos = {a.tipo for a in alertas}
    assert "renta" in tipos and "catastro" in tipos  # renta cambia; catastro heredado de 2025
    assert any("700,00 €/mes con Juan" in a.mensaje for a in alertas)
