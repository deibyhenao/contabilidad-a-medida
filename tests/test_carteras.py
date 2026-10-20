"""Separación física y lógica de las carteras No Residentes / Residentes."""
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from carteras import NR, RAIZ, RES
from controlador import Controlador
from database import Database


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path)
    yield c
    c.cerrar()


def test_cada_cliente_vive_en_la_raiz_de_su_cartera(ctrl, tmp_path):
    nr = ctrl.crear_cliente("Ana", "Ruiz", "Y1111111A", pais="Francia", ejercicio=2025)
    au = ctrl.crear_cliente("Luis", "Gil", "12345678Z", tipo_cliente="Autónomo")
    py = ctrl.crear_cliente("Tienda", "Norte SL", "B12345678", tipo_cliente="Pyme")
    assert Path(nr["carpeta"]).parent == tmp_path / RAIZ[NR]
    assert Path(au["carpeta"]).parent == tmp_path / RAIZ[RES] == Path(py["carpeta"]).parent
    for c in (nr, au, py):
        assert (Path(c["carpeta"]) / "Soportes_PDF").is_dir() and (Path(c["carpeta"]) / "_ARRASTRA_AQUI").is_dir()
    assert [ctrl.db.get_cliente(c["id"])["cartera"] for c in (nr, au, py)] == [NR, RES, RES]
    with pytest.raises(ValueError, match="Tipo de cliente no válido"):
        ctrl.crear_cliente("X", "Y", "Z9999999Z", tipo_cliente="Empresa rara")


def test_600_clientes_listado_contador_y_busqueda_por_cartera(ctrl):
    db = ctrl.db
    with db.transaccion():
        for i in range(300):
            db.crear_cliente(f"Nora{i:03d}", f"Apellido{i % 37:02d}", f"X{i:07d}A")
            db.crear_cliente(f"Raul{i:03d}", f"Apellido{i % 41:02d}", f"R{i:07d}B", tipo_cliente="Autónomo")
    assert (db.contar_clientes(NR), db.contar_clientes(RES), db.contar_clientes()) == (300, 300, 600)
    inicio = time.perf_counter()
    pagina = db.listar_clientes(limite=50, cartera=RES)
    assert time.perf_counter() - inicio < 0.2
    assert len(pagina) == 50 and {c["tipo_cliente"] for c in pagina} == {"Autónomo"}
    claves = [(c["apellidos"], c["nombre"], c["id"]) for c in pagina]
    assert claves == sorted(claves)
    todas = [c["id"] for d in range(0, 300, 50) for c in db.listar_clientes(limite=50, desplazamiento=d, cartera=RES)]
    assert len(set(todas)) == 300
    assert db.listar_clientes(busqueda="x0000123a", cartera=NR)[0]["nif"] == "X0000123A"
    assert db.listar_clientes(busqueda="X0000123A", cartera=RES) == []
    plan = " ".join(r[3] for r in db.con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM clientes WHERE cartera=? ORDER BY apellidos,nombre,id LIMIT 50", (RES,)))
    assert "ix_clientes_cartera_orden" in plan and "TEMP B-TREE" not in plan
    with pytest.raises(ValueError):
        db.listar_clientes(cartera="OTRA")


def test_verificar_cartera_rechaza_mezcla_y_enlaces(ctrl, tmp_path):
    au = ctrl.crear_cliente("Luis", "Gil", "12345678Z", tipo_cliente="Autónomo")
    otro = ctrl.crear_cliente("Eva", "Sanz", "87654321X", tipo_cliente="Autónomo")
    assert ctrl.db.verificar_cartera(au) == Path(au["carpeta"]).resolve()
    mal = tmp_path / RAIZ[NR] / Path(au["carpeta"]).name
    Path(au["carpeta"]).rename(mal)
    ctrl.db.actualizar_cliente(au["id"], carpeta=str(mal))
    with pytest.raises(ValueError, match="no mezclar"):
        ctrl.db.verificar_cartera(ctrl.db.get_cliente(au["id"]))
    enlace = tmp_path / RAIZ[RES] / "Enlace_X"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(otro["carpeta"])], capture_output=True)
    if r.returncode != 0:
        pytest.skip("No se pudo crear la junction en este equipo")
    ctrl.db.actualizar_cliente(au["id"], carpeta=str(enlace))
    with pytest.raises(ValueError, match="enlace"):
        ctrl.db.verificar_cartera(ctrl.db.get_cliente(au["id"]))


