"""Aviso de actualización compacto, en la cabecera de la barra lateral (bajo «Gestoría · a medida»).

Diseño (votación de apple-design, ui-ux-pro-max, emil-design-eng e impeccable, 4/4):
- Reposo, comprobando y al día: un botón discreto con la versión (12 px, texto3), sin fondo ni sombra; al pulsarlo
  se buscan actualizaciones. Es estado de la app, no del expediente: no ocupa una banda a todo el ancho.
- Disponible, descargando, listo, instalando y error: el mismo control crece en su sitio hasta una tarjeta de 244 px
  (radio 12, hairline de 1 px, sin sombra) con su título, detalle, progreso de 4 px y botones compactos.
- Movimiento: ``animate_size`` de 200 ms EASE_OUT_CUBIC (el tamaño explica de dónde sale el contenido) y fundido del
  contenido de 180/120 ms. Las subidas de progreso no cambian la clave, así que no provocan fundido.
- Texto siempre en texto/texto2/texto3 (el azul de acento no llega a AA como texto sobre la barra): el azul queda
  para iconos y el botón principal usa ``relleno`` con texto blanco.

Contrato con el coordinador:
- Este control solo pinta. No hace red, disco, shell ni instalación y nunca llama a update(): refresca el coordinador.
- Constructor y métodos funcionan sin Page montada.
- Los callbacks se invocan sin argumentos (pueden ser async). El coordinador distingue por ``aviso.estado``:
  comprobar  -> Buscar actualizaciones (reposo / al día), Reintentar (error)
  actualizar -> Actualizar (disponible), Reiniciar (listo)
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
COMPACTOS = ("reposo", "comprobando", "actual")   # una línea con la versión; el resto se expande en tarjeta
_VERSION_INSTALADA = ("reposo", "comprobando", "actual")
_ERROR_GENERICO = "Comprueba la conexión e inténtalo de nuevo. Tus datos no se han modificado."
ANCHO = 244                                       # ancho útil de la barra lateral (272 − 2 × 14)
ANIM_TAMANO = ft.Animation(200, ft.AnimationCurve.EASE_OUT_CUBIC)


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
    """Aviso de actualización con estados: reposo, comprobando, disponible, descargando, listo, instalando,
    error y actual. Ver el docstring del módulo para el contrato de callbacks."""

    def __init__(self, p: ui.Paleta, comprobar: Optional[Callable] = None, actualizar: Optional[Callable] = None,
                 cancelar: Optional[Callable] = None):
        super().__init__(border_radius=12, animate=ui.ANIM, animate_size=ANIM_TAMANO,
                         clip_behavior=ft.ClipBehavior.ANTI_ALIAS)
        self._p = p
        self._fn = {"comprobar": comprobar, "actualizar": actualizar, "cancelar": cancelar}
        self.estado = "reposo"
        self._version = ""
        self._version_actual = ""
        self._progreso: Optional[float] = None
        self._mensaje = ""
        self._pulsado = False  # evita lanzar dos veces la misma acción mientras su callback está en curso
        # Controles que el coordinador y los tests consultan; se recrean en cada pintado
        self._titulo = ft.Text("")
        self._detalle = ft.Text("")
        self._barra = ft.ProgressBar(value=None, bar_height=4, semantics_label="Progreso de la descarga")
        self._acciones = ft.Row()
        self._cambio = ft.AnimatedSwitcher(
            content=ft.Container(), duration=180, reverse_duration=120,
            transition=ft.AnimatedSwitcherTransition.FADE,
            switch_in_curve=ft.AnimationCurve.EASE_OUT_CUBIC, switch_out_curve=ft.AnimationCurve.EASE_OUT_CUBIC)
        self.content = self._cambio
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

    @property
    def compacto(self) -> bool:
        return self.estado in COMPACTOS

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
        """Botón compacto (32 px, radio 8) con foco visible; el principal en ``relleno`` con texto blanco."""
        p = self._p
        primario = tipo == "primario"
        color = "#FFFFFF" if primario else p.texto2
        b = ft.TextButton(
            texto, on_click=self._manejador(accion) if accion else None, height=32, tooltip=tooltip,
            style=ft.ButtonStyle(
                color=color, bgcolor=p.relleno if primario else "#00000000",
                padding=ft.Padding.symmetric(horizontal=12, vertical=0),
                overlay_color={ft.ControlState.HOVERED: p.relleno_hover if primario else p.hover,
                               ft.ControlState.FOCUSED: p.relleno_hover if primario else p.acento_suave},
                side={ft.ControlState.FOCUSED: ft.BorderSide(2, p.texto if primario else p.acento),
                      ft.ControlState.DEFAULT: ft.BorderSide(0, "transparent")},
                shape=ft.RoundedRectangleBorder(radius=8),
                text_style=ft.TextStyle(size=13, font_family=ui.FUENTE, weight=ft.FontWeight.W_600)))
        if accion is None:  # instalando: visible pero sin acción posible
            b.disabled = True
            b.style.color = p.texto3
        return b

    def _texto_progreso(self) -> str:
        if self._progreso is None:
            return "Preparando la descarga…"
        return f"{round(self._progreso * 100)} % descargado"

    def _contenido(self) -> tuple[str, Optional[str], str, str, list]:
        """(tono, icono o None = anillo de actividad, título, detalle, botones) del estado actual."""
        e, v, va = self.estado, self._version, self._version_actual
        nueva = f"la versión {v}" if v else "la nueva versión"
        version = f"Versión {va}" if va else "Buscar actualizaciones"
        mas_tarde = ("Más tarde", "neutro", "cancelar", "Recordármelo más adelante")
        if e == "reposo":
            return "neutro", ft.Icons.SYSTEM_UPDATE_ALT, version, "Pulsa para buscar actualizaciones", []
        if e == "comprobando":
            return "acento", None, "Buscando…", version, []
        if e == "actual":
            return "verde", ft.Icons.CHECK_CIRCLE, f"Al día{f' · {va}' if va else ''}", \
                f"Tienes la última versión{f' ({va})' if va else ''}.", []
        if e == "disponible":
            return ("acento", ft.Icons.SYSTEM_UPDATE_ALT, f"Versión {v} disponible" if v else "Nueva versión disponible",
                    "Se descarga en segundo plano: puedes seguir trabajando.",
                    [mas_tarde, ("Actualizar", "primario", "actualizar", f"Descargar {nueva}")])
        if e == "descargando":
            return ("acento", ft.Icons.DOWNLOAD, f"Descargando {v}" if v else "Descargando", self._texto_progreso(),
                    [("Cancelar", "neutro", "cancelar", "Detener la descarga")])
        if e == "listo":
            return ("verde", ft.Icons.RESTART_ALT, "Lista para instalar",
                    "La aplicación se cerrará para instalarla. Guarda antes lo que estés haciendo.",
                    [mas_tarde, ("Reiniciar", "primario", "actualizar", "Cerrar, instalar y volver a abrir")])
        if e == "instalando":
            return ("acento", None, f"Instalando {nueva}…", "No cierres la aplicación; se abrirá al terminar.",
                    [("Instalando…", "neutro", None, "La instalación está en curso")])
        return ("rojo", ft.Icons.ERROR_OUTLINE, "No se pudo actualizar", _ERROR_GENERICO,
                [("Reintentar", "primario", "comprobar", "Volver a intentarlo")])

    def _pintar(self) -> None:
        p = self._p
        tono, icono, titulo, detalle, botones = self._contenido()
        color, fondo = {"neutro": (p.texto3, p.campo), "acento": (p.acento, p.acento_suave),
                        "verde": (p.verde, p.verde_suave), "rojo": (p.rojo, p.rojo_suave)}[tono]
        if self._mensaje and self.estado != "descargando":
            detalle = self._mensaje
        descargando = self.estado == "descargando"
        self._barra = ft.ProgressBar(value=self._progreso if descargando else None, bar_height=4, color=p.acento,
                                     bgcolor=p.separador, semantics_label="Progreso de la descarga")

        if self.compacto:
            # Una sola línea, sin superficie propia: se apoya en la barra lateral
            self.bgcolor, self.border, self.padding, self.width = None, None, None, None
            simbolo = (ft.ProgressRing(width=12, height=12, stroke_width=1.5, color=p.acento) if icono is None
                       else ft.Icon(icono, size=14, color=color))
            self._titulo = ft.Text(titulo, size=12, weight=ft.FontWeight.W_500, font_family=ui.FUENTE,
                                   color=p.texto2 if self.estado == "actual" else p.texto3, no_wrap=True)
            self._detalle = ft.Text(detalle, visible=False)
            self._acciones = ft.Row(visible=False)
            pulsar = None if self.estado == "comprobando" else self._manejador("comprobar")
            cuerpo = ft.TextButton(
                content=ft.Row([simbolo, self._titulo], spacing=6, tight=True), on_click=pulsar, height=28,
                tooltip="Buscar actualizaciones" if pulsar else "Buscando actualizaciones…",
                style=ft.ButtonStyle(
                    padding=ft.Padding.symmetric(horizontal=8, vertical=0),
                    overlay_color={ft.ControlState.HOVERED: p.hover, ft.ControlState.FOCUSED: p.acento_suave},
                    side={ft.ControlState.FOCUSED: ft.BorderSide(2, p.acento),
                          ft.ControlState.DEFAULT: ft.BorderSide(0, "transparent")},
                    shape=ft.RoundedRectangleBorder(radius=ui.PILDORA)))
        else:
            # Tarjeta opaca de 244 px que crece en el mismo sitio
            self.bgcolor, self.border = p.tarjeta, ft.Border.all(1, p.separador)
            self.padding, self.width = ft.Padding.symmetric(horizontal=12, vertical=10), ANCHO
            simbolo = (ft.ProgressRing(width=14, height=14, stroke_width=1.8, color=color) if icono is None
                       else ft.Icon(icono, size=16, color=color))
            chip = ft.Container(simbolo, width=28, height=28, border_radius=8, bgcolor=fondo,
                                alignment=ft.Alignment.CENTER)
            self._titulo = ft.Text(titulo, size=13, weight=ft.FontWeight.W_600, color=p.texto, font_family=ui.FUENTE,
                                   max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
            self._detalle = ft.Text(detalle, size=12, color=p.texto2, font_family=ui.FUENTE, max_lines=3,
                                    overflow=ft.TextOverflow.ELLIPSIS)
            pista = ft.Container(self._barra, border_radius=ui.PILDORA, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                 visible=descargando, tooltip=self._texto_progreso() if descargando else None)
            self._acciones = ft.Row([self._boton(*b) for b in botones], spacing=6,
                                    alignment=ft.MainAxisAlignment.END, visible=bool(botones))
            cuerpo = ft.Column([
                ft.Row([chip, ft.Column([self._titulo, self._detalle], spacing=2, tight=True, expand=True)],
                       spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
                pista, self._acciones,
            ], spacing=8, tight=True)
        # La clave solo cambia con el estado: el progreso actualiza sin fundido
        self._cambio.content = ft.Container(cuerpo, key=f"{self.estado}-{p.oscuro}")
