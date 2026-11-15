"""Arreglos del diagnóstico de la 1.0.7 (supervisado por Fable 5.2). Datos ficticios, sin API."""
from datetime import date

import pytest

import calendario_fiscal as cf
import normativa_patrimonio as np_
import tax_calculator as tc
from controlador import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "C")
    yield c
    c.cerrar()


def test_viernes_santo_y_epifania_en_cualquier_anio():
    assert cf.viernes_santo(2028) == date(2028, 4, 14) and cf.viernes_santo(2030) == date(2030, 4, 19)
    assert cf.siguiente_habil(date(2028, 4, 14)) == date(2028, 4, 17)      # viernes santo + fin de semana
    assert cf.siguiente_habil(date(2029, 1, 6)) == date(2029, 1, 8)         # sábado de Reyes -> lunes
    assert cf.siguiente_habil(date(2033, 1, 6)) == date(2033, 1, 7)         # jueves de Reyes
    assert tc.plazo_210_alquiler(2027, "Anual", 0) == date(2028, 1, 20)


def test_tramo_de_cataluna_cuadra_con_la_cuota_acumulada():
    # 2.632,21 + (1.336.999,75 − 668.499,75) × 0,945 % = 8.949,54: no era una errata
    assert np_.cuota(1336999.75, np_.CATALUNA) == 8949.54
    assert np_.cuota(2673999.01, np_.CATALUNA) == 27199.58


@pytest.mark.parametrize("pais,avisa", [("Federal Republic of Germany", "germany"), ("Republique Francaise", None),
                                        ("España", "España"), ("Rusia", None), ("Estados Unidos de América", None),
                                        ("Francia", None), ("Irlanda del Norte", None), ("Oman", None), ("Northern Ireland", None)])
def test_aviso_de_pais(pais, avisa):
    aviso = tc.aviso_pais_residencia(pais)
    assert (aviso is None) if avisa is None else (avisa in aviso)


def _expediente(ctrl):
    db = ctrl.db
    cid = ctrl.crear_cliente("Prueba", "Residencia", "X0000011T", pais="Alemania", ejercicio=2024)["id"]
    inm = db.upsert("inmuebles", cid, {"direccion": "Calle Ficticia 9", "ref_catastral": "0000000AA0000A0009AA",
                                       "fecha_adquisicion": "2020-01-01", "pct_titularidad": 1})
    db.upsert("catastro", cid, {"ejercicio": 2025, "inmueble": inm, "valor_catastral": 100000,
                                "anio_revision": 2019})
    db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2025-01-01", "fecha_fin_prevista": "2025-12-31",
                                 "estado": "Vigente", "tipo": "Temporada", "renta_inicial": 1000,
                                 "periodicidad": "Mensual"})
    return db, cid, inm


def test_residencia_heredada_deja_el_210_provisional_hasta_confirmarla(ctrl):
    db, cid, inm = _expediente(ctrl)
    inf = tc.recalcular_cliente(db, cid, 2025)
    assert any(a.tipo == "residencia heredada" and a.nivel == "grave" for a in inf.alertas)
    fila = db.listar("m210_alquiler", cid, ejercicio=2025)[0]
    assert fila["estado"] == "Falta documentación"
    ctrl.confirmar_residencia(cid, 2025)
    assert db.residencia(cid, 2025)["ejercicio"] == 2025 and db.residencia(cid, 2025)["pais"] == "Alemania"
    assert db.listar("m210_alquiler", cid, ejercicio=2025)[0]["estado"] == "Pendiente"
    with pytest.raises(ValueError, match="otro año"):
        ctrl.confirmar_residencia(cid, 2025)


def test_amortizacion_sin_desglose_avisa(ctrl):
    db, cid, inm = _expediente(ctrl)
    ctrl.confirmar_residencia(cid, 2025)
    fila = db.listar("m210_alquiler", cid, ejercicio=2025)[0]
    assert "Amortización no calculada" in (fila.get("observaciones") or "")


def test_copia_diaria_propia_sin_tocar_las_previas_a_actualizar(tmp_path):
    from datetime import datetime, timedelta
    import persistencia
    from database import Database
    db = Database(tmp_path / "contabilidad.db")
    db.cerrar()
    carpeta = tmp_path / "Backups_Seguridad"
    previa = persistencia.crear_snapshot(tmp_path / "contabilidad.db")
    base = datetime(2026, 11, 1)
    for d in range(16):
        persistencia.crear_snapshot_diario(tmp_path / "contabilidad.db", base + timedelta(days=d))
    assert persistencia.crear_snapshot_diario(tmp_path / "contabilidad.db", base + timedelta(days=15)) is None
    diarias = sorted(p.name for p in carpeta.glob("backup_diario_*.db"))
    assert len(diarias) == persistencia.RETENCION_DIARIA and diarias[-1] == "backup_diario_20261116.db"
    assert previa.exists()


def test_ejercicios_con_datos_en_el_selector(ctrl):
    db, cid, inm = _expediente(ctrl)
    db.upsert("catastro", cid, {"ejercicio": 2019, "inmueble": inm, "valor_catastral": 90000})
    assert {2019, 2024, 2025} <= ctrl.ejercicios_cliente(cid)
