"""Comprueba si una versión ya está publicada, sin modificarla ni ocultar errores de red."""
from __future__ import annotations
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def publicada(repo: str, tag: str, token: str = "") -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Repositorio no válido")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Tag no válido")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Contabilidad-Release"}
    if token:
        headers["Authorization"] = "Bearer " + token
    def leer_json(url: str):
        with urlopen(Request(url, headers=headers), timeout=30) as respuesta:
            contenido = respuesta.read(1024 * 1024 + 1)
        if len(contenido) > 1024 * 1024:
            raise ValueError("Respuesta de GitHub demasiado grande")
        return json.loads(contenido)

    try:
        datos = leer_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    except HTTPError as exc:
        if exc.code != 404:
            raise  # 403, 500 y red no significan que se pueda crear otra release.
        # El endpoint por tag solo garantiza releases publicadas. Revisar también
        # los borradores visibles con el token de escritura del workflow.
        for pagina in range(1, 101):
            filas = leer_json(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={pagina}")
            if not isinstance(filas, list):
                raise ValueError("Listado de releases no válido")
            coincidencias = [r for r in filas if r.get("tag_name") == tag]
            if len(coincidencias) > 1:
                raise ValueError("Hay varias releases con ese tag; revisar manualmente")
            if coincidencias:
                datos = coincidencias[0]
                break
            if len(filas) < 100:
                return False
        else:
            raise ValueError("No se pudo comprobar todo el listado de releases")
    if not isinstance(datos, dict):
        raise ValueError("Respuesta de release no válida")
    if datos.get("tag_name") != tag or datos.get("draft") is not False or datos.get("prerelease") is not False:
        raise ValueError("La versión existe pero no es una release estable publicada; revisar sin sobrescribir")
    nombre = f"ContabilidadAMedida-{tag[1:]}-windows-x64-Setup.exe"
    for requerido in (nombre, "SHA256SUMS.txt"):
        encontrados = [a for a in datos.get("assets", []) if a.get("name") == requerido]
        if len(encontrados) != 1:
            raise ValueError("La release existente está incompleta; revisar sin sobrescribir")
        a = encontrados[0]
        if (a.get("state") != "uploaded" or type(a.get("size")) is not int or a["size"] <= 0
                or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", a.get("digest") or "")):
            raise ValueError("Archivo publicado sin integridad verificable; no sustituirlo")
    return True


def main() -> None:
    resultado = publicada(os.environ["GH_REPO"], os.environ["TAG_PUBLICACION"], os.environ.get("GH_TOKEN", ""))
    texto = "true" if resultado else "false"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as archivo:
            archivo.write("existe=" + texto + "\n")
    print(texto)


if __name__ == "__main__":
    main()
