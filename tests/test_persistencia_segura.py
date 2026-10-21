"""Migración y respaldos sobre bases ficticias; nunca usan expedientes reales."""
import sqlite3
import threading

import pytest

import database
from database import Database, VERSION_ESQUEMA
from persistencia import crear_snapshot


def antigua(ruta):
    with sqlite3.connect(ruta) as con:
        con.execute("CREATE TABLE config (clave TEXT PRIMARY KEY, valor TEXT)")
        con.execute("INSERT INTO config VALUES ('manual', 'conservar')")


def test_migracion_respalda_antes_y_es_idempotente(tmp_path):
    ruta = tmp_path / "antigua.db"
    antigua(ruta)
    with Database(ruta) as db:
        assert db.get_config("manual") == "conservar"
        assert db.con.execute("PRAGMA user_version").fetchone()[0] == VERSION_ESQUEMA
    copias = list((tmp_path / "Backups_Seguridad").glob("*.db"))
    assert len(copias) == 1
    with sqlite3.connect(copias[0]) as copia:
        assert copia.execute("PRAGMA user_version").fetchone()[0] == 0
        assert copia.execute("SELECT valor FROM config").fetchone()[0] == "conservar"
        assert copia.execute("SELECT count(*) FROM sqlite_master WHERE name='clientes'").fetchone()[0] == 0
    with Database(ruta):
        pass
    assert list((tmp_path / "Backups_Seguridad").glob("*.db")) == copias


def test_migracion_fallida_revierte_ddl_datos_y_version(tmp_path):
    ruta = tmp_path / "antigua.db"
    antigua(ruta)

    class Fallida(Database):
        def _crear_esquema(self):
            super()._crear_esquema()
            self.set_config("manual", "no guardar")
            raise RuntimeError("Fallo de migración simulado")

    with pytest.raises(RuntimeError, match="simulado"):
        Fallida(ruta)
    with sqlite3.connect(ruta) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
        assert con.execute("SELECT valor FROM config").fetchone()[0] == "conservar"
        assert con.execute("SELECT count(*) FROM sqlite_master WHERE name='clientes'").fetchone()[0] == 0


def test_fallo_respaldo_impide_migrar(tmp_path, monkeypatch):
    ruta = tmp_path / "antigua.db"
    antigua(ruta)

    def sin_espacio(_ruta):
        raise OSError("Disco de respaldo lleno")

    monkeypatch.setattr(database, "crear_snapshot", sin_espacio)
    with pytest.raises(OSError, match="lleno"):
        Database(ruta)
    with sqlite3.connect(ruta) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM sqlite_master WHERE name='clientes'").fetchone()[0] == 0


def test_version_futura_rechazada_sin_modificar(tmp_path):
    ruta = tmp_path / "futura.db"
    antigua(ruta)
    with sqlite3.connect(ruta) as con:
        con.execute(f"PRAGMA user_version = {VERSION_ESQUEMA + 1}")
    antes = ruta.read_bytes()
    for lectura in (False, True):
        with pytest.raises(RuntimeError, match="reciente"):
            Database(ruta, solo_lectura=lectura)
    assert ruta.read_bytes() == antes
    assert not (tmp_path / "Backups_Seguridad").exists()


def test_configuracion_conexiones_e_indices(tmp_path):
    ruta = tmp_path / "nueva.db"
    with Database(ruta) as db:
        assert db.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.con.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.con.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert db.con.execute("PRAGMA cache_size").fetchone()[0] == -64000
        assert db.con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.con.execute("PRAGMA index_info(ix_inmuebles_override)").fetchall()
    assert not (tmp_path / "Backups_Seguridad").exists()
    with Database(ruta, solo_lectura=True) as db:
        assert db.con.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.con.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.con.execute("PRAGMA cache_size").fetchone()[0] == -64000


def test_snapshot_incluye_wal_confirmado_y_excluye_transaccion_pendiente(tmp_path):
    ruta = tmp_path / "datos.db"
    with sqlite3.connect(ruta) as escritor:
        escritor.execute("PRAGMA journal_mode=WAL")
        escritor.execute("PRAGMA wal_autocheckpoint=0")
        escritor.execute("CREATE TABLE datos (valor TEXT)")
        escritor.execute("INSERT INTO datos VALUES ('confirmado')")
        escritor.commit()
        escritor.execute("INSERT INTO datos VALUES ('pendiente')")
        copia = crear_snapshot(ruta)
        with sqlite3.connect(copia) as con:
            assert con.execute("SELECT valor FROM datos").fetchall() == [("confirmado",)]
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        escritor.rollback()


