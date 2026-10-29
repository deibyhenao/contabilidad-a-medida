"""Casos hipotéticos A6 con resultados esperados independientes y explícitos."""
from datetime import date
from pathlib import Path

import pytest

import database
import tax_calculator as tc


def inmueble(**cambios):
    datos = dict(codigo="INM-001", fecha_adquisicion="2020-01-01", pct_titularidad=1,
                 precio_escritura=200000)
    datos.update(cambios)
    return datos


def contrato(**cambios):
    datos = dict(codigo="CTR-001", inmueble="INM-001", fecha_inicio="2026-01-01",
                 fecha_fin_prevista="2026-12-31", renta_inicial=1200, periodicidad="Mensual", estado="Vigente")
    datos.update(cambios)
    return datos


@pytest.mark.parametrize("pais,titularidad,ingresos,gastos,base,cuota", [
    ("Francia", 1, 3600, 600, 3000, 570),
    ("Francia", 0.5, 1800, 300, 1500, 285),
    ("Estados Unidos", 1, 3600, 0, 3600, 864),
    ("Estados Unidos", 0.5, 1800, 0, 1800, 432),
])
def test_210_trimestre_ue_extra_y_cotitularidad(pais, titularidad, ingresos, gastos, base, cuota):
    gasto = dict(categoria="Comunidad", total=600, fecha="2026-02-10", ejercicio=2026)
    res = tc.calcular_210_alquiler(inmueble(pct_titularidad=titularidad), [contrato()], {}, [gasto], None,
                                  tc.resolver_residencia({"pais": pais}), 2026)
    assert len(res) == 4
    assert (res[0].ingresos, res[0].gastos, res[0].base, res[0].cuota) == (ingresos, gastos, base, cuota)
    assert res[1].gastos == 0


def test_210_anual_con_amortizacion_y_gananciales():
    inm = inmueble(pct_titularidad=1, gananciales_forzado=1)
    cat = dict(valor_catastral=100000, valor_construccion=80000)
    gasto = dict(categoria="Comunidad", total=600, fecha="2026-02-10", ejercicio=2026)
    res = tc.calcular_210_alquiler(inm, [contrato()], {}, [gasto], cat,
                                  tc.resolver_residencia({"pais": "Francia"}), 2026, periodicidad="Anual")
    # 14.400 ingresos × 50 %; comunidad 300; amortización de construcción 2.400.
    assert len(res) == 1
    assert (res[0].ingresos, res[0].gastos, res[0].base, res[0].cuota) == (7200, 2700, 4500, 855)


def test_alquiler_tres_meses_y_gasto_anual_prorrateado():
    inm = inmueble(pct_titularidad=0.5)
    ctr = contrato(fecha_inicio="2026-07-01", fecha_fin_prevista="2026-09-30")
    gasto = dict(categoria="Seguro", total=365, periodo_desde="2026-01-01", periodo_hasta="2026-12-31",
                 ejercicio=2026)
    res = tc.calcular_210_alquiler(inm, [ctr], {}, [gasto], None,
                                  tc.resolver_residencia({"pais": "Francia"}), 2026)
    assert len(res) == 1 and res[0].periodo == "3T"
    assert (res[0].ingresos, res[0].gastos, res[0].base, res[0].cuota) == (1800, 46, 1754, 333.26)


def test_bisiesto_91_dias_no_se_trata_como_90():
    gasto = dict(total=366, periodo_desde="2024-01-01", periodo_hasta="2024-12-31", ejercicio=2024)
    ventana = (date(2024, 1, 1), date(2024, 3, 31))
    assert tc.importe_gasto_periodo(gasto, ventana, [ventana], 0.5) == 45.5
    assert tc.amortizacion_periodo(inmueble(), dict(valor_catastral=100000, valor_construccion=80000),
                                   366, 2024, 0.5) == 2400


def test_gasto_entre_dos_ejercicios_se_reparte():
    gasto = dict(total=620, periodo_desde="2025-12-01", periodo_hasta="2026-01-31", ejercicio=2025)
    a = (date(2025, 1, 1), date(2025, 12, 31))
    b = (date(2026, 1, 1), date(2026, 12, 31))
    assert tc.importe_gasto_periodo(gasto, a, [a], 1) == 310
    assert tc.importe_gasto_periodo(gasto, b, [b], 1) == 310


def test_importe_final_no_vuelve_a_aplicar_la_titularidad():
    ventana = (date(2026, 1, 1), date(2026, 12, 31))
    gasto = dict(total=1000, importe_deducible=200, pct_inmueble=0.5, pct_alquiler=0.5, ejercicio=2026)
    assert tc.importe_gasto_periodo(gasto, ventana, [ventana], 0.5) == 200


@pytest.mark.parametrize("campos", [dict(total=0, base=600), dict(total=600, pct_inmueble=0),
                                   dict(total=600, pct_alquiler=0), dict(total=600, importe_deducible=0)])
def test_ceros_explicitos_no_se_convierten_en_gastos(campos):
    ventana = (date(2026, 1, 1), date(2026, 12, 31))
    assert tc.importe_gasto_periodo({"ejercicio": 2026, **campos}, ventana, [ventana], 1) == 0


