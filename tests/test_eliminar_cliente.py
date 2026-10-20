"""Eliminar cliente: sale de la BD y su carpeta se conserva en _Eliminados (recuperable)."""
from pathlib import Path

import pytest

import fixtures_pdf as f
from main import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


def test_eliminar_cliente_conserva_carpeta_y_borra_datos(ctrl, tmp_path):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2026)["id"]
    otro = ctrl.crear_cliente("Ana", "Ruiz", "Y7654321B", pais="Italia", ejercicio=2026)["id"]
    ctrl.procesar_pdf(cid, f.escritura_compra(tmp_path / "esc.pdf"), 2026)
    carpeta = Path(ctrl.db.get_cliente(cid)["carpeta"])
    destino = ctrl.eliminar_cliente(cid)
    assert not carpeta.exists() and destino.exists()
    assert destino.parent.name == "_Eliminados"
    assert (destino / "Soportes_PDF").is_dir() and list(destino.glob("NR_*.xlsx"))  # PDFs y Excel intactos
    assert ctrl.db.get_cliente(cid) is None
    for tabla in ("inmuebles", "documentos", "residencias", "m210_imputacion", "obligaciones"):
        assert ctrl.db.listar(tabla, cid) == [], tabla
    assert ctrl.db.get_config(f"excel_firma|{cid}") is None
    assert ctrl.db.get_cliente(otro)  # el resto no se toca


def test_eliminar_cliente_con_carpeta_bloqueada_no_toca_nada(ctrl, monkeypatch):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A")["id"]

    def mover_bloqueado(*_a, **_k):
        raise PermissionError("archivo en uso")

    monkeypatch.setattr(Path, "rename", mover_bloqueado)
    with pytest.raises(ValueError, match="cierra su Excel"):
        ctrl.eliminar_cliente(cid)
    assert ctrl.db.get_cliente(cid)


def test_nif_reutilizable_tras_eliminar(ctrl):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A")["id"]
    ctrl.eliminar_cliente(cid)
    nuevo = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A")
    assert nuevo["id"] != cid and Path(nuevo["carpeta"]).is_dir()
