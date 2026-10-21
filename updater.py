"""Actualizaciones de GitHub Releases: HTTPS, versión, tamaño y SHA-256 obligatorios.

Las copias preventivas usan SQLite backup; el instalador sustituye únicamente
los archivos del programa una vez cerrada la aplicación.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

import app_paths
from version import RELEASE_REPOSITORY, VERSION, EXE_NAME

MAX_INSTALLER = 1024 * 1024 * 1024
MAX_JSON = 1024 * 1024


class ErrorActualizacion(RuntimeError):
    pass


class DescargaCancelada(ErrorActualizacion):
    pass


def version_numerica(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str):
        raise ErrorActualizacion("La versión publicada no tiene un formato válido.")
    m = re.fullmatch(r"v?(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})", version)
    if not m:
        raise ErrorActualizacion("La versión publicada no tiene un formato válido.")
    return tuple(int(x) for x in m.groups())


def _url_segura(url: str, *, descarga: bool = False) -> str:
    p = urlsplit(url)
    permitidos = {"api.github.com", "github.com"}
    if descarga:
        permitidos |= {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    if p.scheme != "https" or p.hostname not in permitidos or p.username or p.password or p.port not in (None, 443) or p.fragment:
        raise ErrorActualizacion("Origen de actualización no permitido.")
    return url


class _RedirectSeguro(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url_segura(newurl, descarga=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _abrir(url: str):
    _url_segura(url, descarga=True)
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ContabilidadAMedida/" + VERSION})
    return build_opener(HTTPSHandler(), _RedirectSeguro()).open(request, timeout=20)


@dataclass(frozen=True)
class Publicacion:
    version: str
    url: str
    sha256: str
    size: int
    nombre: str


def comprobar(version_actual: str = VERSION, *, abrir=None) -> Publicacion | None:
    """Solo releases estables publicadas; ignora previews y nunca baja de versión."""
    abrir = abrir or _abrir
    try:
        with abrir(f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest") as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ErrorActualizacion("La respuesta de actualización es demasiado grande.")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
            raise ErrorActualizacion("No hay una publicación estable válida.")
        nueva = data.get("tag_name", "")
        numero = version_numerica(nueva)
        if numero <= version_numerica(version_actual):
            return None
        version = ".".join(str(x) for x in numero)
        nombre = f"ContabilidadAMedida-{version}-windows-x64-Setup.exe"
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise ErrorActualizacion("La publicación no contiene un instalador válido.")
        candidatos = [a for a in assets if isinstance(a, dict) and a.get("name") == nombre and a.get("state") == "uploaded"]
        if len(candidatos) != 1:
            raise ErrorActualizacion("No se ha publicado un instalador único para Windows x64.")
        a = candidatos[0]
        digest = a.get("digest", "")
        size = a.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ErrorActualizacion("GitHub no proporciona la huella SHA-256 del instalador.")
        if type(size) is not int or not 0 < size <= MAX_INSTALLER:
            raise ErrorActualizacion("Tamaño del instalador no permitido.")
        url = _url_segura(a.get("browser_download_url", ""))
        if urlsplit(url).path != f"/{RELEASE_REPOSITORY}/releases/download/{nueva}/{nombre}":
            raise ErrorActualizacion("El instalador no pertenece al repositorio y versión esperados.")
        return Publicacion(version, url, digest[7:].lower(), size, nombre)
    except ErrorActualizacion:
        raise
    except Exception as exc:
        raise ErrorActualizacion("No se pudieron buscar actualizaciones. Comprueba la conexión e inténtalo de nuevo.") from exc


def _snapshot_previo(db_path: Path | None) -> None:
    if db_path is None and getattr(sys, "frozen", False):
        db_path = app_paths.carpeta_datos() / "contabilidad.db"
    if db_path is not None:
        from persistencia import crear_snapshot
        try:
            crear_snapshot(db_path)
        except Exception as exc:
            raise ErrorActualizacion("No se pudo guardar la copia de seguridad. La actualización se ha detenido.") from exc


def _validar_ejecutable(ruta: Path) -> None:
    # MZ solo no basta: comprueba también el desplazamiento y la firma PE.
    with ruta.open("rb") as archivo:
        cabecera = archivo.read(64)
        if len(cabecera) < 64 or cabecera[:2] != b"MZ":
            raise ErrorActualizacion("El archivo descargado no es un ejecutable válido de Windows.")
        offset = int.from_bytes(cabecera[60:64], "little")
        if offset < 64 or offset > ruta.stat().st_size - 4:
            raise ErrorActualizacion("La cabecera del instalador no es válida.")
        archivo.seek(offset)
        if archivo.read(4) != b"PE\x00\x00":
            raise ErrorActualizacion("La firma del instalador no es válida.")


def descargar(publicacion: Publicacion, progreso, cancelar: threading.Event, *, carpeta: Path | None = None, abrir=None, db_path: Path | None = None) -> Path:
    if cancelar.is_set():
        raise DescargaCancelada("Descarga cancelada.")
    _snapshot_previo(db_path)
    abrir = abrir or _abrir
    base = Path(carpeta) if carpeta is not None else app_paths.carpeta_actualizaciones()
    base.mkdir(parents=True, exist_ok=True)
    if base.resolve() != Path(os.path.abspath(base)):
        raise ErrorActualizacion("La carpeta de descarga no es segura.")
    # Nombre local aleatorio; no se utiliza una ruta proporcionada por el servidor.
    fd, nombre = tempfile.mkstemp(prefix="CAM-", suffix=".exe.part", dir=base)
    ruta = Path(nombre)
    h = hashlib.sha256()
    recibidos = 0
    try:
        with os.fdopen(fd, "wb") as output, abrir(publicacion.url) as response:
            while True:
                if cancelar.is_set():
                    raise DescargaCancelada("Descarga cancelada.")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                recibidos += len(chunk)
                if recibidos > publicacion.size or recibidos > MAX_INSTALLER:
                    raise ErrorActualizacion("El tamaño de la descarga no coincide con la publicación.")
                h.update(chunk)
                output.write(chunk)
                progreso(recibidos / publicacion.size)
            output.flush()
            os.fsync(output.fileno())
        if cancelar.is_set():
            raise DescargaCancelada("Descarga cancelada.")
        if recibidos != publicacion.size or h.hexdigest() != publicacion.sha256:
            raise ErrorActualizacion("La descarga no supera la verificación de integridad. No se instalará.")
        _validar_ejecutable(ruta)
        destino = ruta.with_suffix("")
        ruta.rename(destino)
        return destino
    except Exception as exc:
        ruta.unlink(missing_ok=True)
        if isinstance(exc, ErrorActualizacion):
            raise
        raise ErrorActualizacion("No se pudo descargar la actualización. Puedes volver a intentarlo.") from exc


def preparar_instalacion(ruta: Path, publicacion: Publicacion, *, db_path: Path | None = None) -> subprocess.Popen:
    """Arranca un auxiliar independiente y espera su acuse antes de cerrar la app.

    Disponible solo en la instalación de Windows. No hay comandos interpolados
    en shell ni ejecución de archivos recibidos sin comprobar hash nuevamente.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise ErrorActualizacion("Instala la aplicación con el instalador para usar la actualización automática.")
    instalacion = Path(sys.executable).resolve().parent
    if Path(sys.executable).name != EXE_NAME or not (instalacion / "instalacion.ini").is_file():
        raise ErrorActualizacion("No se ha reconocido una instalación compatible.")
    ruta = Path(ruta)
    cache = app_paths.carpeta_actualizaciones().resolve()
    if ruta.parent.resolve() != cache or ruta.is_symlink() or ruta.resolve() != Path(os.path.abspath(ruta)) or ruta.suffix != ".exe":
        raise ErrorActualizacion("El instalador no está en la carpeta segura de actualizaciones.")
    huella = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for chunk in iter(lambda: archivo.read(256 * 1024), b""):
            huella.update(chunk)
    if ruta.stat().st_size != publicacion.size or huella.hexdigest() != publicacion.sha256:
        raise ErrorActualizacion("El instalador ha cambiado. Descarga la actualización otra vez.")
    _validar_ejecutable(ruta)
    _snapshot_previo(db_path)
    auxiliar = cache / ("aplicar-" + ruta.stem + ".ps1")
    with (app_paths.RECURSOS / "packaging" / "aplicar_actualizacion.ps1").open("rb") as origen, auxiliar.open("xb") as out:
        shutil.copyfileobj(origen, out)
    acuse = auxiliar.with_suffix(".ready")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    args = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(auxiliar),
            "-ParentId", str(os.getpid()), "-Installer", str(ruta), "-SHA256", publicacion.sha256,
            "-InstallDir", str(instalacion), "-ReadyFile", str(acuse)]
    proceso = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    limite = time.monotonic() + 8
    while time.monotonic() < limite:
        if acuse.is_file():
            acuse.unlink(missing_ok=True)
            return proceso
        if proceso.poll() is not None:
            break
        time.sleep(0.05)
    proceso.terminate()
    raise ErrorActualizacion("No se pudo preparar la instalación. La aplicación sigue abierta.")


def descartar_descarga(ruta: Path | None) -> None:
    """Borra solo la descarga aleatoria propia al sustituirla por otra búsqueda."""
    if ruta is None:
        return
    ruta = Path(ruta)
    cache = app_paths.carpeta_actualizaciones().resolve()
    if ruta.parent.resolve() != cache or not re.fullmatch(r"CAM-[a-zA-Z0-9_-]+\.exe", ruta.name) or ruta.is_symlink():
        raise ErrorActualizacion("La descarga pendiente no pertenece al actualizador.")
    ruta.unlink(missing_ok=True)