def _legado(tmp_path, nombres_tipos):
    """BD «antigua»: carpeta_clientes = tmp/Clientes y expedientes dentro, sin migrar."""
    db = Database(tmp_path / "t.db")
    antigua = tmp_path / "Clientes"
    db.set_config("carpeta_clientes", str(antigua))
    ids = []
    for nombre, tipo, nif in nombres_tipos:
        carpeta = antigua / f"{nombre}_{nif}"
        (carpeta / "Soportes_PDF").mkdir(parents=True)
        (carpeta / "Soportes_PDF" / "doc.pdf").write_bytes(b"%PDF-1.4 demo")
        if tipo == "Autónomo":
            (carpeta / f"NR_{nombre.upper()}_{nif}.xlsx").write_bytes(b"excel")
        cur = db.con.execute("INSERT INTO clientes(nombre, apellidos, nif, tipo_cliente, carpeta, creado, cartera) "
                             "VALUES(?,?,?,?,?,?,?)", (nombre, "", nif, tipo, str(carpeta),
                                                       datetime.now().isoformat(), "NR"))
        ids.append(cur.lastrowid)
    (antigua / "_Eliminados" / "Viejo").mkdir(parents=True)
    db.con.commit()
    db.cerrar()
    return ids


def test_migracion_mueve_cada_expediente_y_es_idempotente(tmp_path):
    ids = _legado(tmp_path, [("Ana", "No residente", "Y1"), ("Luis", "Autónomo", "Z2")])
    c = Controlador(tmp_path / "t.db")
    try:
        assert c.migracion["movidos"] == 2 and Path(c.migracion["copia"]).exists()
        nr, au = (c.db.get_cliente(i) for i in ids)
        assert Path(nr["carpeta"]) == tmp_path / "Clientes" / RAIZ[NR] / "Ana_Y1"
        assert Path(au["carpeta"]) == tmp_path / "Clientes" / RAIZ[RES] / "Luis_Z2"
        assert (Path(au["carpeta"]) / "Soportes_PDF" / "doc.pdf").exists() and au["cartera"] == RES
        assert not list(Path(au["carpeta"]).glob("NR_*.xlsx"))
        assert (Path(au["carpeta"]) / "_backups" / "NR_LUIS_Z2_antes_residentes.xlsx").exists()
        assert (tmp_path / "Clientes" / RAIZ[NR] / "_Eliminados" / "Viejo").is_dir()
        with sqlite3.connect(c.migracion["copia"]) as copia:  # la copia guarda las rutas antiguas
            assert "Clientes\\Ana_Y1" in copia.execute("SELECT carpeta FROM clientes WHERE id=?", (ids[0],)).fetchone()[0]
    finally:
        c.cerrar()
    c2 = Controlador(tmp_path / "t.db")
    try:
        assert c2.migracion["movidos"] == 0 and c2.aviso_migracion is None
    finally:
        c2.cerrar()


def test_migracion_fallida_deshace_todo(tmp_path, monkeypatch):
    ids = _legado(tmp_path, [("Ana", "No residente", "Y1"), ("Luis", "Autónomo", "Z2")])
    original = Path.rename

    def falso(self, destino):
        if self.name == "Luis_Z2" and RAIZ[RES] in str(destino):
            raise OSError("archivo en uso")
        return original(self, destino)

    monkeypatch.setattr(Path, "rename", falso)
    c = Controlador(tmp_path / "t.db")
    try:
        assert c.migracion is None and "No se ha movido nada" in c.aviso_migracion
        assert (tmp_path / "Clientes" / "Ana_Y1" / "Soportes_PDF" / "doc.pdf").exists()
        assert Path(c.db.get_cliente(ids[0])["carpeta"]) == tmp_path / "Clientes" / "Ana_Y1"
        assert c.db.get_config("version_carteras") is None
    finally:
        c.cerrar()


def test_cambiar_tipo_mueve_la_carpeta_de_cartera(ctrl, tmp_path):
    cid = ctrl.crear_cliente("Ana", "Ruiz", "Y1111111A", pais="Francia", ejercicio=2025)["id"]
    ctrl.sincronizar(cid, 2025)  # genera su Excel NR
    carpeta = Path(ctrl.db.get_cliente(cid)["carpeta"])
    (carpeta / "Soportes_PDF" / "factura.pdf").write_bytes(b"%PDF-1.4")
    ctrl.cambiar_tipo_cliente(cid, "Autónomo", 2025)
    nueva = Path(ctrl.db.get_cliente(cid)["carpeta"])
    assert nueva.parent == tmp_path / RAIZ[RES] and not carpeta.exists()
    assert (nueva / "Soportes_PDF" / "factura.pdf").exists()
    assert list(nueva.glob("RES_*.xlsx")) and list((nueva / "_backups").glob("NR_*_antes_cambio_cartera.xlsx"))
    fila = ctrl.db.get_por_clave("m303", cid, "303|2025|1T")
    ctrl.db.upsert("m303", cid, {"codigo": fila["codigo"], "estado": "Presentado"})
    with pytest.raises(ValueError, match="declaraciones presentadas"):
        ctrl.cambiar_tipo_cliente(cid, "No residente", 2025)


def test_eliminar_cliente_va_a_la_papelera_de_su_cartera(ctrl, tmp_path):
    cid = ctrl.crear_cliente("Luis", "Gil", "12345678Z", tipo_cliente="Autónomo")["id"]
    destino = ctrl.eliminar_cliente(cid)
    assert destino.parent == tmp_path / RAIZ[RES] / "_Eliminados"