def test_gasto_de_un_dia_sin_alquiler_no_se_deduce():
    ventana = (date(2026, 1, 1), date(2026, 12, 31))
    alquilado = [(date(2026, 7, 1), date(2026, 9, 30))]
    assert tc.importe_gasto_periodo(dict(total=100, fecha="2026-02-01", ejercicio=2026),
                                   ventana, alquilado, 1) == 0


def test_imputacion_solo_por_los_273_dias_vacios():
    r = tc.calcular_210_imputacion(inmueble(pct_titularidad=0.5),
                                  [contrato(fecha_inicio="2026-07-01", fecha_fin_prevista="2026-09-30")],
                                  dict(valor_catastral=100000, anio_revision=2020),
                                  tc.resolver_residencia({"pais": "Francia"}), 2026)
    assert (r.dias, r.renta_imputada, r.cuota) == (273, 411.37, 78.16)


def test_venta_con_amortizacion_ya_del_cliente():
    r = tc.calcular_venta(inmueble(precio_escritura=180000, precio_venta=240000, gastos_venta=6000,
                                  fecha_venta="2026-07-01", gananciales_forzado=1), amortizaciones=5000)
    assert (r.ganancia, r.cuota, r.retencion, r.resultado) == (32000, 6080, 3600, 2480)


def test_patrimonio_umbral_de_bienes_aunque_no_haya_cuota():
    r = tc.calcular_patrimonio([inmueble(precio_escritura=2100000)], {}, 2026, deudas=2000000)
    assert r.valor_bienes == 2100000
    assert r.resultado == 0
    assert r.obligado


def test_its_descuenta_solo_la_cuota_de_patrimonio_pagada():
    assert tc.calcular_its(3500000, 2000) == (8500, 2000, 6500)


def test_pipeline_sqlite_recalculo_anual_sin_excel_real(tmp_path):
    with database.Database(tmp_path / "hipotetico.db") as db:
        db.carpeta_clientes = tmp_path / "Clientes"
        cid = db.crear_cliente("Caso", "Hipotético", "HIP-A6")["id"]
        db.guardar_residencia(cid, 2026, pais="Francia")
        db.set_config("periodicidad_210_alquiler", "Anual")
        db.upsert("inmuebles", cid, inmueble(gananciales_forzado=1))
        db.upsert("catastro", cid, dict(inmueble="INM-001", ejercicio=2026, valor_catastral=100000,
                                      valor_construccion=80000))
        db.upsert("contratos", cid, contrato())
        db.upsert("gastos", cid, dict(inmueble="INM-001", ejercicio=2026, categoria="Comunidad", total=600,
                                     fecha="2026-02-10"))
        tc.recalcular_cliente(db, cid, 2026)
        filas = db.listar("m210_alquiler", cid, ejercicio=2026)
        assert len(filas) == 1
        assert (filas[0]["ingresos"], filas[0]["gastos"], filas[0]["base"], filas[0]["cuota"]) == (7200, 2700, 4500, 855)
        tc.recalcular_cliente(db, cid, 2026)
        assert len(db.listar("m210_alquiler", cid, ejercicio=2026)) == 1


def test_factura_pdf_a_bd_calculo_excel_y_soporte(tmp_path, monkeypatch):
    import openpyxl
    import excel_generator
    import fixtures_pdf as f
    import ia_extractor
    from controlador import Controlador

    monkeypatch.setattr(ia_extractor, "obtener_clave", lambda: None)
    ctrl = Controlador(tmp_path / "flujo.db")
    ctrl.db.set_config("periodicidad_210_alquiler", "Trimestral")  # se contrasta explícitamente el 1T
    try:
        ctrl.cambiar_carpeta_clientes(tmp_path / "Clientes")
        cliente = ctrl.crear_cliente("Caso", "Factura", "FACT-A6", pais="Francia", ejercicio=2026)
        cid = cliente["id"]
        ctrl.db.upsert("inmuebles", cid, inmueble(estado="Activo"))
        ctrl.db.upsert("contratos", cid, contrato())
        pdf = f.factura_gasto(tmp_path / "luz.pdf", categoria="Suministros", concepto="Luz",
                              base=100, iva=21, total=121, fecha="2026-03-10",
                              periodo_desde="2026-03-01", periodo_hasta="2026-03-31")
        resultado = ctrl.procesar_pdf(cid, pdf, 2026)
        gastos = ctrl.db.listar("gastos", cid)
        assert len(gastos) == 1 and gastos[0]["total"] == 121
        anual = next(r for r in ctrl.db.listar("m210_alquiler", cid) if r["periodo"] == "Anual")
        assert (anual["ingresos"], anual["gastos"], anual["base"], anual["cuota"]) == (14400, 121, 14279, 2713.01)
        assert (Path(cliente["carpeta"]) / gastos[0]["ruta"]).is_file()
        assert resultado["error_excel"] is None
        assert excel_generator.verificar_estructura(resultado["excel"]) == []
        wb = openpyxl.load_workbook(resultado["excel"])
        try:
            assert len(wb.sheetnames) == 17
            assert wb["08_GASTOS"]["N2"].value == 121
            assert wb["08_GASTOS"]["U2"].hyperlink.target.startswith("Soportes_PDF/")
        finally:
            wb.close()
    finally:
        ctrl.cerrar()
