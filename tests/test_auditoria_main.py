"""Regresiones de la auditoría A3 (Claude sobre main.py de Antigravity)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import tax_calculator as tc  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures_pdf  # noqa: E402


@pytest.fixture
def ctrl(tmp_path):
    c = main.Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


def test_a3_01_descartado_sale_de_la_bandeja(ctrl, tmp_path):
    cliente = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2026)
    bandeja = Path(cliente["carpeta"]) / "_ARRASTRA_AQUI"
    fixtures_pdf.certificado_residencia(bandeja / "cert.pdf")

    def descartar(_ext):
        raise main.CanceladoPorUsuario()

    res = ctrl.procesar_bandeja(cliente["id"], 2026, confirmar=descartar)
    assert res[0]["cancelado"]
    assert not (bandeja / "cert.pdf").exists()            # el vigilante no lo volverá a ofrecer
    assert (bandeja / "_descartados" / "cert.pdf").exists()
    assert ctrl.procesar_bandeja(cliente["id"], 2026, confirmar=descartar) == []


def test_a3_02_apagar_gananciales_no_machaca_pct_real(ctrl):
    cliente = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2026)
    cid = cliente["id"]
    inm = ctrl.db.upsert("inmuebles", cid, {"fecha_adquisicion": "2019-01-01", "pct_titularidad": 0.25})
    ctrl.set_gananciales(cid, inm, True, 2026)
    assert tc.pct_titularidad(ctrl.db.get("inmuebles", cid, inm)) == 0.5
    ctrl.set_gananciales(cid, inm, False, 2026)
    fila = ctrl.db.get("inmuebles", cid, inm)
    assert fila["pct_titularidad"] == 0.25 and tc.pct_titularidad(fila) == 0.25
