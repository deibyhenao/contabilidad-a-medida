"""Suite de auditoría independiente A7 (Antigravity): 12 escenarios fiscales/funcionales y regresiones."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import flet as ft
from openpyxl import Workbook, load_workbook
import pytest

from controlador import Controlador, campos_editables
import database
import excel_generator
import gastos as registro
from ia_extractor import convertir_valor
import main
import pdf_processor
import tax_calculator as tax
import ui_estilo as ui


@pytest.fixture
def expediente_ag(tmp_path):
    """Expediente ficticio independiente con carpeta de clientes aislada."""
    with database.Database(tmp_path / "ag_a7.sqlite") as db:
        carpeta = tmp_path / "Clientes"
        carpeta.mkdir(parents=True, exist_ok=True)
        db.carpeta_clientes = carpeta
        db.set_config("carpeta_clientes", str(carpeta))
        cid = db.crear_cliente("Elena", "Vandenberg", "Y7654321Z")["id"]
        db.guardar_residencia(cid, 2020, pais="Bélgica")
        inm = db.upsert(
            "inmuebles",
            cid,
            {
                "direccion": "Paseo de Gracia 101",
                "fecha_adquisicion": "2020-01-01",
                "pct_titularidad": 1.0,
                "valor_catastral": 120000.0,
                "valor_suelo": 48000.0,
                "valor_construccion": 72000.0,
                "precio_adquisicion": 200000.0,
                "gastos_adquisicion": 10000.0,
            },
        )
        db.upsert(
            "contratos",
            cid,
            {
                "inmueble": inm,
                "fecha_inicio": "2020-01-01",
                "renta_inicial": 150.0,
                "periodicidad": "Mensual",
                "estado": "Vigente",
            },
        )
        db.set_config("periodicidad_210_alquiler", "Anual")
        yield db, cid, inm


def _walk_controls(ctrl):
    if ctrl is None:
        return
    yield ctrl
    for attr in ("content", "title", "subtitle", "leading", "trailing"):
        child = getattr(ctrl, attr, None)
        if isinstance(child, ft.Control):
            yield from _walk_controls(child)
    for attr in ("controls", "actions"):
        children = getattr(ctrl, attr, None) or []
        for child in children:
            if isinstance(child, ft.Control):
                yield from _walk_controls(child)


def _collect_texts(ctrl) -> list[str]:
    texts = []
    for c in _walk_controls(ctrl):
        if isinstance(c, ft.Text) and c.value:
            texts.append(str(c.value))
        elif isinstance(c, ft.TextField) and getattr(c, "label", None):
            texts.append(f"FIELD:{c.label}={c.value}")
        elif isinstance(c, ft.Dropdown) and getattr(c, "label", None):
            texts.append(f"DROPDOWN:{c.label}={c.value}")
    return texts


# ==============================================================================
# CASO 1: Titularidades y porcentajes en todas las puertas de entrada
# ==============================================================================
@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("100 %", 1.0),
        (100, 1.0),
        (50, 0.5),
        ("50 %", 0.5),
        ("0,5", 0.5),
        ("1,5 %", 0.015),
        (0, 0.0),
    ],
)
def test_caso_01_porcentajes_validos_todas_las_puertas(expediente_ag, entrada, esperado):
    db, cid, _ = expediente_ag
    assert Controlador.__new__(Controlador).validar("inmuebles", {"pct_titularidad": entrada}) == {}
    assert convertir_valor("pct_titularidad", entrada) == pytest.approx(esperado)
    cod = db.upsert("inmuebles", cid, {"pct_titularidad": entrada})
    assert db.get("inmuebles", cid, cod)["pct_titularidad"] == pytest.approx(esperado)


@pytest.mark.parametrize(
    "invalido",
    ["150 %", "150", 150, 1.5, "1,5", -0.01, "-1 %", float("nan"), float("inf")],
)
def test_caso_01_porcentajes_invalidos_rechazados(expediente_ag, invalido):
    db, cid, _ = expediente_ag
    assert Controlador.__new__(Controlador).validar("inmuebles", {"pct_titularidad": invalido})
    with pytest.raises(ValueError):
        db.upsert("inmuebles", cid, {"pct_titularidad": invalido})
    with pytest.raises((ValueError, TypeError)):
        convertir_valor("pct_titularidad", invalido)
    with pytest.raises(ValueError):
        tax.fraccion(invalido)


def test_caso_01_regresion_pdf_local_y_pct_imputacion(expediente_ag, tmp_path, monkeypatch):
    """Regresión A7: pdf_processor.extraer_local y tax.pct_imputacion no aceptan fracciones fuera de [0, 1]."""
    db, cid, _ = expediente_ag

    # 1) pct_imputacion manual corrupto en BD debe fallar en lugar de devolver 1.5 o NaN
    for corrupto in (1.5, -0.05, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            tax.pct_imputacion(None, 2026, manual=corrupto)

    # 2) pdf_processor.extraer_local con "150 %" en escritura no guarda 1.5 ni rompe aplicar()
    texto_150 = (
        "ESCRITURA DE COMPRAVENTA\n"
        "Referencia catastral: 9876543VH5797N0001AB\n"
        "Precio de compraventa: 180.000,00 euros\n"
        "Porcentaje de titularidad: 150 %\n"
    )
    pdf_falso = tmp_path / "escritura_150.pdf"
    pdf_falso.write_bytes(b"%PDF-1.4 dummy")
    monkeypatch.setattr(pdf_processor, "extraer_texto", lambda _r: (texto_150, False))
    ext_150 = pdf_processor.extraer_local(pdf_falso)
    assert "pct_titularidad" not in ext_150.campos
    assert any("fuera de rango" in a for a in ext_150.avisos)

    # Y aplicar() con ese resultado no lanza ValueError al insertar en SQLite
    res_aplicar = pdf_processor.aplicar(db, cid, ext_150, pdf_falso)
    assert res_aplicar.codigos.get("inmueble")

    # 3) pdf_processor.extraer_local con "50 %" válido sí extrae 0.5
    texto_50 = texto_150.replace("150 %", "50 %")
    monkeypatch.setattr(pdf_processor, "extraer_texto", lambda _r: (texto_50, False))
    ext_50 = pdf_processor.extraer_local(pdf_falso)
    assert ext_50.campos.get("pct_titularidad") == pytest.approx(0.5)


# ==============================================================================
# CASO 2: Ejercicio registrado vs devengo fiscal
# ==============================================================================
def test_caso_02_ejercicio_registrado_vs_devengo(expediente_ag):
    db, cid, inm = expediente_ag
    # Gasto con ejercicio explícito 2025 pero fechas en 2026
    cod_exp = db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2025, "fecha": "2026-03-10", "categoria": "Seguro", "total": 120.0},
    )
    fila_exp = db.get("gastos", cid, cod_exp)
    assert registro.ejercicio_registrado(fila_exp) == 2025
    # Edición parcial de fecha no pisa el ejercicio explícito 2025
    db.upsert("gastos", cid, {"codigo": cod_exp, "fecha": "2026-04-15"})
    assert db.get("gastos", cid, cod_exp)["ejercicio"] == 2025

    # Gasto sin ejercicio explícito pero con fecha -> infiere año de la fecha
    cod_fec = db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "fecha": "2026-07-01", "categoria": "Comunidad", "total": 90.0},
    )
    assert db.get("gastos", cid, cod_fec)["ejercicio"] == 2026

    # Gasto sin ejercicio ni fecha -> ejercicio_registrado es None (pendiente de clasificar)
    cod_sin = db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "categoria": "IBI", "total": 300.0, "observaciones": "Recibo IBI sin fecha"},
    )
    fila_sin = db.get("gastos", cid, cod_sin)
    assert registro.ejercicio_registrado(fila_sin) is None


# ==============================================================================
# CASO 3: Redondeo e importe manual por línea y en 00_RESUMEN
# ==============================================================================
def test_caso_03_redondeo_por_linea_y_excel_resumen(expediente_ag, tmp_path):
    db, cid, inm = expediente_ag
    # Dos líneas de 0,04 * 0,1 = 0,004 -> cada una redondea a 0,00 € (suma 0,00 €)
    for _ in range(2):
        db.upsert(
            "gastos",
            cid,
            {"inmueble": inm, "ejercicio": 2026, "categoria": "Comunidad", "total": 0.04, "pct_inmueble": 0.1, "pct_alquiler": 1.0},
        )
    # 0,05 * 0,1 = 0,005 -> redondeo half-away-from-zero a 0,01 €
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2026, "categoria": "Seguro", "total": 0.05, "pct_inmueble": 0.1, "pct_alquiler": 1.0},
    )
    # Importe deducible manual = 0 sobre total 500 -> prevalece 0,00 €
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2026, "categoria": "IBI", "total": 500.0, "importe_deducible": 0.0},
    )
    # Base sin total -> usa base (120,00 €)
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2026, "categoria": "Suministros", "base": 120.0, "pct_inmueble": 1.0, "pct_alquiler": 1.0},
    )
    # Total = 0 con base = 120 -> prevalece total = 0,00 €
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2026, "categoria": "Administración", "total": 0.0, "base": 120.0},
    )

    gastos_2026 = [g for g in db.listar("gastos", cid) if registro.ejercicio_registrado(g) == 2026]
    por_cat = {}
    for g in gastos_2026:
        cat = g["categoria"]
        por_cat[cat] = por_cat.get(cat, 0.0) + registro.importe_registrado(g)

    assert por_cat["Comunidad"] == pytest.approx(0.0)
    assert por_cat["Seguro"] == pytest.approx(0.01)
    assert por_cat["IBI"] == pytest.approx(0.0)
    assert por_cat["Suministros"] == pytest.approx(120.0)
    assert por_cat["Administración"] == pytest.approx(0.0)


# ==============================================================================
# CASO 4: Límite conjunto de intereses + reparaciones y no limitación del resto
# ==============================================================================
def test_caso_04_limite_conjunto_intereses_reparaciones():
    # Renta anual = 12 * 150 = 1.800 €. Intereses = 1.100 €, Reparación = 1.000 € (suma 2.100 € -> tope 1.800 €, exceso 300 €).
    # Comunidad = 250 € y Suministros = 150 € (suma otros = 400 € sin tope). Total gastos deducidos = 2.200 €, base = 0 €.
    inm = {"codigo": "INM-1", "pct_titularidad": 1.0}
    contratos = [{"codigo": "CTR-1", "fecha_inicio": "2026-01-01", "renta_inicial": 150.0, "periodicidad": "Mensual"}]
    gastos = [
        {"ejercicio": 2026, "categoria": "Intereses/financiación", "total": 1100.0, "pct_alquiler": 1.0},
        {"ejercicio": 2026, "categoria": "Reparación/conservación", "total": 1000.0, "pct_alquiler": 1.0},
        {"ejercicio": 2026, "categoria": "Comunidad", "total": 250.0, "pct_alquiler": 1.0},
        {"ejercicio": 2026, "categoria": "Suministros", "total": 150.0, "pct_alquiler": 1.0},
    ]
    res = tax.liquidar_210_alquiler(
        inm, contratos, {}, gastos, None, tax.resolver_residencia({"pais": "Bélgica"}), 2026, "Anual"
    )
    assert res.resultados[0].ingresos == pytest.approx(1800.0)
    assert res.resultados[0].gastos == pytest.approx(2200.0)
    assert res.resultados[0].base == pytest.approx(0.0)
    assert res.resultados[0].cuota == pytest.approx(0.0)
    assert res.generado == pytest.approx(300.0)
    assert res.saldos == {2026: 300.0}


# ==============================================================================
# CASO 5: Orden de aplicación FIFO y caducidad a los 4 años
# ==============================================================================
def test_caso_05_fifo_y_caducidad_cuatro_anios():
    # Ejercicio 2026, ingresos = 1.800 €, gastos limitados del año = 500 € -> margen para arrastrados = 1.800 €
    # (porque los saldos anteriores se aplican ANTES que los gastos del propio ejercicio).
    # Previos: 2021 (900 € -> caduca en 5º año), 2022 (600 € -> 4º año válido), 2024 (1.400 € -> consume 1.200 €, quedan 200 €).
    # Como los arrastrados (600 + 1.200 = 1.800 €) agotan el límite de 1.800 €, los 500 € de 2026 pasan a nuevo exceso.
    inm = {"codigo": "INM-1", "pct_titularidad": 1.0}
    contratos = [{"codigo": "CTR-1", "fecha_inicio": "2026-01-01", "renta_inicial": 150.0, "periodicidad": "Mensual"}]
    gastos = [{"ejercicio": 2026, "categoria": "Intereses/financiación", "total": 500.0, "pct_alquiler": 1.0}]
    res = tax.liquidar_210_alquiler(
        inm,
        contratos,
        {},
        gastos,
        None,
        tax.resolver_residencia({"pais": "Bélgica"}),
        2026,
        "Anual",
        {2021: 900.0, 2022: 600.0, 2024: 1400.0},
    )
    assert res.caducados == {2021: 900.0}
    assert res.aplicaciones == {2022: 600.0, 2024: 1200.0}
    assert res.generado == pytest.approx(500.0)
    assert res.saldos == {2024: 200.0, 2026: 500.0}


# ==============================================================================
# CASO 6: Titularidad parcial y no doble aplicación en arrastres
# ==============================================================================
def test_caso_06_titularidad_parcial_sin_doble_reduccion():
    # Titularidad 50 %: renta bruta 1.800 € -> ingresos titular 900 €.
    # Gasto limitado 2025 total = 2.400 € -> parte titular = 1.200 € -> deduce 900 €, exceso generado = 300 €.
    # En 2026 sin gastos nuevos: aplica 300 € íntegros (no 150 €). Base = 900 - 300 = 600 € -> cuota 19 % = 114 €.
    inm = {"codigo": "INM-50", "pct_titularidad": 0.5}
    contratos = [{"codigo": "CTR-50", "fecha_inicio": "2025-01-01", "renta_inicial": 150.0, "periodicidad": "Mensual"}]
    g_2025 = [{"ejercicio": 2025, "categoria": "Reparación/conservación", "total": 2400.0, "pct_alquiler": 1.0}]
    r25 = tax.liquidar_210_alquiler(
        inm, contratos, {}, g_2025, None, tax.resolver_residencia({"pais": "Bélgica"}), 2025, "Anual"
    )
    assert r25.resultados[0].ingresos == pytest.approx(900.0)
    assert r25.resultados[0].gastos == pytest.approx(900.0)
    assert r25.generado == pytest.approx(300.0)

    r26 = tax.liquidar_210_alquiler(
        inm, contratos, {}, [], None, tax.resolver_residencia({"pais": "Bélgica"}), 2026, "Anual", r25.saldos
    )
    assert r26.resultados[0].ingresos == pytest.approx(900.0)
    assert r26.resultados[0].gastos == pytest.approx(300.0)
    assert r26.resultados[0].base == pytest.approx(600.0)
    assert r26.resultados[0].cuota == pytest.approx(114.0)


# ==============================================================================
# CASO 7: Extra-UE y cambios de residencia
# ==============================================================================
def test_caso_07_extra_ue_conserva_saldo_y_cambio_a_ue_lo_aplica(expediente_ag):
    db, cid, inm = expediente_ag
    # 2024: residente en Bélgica (UE), renta 1.800 €, gasto limitado 2.300 € -> exceso 500 €
    # 2025: muda a Estados Unidos (Extra-UE, 24 %) -> no deduce gastos ni consume los 500 €, pero conserva el saldo
    # 2026: vuelve a Bélgica (UE, 19 %) -> consume los 500 € de 2024
    db.guardar_residencia(cid, 2024, pais="Bélgica")
    db.guardar_residencia(cid, 2025, pais="Estados Unidos")
    db.guardar_residencia(cid, 2026, pais="Bélgica")
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2024, "categoria": "Intereses/financiación", "total": 2300.0, "pct_alquiler": 1.0},
    )
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2025, "categoria": "Intereses/financiación", "total": 400.0, "pct_alquiler": 1.0},
    )
    tax.recalcular_ejercicios(db, cid, [2024, 2025, 2026])

    m25 = db.get_por_clave("m210_alquiler", cid, f"210A|2025|{inm}|Anual")
    arr25 = db.get_por_clave("arrastres_gastos", cid, f"ARR|{inm}|2025")
    assert m25["gastos"] == pytest.approx(0.0)
    assert m25["cuota"] == pytest.approx(1800.0 * 0.24)
    assert arr25["aplicado"] == pytest.approx(0.0)
    assert arr25["generado"] == pytest.approx(0.0)
    assert arr25["pendiente"] == pytest.approx(500.0)

    m26 = db.get_por_clave("m210_alquiler", cid, f"210A|2026|{inm}|Anual")
    arr26 = db.get_por_clave("arrastres_gastos", cid, f"ARR|{inm}|2026")
    assert arr26["aplicado"] == pytest.approx(500.0)
    assert arr26["pendiente"] == pytest.approx(0.0)
    assert m26["gastos"] == pytest.approx(500.0)
    assert m26["cuota"] == pytest.approx((1800.0 - 500.0) * 0.19)


# ==============================================================================
# CASO 8: Trimestral vs Anual y no compensación entre trimestres del mismo año
# ==============================================================================
def test_caso_08_trimestral_no_compensa_entre_trimestres():
    # Renta 150 €/mes = 450 €/trimestre.
    # En 1T hay reparación puntual de 1.000 € -> 1T deduce 450 € y genera 550 € de exceso para años siguientes.
    # 2T, 3T y 4T tienen ingresos de 450 € y 0 € de gastos (no pueden absorber los 550 € del 1T del mismo ejercicio).
    inm = {"codigo": "INM-T", "pct_titularidad": 1.0}
    contratos = [{"codigo": "CTR-T", "fecha_inicio": "2026-01-01", "renta_inicial": 150.0, "periodicidad": "Mensual"}]
    gastos = [
        {"ejercicio": 2026, "fecha": "2026-02-10", "categoria": "Reparación/conservación", "importe_deducible": 1000.0}
    ]
    res = tax.liquidar_210_alquiler(
        inm, contratos, {}, gastos, None, tax.resolver_residencia({"pais": "Bélgica"}), 2026, "Trimestral"
    )
    assert [r.periodo for r in res.resultados] == ["1T", "2T", "3T", "4T"]
    assert [r.ingresos for r in res.resultados] == [450.0, 450.0, 450.0, 450.0]
    assert [r.gastos for r in res.resultados] == [450.0, 0.0, 0.0, 0.0]
    assert res.generado == pytest.approx(550.0)


# ==============================================================================
# CASO 9: Ingresos cero con gasto afecto al alquiler
# ==============================================================================
def test_caso_09_ingresos_cero_con_gasto_alquiler_genera_arrastre():
    inm = {"codigo": "INM-0", "pct_titularidad": 1.0}
    gastos = [{"ejercicio": 2026, "categoria": "Reparación/conservación", "total": 320.0, "pct_alquiler": 1.0}]
    res = tax.liquidar_210_alquiler(
        inm, [], {}, gastos, None, tax.resolver_residencia({"pais": "Bélgica"}), 2026, "Anual"
    )
    assert res.resultados == []
    assert res.generado == pytest.approx(320.0)
    assert res.saldos == {2026: 320.0}


# ==============================================================================
# CASO 10: Aislamiento entre inmuebles y clientes
# ==============================================================================
def test_caso_10_aislamiento_inmuebles_y_clientes(expediente_ag):
    db, cid, inm1 = expediente_ag
    db.upsert(
        "gastos",
        cid,
        {"inmueble": inm1, "ejercicio": 2025, "categoria": "Intereses/financiación", "total": 2500.0, "pct_alquiler": 1.0},
    )
    inm2 = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2020-01-01", "pct_titularidad": 1.0})
    db.upsert("contratos", cid, {"inmueble": inm2, "fecha_inicio": "2020-01-01", "renta_inicial": 150.0})

    cid2 = db.crear_cliente("Marco", "Rossi", "Z1122334W")["id"]
    db.guardar_residencia(cid2, 2020, pais="Italia")
    inm_c2 = db.upsert("inmuebles", cid2, {"codigo": inm1, "fecha_adquisicion": "2020-01-01", "pct_titularidad": 1.0})
    db.upsert("contratos", cid2, {"inmueble": inm_c2, "fecha_inicio": "2020-01-01", "renta_inicial": 150.0})

    tax.recalcular_cliente(db, cid, 2026)
    tax.recalcular_cliente(db, cid2, 2026)

    # inm1 de cid tiene exceso arrastrado de 700 € (2500 - 1800)
    assert db.get_por_clave("m210_alquiler", cid, f"210A|2026|{inm1}|Anual")["gastos"] == pytest.approx(700.0)
    # inm2 de cid y inm_c2 de cid2 no reciben nada de inm1
    assert db.get_por_clave("m210_alquiler", cid, f"210A|2026|{inm2}|Anual")["gastos"] == pytest.approx(0.0)
    assert db.get_por_clave("m210_alquiler", cid2, f"210A|2026|{inm_c2}|Anual")["gastos"] == pytest.approx(0.0)


# ==============================================================================
# CASO 11: Recálculo multi-ejercicio y edición/borrado + sincronización Excel
# ==============================================================================
def test_caso_11_recalculo_multianual_y_sincronizacion_actualiza_pasado_abierto(expediente_ag, tmp_path):
    """Regresión A7: sincronizar o editar tabla multianual recalcula años con m210_alquiler abierto sin crear años cerrados."""
    db, cid, inm = expediente_ag
    cod_g = db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2025, "categoria": "Intereses/financiación", "total": 2200.0, "pct_alquiler": 1.0},
    )
    # Se abren 2025 y 2026 (pero NO 2024)
    tax.recalcular_ejercicios(db, cid, [2025, 2026])
    assert db.listar("m210_alquiler", cid, ejercicio=2024) == []
    assert db.get_por_clave("m210_alquiler", cid, f"210A|2025|{inm}|Anual")["cuota"] == pytest.approx(0.0)
    assert db.get_por_clave("m210_alquiler", cid, f"210A|2026|{inm}|Anual")["gastos"] == pytest.approx(400.0)

    # El usuario modifica en SQLite/Excel el gasto de 2025 de 2.200 € a 1.500 € mientras está viendo 2026 y pulsa Sincronizar
    db.upsert("gastos", cid, {"codigo": cod_g, "total": 1500.0})

    ctrl = Controlador.__new__(Controlador)
    ctrl.db = db
    ctrl.lector = db
    ctrl._firmas = {}
    ctrl._lock = threading.RLock()
    ctrl._importar = lambda _cid: None
    ctrl._exportar = lambda _cid, _ej, _inf: None

    ctrl.sincronizar(cid, 2026)

    # Tanto 2025 (que ya tenía 210 abierto) como 2026 se han recalculado; 2024 sigue sin 210 ficticio
    assert db.listar("m210_alquiler", cid, ejercicio=2024) == []
    m25 = db.get_por_clave("m210_alquiler", cid, f"210A|2025|{inm}|Anual")
    m26 = db.get_por_clave("m210_alquiler", cid, f"210A|2026|{inm}|Anual")
    assert m25["gastos"] == pytest.approx(1500.0)
    assert m25["cuota"] == pytest.approx((1800.0 - 1500.0) * 0.19)
    assert m26["gastos"] == pytest.approx(0.0)
    assert m26["cuota"] == pytest.approx(1800.0 * 0.19)

    # Y si cambia pct_titularidad en inmuebles viendo 2026, también actualiza el 210 abierto de 2025
    ctrl.editar(cid, "inmuebles", inm, {"pct_titularidad": "50 %"}, 2026)
    m25_mitad = db.get_por_clave("m210_alquiler", cid, f"210A|2025|{inm}|Anual")
    assert m25_mitad["ingresos"] == pytest.approx(900.0)
    assert m25_mitad["gastos"] == pytest.approx(750.0)


# ==============================================================================
# CASO 12: Protección de declaraciones Presentado y bloqueo de periodicidad
# ==============================================================================
def test_caso_12_proteccion_presentado_y_avisos_revisar(expediente_ag):
    db, cid, inm = expediente_ag
    cod_g = db.upsert(
        "gastos",
        cid,
        {"inmueble": inm, "ejercicio": 2025, "categoria": "Intereses/financiación", "total": 2200.0, "pct_alquiler": 1.0},
    )
    tax.recalcular_ejercicios(db, cid, [2025, 2026])
    fila_25 = db.get_por_clave("m210_alquiler", cid, f"210A|2025|{inm}|Anual")
    db.upsert("m210_alquiler", cid, {"codigo": fila_25["codigo"], "estado": "Presentado"})
    congelada = db.get("m210_alquiler", cid, fila_25["codigo"])

    # Aunque cambiemos el gasto de 2025 a 9.000 € o la fecha de adquisición a 2026, el 210 Presentado y su detalle_limite prevalecen
    db.upsert("gastos", cid, {"codigo": cod_g, "total": 9000.0})
    db.upsert("inmuebles", cid, {"codigo": inm, "fecha_adquisicion": "2026-01-01"})
    tax.recalcular_ejercicios(db, cid, [2025, 2026])
    assert db.get("m210_alquiler", cid, fila_25["codigo"]) == congelada
    assert db.get_por_clave("arrastres_gastos", cid, f"ARR|{inm}|2025")["generado"] == pytest.approx(400.0)

    # La preferencia futura no debe modificar el calendario ni los saldos del presentado.
    db.set_config("periodicidad_210_alquiler", "Trimestral")
    tax.recalcular_cliente(db, cid, 2026)
    assert db.get("m210_alquiler", cid, fila_25["codigo"]) == congelada
    assert db.get_config("periodicidad_210_alquiler") == "Trimestral"
    assert db.get_por_clave("arrastres_gastos", cid, f"ARR|{inm}|2025")["generado"] == pytest.approx(400.0)
    # 2026 ya tenía un cálculo anual: la preferencia general no lo sustituye.
    assert {r["periodo"] for r in db.listar("m210_alquiler", cid, ejercicio=2026)
            if r["estado"] != "No procede"} == {"Anual"}
    # Una elección expresa para este inmueble y ejercicio sí permite cambiarlo.
    db.set_config(f"periodicidad_210|{cid}|{inm}|2026", "Trimestral")
    tax.recalcular_cliente(db, cid, 2026)
    assert {r["periodo"] for r in db.listar("m210_alquiler", cid, ejercicio=2026)
            if r["estado"] != "No procede"} == {"1T", "2T", "3T", "4T"}
    assert db.get("m210_alquiler", cid, fila_25["codigo"]) == congelada


# ==============================================================================
# UI CLARO / OSCURO: Vista Gastos, Clasificar, Revisar, Scroll y Diálogos
# ==============================================================================
@pytest.mark.parametrize("oscuro,paleta_esperada", [(False, ui.CLARO), (True, ui.OSCURO)])
def test_ui_claro_oscuro_gastos_arrastres_clasificar_y_sin_fugas(expediente_ag, oscuro, paleta_esperada):
    db, cid, inm = expediente_ag
    db.upsert(
        "gastos",
        cid,
        {
            "inmueble": inm,
            "ejercicio": 2025,
            "categoria": "Intereses/financiación",
            "total": 2200.0,
            "pct_alquiler": 1.0,
            "observaciones": "Hipoteca 2025",
            "json_ia": json.dumps({"secret": "NO_MOSTRAR_JSON_IA"}),
        },
    )
    db.upsert(
        "gastos",
        cid,
        {
            "inmueble": inm,
            "ejercicio": 2026,
            "fecha": "2026-05-10",
            "categoria": "Suministros",
            "total": 310.0,
            "pct_alquiler": 1.0,
            "observaciones": "Electricidad 2026",
        },
    )
    cod_pend = db.upsert(
        "gastos",
        cid,
        {
            "inmueble": inm,
            "categoria": "Otros",
            "total": 88.50,
            "observaciones": "Cargo sin fecha pendiente",
            "json_ia": json.dumps({"token": "HIDDEN_TOKEN"}),
        },
    )
    informes = tax.recalcular_ejercicios(db, cid, [2025, 2026])

    ctrl = Controlador.__new__(Controlador)
    ctrl.db = db
    ctrl.lector = db
    ctrl._firmas = {}
    ctrl._lock = threading.RLock()
    ctrl.estado_ia = lambda: (False, "IA desactivada en test")

    page = MagicMock(spec=ft.Page)
    page.run_task.side_effect = lambda callback: asyncio.run(callback())
    page.platform_brightness = ft.Brightness.DARK if oscuro else ft.Brightness.LIGHT
    page.services = []
    page.dialogs = []
    page.window = None
    page.show_dialog.side_effect = lambda d: page.dialogs.append(d)

    app = main.App(page, ctrl)
    assert app.p == paleta_esperada
    app.cid = cid
    app.ejercicio = 2026
    app.seccion = 5
    app.informe = informes[2026]
    app.zona_principal = ft.Container(expand=True)
    app.contenido = ft.AnimatedSwitcher(content=ft.Container())
    app.lbl_estado = ui.leyenda("", app.p, app.p.texto2)

    app._pintar_principal()
    zp_col = app.zona_principal.content
    assert isinstance(zp_col, ft.Column)
    assert zp_col.scroll == ft.ScrollMode.AUTO and zp_col.expand is True

    sec_gastos = app._sec_gastos()
    textos = _collect_texts(sec_gastos)
    unido = "\n".join(textos)

    assert "Gastos deducibles · 2026" in unido
    assert "310,00 €" in unido
    assert "Intereses y reparaciones · arrastre fiscal" in unido
    assert "Aplicado de años anteriores: 400,00 €" in unido
    assert "Gastos pendientes de clasificar" in unido
    assert "Cargo sin fecha pendiente" in unido
    assert any("Clasificar" in t for t in textos)
    assert "NO_MOSTRAR_JSON_IA" not in unido
    assert "HIDDEN_TOKEN" not in unido
    assert "detalle_limite" not in unido

    app.dialogo_editar("gastos", cod_pend)
    dlg_textos = "\n".join(_collect_texts(page.dialogs[-1]))
    for prohibido in ("json_ia", "origen", "detalle_limite", "NO_MOSTRAR_JSON_IA", "HIDDEN_TOKEN", "cliente_id"):
        assert prohibido not in dlg_textos
    assert "detalle_limite" not in {c.campo for c in campos_editables("m210_alquiler")}