def test_snapshot_rotacion_concurrente_solo_archivos_propios(tmp_path):
    ruta = tmp_path / "datos.db"
    antigua(ruta)
    carpeta = tmp_path / "Backups_Seguridad"
    carpeta.mkdir()
    ajenas = (carpeta / "manual.db", carpeta / "backup_pre_update_20260101_120000.db")
    for ajena in ajenas:
        ajena.write_text("conservar")
    errores = []

    def ejecutar():
        try:
            crear_snapshot(ruta)
        except Exception as exc:
            errores.append(exc)

    hilos = [threading.Thread(target=ejecutar) for _ in range(14)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(15)
        assert not hilo.is_alive()
    assert not errores
    assert len(list(carpeta.glob("*.db"))) == 12
    assert all(p.read_text() == "conservar" for p in ajenas)
    assert not list(carpeta.glob("*.part"))


def test_snapshot_no_crea_origen_ausente_ni_publica_bd_corrupta(tmp_path):
    ruta = tmp_path / "ausente.db"
    with pytest.raises(FileNotFoundError):
        crear_snapshot(ruta)
    assert not ruta.exists()
    ruta.write_bytes(b"documento corrupto")
    with pytest.raises(sqlite3.DatabaseError):
        crear_snapshot(ruta)
    assert not list((tmp_path / "Backups_Seguridad").iterdir())


def test_database_rechaza_escape_antes_de_crear_archivo(tmp_path):
    ruta = tmp_path / "carpeta" / ".." / "no_crear.db"
    with pytest.raises(ValueError):
        Database(ruta)
    assert not (tmp_path / "no_crear.db").exists()
    assert not (tmp_path / "carpeta").exists()


def test_migracion_no_toca_bd_si_carpeta_backup_esta_bloqueada(tmp_path):
    ruta = tmp_path / "antigua.db"
    antigua(ruta)
    (tmp_path / "Backups_Seguridad").write_text("Archivo ajeno; conservar")
    antes = ruta.read_bytes()
    with pytest.raises(OSError):
        Database(ruta)
    assert ruta.read_bytes() == antes


def test_repara_indices_actuales_con_copia_previa_sin_repetir_migracion(tmp_path, monkeypatch):
    ruta = tmp_path / "indices.db"
    with Database(ruta) as db:
        db.set_config("manual", "conservar")
        esperados = set(database._indices_esquema())
        for nombre in esperados:
            db.con.execute(f'DROP INDEX "{nombre}"')
        db.con.commit()
    eventos = []
    snapshot_real = database.crear_snapshot

    def snapshot_antes(ruta):
        with sqlite3.connect(ruta) as con:
            presentes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert not presentes & esperados
        eventos.append("backup")
        return snapshot_real(ruta)

    def no_migrar(_self):
        pytest.fail("La reparación de índices no debe repetir el esquema")

    monkeypatch.setattr(database, "crear_snapshot", snapshot_antes)
    monkeypatch.setattr(Database, "_crear_esquema", no_migrar)
    for _ in range(2):
        with Database(ruta) as db:
            presentes = {r[0] for r in db.con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert esperados <= presentes
            assert db.get_config("manual") == "conservar"
            assert db.con.execute("PRAGMA user_version").fetchone()[0] == VERSION_ESQUEMA
    assert eventos == ["backup"]
    assert len(list((tmp_path / "Backups_Seguridad").glob("*.db"))) == 1


def test_reparacion_indices_fallo_backup_no_ejecuta_ddl(tmp_path, monkeypatch):
    ruta = tmp_path / "indices.db"
    with Database(ruta) as db:
        db.set_config("manual", "conservar")
        db.con.execute("DROP INDEX ix_clientes_orden")
        db.con.commit()

    def sin_copia(_ruta):
        raise OSError("Sin espacio para respaldo")

    monkeypatch.setattr(database, "crear_snapshot", sin_copia)
    with pytest.raises(OSError, match="respaldo"):
        Database(ruta)
    with sqlite3.connect(ruta) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='ix_clientes_orden'").fetchall()
        assert con.execute("SELECT valor FROM config WHERE clave='manual'").fetchone()[0] == "conservar"
        assert con.execute("PRAGMA user_version").fetchone()[0] == VERSION_ESQUEMA
