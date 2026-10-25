"""Modelos 303, 130, 111 y 115 de residentes con importes calculados a mano."""
from datetime import date

import pytest

import fiscal_residentes as fr
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.carpeta_base = tmp_path
    yield d
    d.cerrar()


def _cliente(db, tipo="Autónomo", nif="12345678Z"):
    return db.crear_cliente("Laura", "Gómez", nif, tipo_cliente=tipo)["id"]


def _emitida(db, cid, fecha, base, iva=0.21, ret=0.0, numero=None):
    return db.upsert("facturas_emitidas", cid, {
        "fecha": fecha, "numero": numero or f"E-{fecha}-{base}", "receptor": "Cliente SL",
        "nif_receptor": "B11111111", "base": base, "tipo_iva": iva, "cuota_iva": round(base * iva, 2),
        "tipo_retencion": ret, "retencion": round(base * ret, 2)})


def _recibida(db, cid, fecha, base, iva=0.21, deducible=1.0, categoria="Suministros", ret=0.0, nif="A22222222"):
    return db.upsert("facturas_recibidas", cid, {
        "fecha": fecha, "numero": f"R-{fecha}-{base}", "proveedor": "Proveedor SA", "nif_proveedor": nif,
        "categoria": categoria, "base": base, "tipo_iva": iva, "cuota_iva": round(base * iva, 2),
        "pct_iva_deducible": deducible, "tipo_retencion": ret, "retencion": round(base * ret, 2)})


def _fila(db, tabla, cid, clave):
    return db.get_por_clave(tabla, cid, clave)


def test_303_y_130_trimestrales_con_compensacion_y_dificil_justificacion(db):
    cid = _cliente(db)
    for n in range(3):
        _emitida(db, cid, f"2025-0{n + 1}-15", 1000, ret=0.15, numero=f"E{n}")
    _recibida(db, cid, "2025-02-10", 500)                        # IVA 105 deducible
    _recibida(db, cid, "2025-03-10", 200, deducible=0.5, categoria="Compras")  # 42 de IVA, la mitad deducible
    _recibida(db, cid, "2025-05-10", 1000)                       # 2T: solo gastos -> 303 negativo
    _emitida(db, cid, "2025-08-01", 2000, numero="E3")           # 3T sin retención
    inf = fr.recalcular_residente(db, cid, 2025, hoy=date(2026, 3, 1))

    t1 = _fila(db, "m303", cid, "303|2025|1T")
    assert (t1["iva_devengado"], t1["iva_deducible"], t1["resultado"], t1["a_ingresar"]) == (630, 126, 504, 504)
    t2 = _fila(db, "m303", cid, "303|2025|2T")
    assert (t2["resultado"], t2["a_ingresar"], t2["a_compensar"]) == (-210, 0, 210)
    t3 = _fila(db, "m303", cid, "303|2025|3T")
    assert (t3["compensacion_anterior"], t3["resultado"], t3["a_ingresar"]) == (210, 210, 210)

    # 130 1T: ingresos 3.000; gastos 500 + (200 + 21 IVA no deducible) = 721; difícil 5 % de 2.279 = 113,95
    c1 = _fila(db, "m130", cid, "130|2025|1T")
    assert (c1["ingresos"], c1["gastos"], c1["rendimiento"], c1["pago_20"]) == (3000, 834.95, 2165.05, 433.01)
    assert (c1["retenciones"], c1["resultado"], c1["a_ingresar"]) == (450, -16.99, 0)
    # 3T acumulado: ingresos 5.000; gastos 1.721 + 163,95; pago 623,01 − 450 retenciones = 173,01
    c3 = _fila(db, "m130", cid, "130|2025|3T")
    assert (c3["gastos"], c3["pago_20"], c3["pagos_anteriores"], c3["a_ingresar"]) == (1884.95, 623.01, 0, 173.01)
    c4 = _fila(db, "m130", cid, "130|2025|4T")
    assert (c4["pagos_anteriores"], c4["a_ingresar"]) == (173.01, 0)
    assert inf.trimestres[0].ingresos == 3000 and inf.trimestres[0].a_ingresar_303 == 504
    assert any("303 1T/2025" in e for e in inf.estados)


def test_tope_de_2000_en_dificil_justificacion(db):
    cid = _cliente(db)
    _emitida(db, cid, "2025-01-20", 100_000, iva=0.21, numero="BIG")
    fr.recalcular_residente(db, cid, 2025)
    c1 = _fila(db, "m130", cid, "130|2025|1T")
    assert c1["gastos"] == 2000 and c1["pago_20"] == 19_600


