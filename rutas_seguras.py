"""Validación común de rutas locales sin cambiar la raíz elegida por el usuario."""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import re
import stat
import unicodedata


def _componente_valido(nombre: str) -> bool:
    return not (
        re.search(r'[<>:"|?*]', nombre)
        or nombre.endswith((" ", "."))
        or any(unicodedata.category(c).startswith("C") for c in nombre)
        or re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]",
                        nombre.split(".", 1)[0].rstrip(" "), re.I)
    )


def _sin_enlaces(ruta: Path) -> None:
    # lstat también detecta enlaces rotos; exists() los ocultaría.
    for parte in (*reversed(ruta.parents), ruta):
        try:
            datos = parte.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(datos.st_mode) or getattr(datos, "st_file_attributes", 0) & 1024:
            raise ValueError("La ruta contiene un enlace o una junction no permitidos.")


def sanitize_safe_path(base_dir: Path | str, target_path: Path | str) -> Path:
    """Devuelve una ruta real confinada a base_dir, sin escapes ni reparse points.

    Una ruta relativa se interpreta respecto de base_dir. No crea archivos ni
    directorios. Acepta destinos aún inexistentes y conserva Unicode. La raíz
    debe ser la del cliente para operaciones documentales, no la de la cartera.
    Esta comprobación no sustituye permisos Windows contra otro proceso local
    que pueda intercambiar directorios entre validación y apertura (TOCTOU).
    """
    base = Path(base_dir)
    texto = os.fspath(target_path)
    objetivo = Path(texto.replace("\\", "/"))
    if ".." in base.parts or ".." in objetivo.parts:
        raise ValueError("La ruta contiene un escape de directorio no permitido.")
    if PureWindowsPath(texto).drive and not objetivo.is_absolute():
        raise ValueError("La ruta de Windows no es absoluta.")
    base = base.absolute()
    candidato = objetivo if objetivo.is_absolute() else base / objetivo
    try:
        relativa = candidato.relative_to(base)
    except ValueError:
        raise ValueError("La ruta sale de la carpeta autorizada.") from None
    if any(not _componente_valido(parte) for parte in relativa.parts):
        raise ValueError("La ruta contiene un nombre no permitido en Windows.")
    try:
        _sin_enlaces(base)
        _sin_enlaces(candidato)
        real_base, real = base.resolve(), candidato.resolve()
        real.relative_to(real_base)
    except (OSError, RuntimeError):
        raise ValueError("No se puede verificar la ruta autorizada.") from None
    except ValueError:
        raise ValueError("La ruta sale de la carpeta autorizada o contiene un enlace.") from None
    return real
