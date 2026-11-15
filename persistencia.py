"""Respaldos SQLite consistentes, independientes de las conexiones de la interfaz."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from rutas_seguras import sanitize_safe_path


_PATRON = re.compile(r"backup_pre_update_\d{8}_\d{6}_\d{6}_[0-9a-f]{32}\.db\Z")
_PATRON_DIARIO = re.compile(r"backup_diario_(\d{8})(?:_[0-9a-f]{12})?\.db\Z")
RETENCION_DIARIA = 14
_BLOQUEO = threading.Lock()


def crear_snapshot_diario(db_path: Path | str, hoy: datetime | None = None) -> Path | None:
    """Una copia al día al abrir la app, con nombre y rotación propios (14 días).

    No toca las copias previas a una actualización. Devuelve None si la de hoy ya existe y es válida.
    """
    hoy = hoy or datetime.now()
    with _BLOQUEO:
        origen = _ruta_sin_enlaces(Path(db_path))
        if not origen.is_file():
            return None
        carpeta = _ruta_sin_enlaces(origen.parent / "Backups_Seguridad")
        carpeta.mkdir(exist_ok=True)
        _ruta_sin_enlaces(carpeta)
        destino = carpeta / f"backup_diario_{hoy:%Y%m%d}.db"
        candidatas = ([destino] if destino.exists() else []) + sorted(
            (p for p in carpeta.iterdir() if p != destino and _PATRON_DIARIO.fullmatch(p.name)
             and p.name.startswith(f'backup_diario_{hoy:%Y%m%d}_')), reverse=True)
        for candidata in candidatas:
            existente = _ruta_sin_enlaces(candidata)
            try:
                with closing(sqlite3.connect(existente.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=5)) as copia:
                    if (copia.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                            and copia.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clientes'").fetchone()):
                        return None
            except (sqlite3.Error, OSError):
                pass
        if destino.exists():
            # No dar por buena una copia truncada, ni sobrescribir el archivo encontrado.
            destino = carpeta / f'backup_diario_{hoy:%Y%m%d}_{uuid.uuid4().hex[:12]}.db'
        temporal = destino.with_suffix(f".{uuid.uuid4().hex}.part")
        with temporal.open("xb"):  # exclusivo: nunca trunca un archivo ajeno
            pass
        try:
            limite = time.monotonic() + 60

            def progreso(_estado, _pendientes, _total):
                if time.monotonic() > limite:
                    raise TimeoutError("La copia diaria no terminó dentro del plazo.")

            with closing(sqlite3.connect(origen.as_uri() + "?mode=ro", uri=True, timeout=5)) as lectura:
                with closing(sqlite3.connect(temporal, timeout=5)) as copia:
                    lectura.backup(copia, pages=256, progress=progreso, sleep=0.05)
                    if copia.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("La copia diaria no supera la verificación.")
            with temporal.open("r+b") as archivo:
                os.fsync(archivo.fileno())
            temporal.rename(destino)
        except BaseException:
            temporal.unlink(missing_ok=True)
            raise
        diarias = sorted((p for p in carpeta.iterdir() if _PATRON_DIARIO.fullmatch(p.name)
                          and p.is_file() and not p.is_symlink()
                          and not getattr(p.lstat(), "st_file_attributes", 0) & 0x400),
                         key=lambda p: p.name, reverse=True)
        # Un reloj corregido hacia atrás no debe borrar la copia recién creada.
        conservar = {destino, *[p for p in diarias if p != destino][:RETENCION_DIARIA - 1]}
        for antigua in (p for p in diarias if p not in conservar):
            _ruta_sin_enlaces(antigua).unlink()
        return destino


def _ruta_sin_enlaces(ruta: Path) -> Path:
    """No seguir enlaces ni junctions al abrir origen, destino o respaldos."""
    ruta = ruta.absolute()
    return sanitize_safe_path(ruta.parent, ruta)


def crear_snapshot(db_path: Path | str) -> Path:
    """Copia online incluyendo WAL; nunca crea el origen si falta.

    Llamar fuera de una transacción pendiente del llamante. La copia contiene
    solo cambios confirmados. Los fallos se propagan para impedir la operación
    protegida. Retiene diez copias propias, sin tocar otros archivos.
    """
    with _BLOQUEO:
        origen = _ruta_sin_enlaces(Path(db_path))
        if not origen.is_file():
            raise FileNotFoundError("No existe la base de datos que se debe proteger.")
        carpeta = _ruta_sin_enlaces(origen.parent / "Backups_Seguridad")
        carpeta.mkdir(exist_ok=True)
        _ruta_sin_enlaces(carpeta)
        nombre = f"backup_pre_update_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex}.db"
        destino = carpeta / nombre
        temporal = destino.with_suffix(".db.part")
        # Exclusivo para no truncar un archivo ajeno, incluso ante colisiones.
        with temporal.open("xb"):
            pass
        try:
            limite = time.monotonic() + 60

            def progreso(_estado, _pendientes, _total):
                if time.monotonic() > limite:
                    raise TimeoutError("La copia de seguridad no terminó dentro del plazo.")

            with closing(sqlite3.connect(origen.as_uri() + "?mode=ro", uri=True, timeout=5)) as lectura:
                with closing(sqlite3.connect(temporal, timeout=5)) as copia:
                    lectura.backup(copia, pages=256, progress=progreso, sleep=0.05)
                    if copia.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("La copia de seguridad no supera la verificación.")
            # Cerrar las conexiones antes de publicar y rotar (Windows).
            with temporal.open("r+b") as archivo:
                os.fsync(archivo.fileno())
            temporal.rename(destino)
            propias = [p for p in carpeta.iterdir() if _PATRON.fullmatch(p.name)
                       and not p.is_symlink() and p.is_file()
                       and not getattr(p.lstat(), "st_file_attributes", 0) & 0x400]
            propias.sort(key=lambda p: p.name, reverse=True)
            for antigua in propias[10:]:
                _ruta_sin_enlaces(antigua).unlink()
            return destino
        except BaseException:
            temporal.unlink(missing_ok=True)
            raise
