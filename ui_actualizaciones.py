"""Aviso de actualización: banner superior no modal que refleja el estado del actualizador.

Criterios (DISENO.md): tarjeta opaca de radio 16, hairline de 1 px y sombra difusa; un solo azul (acento) y estados
en pastel de la paleta; pesos 400 / 600 / 700; botones nativos de 44 px con foco visible (ui.accion); el chip del icono
cambia de tinte con ui.ANIM (160 ms, ease-out, sin rebote) porque se muta el mismo control, no se reemplaza.

Contrato con el coordinador:
- Este control solo pinta. No hace red, disco, shell ni instalación y nunca llama a update(): refresca el coordinador.
- Constructor y métodos funcionan sin Page montada.
- Los callbacks se invocan sin argumentos (pueden ser async). El coordinador distingue por ``aviso.estado``:
  comprobar  -> Buscar actualizaciones (reposo), Reintentar (error), Comprobar de nuevo (actual)
  actualizar -> Actualizar ahora (disponible), Reiniciar e instalar (listo)
  cancelar   -> Más tarde (disponible, listo), Cancelar (descargando)
- ``version``: en reposo / comprobando / actual es la versión instalada; en el resto, la versión nueva. Si se omite,
  se conserva la última recibida. ``mensaje`` sustituye al texto secundario salvo en descargando (muestra el
  progreso) y en error (siempre un texto local genérico: nunca se enseñan detalles técnicos ni rutas).
"""
from __future__ import annotations

import inspect
import math
from typing import Callable, Optional

import flet as ft

import ui_estilo as ui

ESTADOS = ("reposo", "comprobando", "disponible", "descargando", "listo", "instalando", "error", "actual")
_VERSION_INSTALADA = ("reposo", "comprobando", "actual")
_ERROR_GENERICO = "Comprueba la conexión e inténtalo de nuevo. Tus datos no se han modificado."


def _normalizar(progreso) -> Optional[float]:
    """Progreso real en 0..1; None (o un valor no numérico) significa indeterminado."""
    if progreso is None:
        return None
    try:
        v = float(progreso)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else min(1.0, max(0.0, v))


