"""La nota automática de la imputación se actualiza al recalcular (no se queda la del cálculo anterior)."""
import pytest

from controlador import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


def _imputacion(ctrl, cid, ejercicio):
    return next(f for f in ctrl.db.listar("m210_imputacion", cid) if int(f["ejercicio"]) == ejercicio)


def test_nota_sin_valor_catastral_desaparece_al_llegar_el_ibi(ctrl):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2025)["id"]
    ctrl.editar(cid, "inmuebles", None, {"direccion": "Calle Mayor 1", "fecha_adquisicion": "01/01/2019",
                                         "precio_escritura": "180000", "estado": "Activo"}, 2025)
    assert _imputacion(ctrl, cid, 2025)["observaciones"].startswith("Sin valor catastral")
    ctrl.editar(cid, "catastro", None, {"ejercicio": 2025, "inmueble": "INM-001", "valor_catastral": "90000",
                                        "anio_revision": 2019}, 2025)
    fila = _imputacion(ctrl, cid, 2025)
    assert fila["valor_catastral"] == 90000 and not fila["observaciones"]


def test_nota_manual_se_conserva(ctrl):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2025)["id"]
    ctrl.editar(cid, "inmuebles", None, {"direccion": "Calle Mayor 1", "fecha_adquisicion": "01/01/2019",
                                         "precio_escritura": "180000", "estado": "Activo"}, 2025)
    ctrl.editar(cid, "catastro", None, {"ejercicio": 2025, "inmueble": "INM-001", "valor_catastral": "90000",
                                        "anio_revision": 2019}, 2025)
    codigo = _imputacion(ctrl, cid, 2025)["codigo"]
    ctrl.db.upsert("m210_imputacion", cid, {"codigo": codigo, "observaciones": "Llamar al cliente"})
    ctrl.sincronizar(cid, 2025)
    assert _imputacion(ctrl, cid, 2025)["observaciones"] == "Llamar al cliente"


def test_fila_solo_con_valor_de_referencia_hereda_el_catastral_anterior(ctrl):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2025)["id"]
    ctrl.editar(cid, "inmuebles", None, {"direccion": "Calle Mayor 1", "fecha_adquisicion": "01/01/2019",
                                         "precio_escritura": "180000", "estado": "Activo"}, 2025)
    ctrl.editar(cid, "catastro", None, {"ejercicio": 2024, "inmueble": "INM-001", "valor_catastral": "90000",
                                        "valor_construccion": "54000", "anio_revision": 2012}, 2024)
    ctrl.editar(cid, "catastro", None, {"ejercicio": 2025, "inmueble": "INM-001", "valor_referencia": "210000"},
                2025)
    cat = ctrl.db.catastro(cid, "INM-001", 2025)
    assert cat["valor_catastral"] == 90000 and cat["valor_referencia"] == 210000 and cat["_heredado"] == 2024
    fila = _imputacion(ctrl, cid, 2025)
    assert fila["valor_catastral"] == 90000 and fila["pct_imputacion"] == pytest.approx(0.011)  # DA 55: revisión desde 2012 en 2025.
    assert fila["observaciones"].startswith("Valor catastral heredado de 2024")
