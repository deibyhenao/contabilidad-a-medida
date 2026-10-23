"""Revisión de documentos antes de procesarlos: lista de adjuntos, destino y «Comentario para la IA».

Diseño (propuesta ganadora de VEREDICTO.md, «Hoja de revisión agrupada»):
- Destino primero (cliente, NIF y ejercicio) en una banda neutra de radio 12: es lo que más importa no equivocar.
- Comentario para la IA justo debajo, a todo el ancho y con 3 líneas visibles: se aplica a todo el lote y no queda
  escondido al final de una lista larga. Etiqueta visible + ejemplo en el hint + contador nativo (máx. 4000).
- Lista agrupada (radio 14, hairline, sin sombra) con un chip de formato por fila: PDF rojo, Excel verde, Word azul,
  CSV neutro. El color nunca va solo: el chip lleva icono y la leyenda dice el formato y el tamaño.
- Quitar un adjunto: papelera neutra con tooltip que solo se tiñe de rojo al pasar el ratón o con el foco.
- Un único scroll (el del diálogo). Procesar indica el número de documentos y se desactiva con la lista vacía.

Contrato con main.py:
- Este módulo no hace disco (salvo ``os.stat`` para conocer el tamaño de rutas sin él), ni red, ni importa nada.
- ``al_procesar(rutas, comentario)`` devuelve None si acepta el lote (main cierra el diálogo y lanza el trabajo) o un
  texto de error para mostrarlo en el diálogo sin cerrarlo. ``destino_vigente()`` se consulta antes de aceptar.
- ``al_cancelar()`` cierra el diálogo; cancelar o descartar no procesa nada.
- ``refrescar()`` repinta la página tras retirar un adjunto (en tests puede omitirse).

Arrastrar y soltar desde el Explorador: ``ft.DragTarget`` solo acepta ``ft.Draggable`` de la propia app, no archivos del
sistema operativo, y la dependencia instalada no expone un evento de soltar archivos externos. Por eso no se ofrece una
zona de arrastre: se usa el selector de archivos y, para estos formatos, la bandeja ``_ARRASTRA_AQUI``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import flet as ft

import ui_estilo as ui

EXTENSIONES = ("pdf", "xlsx", "xls", "docx", "csv")
MAX_ARCHIVOS = 30
MAX_COMENTARIO = 4000
HINT_COMENTARIO = ("Ej.: «Extrae los ingresos por meses de 2025» o «Ignora la Hoja2; los gastos están en la Hoja1»")

# formato -> (icono, nombre legible); el color sale de la paleta en ``colores_formato``
_FORMATOS = {
    "pdf": ("PDF", ft.Icons.PICTURE_AS_PDF_OUTLINED),
    "xlsx": ("Excel", ft.Icons.TABLE_CHART_OUTLINED),
    "xls": ("Excel", ft.Icons.TABLE_CHART_OUTLINED),
    "docx": ("Word", ft.Icons.DESCRIPTION_OUTLINED),
    "csv": ("CSV", ft.Icons.TEXT_SNIPPET_OUTLINED),
}


# ------------------------------------------------------------------------------------ datos (sin Flet)
@dataclass(frozen=True)
class Adjunto:
    ruta: Path
    nombre: str
    tamano: Optional[int]   # bytes; None si no se pudo conocer

    @property
    def extension(self) -> str:
        return self.ruta.suffix.lower().lstrip(".")

    @property
    def formato(self) -> str:
        return formato_de(self.ruta)


@dataclass
class Lote:
    adjuntos: list[Adjunto] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Destino:
    nombre: str
    nif: str
    ejercicio: int


def formato_de(ruta) -> str:
    """'PDF', 'Excel', 'Word', 'CSV' o la extensión en mayúsculas si no es un formato admitido."""
    ext = Path(str(ruta)).suffix.lower().lstrip(".")
    return _FORMATOS[ext][0] if ext in _FORMATOS else (ext.upper() or "Archivo")


def tamano_legible(n: Optional[int]) -> str:
    """Bytes -> «850 KB», «1,2 MB» (coma decimal, como el resto de la app)."""
    if n is None or n < 0:
        return "Tamaño desconocido"
    if n < 1024:
        return f"{n} B"
    for unidad in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unidad == "GB":
            texto = f"{n:.0f}" if n >= 100 or unidad == "KB" else f"{n:.1f}"
            return f"{texto.replace('.', ',')} {unidad}"
    return f"{n} B"  # inalcanzable


def normalizar_comentario(texto: Optional[str]) -> str:
    """Sin espacios sobrantes y como mucho ``MAX_COMENTARIO`` caracteres."""
    return str(texto or "").strip()[:MAX_COMENTARIO]


def _clave(ruta: Path) -> str:
    return os.path.normcase(os.path.abspath(str(ruta)))


def _datos(archivo: Any) -> tuple[Optional[str], str, Optional[int]]:
    """(ruta, nombre, tamaño) de un ``ft.FilePickerFile``, un ``Path`` o un ``str``."""
    if isinstance(archivo, (str, os.PathLike)):
        ruta = os.fspath(archivo) or None
        return ruta, Path(ruta).name if ruta else "", None
    ruta = getattr(archivo, "path", None) or None
    nombre = getattr(archivo, "name", None) or (Path(ruta).name if ruta else "")
    tamano = getattr(archivo, "size", None)
    return ruta, nombre, tamano if isinstance(tamano, int) else None


def preparar_lote(archivos: Optional[Iterable[Any]], existentes: Iterable[Adjunto] = ()) -> Lote:
    """Filtra la selección: descarta lo que no tiene ruta local, formatos no admitidos y duplicados (también
    respecto a ``existentes``) y corta en ``MAX_ARCHIVOS``. Cada descarte deja un aviso legible."""
    lote = Lote()
    vistos = {_clave(a.ruta) for a in existentes}
    sin_ruta, no_admitidos, duplicados, sobrantes = [], [], 0, 0
    for archivo in archivos or []:
        ruta, nombre, tamano = _datos(archivo)
        if not ruta:
            sin_ruta.append(nombre or "sin nombre")
            continue
        p = Path(ruta)
        if p.suffix.lower().lstrip(".") not in EXTENSIONES:
            no_admitidos.append(p.name)
            continue
        clave = _clave(p)
        if clave in vistos:
            duplicados += 1
            continue
        if len(vistos) >= MAX_ARCHIVOS:
            sobrantes += 1
            continue
        if tamano is None:
            try:
                tamano = p.stat().st_size
            except OSError:
                tamano = None
        vistos.add(clave)
        lote.adjuntos.append(Adjunto(p, nombre or p.name, tamano))
    if sin_ruta:
        lote.avisos.append(f"Sin ruta local, no se pueden leer: {', '.join(sin_ruta[:3])}"
                           + (f" y {len(sin_ruta) - 3} más" if len(sin_ruta) > 3 else "") + ".")
    if no_admitidos:
        lote.avisos.append(f"Formato no admitido (solo PDF, Excel, Word o CSV): {', '.join(no_admitidos[:3])}"
                           + (f" y {len(no_admitidos) - 3} más" if len(no_admitidos) > 3 else "") + ".")
    if duplicados:
        lote.avisos.append(f"{duplicados} {'archivo repetido omitido' if duplicados == 1 else 'archivos repetidos omitidos'}.")
    if sobrantes:
        lote.avisos.append(f"Máximo {MAX_ARCHIVOS} archivos por lote: {sobrantes} "
                           f"{'queda' if sobrantes == 1 else 'quedan'} fuera. Elígelos en otro lote.")
    return lote


def texto_procesar(n: int) -> str:
    return "Procesar" if n <= 0 else f"Procesar {n} {'documento' if n == 1 else 'documentos'}"


# ------------------------------------------------------------------------------------ visual
def colores_formato(formato: str, p: ui.Paleta) -> tuple[str, str]:
    """(color del icono, fondo del chip): PDF rojo, Excel verde, Word azul, CSV y resto neutro."""
    return {"PDF": (p.rojo, p.rojo_suave), "Excel": (p.verde, p.verde_suave),
            "Word": (p.acento, p.acento_suave)}.get(formato, (p.texto2, p.campo))


def chip_formato(adjunto: Adjunto, p: ui.Paleta) -> ft.Container:
    icono = _FORMATOS.get(adjunto.extension, ("", ft.Icons.INSERT_DRIVE_FILE_OUTLINED))[1]
    color, fondo = colores_formato(adjunto.formato, p)
    return ft.Container(ft.Icon(icono, size=18, color=color), width=34, height=34, border_radius=10,
                        bgcolor=fondo, alignment=ft.Alignment.CENTER)


class RevisionDocumentos:
    """Contenido y acciones del diálogo «Revisar documentos». No abre el diálogo: main lo monta con ``_dialogo``
    para heredar cabecera, forma y scroll únicos del resto de la app."""

    TITULO = "Revisar documentos"

    def __init__(self, p: ui.Paleta, adjuntos: list[Adjunto], destino: Destino,
                 al_procesar: Callable[[list[Path], str], Optional[str]], al_cancelar: Callable[[], None],
                 destino_vigente: Callable[[], bool] = lambda: True, refrescar: Callable[[], None] = lambda: None,
                 avisos: Optional[list[str]] = None):
        self._p = p
        self.adjuntos = list(adjuntos)[:MAX_ARCHIVOS]
        self.destino = destino
        self._al_procesar, self._al_cancelar = al_procesar, al_cancelar
        self._destino_vigente, self._refrescar = destino_vigente, refrescar
        self.enviado = False          # evita un doble envío
        self.destino_invalido = False  # el expediente cambió: ya no se puede procesar este lote

        self.comentario = ft.TextField(
            label="Comentario para la IA (opcional)", hint_text=HINT_COMENTARIO,
            helper="Se aplica a todos los documentos de este lote.", multiline=True, min_lines=3, max_lines=6,
            max_length=MAX_COMENTARIO, autofocus=True, filled=True, fill_color=p.campo, border=ui._bordes(p),
            text_size=14, color=p.texto, cursor_color=p.acento,
            label_style=ft.TextStyle(color=p.texto2, size=13), helper_style=ft.TextStyle(color=p.texto3, size=12),
            hint_style=ft.TextStyle(color=p.texto3, size=13), counter_style=ft.TextStyle(color=p.texto3, size=11),
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12))
        self.lista = ft.Column(spacing=0, tight=True)
        self.encabezado_lista = ui.leyenda("", p, p.texto2)
        self.error = ft.Container(visible=False, bgcolor=p.rojo_suave, border_radius=12, padding=12)
        self.boton_cancelar = ui.accion("Cancelar", self._cancelar, p, tooltip="Cerrar sin importar nada")
        self.boton_procesar = ui.accion(texto_procesar(len(self.adjuntos)), self._procesar, p, ft.Icons.CHECK,
                                        "primario")
        self.contenido = ft.Column([
            self._banda_destino(),
            self.comentario,
            *[self._aviso(a) for a in (avisos or [])],
            self.encabezado_lista,
            ft.Container(self.lista, border=ft.Border.all(1, p.separador), border_radius=14, bgcolor=p.tarjeta,
                         clip_behavior=ft.ClipBehavior.ANTI_ALIAS),
            self.error,
        ], spacing=12, tight=True)
        self.acciones = [self.boton_cancelar, self.boton_procesar]
        self._pintar_lista()

    # ------------------------------------------------------------------------------------ API
    @property
    def rutas(self) -> list[Path]:
        return [a.ruta for a in self.adjuntos]

    def retirar(self, adjunto: Adjunto) -> None:
        if self.enviado or adjunto not in self.adjuntos:
            return
        self.adjuntos.remove(adjunto)
        self._pintar_lista()
        self._refrescar()

    def descartar(self, _e=None) -> None:
        """El diálogo se cerró por fuera (Esc, cierre de ventana): no se procesa nada."""
        self.enviado = True

    # ------------------------------------------------------------------------------------ interno
    def _banda_destino(self) -> ft.Container:
        p, d = self._p, self.destino
        return ft.Container(ft.Row([
            ft.Container(ft.Icon(ft.Icons.FOLDER_SHARED_OUTLINED, size=18, color=p.texto2), width=34, height=34,
                         border_radius=10, bgcolor=p.tarjeta, alignment=ft.Alignment.CENTER),
            ft.Column([
                ui.leyenda("SE AÑADIRÁN A", p, p.texto2, 11),
                ui.cuerpo(d.nombre or "Cliente seleccionado", p, peso=ft.FontWeight.W_600, max_lines=1),
                ui.leyenda(f"NIF/NIE {d.nif or 'sin indicar'} · Ejercicio {d.ejercicio}", p),
            ], spacing=1, tight=True, expand=True),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=p.campo, border_radius=12, padding=12)

    def _aviso(self, texto: str) -> ft.Container:
        p = self._p
        return ft.Container(ft.Row([ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=16, color=p.naranja),
                                    ft.Container(ui.cuerpo(texto, p, p.naranja, 13), expand=True)], spacing=8),
                            bgcolor=p.naranja_suave, border_radius=12, padding=10)

    def _fila(self, a: Adjunto, ultima: bool) -> ft.Container:
        p = self._p
        nombre = ui.cuerpo(a.nombre, p, peso=ft.FontWeight.W_600, max_lines=1)
        nombre.tooltip = str(a.ruta)
        quitar = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE, icon_size=18, tooltip=f"Quitar {a.nombre} del lote",
            on_click=lambda e, a=a: self.retirar(a),
            style=ft.ButtonStyle(
                icon_color={ft.ControlState.HOVERED: p.rojo, ft.ControlState.FOCUSED: p.rojo,
                            ft.ControlState.DEFAULT: p.texto3},
                overlay_color={ft.ControlState.HOVERED: p.rojo_suave, ft.ControlState.FOCUSED: p.rojo_suave},
                shape=ft.RoundedRectangleBorder(radius=12)))
        return ft.Container(ft.Row([
            chip_formato(a, p),
            ft.Column([nombre, ui.leyenda(f"{a.formato} · {tamano_legible(a.tamano)}", p)],
                      spacing=1, tight=True, expand=True),
            quitar,
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.only(left=12, right=6, top=8, bottom=8),
            border=None if ultima else ft.Border.only(bottom=ft.BorderSide(1, p.separador)))

    def _pintar_lista(self) -> None:
        p, n = self._p, len(self.adjuntos)
        if n:
            self.lista.controls = [self._fila(a, i == n - 1) for i, a in enumerate(self.adjuntos)]
            self.encabezado_lista.value = (f"{n} {'DOCUMENTO' if n == 1 else 'DOCUMENTOS'} · "
                                           f"máximo {MAX_ARCHIVOS} por lote")
        else:
            self.lista.controls = [ft.Container(
                ui.leyenda("No queda ningún documento. Cancela y vuelve a elegirlos.", p, p.texto2),
                padding=16, alignment=ft.Alignment.CENTER)]
            self.encabezado_lista.value = "SIN DOCUMENTOS"
        self._estado_procesar()

    def _estado_procesar(self) -> None:
        p, b = self._p, self.boton_procesar
        activo = bool(self.adjuntos) and not self.destino_invalido and not self.enviado
        b.content = texto_procesar(len(self.adjuntos))
        b.disabled = not activo
        b.style.bgcolor = p.relleno if activo else p.campo
        b.style.color = b.style.icon_color = "#FFFFFF" if activo else p.texto3

    def _mostrar_error(self, texto: str) -> None:
        p = self._p
        self.error.content = ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, size=16, color=p.rojo),
                                     ft.Container(ui.cuerpo(texto, p, p.rojo, 13), expand=True)], spacing=8)
        self.error.visible = True
        self._estado_procesar()
        self._refrescar()

    def _procesar(self, _e=None) -> None:
        if self.enviado or self.destino_invalido:
            return
        if not self.adjuntos:
            self._mostrar_error("Añade al menos un documento para procesar.")
            return
        if not self._destino_vigente():
            self.destino_invalido = True
            self._mostrar_error("El expediente o el ejercicio ha cambiado desde que abriste esta revisión. "
                                "Cancela y vuelve a elegir los documentos en el destino correcto.")
            return
        self.enviado = True
        try:
            error = self._al_procesar(self.rutas, normalizar_comentario(self.comentario.value))
        except Exception:
            self.enviado = False
            self._mostrar_error("No se pudo iniciar el procesado. Revisa los documentos y vuelve a intentarlo.")
            return
        if error:
            self.enviado = False
            self._mostrar_error(error)

    def _cancelar(self, _e=None) -> None:
        if self.enviado:
            return
        self.enviado = True
        self._al_cancelar()