class AvisoActualizacion(ft.Container):
    """Banner de actualización con estados: reposo, comprobando, disponible, descargando, listo, instalando,
    error y actual. Ver el docstring del módulo para el contrato de callbacks."""

    def __init__(self, p: ui.Paleta, comprobar: Optional[Callable] = None, actualizar: Optional[Callable] = None,
                 cancelar: Optional[Callable] = None):
        super().__init__(border_radius=16, animate=ui.ANIM)
        self._p = p
        self._fn = {"comprobar": comprobar, "actualizar": actualizar, "cancelar": cancelar}
        self.estado = "reposo"
        self._version = ""
        self._version_actual = ""
        self._progreso: Optional[float] = None
        self._mensaje = ""
        self._pulsado = False  # evita lanzar dos veces la misma acción mientras su callback está en curso

        self._icono = ft.Icon(ft.Icons.SYSTEM_UPDATE_ALT, size=18)
        self._anillo = ft.ProgressRing(width=18, height=18, stroke_width=2)
        self._chip = ft.Container(self._icono, width=36, height=36, border_radius=10,
                                  alignment=ft.Alignment.CENTER, animate=ui.ANIM)
        self._titulo = ui.cuerpo("", p, peso=ft.FontWeight.W_600, max_lines=2)
        self._detalle = ui.leyenda("", p)
        self._barra = ft.ProgressBar(value=None, bar_height=6, semantics_label="Progreso de la descarga")
        self._pista = ft.Container(self._barra, border_radius=ui.PILDORA, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                   visible=False)
        self._texto = ft.Row(
            [self._chip, ft.Column([self._titulo, self._detalle, self._pista], spacing=4, tight=True, expand=True)],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER, col={"xs": 12, "md": 7},
        )
        self._acciones = ft.Row(spacing=8, run_spacing=8, wrap=True, alignment=ft.MainAxisAlignment.END,
                                col={"xs": 12, "md": 5})
        # Responsive: en ventanas estrechas los botones bajan debajo del texto, alineados a la derecha
        self.content = ft.ResponsiveRow([self._texto, self._acciones], spacing=12, run_spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self._pintar()

    # ------------------------------------------------------------------------------------ API pública
    def mostrar(self, estado: str, version: str = "", progreso: Optional[float] = None, mensaje: str = "") -> None:
        if estado not in ESTADOS:
            raise ValueError(f"Estado de actualización desconocido: {estado!r}")
        self.estado = estado
        if version:
            if estado in _VERSION_INSTALADA:
                self._version_actual = str(version)
            else:
                self._version = str(version)
        self._progreso = _normalizar(progreso)
        self._mensaje = "" if estado == "error" else str(mensaje or "").strip()
        self._pulsado = False
        self._pintar()

    def cambiar_paleta(self, p: ui.Paleta) -> None:
        self._p = p
        self._pintar()

    # ------------------------------------------------------------------------------------ interno
    def _manejador(self, nombre: str):
        fn = self._fn.get(nombre)
        if fn is None:
            return None
        if inspect.iscoroutinefunction(fn):
            async def asincrono(e):
                if self._pulsado:
                    return
                self._pulsado = True
                try:
                    await fn()
                finally:
                    self._pulsado = False
            return asincrono

        def sincrono(e):
            if self._pulsado:
                return
            self._pulsado = True
            try:
                fn()
            finally:
                self._pulsado = False
        return sincrono

    def _boton(self, texto: str, tipo: str, accion: Optional[str], tooltip: str) -> ft.TextButton:
        b = ui.accion(texto, self._manejador(accion) if accion else None, self._p, tipo=tipo, tooltip=tooltip)
        if accion is None:  # instalando: visible pero sin acción posible
            b.disabled = True
            b.style.color = self._p.texto3
        return b

    def _texto_progreso(self) -> str:
        if self._progreso is None:
            return "Preparando la descarga…"
        return f"{round(self._progreso * 100)} % descargado"

    def _contenido(self) -> tuple[str, Optional[str], str, str, list]:
        """(tono, icono o None = anillo de actividad, título, detalle, botones) del estado actual."""
        e, v, va = self.estado, self._version, self._version_actual
        nueva = f"la versión {v}" if v else "la nueva versión"
        instalada = f"Versión actual {va}" if va else "Comprueba si hay una versión nueva."
        mas_tarde = ("Más tarde", "neutro", "cancelar", "Recordármelo más adelante")
        if e == "reposo":
            return ("neutro", ft.Icons.SYSTEM_UPDATE_ALT, "Actualizaciones", instalada,
                    [("Buscar actualizaciones", "neutro", "comprobar", "Comprobar si hay una versión nueva")])
        if e == "comprobando":
            return "acento", None, "Buscando actualizaciones…", instalada, []
        if e == "disponible":
            return ("acento", ft.Icons.SYSTEM_UPDATE_ALT, f"Ya puedes descargar {nueva}",
                    "Se descarga en segundo plano: puedes seguir trabajando.",
                    [mas_tarde, ("Actualizar ahora", "primario", "actualizar", f"Descargar {nueva}")])
        if e == "descargando":
            return ("acento", ft.Icons.DOWNLOAD, f"Descargando {nueva}", self._texto_progreso(),
                    [("Cancelar", "neutro", "cancelar", "Detener la descarga")])
        if e == "listo":
            return ("verde", ft.Icons.RESTART_ALT, f"{nueva[0].upper()}{nueva[1:]} está lista",
                    "La aplicación se cerrará para instalarla. Guarda antes lo que estés haciendo.",
                    [mas_tarde, ("Reiniciar e instalar", "primario", "actualizar", "Cerrar, instalar y volver a abrir")])
        if e == "instalando":
            return ("acento", None, f"Instalando {nueva}…", "No cierres la aplicación; se abrirá al terminar.",
                    [("Instalando…", "neutro", None, "La instalación está en curso")])
        if e == "error":
            return ("rojo", ft.Icons.ERROR_OUTLINE, "No se pudo completar la actualización", _ERROR_GENERICO,
                    [("Reintentar", "primario", "comprobar", "Volver a intentarlo")])
        return ("verde", ft.Icons.CHECK_CIRCLE_OUTLINE, "Estás al día",
                f"Tienes la última versión{f' ({va})' if va else ''}.",
                [("Comprobar de nuevo", "neutro", "comprobar", "Buscar otra vez una versión nueva")])

    def _pintar(self) -> None:
        p = self._p
        tono, icono, titulo, detalle, botones = self._contenido()
        color, fondo = {"neutro": (p.texto2, p.campo), "acento": (p.acento, p.acento_suave),
                        "verde": (p.verde, p.verde_suave), "rojo": (p.rojo, p.rojo_suave)}[tono]
        if self._mensaje and self.estado != "descargando":
            detalle = self._mensaje

        # Superficie opaca (sin transparencia que rebaje el contraste del texto)
        self.bgcolor = p.tarjeta
        self.border = ft.Border.all(1, p.separador)
        self.shadow = ui.sombra_flotante(p)
        self.padding = ft.Padding.symmetric(horizontal=16, vertical=10 if self.estado == "reposo" else 14)

        self._chip.bgcolor = fondo
        if icono is None:
            self._anillo.color = color
            self._chip.content = self._anillo
        else:
            self._icono.icon, self._icono.color = icono, color
            self._chip.content = self._icono
        self._titulo.value, self._titulo.color = titulo, p.texto
        self._detalle.value, self._detalle.color = detalle, p.texto2

        descargando = self.estado == "descargando"
        self._pista.visible = descargando
        self._barra.value = self._progreso if descargando else None
        self._barra.color, self._barra.bgcolor = p.acento, p.separador
        self._pista.tooltip = self._texto_progreso() if descargando else None

        self._acciones.controls = [self._boton(*b) for b in botones]
        self._acciones.visible = bool(botones)
        self._texto.col = {"xs": 12, "md": 7} if botones else 12
