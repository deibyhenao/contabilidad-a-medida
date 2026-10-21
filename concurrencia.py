"""Notificaciones de workers al bucle de Flet, sin dependencias nuevas."""
from __future__ import annotations

import asyncio
from typing import Callable


def ejecutar_en_ui(page, accion: Callable[[], None]):
    """Envía al bucle Flet sin bloquear al worker ni modificar controles desde él.

    Adaptadores sin ``run_task`` (tests o integraciones sin sesión) son síncronos.
    Los errores al programar en una sesión real se propagan al controlador.
    """
    programar = getattr(page, "run_task", None)
    if not callable(programar):
        return accion()
    try:
        actual = asyncio.get_running_loop()
    except RuntimeError:
        actual = None
    sesion = getattr(page, "session", None)
    conexion = getattr(sesion, "connection", None)
    if actual is not None and actual is getattr(conexion, "loop", None):
        return accion()

    async def aplicar():
        accion()

    return programar(aplicar)
