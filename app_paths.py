"""Recursos de solo lectura separados del almacén persistente del usuario."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import sys

RECURSOS = Path(__file__).resolve().parent


class AlmacenNoDisponible(RuntimeError):
    pass


def configuracion_usuario() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("No se puede localizar la configuración del usuario de Windows.")
    return Path(local) / "ContabilidadAMedida" / "ubicacion.ini"


def carpeta_datos() -> Path:
    """En desarrollo conserva la ubicación anterior; instalada exige el puntero del instalador.

    Si se desconecta una unidad o falla el puntero no crea una BD vacía en otra ubicación.
    """
    if not getattr(sys, "frozen", False):
        return RECURSOS
    parser = configparser.ConfigParser(interpolation=None)
    try:
        # Inno Setup utiliza UTF-16LE para los INI Unicode nuevos.
        raw = configuracion_usuario().read_bytes()
        text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
        parser.read_string(text)
        ruta = Path(parser["Datos"]["Carpeta"])
        if not ruta.is_absolute() or not ruta.is_dir():
            raise ValueError("Carpeta no disponible")
        absoluta = Path(os.path.abspath(ruta))
        if absoluta.resolve() != absoluta:
            raise ValueError("La carpeta es un enlace")
        instalacion = Path(sys.executable).resolve().parent
        if ruta.resolve() == instalacion or instalacion in ruta.resolve().parents:
            raise ValueError("Los datos no pueden estar dentro del programa")
        return absoluta
    except (OSError, ValueError, KeyError, configparser.Error, UnicodeError) as exc:
        raise AlmacenNoDisponible("No se puede abrir la carpeta de datos elegida. Comprueba que la unidad esté conectada y que tengas permisos; no se ha creado otra base de datos.") from exc


def carpeta_actualizaciones() -> Path:
    base = configuracion_usuario().parent / "Actualizaciones"
    base.mkdir(parents=True, exist_ok=True)
    if base.resolve() != Path(os.path.abspath(base)):
        raise ValueError("La carpeta de actualizaciones no puede ser un enlace.")
    return base
