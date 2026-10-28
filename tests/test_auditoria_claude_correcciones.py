"""Auditoría de Claude sobre las correcciones del cliente (26/10/2026). Expedientes ficticios, sin API."""
import pytest

import tax_calculator as tc
from controlador import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "C")
    yield c
    c.cerrar()


def _expediente(ctrl):
    db = ctrl.db
    cid = ctrl.crear_cliente("Prueba", "Periodo", "X0000077T", pais="Alemania", ejercicio=2025)["id"]
    inm = db.upsert("inmuebles", cid, {"direccion": "Calle Ficticia 7", "fecha_adquisicion": "2020-01-01",
                                       "pct_titularidad": 1})
    db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2024-01-01", "estado": "Vigente",
                                 "tipo": "Vivienda", "renta_inicial": 1000, "periodicidad": "Mensual"})
    return db, cid, inm


def test_fila_210_con_periodo_vacio_o_desconocido_no_bloquea_el_recalculo(ctrl):
    db, cid, inm = _expediente(ctrl)
    for clave, periodo in (("manual-vacio", ""), ("manual-texto", "Enero")):
        db.upsert("m210_alquiler", cid, {"ejercicio": 2025, "inmueble": inm, "periodo": periodo,
                                         "estado": "En preparación"}, clave=clave)
    tc.recalcular_cliente(db, cid, 2025)                         # antes: ValueError en todo el cliente
    assert tc.periodicidad_210_alquiler(db, cid, inm, 2025) == "Anual"
    with pytest.raises(ValueError, match="otra periodicidad"):   # el selector pide revisarlas antes de cambiar
        ctrl.establecer_periodicidad_210(cid, inm, 2025, "Trimestral")


def test_210_presentado_guardado_como_OA_se_reconoce_como_anual(ctrl):
    db, cid, inm = _expediente(ctrl)
    db.upsert("m210_alquiler", cid, {"ejercicio": 2024, "inmueble": inm, "periodo": "OA", "base": 12000,
                                     "cuota": 2280, "estado": "Presentado"}, clave="antiguo-OA")
    tc.recalcular_cliente(db, cid, 2024)
    filas = db.listar("m210_alquiler", cid, inmueble=inm, ejercicio=2024)
    assert [f["periodo"] for f in filas if f.get("estado") == "Presentado"] == ["OA"]
    assert not [f for f in filas if f.get("estado") not in ("Presentado", "No procede")]   # sin anual duplicado
    assert tc.periodicidad_210_alquiler(db, cid, inm, 2024) == "Anual"


@pytest.mark.parametrize("ejercicio,periodo,plazo", [
    (2025, "Anual", "2026-01-20"), (2026, "Anual", "2027-04-20"), (2024, "0A", "2025-01-20"),
    (2026, "1T", "2026-04-20"), (2026, "3T", "2026-10-20"), (2026, "4T", "2027-04-20"),
    (2027, "2T", "2028-04-20"), (2025, "4T", "2026-01-20")])
def test_plazos_210_alquiler_segun_aeat(ejercicio, periodo, plazo):
    """AEAT, modelo 210 (consulta 26/10/2026): anual de 2026 en adelante en abril; separadas desde octubre de 2026."""
    assert str(tc.plazo_210_alquiler(ejercicio, periodo)) == plazo
