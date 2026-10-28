"""Respaldo previo a una versión nueva, aunque no cambie el esquema SQL."""
from pathlib import Path
import threading

from persistencia import crear_snapshot
from version import VERSION
from app_paths import AlmacenNoDisponible

_BLOQUEO = threading.Lock()


def validar_almacen_existente(db_path: Path) -> None:
    """No crear un almacén vacío si sobreviven señales del expediente anterior."""
    import sqlite3
    from contextlib import closing
    ruta = Path(db_path).absolute()
    if ruta.name.casefold() != 'contabilidad.db':
        return
    copias = ruta.parent / 'Backups_Seguridad'
    hay_copia = copias.is_dir() and any(p.is_file() for p in copias.glob('backup_pre_update_*.db'))
    hay_expedientes = any(p.is_dir() and next(p.iterdir(), None) is not None
                         for p in (ruta.parent / n for n in ('Clientes', 'Clientes_No_Residentes', 'Clientes_Residentes')))
    hay_diario = any(p.is_file() and p.stat().st_size > 0
                     for p in (ruta.with_name(ruta.name + sufijo) for sufijo in ('-wal', '-journal')))
    if not (hay_copia or hay_expedientes or hay_diario):
        return
    if ruta.is_file() and ruta.stat().st_size > 0:
        try:
            with closing(sqlite3.connect(ruta.as_uri() + '?mode=ro', uri=True)) as con:
                existente = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clientes' COLLATE NOCASE").fetchone()
            if existente:
                return
        except sqlite3.Error as exc:
            raise AlmacenNoDisponible('Hay expedientes o copias anteriores, pero no se puede comprobar '
                                      'su base de datos. No se ha creado un almacén vacío. Revisa la unidad '
                                      'y recupera la copia de seguridad si es necesario.') from exc
    raise AlmacenNoDisponible('Falta contabilidad.db, está vacía o no contiene la tabla de clientes, '
                              'pero hay expedientes, diarios o copias anteriores. No se ha creado una '
                              'base de datos vacía. Recupera el almacén desde su copia antes de continuar.')


def preparar_actualizacion(db) -> Path | None:
    """Un fallo de respaldo impide iniciar las transformaciones de la aplicación.

    La migración de esquema tiene su propio respaldo anterior al DDL. Esta copia
    protege también recalculados y conversiones de libros sin cambios de esquema.
    El launcher impide dos procesos de la aplicación en la misma sesión.
    """
    with _BLOQUEO:
        if db.get_config('version_datos_aplicacion') == VERSION:
            return None
        copia = None
        if db.con.execute('SELECT 1 FROM clientes LIMIT 1').fetchone():
            copia = crear_snapshot(db.db_path)
        with db.transaccion():
            if copia is not None:
                db.set_config('respaldo_actualizacion|' + VERSION, str(copia))
            db.set_config('version_datos_aplicacion', VERSION)
        return copia
