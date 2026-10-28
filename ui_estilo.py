"""Sistema visual estilo Apple para la app Flet: paleta, tipografía y componentes con micro-interacciones.

Fuentes de criterio (skills apple-design, ui-ux-pro-max, emil-design-eng y DESIGN.md de Apple de awesome-design-md):
- Un solo color de acción (Action Blue #0071E3; #2997FF sobre oscuro). Nada más es azul.
- Tarjetas flotantes: borde fino de 1 px, radio 12 y sombra muy difusa (< 6 % de opacidad) que se eleva al
  pasar el ratón (petición del usuario + guía minimalista). Estados en pastel: menta, ámbar, rojo pastel.
- Botón principal en píldora; pulsar encoge a 0.97 (100 ms) y soltar vuelve (160 ms, ease-out fuerte).
- Hover solo en lo que se puede pulsar: tinte de fondo en 140 ms. Lo que no es pulsable no reacciona.
- Pesos 400 / 600 / 700 (sin 500). Títulos con tracking negativo proporcional al tamaño.
- Animaciones cortas (<= 200 ms en interacción) y sin rebote en una herramienta profesional.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable, Optional

import flet as ft

FUENTE = "Segoe UI Variable Text"      # fuente del sistema en Windows 11 (equivalente a SF Pro Text)
FUENTE_TITULO = "Segoe UI Variable Display"


@dataclass(frozen=True)
class Paleta:
    oscuro: bool
    fondo: str            # lienzo (Parchment)
    barra: str            # barra lateral
    tarjeta: str
    campo: str            # relleno de campos y fondos neutros
    hover: str            # tinte de fila al pasar el ratón
    texto: str
    texto2: str
    texto3: str
    separador: str        # hairline
    borde_activo: str     # hairline al pasar el ratón por una tarjeta pulsable
    acento: str
    acento_hover: str
    acento_suave: str
    acento_suave_hover: str
    relleno: str          # fondo del botón principal (blanco encima >= 4,5:1 en claro y oscuro)
    relleno_hover: str
    verde: str
    verde_suave: str
    naranja: str
    naranja_suave: str
    rojo: str
    rojo_suave: str
    sombra: str
    segmento: str = "#FFFFFF"   # pastilla del segmento elegido (control segmentado)


CLARO = Paleta(False, fondo="#F5F5F7", barra="#EDEDF0", tarjeta="#FFFFFF", campo="#F2F2F5", hover="#F0F0F3",
               texto="#1D1D1F", texto2="#545459", texto3="#66666B", separador="#E3E3E8", borde_activo="#B9D3F2",
               acento="#0071E3", acento_hover="#0077ED", acento_suave="#E8F1FC", acento_suave_hover="#D9E8FA",
               relleno="#0071E3", relleno_hover="#0066CC",
               # Estados en pastel desaturado (menta / ámbar / rojo pastel) con texto oscuro legible (AA)
               verde="#2F6B3A", verde_suave="#EAF5EC", naranja="#8A5A00", naranja_suave="#FBF3DB",
               rojo="#9F2F2D", rojo_suave="#FDEBEC", sombra="#0D000000")
OSCURO = Paleta(True, fondo="#161617", barra="#1E1E20", tarjeta="#232325", campo="#2E2E31", hover="#2C2C2F",
                texto="#F5F5F7", texto2="#AEAEB2", texto3="#9C9CA1", separador="#333336", borde_activo="#1F4F86",
                acento="#2997FF", acento_hover="#47A6FF", acento_suave="#10294A", acento_suave_hover="#16365F",
                relleno="#0071E3", relleno_hover="#0066CC",
                verde="#30D158", verde_suave="#12341E", naranja="#FF9F0A", naranja_suave="#3A2708",
                rojo="#FF6961", rojo_suave="#45191A", sombra="#40000000", segmento="#636366")

# Curvas: ease-out fuerte para responder (empieza rápido); nada de ease-in en interfaz
ANIM = ft.Animation(160, ft.AnimationCurve.EASE_OUT_QUART)          # cambios de superficie
ANIM_HOVER = ft.Animation(140, ft.AnimationCurve.EASE_OUT_CUBIC)    # tinte al pasar el ratón
ANIM_PULSAR = ft.Animation(110, ft.AnimationCurve.EASE_OUT_QUART)   # escala al pulsar/soltar
PILDORA = 999


def eur(x) -> str:
    x = float(x or 0)
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x) -> str:
    x = float(x or 0) * 100
    return f"{x:.2f}".rstrip("0").rstrip(",.").replace(".", ",") + " %"


# ------------------------------------------------------------------------------------ tipografía
def titulo(texto: str, p: Paleta, size: int = 28) -> ft.Text:
    return ft.Text(texto, size=size, weight=ft.FontWeight.W_700, color=p.texto, font_family=FUENTE_TITULO,
                   style=ft.TextStyle(letter_spacing=-0.022 * size, height=1.1))


def seccion(texto: str, p: Paleta, size: int = 17) -> ft.Text:
    return ft.Text(texto, size=size, weight=ft.FontWeight.W_600, color=p.texto, font_family=FUENTE_TITULO,
                   style=ft.TextStyle(letter_spacing=-0.015 * size))


def cuerpo(texto: str, p: Paleta, color: Optional[str] = None, size: int = 14, peso=ft.FontWeight.W_400,
           max_lines: Optional[int] = None) -> ft.Text:
    if peso == ft.FontWeight.W_500:  # la escala de Apple no usa 500
        peso = ft.FontWeight.W_600
    return ft.Text(texto, size=size, color=color or p.texto, weight=peso, font_family=FUENTE,
                   max_lines=max_lines, overflow=ft.TextOverflow.ELLIPSIS if max_lines else None,
                   style=ft.TextStyle(height=1.4))


def leyenda(texto: str, p: Paleta, color: Optional[str] = None, size: int = 12) -> ft.Text:
    return ft.Text(texto, size=size, color=color or p.texto2, font_family=FUENTE,
                   style=ft.TextStyle(letter_spacing=0.1, height=1.35))


# ------------------------------------------------------------------------------------ interacción
def _llamar(fn: Optional[Callable], antes: Callable[[], None]):
    """Envuelve un manejador (síncrono o async) ejecutando ``antes`` primero (restaurar la escala)."""
    if fn is None:
        return None
    if inspect.iscoroutinefunction(fn):
        async def asincrono(e):
            antes()
            await fn(e)
        return asincrono

    def sincrono(e):
        antes()
        return fn(e)
    return sincrono


def pulsable(c: ft.Container, on_click: Optional[Callable], reposo: dict, encima: dict,
             escala: float = 0.97) -> ft.Container:
    """Da a un Container el comportamiento de un control de Apple:
    - al pasar el ratón cambia a ``encima`` (p.ej. bgcolor/border) con un fundido corto;
    - al pulsar encoge a ``escala`` al instante (respuesta en el pointer-down, no al soltar);
    - al soltar o salir vuelve a ``reposo``. La animación siempre parte del valor actual (interrumpible).
    """
    c.animate = ANIM_HOVER
    c.animate_scale = ANIM_PULSAR
    c.scale = 1
    for k, v in reposo.items():
        setattr(c, k, v)

    def aplicar(estado: dict, s: float):
        for k, v in estado.items():
            setattr(c, k, v)
        c.scale = s
        try:
            c.update()
        except Exception:  # aún no está en la página
            pass

    def on_hover(e):
        aplicar(encima if e.data in (True, "true") else reposo, 1)

    c.on_hover = on_hover
    if on_click is not None:
        c.on_tap_down = lambda e: aplicar(encima, escala)
        c.on_long_press = _llamar(c.on_long_press or (lambda e: None), lambda: aplicar(encima, 1))
        c.on_click = _llamar(on_click, lambda: aplicar(encima, 1))
        c.ink = False
    return c


# ------------------------------------------------------------------------------------ superficies
def sombra_flotante(p: Paleta, elevada: bool = False) -> list:
    """Sombra muy difusa y de baja opacidad: la tarjeta «flota» sin parecer pesada. Al pasar el ratón se eleva."""
    if elevada:
        return [ft.BoxShadow(blur_radius=4, color=p.sombra, offset=ft.Offset(0, 1)),
                ft.BoxShadow(blur_radius=28, color=p.sombra, offset=ft.Offset(0, 10))]
    return [ft.BoxShadow(blur_radius=2, color=p.sombra, offset=ft.Offset(0, 1)),
            ft.BoxShadow(blur_radius=18, color=p.sombra, offset=ft.Offset(0, 6))]


def tarjeta(content: ft.Control, p: Paleta, padding: int = 20, on_click: Optional[Callable] = None,
            bgcolor: Optional[str] = None, expand=None) -> ft.Container:
    """Tarjeta flotante: blanca, hairline de 1 px, radio 12 y sombra difusa. Si es pulsable, se eleva y el borde
    se ilumina al pasar el ratón; al pulsar encoge a 0.99."""
    c = ft.Container(content=content, padding=padding, border_radius=12, expand=expand)
    reposo = {"bgcolor": bgcolor or p.tarjeta, "border": ft.Border.all(1, p.separador), "shadow": sombra_flotante(p)}
    if on_click is None:
        for k, v in reposo.items():
            setattr(c, k, v)
        c.animate = ANIM
        return c
    return pulsable(c, on_click, reposo, {"bgcolor": bgcolor or p.tarjeta, "border": ft.Border.all(1, p.borde_activo),
                                          "shadow": sombra_flotante(p, elevada=True)}, escala=0.99)


def separador(p: Paleta) -> ft.Container:
    return ft.Container(height=1, bgcolor=p.separador)


def pastilla(texto: str, color: str, fondo: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(texto, size=12, weight=ft.FontWeight.W_600, color=color, font_family=FUENTE),
        bgcolor=fondo, border_radius=PILDORA, padding=ft.Padding.symmetric(horizontal=10, vertical=3),
    )


def colores_estado(estado: Optional[str], p: Paleta) -> tuple[str, str]:
    e = (estado or "").lower()
    if e in ("presentado", "vigente", "activo", "disponible", "cobrado", "sí"):
        return p.verde, p.verde_suave
    if e in ("pendiente", "parcial", "prorrogado", "pendiente actualizar", "pendiente firma"):
        return p.naranja, p.naranja_suave
    if e in ("falta documentación", "falta", "impagado", "caducado", "vencida", "grave"):
        return p.rojo, p.rojo_suave
    if e in ("en preparación", "revisar", "en revisión", "aviso"):
        return p.acento, p.acento_suave
    return p.texto2, p.campo


def pastilla_estado(estado: Optional[str], p: Paleta) -> ft.Container:
    """Estado con su color pastel. «Presentado» (también un antiguo «PRESENTADO[OK]») se muestra con el texto
    canónico y un icono de confirmación: el OK no se guarda ni depende solo del color."""
    if estado and "".join(ch for ch in str(estado).lower() if ch.isalpha()) in ("presentado", "presentadook"):
        estado = "Presentado"
    color, fondo = colores_estado(estado, p)
    c = pastilla(estado or "—", color, fondo)
    if estado == "Presentado":
        c.content = ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE, size=13, color=color, semantics_label="Confirmado"),
                            c.content], spacing=4, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        c.tooltip = "Presentado: importe fijado con el justificante"
    return c


def kpi(titulo_kpi: str, valor: str, p: Paleta, icono=None, color: Optional[str] = None,
        detalle: Optional[str] = None, on_click: Optional[Callable] = None) -> ft.Container:
    filas: list[ft.Control] = [
        ft.Row([ft.Icon(icono, size=16, color=color or p.texto2)] if icono else [], spacing=6),
        leyenda(titulo_kpi, p),
        ft.Text(valor, size=24, weight=ft.FontWeight.W_700, color=color or p.texto, font_family=FUENTE_TITULO,
                style=ft.TextStyle(letter_spacing=-0.5)),
    ]
    filas.append(leyenda(detalle or "\u00a0", p, p.texto3))  # misma altura en toda la fila de KPIs
    return tarjeta(ft.Column(filas, spacing=4, tight=True), p, padding=16, expand=True, on_click=on_click)


# ------------------------------------------------------------------------------------ controles
def _bordes(p: Paleta) -> dict:
    """Bordes de campo con la API de Flet >= 1.0 (border_color/border_radius desaparecen en 1.3)."""
    return {
        ft.ControlState.DEFAULT: ft.OutlineInputBorder(border_radius=12, side=ft.BorderSide(0, "transparent")),
        ft.ControlState.FOCUSED: ft.OutlineInputBorder(border_radius=12, side=ft.BorderSide(2, p.acento)),
    }


def boton(texto: str, on_click, p: Paleta, icono=None, tipo: str = "primario", tooltip: Optional[str] = None):
    """Botón propio (no Material) para controlar hover y pulsación al estilo Apple.

    tipo: primario (píldora azul) | secundario (píldora tintada) | texto (enlace azul) | peligro (texto rojo).
    """
    colores = {
        "primario": ("#FFFFFF", p.relleno, p.relleno_hover),  # hover más oscuro: mantiene el contraste
        "secundario": (p.acento, p.acento_suave, p.acento_suave_hover),
        "texto": (p.acento, "#00000000", p.hover),
        "peligro": (p.rojo, "#00000000", p.rojo_suave),
    }[tipo]
    color, fondo, fondo_encima = colores
    peso = ft.FontWeight.W_600 if tipo in ("primario", "secundario") else ft.FontWeight.W_400
    contenido = [ft.Icon(icono, size=17, color=color)] if icono else []
    contenido.append(ft.Text(texto, size=14, weight=peso, color=color, font_family=FUENTE, no_wrap=True))
    c = ft.Container(
        content=ft.Row(contenido, spacing=6, tight=True, alignment=ft.MainAxisAlignment.CENTER),
        padding=ft.Padding.symmetric(horizontal=18 if tipo in ("primario", "secundario") else 12, vertical=9),
        border_radius=PILDORA if tipo in ("primario", "secundario") else 8, tooltip=tooltip,
    )
    return pulsable(c, on_click, {"bgcolor": fondo}, {"bgcolor": fondo_encima})


def boton_icono(icono, on_click, p: Paleta, tooltip: str, color: Optional[str] = None,
                peligro: bool = False) -> ft.Container:
    """Icono pulsable de 34 px (área de clic cómoda), círculo tintado al pasar el ratón.
    ``peligro``: en reposo es neutro y solo al pasar el ratón se vuelve rojo sobre rojo suave."""
    icono_c = ft.Icon(icono, size=19, color=color or p.acento, animate_opacity=ANIM_HOVER)
    c = ft.Container(icono_c, width=34, height=34, border_radius=17, alignment=ft.Alignment.CENTER, tooltip=tooltip)
    c = pulsable(c, on_click, {"bgcolor": "#00000000"}, {"bgcolor": p.rojo_suave if peligro else p.hover},
                 escala=0.95)
    if peligro:
        reposo_color, base = icono_c.color, c.on_hover

        def on_hover(e):
            icono_c.color = p.rojo if e.data in (True, "true") else reposo_color
            base(e)
        c.on_hover = on_hover
    return c


def accion(texto: str, on_click, p: Paleta, icono=None, tipo: str = "neutro",
           tooltip: Optional[str] = None) -> ft.TextButton:
    """Acción de expediente nativa: teclado, foco visible y área mínima de 44 px."""
    color = p.rojo if tipo == "peligro" else "#FFFFFF" if tipo == "primario" else p.texto2
    fondo = p.relleno if tipo == "primario" else "#00000000"
    return ft.TextButton(texto, icon=icono, on_click=on_click, height=44, tooltip=tooltip or texto,
                         style=ft.ButtonStyle(
                             color=color, icon_color=color, bgcolor=fondo,
                             overlay_color={ft.ControlState.HOVERED: p.relleno_hover if tipo == "primario" else p.hover,
                                            ft.ControlState.FOCUSED: p.relleno_hover if tipo == "primario" else p.acento_suave},
                             side={ft.ControlState.FOCUSED: ft.BorderSide(2, p.texto if tipo == "primario" else p.acento),
                                   ft.ControlState.DEFAULT: ft.BorderSide(0, "transparent")},
                             shape=ft.RoundedRectangleBorder(radius=12),
                             text_style=ft.TextStyle(size=13, font_family=FUENTE, weight=ft.FontWeight.W_600)))


def accion_icono(icono, on_click, p: Paleta, tooltip: str, color: Optional[str] = None,
                 peligro: bool = False) -> ft.IconButton:
    """Versión nativa de ``boton_icono`` (mismo icono y círculo): recibe el foco con Tab, se activa con
    teclado, muestra un borde de foco y su área es de 44 px. ``tooltip`` sirve también de nombre accesible."""
    reposo = color or p.acento
    activo = p.rojo if peligro else reposo
    return ft.IconButton(icono, on_click=on_click, tooltip=tooltip, icon_size=19, width=44, height=44,
                         style=ft.ButtonStyle(
                             icon_color={ft.ControlState.HOVERED: activo, ft.ControlState.FOCUSED: activo,
                                         ft.ControlState.DEFAULT: reposo},
                             overlay_color={ft.ControlState.HOVERED: p.rojo_suave if peligro else p.hover,
                                            ft.ControlState.FOCUSED: p.rojo_suave if peligro else p.acento_suave},
                             side={ft.ControlState.FOCUSED: ft.BorderSide(2, activo),
                                   ft.ControlState.DEFAULT: ft.BorderSide(0, "transparent")},
                             shape=ft.CircleBorder()))


def campo(etiqueta: str, p: Paleta, valor: str = "", ayuda: Optional[str] = None, solo_lectura: bool = False,
          password: bool = False, on_change=None, on_submit=None, prefijo=None, hint: Optional[str] = None,
          expand=None, width=None) -> ft.TextField:
    return ft.TextField(
        label=etiqueta, value=valor, helper=ayuda, read_only=solo_lectura, password=password,
        can_reveal_password=password, on_change=on_change, on_submit=on_submit, prefix_icon=prefijo, hint_text=hint,
        filled=True, fill_color=p.campo if not solo_lectura else p.fondo, border=_bordes(p),
        text_size=14, dense=False, expand=expand, width=width, color=p.texto if not solo_lectura else p.texto2,
        label_style=ft.TextStyle(color=p.texto2, size=13), helper_style=ft.TextStyle(color=p.texto3, size=12),
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=12), cursor_color=p.acento,
    )


def desplegable(etiqueta: str, opciones, p: Paleta, valor: Optional[str] = None, on_select=None,
                expand=None, width=None) -> ft.Dropdown:
    return ft.Dropdown(
        label=etiqueta, value=valor, on_select=on_select, expand=expand, width=width, filled=True,
        fill_color=p.campo, border=_bordes(p), text_size=14,
        dense=True, label_style=ft.TextStyle(color=p.texto2, size=13),
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        menu_style=ft.MenuStyle(bgcolor=p.tarjeta, shape=ft.RoundedRectangleBorder(radius=12),
                                padding=ft.Padding.all(6), elevation=6),
        options=[ft.DropdownOption(key=k, text=t) for k, t in opciones],
    )


def interruptor(valor: bool, on_change, p: Paleta, etiqueta: Optional[str] = None) -> ft.CupertinoSwitch:
    return ft.CupertinoSwitch(value=valor, on_change=on_change, label=etiqueta, active_track_color=p.verde)


def fila(icono, color: str, titulo_fila: str, detalle: Optional[str], p: Paleta, derecha: Optional[list] = None,
         on_click=None, fondo_icono: Optional[str] = None) -> ft.Container:
    c = ft.Container(
        content=ft.Row([
            ft.Container(ft.Icon(icono, size=18, color=color), width=34, height=34, border_radius=8,
                         bgcolor=fondo_icono or p.campo, alignment=ft.Alignment.CENTER),
            ft.Column([cuerpo(titulo_fila, p, peso=ft.FontWeight.W_600, max_lines=1),
                       *([leyenda(detalle, p)] if detalle else [])], spacing=1, expand=True, tight=True),
            *(derecha or []),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.Padding.symmetric(horizontal=12, vertical=10), border_radius=12,
    )
    if on_click is None:  # sin falsa promesa: una fila que no se pulsa no reacciona al ratón
        return c
    return pulsable(c, on_click, {"bgcolor": "#00000000"}, {"bgcolor": p.hover}, escala=0.99)


def vacio(icono, titulo_vacio: str, texto: str, p: Paleta, accion: Optional[ft.Control] = None) -> ft.Container:
    return ft.Container(
        content=ft.Column([
            ft.Icon(icono, size=44, color=p.texto3),
            seccion(titulo_vacio, p),
            cuerpo(texto, p, p.texto2),
            *([accion] if accion else []),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8, tight=True),
        padding=40, alignment=ft.Alignment.CENTER,
    )


def avatar(nombre: str, apellidos: str, p: Paleta, activo: bool = False) -> ft.Container:
    iniciales = ((nombre[:1] or "") + (apellidos[:1] or "")).upper() or "?"
    return ft.Container(
        ft.Text(iniciales, size=12, weight=ft.FontWeight.W_700, color="#FFFFFF" if activo else p.acento),
        width=30, height=30, border_radius=15, alignment=ft.Alignment.CENTER,
        bgcolor="#33FFFFFF" if activo else p.acento_suave,
    )


def icono_app(lado: int = 120, sombra: bool = True) -> ft.Container:
    """Icono de la app (assets/icono.png). Lleva la única sombra del sistema, como la imagen de producto de Apple."""
    return ft.Container(
        ft.Image(src="icono.png", width=lado, height=lado, fit=ft.BoxFit.CONTAIN),
        width=lado, height=lado, border_radius=int(lado * 0.2237),
        shadow=[ft.BoxShadow(blur_radius=30, color="#38000000", offset=ft.Offset(3, 5))] if sombra else None,
    )