def test_compensacion_del_4t_del_ano_anterior(db):
    cid = _cliente(db)
    _recibida(db, cid, "2024-11-05", 1000)            # 4T 2024: −210 a compensar
    fr.recalcular_residente(db, cid, 2024)
    _emitida(db, cid, "2025-02-01", 2000, numero="X")  # 1T 2025: 420 − 210 = 210
    fr.recalcular_residente(db, cid, 2025)
    t1 = _fila(db, "m303", cid, "303|2025|1T")
    assert (t1["compensacion_anterior"], t1["a_ingresar"]) == (210, 210)
    assert "VERIFICAR" in (_fila(db, "m303", cid, "303|2024|4T")["observaciones"] or "")


def test_111_y_115_separan_profesionales_y_alquiler_de_local(db):
    cid = _cliente(db)
    _recibida(db, cid, "2025-01-31", 800, categoria="Alquiler local", ret=0.19, nif="B33333333")
    _recibida(db, cid, "2025-02-28", 300, categoria="Servicios profesionales", ret=0.15, nif="11111111H")
    _recibida(db, cid, "2025-03-15", 100, categoria="Servicios profesionales", ret=0.15, nif="11111111H")
    fr.recalcular_residente(db, cid, 2025)
    m115 = _fila(db, "m111_115", cid, "RET|2025|1T|115")
    m111 = _fila(db, "m111_115", cid, "RET|2025|1T|111")
    assert (m115["perceptores"], m115["base"], m115["retenciones"]) == (1, 800, 152)
    assert (m111["perceptores"], m111["base"], m111["retenciones"]) == (1, 400, 60)
    assert _fila(db, "m111_115", cid, "RET|2025|2T|111") is None


def test_pyme_sin_130_y_con_calendario_de_sociedades(db):
    cid = _cliente(db, tipo="Pyme", nif="B44444444")
    _emitida(db, cid, "2025-04-10", 1000, numero="P1")
    inf = fr.recalcular_residente(db, cid, 2025)
    assert not inf.calcula_130 and _fila(db, "m130", cid, "130|2025|2T") is None
    assert _fila(db, "m303", cid, "303|2025|2T")["a_ingresar"] == 210
    notas = {o["notas"] for o in db.listar("obligaciones", cid, ejercicio=2025)}
    assert any(n and n.startswith("Modelo 202") for n in notas) and any(n and n.startswith("Modelo 200") for n in notas)


def test_fila_presentada_queda_congelada(db):
    cid = _cliente(db)
    _emitida(db, cid, "2025-01-10", 1000, numero="A")
    fr.recalcular_residente(db, cid, 2025)
    t1 = _fila(db, "m303", cid, "303|2025|1T")
    db.upsert("m303", cid, {"codigo": t1["codigo"], "estado": "Presentado", "fecha_presentacion": "2025-04-15"})
    _emitida(db, cid, "2025-02-10", 5000, numero="B")
    fr.recalcular_residente(db, cid, 2025)
    assert _fila(db, "m303", cid, "303|2025|1T")["a_ingresar"] == 210
    ob = db.get_por_clave("obligaciones", cid, "OBL|303|2025|1T")
    assert ob["estado"] == "Presentado"


def test_regimen_no_general_no_calcula_303_y_falta_036(db):
    cid = _cliente(db)
    db.upsert("actividades", cid, {"fecha_alta": "2024-01-01", "regimen_iva": "Exento",
                                   "regimen_irpf": "Directa normal", "modelos": "130",
                                   "ruta": "Soportes_PDF/2024-01-01_MODELO_036_037_alta.pdf"})
    _emitida(db, cid, "2025-01-10", 1000, iva=0.0, numero="S")
    inf = fr.recalcular_residente(db, cid, 2025)
    assert _fila(db, "m303", cid, "303|2025|1T") is None
    assert _fila(db, "m130", cid, "130|2025|1T")["gastos"] == 0  # directa normal: sin 5 %
    assert not any(p.titulo == "Falta el modelo 036/037" for p in inf.pendientes)
    otro = _cliente(db, nif="X7777777X")
    assert any(p.titulo == "Falta el modelo 036/037" for p in fr.recalcular_residente(db, otro, 2025).pendientes)


def test_trimestre_y_ejercicio_salen_de_la_fecha(db):
    cid = _cliente(db)
    cod = _emitida(db, cid, "2025-11-03", 100, numero="Q")
    f = db.get("facturas_emitidas", cid, cod)
    assert (f["ejercicio"], f["trimestre"]) == (2025, "4T")
    assert fr.total_factura({"base": 1000, "tipo_iva": 0.21, "tipo_retencion": 0.15}) == 1060
