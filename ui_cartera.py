"""Presentación de la cartera de inmuebles y alquileres: ocupación del ejercicio, comparativa de valores,
periodicidad del 210 y estados. Solo formatea lo que entrega el controlador; no calcula días, rentas ni
cuotas (eso vive en el backend).

Criterio (propuesta «minimalismo claro», ver INFORME_UI_CLAUDE.md): superficies sólidas, cifras grandes con
etiqueta explícita, estados con texto + icono (no solo color) y «Sin dato» en lugar de 0 cuando falta un valor.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

import flet as ft

import ui_estilo as ui

SIN_DATO = "Sin dato"
PERIODICIDADES_210 = ("Anual", "Trimestral")
PERIODICIDAD_DESDE = 2024  # el selector Anual/Trimestral solo se ofrece para ejercicios nuevos (encargo 26/10/2026)


# ------------------------------------------------------------------------------------ formato
def _ausente(valor: Any) -> bool:
    return valor is None or valor == ""


def dias(valor: Any) -> str:
    """Días tal cual los da el backend; ausente -> «Sin dato» (nunca 0 inventado)."""
    if _ausente(valor):
        return SIN_DATO
    n = int(valor)
    return f"{n} día" if n == 1 else f"{n} días"


def importe(valor: Any) -> str:
    return SIN_DATO if _ausente(valor) else ui.eur(valor)


def fecha(valor: Any) -> str:
    if _ausente(valor):
        return SIN_DATO
    if isinstance(valor, datetime):
        valor = valor.date()
    if isinstance(valor, date):
        return f"{valor:%d/%m/%Y}"
    texto = str(valor).strip()
    try:
        return f"{date.fromisoformat(texto[:10]):%d/%m/%Y}"
    except ValueError:
        return texto


def estado_visible(estado: Optional[str]) -> Optional[str]:
    """Estado canónico para mostrar. Un valor heredado como «PRESENTADO[OK]» se enseña como «Presentado»;
    la confirmación es un icono, no parte del texto guardado."""
    if not estado:
        return estado
    if re.sub(r"[^a-záéíóú]", "", str(estado).lower()) in ("presentado", "presentadook"):
        return "Presentado"
    return estado


def periodicidad_210(v: dict) -> Optional[str]:
    valor = v.get("_periodicidad_210")
    return valor if valor in PERIODICIDADES_210 else None


def anio_compra(v: dict) -> Optional[int]:
    """Año de compra del backend; si aún no lo da, el de la fecha de adquisición (solo para la etiqueta)."""
    if not _ausente(v.get("_anio_compra")):
        return int(v["_anio_compra"])
    f = v.get("fecha_adquisicion")
    if isinstance(f, (date, datetime)):
        return f.year
    m = re.match(r"(\d{4})-\d{2}-\d{2}", str(f or ""))
    return int(m.group(1)) if m else None


def inmuebles_con_presentado(filas_210: list[dict]) -> set[str]:
    """Inmuebles con algún 210 de rentas ya presentado en el ejercicio: su periodicidad no se toca."""
    return {f.get("inmueble") for f in filas_210 if estado_visible(f.get("estado")) == "Presentado"}


# ------------------------------------------------------------------------------------ datos de la tarjeta
def ocupacion(v: dict, ejercicio: int) -> list[tuple[str, str, Optional[str]]]:
    """(etiqueta, valor, ayuda) del ejercicio. Devengado y cobrado van separados y con nombre distinto:
    un movimiento bancario no es renta devengada."""
    return [
        ("Fecha de compra", fecha(v.get("fecha_adquisicion")), None),
        (f"Días de propiedad {ejercicio}", dias(v.get("_dias_propiedad")), None),
        ("Días alquilados", dias(v.get("_dias_alquilado")), "Según contratos"),
        ("Días vacíos", dias(v.get("_dias_vacio")), "Base de la imputación de rentas"),
        (f"Renta devengada {ejercicio}", importe(v.get("_ingresos_devengados")), "Según contratos y fechas"),
        (f"Cobros registrados {ejercicio}", importe(v.get("_ingresos_cobrados")),
         "Cobros anotados; no sustituyen a la renta devengada"),
    ]


def comparativa_valores(v: dict, resumen: dict, catastro: dict, ejercicio: int) -> list[tuple[str, str, Optional[str]]]:
    """Escritura, adquisición, referencia del año de compra, referencia y catastral del ejercicio consultado.
    Cada referencia lleva su año; un dato de Catastro heredado de otro año se dice, no se presenta como propio."""
    anio = anio_compra(v)
    ref_compra = v["_valor_referencia_compra"] if "_valor_referencia_compra" in v else resumen.get("referencia_compra")
    ref_actual = v["_valor_referencia_actual"] if "_valor_referencia_actual" in v else resumen.get("referencia_catastro")
    heredado = (catastro or {}).get("_heredado")
    nota_heredado = f"Dato de {heredado}; no consta certificado para {ejercicio}" if heredado else None
    adquisicion = importe(resumen.get("adquisicion"))
    if resumen.get("parcial"):
        adquisicion += " · parcial"
    return [
        ("Precio de escritura:", importe(resumen.get("escritura")), None),
        ("Gastos de compra:", importe(resumen.get("gastos")), None),
        ("Valor de adquisición (escritura + gastos):", adquisicion, None),
        (f"Referencia en adquisición ({anio if anio else 'año sin dato'}):", importe(ref_compra),
         "Valor de referencia a la fecha de compra"),
        (f"Referencia Catastro {ejercicio}:", importe(ref_actual), nota_heredado if _ausente(ref_actual) else None),
        (f"Valor catastral {ejercicio}:", importe(resumen.get("catastral")), nota_heredado),
    ]


# ------------------------------------------------------------------------------------ componentes
def celda(etiqueta: str, valor: str, p: ui.Paleta, ayuda: Optional[str] = None, col=None) -> ft.Container:
    """Cifra con su etiqueta encima. «Sin dato» va en gris para no leerse como un importe."""
    falta = valor == SIN_DATO
    return ft.Container(
        ft.Column([ui.leyenda(etiqueta, p, p.texto2, 12),
                   ft.Text(valor, size=17, weight=ft.FontWeight.W_400 if falta else ft.FontWeight.W_600,
                           color=p.texto3 if falta else p.texto, font_family=ui.FUENTE),
                   *([ui.leyenda(ayuda, p, p.texto3, 11)] if ayuda else [])], spacing=3, tight=True),
        padding=12, border_radius=12, bgcolor=p.campo, col=col or {"xs": 6, "md": 4, "xl": 2},
        tooltip=f"{etiqueta}: {valor}" + (f". {ayuda}" if ayuda else ""))


def rejilla_ocupacion(v: dict, ejercicio: int, p: ui.Paleta) -> ft.Control:
    return ft.Column([
        ui.leyenda(f"OCUPACIÓN Y RENTAS · {ejercicio}", p, p.texto3, 11),
        ft.ResponsiveRow([celda(e, x, p, a) for e, x, a in ocupacion(v, ejercicio)], spacing=8, run_spacing=8),
    ], spacing=8, tight=True)


def tabla_comparativa(filas: list[tuple[str, str, Optional[str]]], p: ui.Paleta) -> ft.Control:
    """Tabla de dos columnas alineada a la derecha para comparar importes de un vistazo."""
    controles: list[ft.Control] = [ui.leyenda("COMPARATIVA DE VALORES", p, p.texto3, 11)]
    for etiqueta, valor, nota in filas:
        falta = valor == SIN_DATO
        controles.append(ft.Row([
            ft.Column([ui.cuerpo(etiqueta, p, p.texto2, 13), *([ui.leyenda(nota, p, p.naranja, 11)] if nota else [])],
                      spacing=0, expand=True, tight=True),
            ft.Text(valor, size=14, weight=ft.FontWeight.W_400 if falta else ft.FontWeight.W_600,
                    color=p.texto3 if falta else p.texto, font_family=ui.FUENTE, text_align=ft.TextAlign.RIGHT),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER))
    return ft.Container(ft.Column(controles, spacing=6, tight=True), padding=12, border_radius=12,
                        border=ft.Border.all(1, p.separador))


def marcar_periodicidad(selector: ft.CupertinoSlidingSegmentedButton, p: ui.Paleta) -> None:
    """Pinta el segmento de ``selected_index`` con icono de marca, negrita y «(elegido)» en la ayuda."""
    for i, (segmento, n) in enumerate(zip(selector.controls, PERIODICIDADES_210)):
        elegido = i == selector.selected_index
        segmento.content = ft.Row([
            *([ft.Icon(ft.Icons.CHECK, size=14, color=p.texto)] if elegido else []),
            ft.Text(n, size=13, weight=ft.FontWeight.W_700 if elegido else ft.FontWeight.W_400,
                    color=p.texto if elegido else p.texto2, no_wrap=True),
        ], spacing=4, tight=True)
        segmento.tooltip = f"Modelo 210 {n.lower()}" + (" (elegido)" if elegido else "")


def mostrar_progreso_periodicidad(selector: ft.CupertinoSlidingSegmentedButton, texto: Optional[str],
                                  p: ui.Paleta) -> None:
    """Feedback en el propio selector («Calculando…», «Guardando Excel…»); ``None`` lo oculta."""
    fila = selector.data
    if not isinstance(fila, ft.Row):
        return
    fila.visible = bool(texto)
    fila.controls = [ft.ProgressRing(width=13, height=13, stroke_width=2, color=p.acento),
                     ui.leyenda(texto, p, p.texto2)] if texto else []


def selector_periodicidad(actual: str, p: ui.Paleta, on_change, bloqueo: Optional[str] = None,
                          progreso: Optional[str] = None) -> ft.Control:
    """Anual / Trimestral con el control segmentado del sistema. Bloqueado -> el motivo se lee en texto.
    El segmento elegido lleva icono de marca y negrita: no depende solo del contraste de la pastilla.
    ``progreso``: hay un cambio en curso; el selector queda bloqueado y muestra la fase."""
    segmentos = [ft.Container(padding=ft.Padding.symmetric(horizontal=10, vertical=3)) for _ in PERIODICIDADES_210]

    def cambiar(e):
        marcar_periodicidad(selector, p)
        on_change(e)
    selector = ft.CupertinoSlidingSegmentedButton(
        controls=segmentos, selected_index=PERIODICIDADES_210.index(actual), bgcolor=p.campo,
        thumb_color=p.segmento, on_change=cambiar, padding=ft.Padding.all(3), disabled=bool(bloqueo or progreso))
    selector.data = ft.Row(spacing=6, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    marcar_periodicidad(selector, p)
    mostrar_progreso_periodicidad(selector, progreso, p)
    return ft.Row([
        ui.cuerpo("210 de rentas", p, p.texto2, 13),
        ft.Container(selector, tooltip=bloqueo or progreso or "Cómo se declaran las rentas de este inmueble en el ejercicio"),
        *([ft.Icon(ft.Icons.LOCK_OUTLINE, size=15, color=p.texto3), ui.leyenda(bloqueo, p, p.texto3)] if bloqueo else []),
        selector.data,
    ], spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)


# ------------------------------------------------------------------------------------ desglose del 210
_ORDEN_PERIODOS = {"Anual": 0, "0A": 0, "OA": 0, "1T": 1, "2T": 2, "3T": 3, "4T": 4}


def _es_anual(periodo: Any) -> bool:
    return str(periodo or "").strip().upper() in ("ANUAL", "0A", "OA")


def tipo_210(valor: Any) -> str:
    """Tipo tal cual lo da el backend: 0,19 o 19 se muestran «19 %». No se usa para calcular nada."""
    if _ausente(valor):
        return SIN_DATO
    t = float(valor)
    return ui.pct(t if t <= 1 else t / 100)


def filas_desglose_210(v: dict, periodicidad: Optional[str]) -> Optional[list[dict]]:
    """Modelos 210 del contribuyente para mostrar. ``None`` si el backend no da el desglose (adaptador antiguo:
    no se inventa). Se quitan «No procede» y los periodos de la otra periodicidad (obsoletos tras el cambio),
    para que no se lean como pendientes. Sin sumas ni redondeos: cada fila es la del backend."""
    filas = v.get("_desglose_210")
    if not isinstance(filas, list):
        return None
    visibles = [f for f in filas if isinstance(f, dict)
                and (str(f.get("estado") or "").strip().lower() != "no procede" or v.get("_revision_210"))]
    if periodicidad in PERIODICIDADES_210 and not v.get("_revision_210"):
        anual = periodicidad == "Anual"
        visibles = [f for f in visibles if _es_anual(f.get("periodo")) == anual]
    return sorted(visibles, key=lambda f: _ORDEN_PERIODOS.get(str(f.get("periodo") or "").strip(), 9))


def _cifra(etiqueta: str, valor: str, p: ui.Paleta, ayuda: Optional[str] = None) -> ft.Container:
    falta = valor == SIN_DATO
    return ft.Container(ft.Column([
        ui.leyenda(etiqueta, p, p.texto3, 11),
        ft.Text(valor, size=14, weight=ft.FontWeight.W_400 if falta else ft.FontWeight.W_600,
                color=p.texto3 if falta else p.texto, font_family=ui.FUENTE),
        *([ui.leyenda(ayuda, p, p.texto3, 11)] if ayuda else []),
    ], spacing=1, tight=True), col={"xs": 6, "sm": 3, "md": 2})


def desglose_210(v: dict, periodicidad: Optional[str], ejercicio: int, p: ui.Paleta) -> list[ft.Control]:
    """Bloque compacto bajo el selector: una fila por periodo (Anual o 1T..4T). Cada cifra lleva su etiqueta,
    así se lee igual en ancho o en estrecho (en móvil las cifras bajan a dos columnas)."""
    filas = filas_desglose_210(v, periodicidad)
    if filas is None:
        return []
    controles: list[ft.Control] = [
        ui.leyenda(f"MODELO 210 POR PERIODO · {ejercicio}", p, p.texto3, 11),
        ui.leyenda("Importes del contribuyente según titularidad. La ocupación y la renta devengada de arriba "
                   "son del inmueble al 100 %.", p),
    ]
    ocupacion = v.get("_ocupacion_trimestral") or []
    if ocupacion:
        controles.extend([
            ui.leyenda("OCUPACIÓN POR TRIMESTRE · INFORMATIVO", p, p.texto3, 11),
            ui.leyenda("Días y rentas del inmueble al 100 %. Este bloque no sustituye las declaraciones fiscales.", p),
        ])
        for tramo in ocupacion:
            controles.append(ft.Container(ft.ResponsiveRow([
                ft.Text(str(tramo["periodo"]), color=p.texto, col=2),
                _cifra("Alquilado", dias(tramo.get("dias_alquilado")), p),
                _cifra("Vacío", dias(tramo.get("dias_vacio")), p),
                _cifra("Renta devengada", importe(tramo.get("ingresos_devengados")), p),
            ], spacing=8, run_spacing=6), padding=12, border_radius=12, bgcolor=p.campo))
    if v.get("_revision_210"):
        controles.append(ui.leyenda("PENDIENTE DE REVISIÓN · " + str(v["_revision_210"])
                                    + " Importes históricos conservados; no son un cálculo actualizado.", p, p.naranja))
    if not filas:
        controles.append(ui.leyenda(f"Sin modelos 210 calculados para {ejercicio}.", p, p.texto3))
    for f in filas:
        periodo = str(f.get("periodo") or SIN_DATO)
        valores = [("Ingresos", importe(f.get("ingresos")), None), ("Gastos", importe(f.get("gastos")), None),
                   ("Base", importe(f.get("base")), None),
                   ("Cuota", importe(f.get("cuota")),
                    None if _ausente(f.get("tipo")) else f"Tipo {tipo_210(f.get('tipo'))}")]
        estado = estado_visible(f.get("estado")) or SIN_DATO
        controles.append(ft.Container(ft.ResponsiveRow([
            ft.Container(ft.Column([ft.Text(periodo, size=14, weight=ft.FontWeight.W_700, color=p.texto,
                                            font_family=ui.FUENTE),
                                    *([ui.leyenda(str(f["codigo"]), p, p.texto3, 11)] if f.get("codigo") else [])],
                                   spacing=1, tight=True), col={"xs": 6, "sm": 12, "md": 2}),
            *[_cifra(e, x, p, a) for e, x, a in valores],
            ft.Container(ui.pastilla_estado(estado, p), col={"xs": 6, "sm": 12, "md": 2},
                         alignment=ft.Alignment.CENTER_LEFT),
        ], spacing=8, run_spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8), border_radius=12, bgcolor=p.campo,
            tooltip=f"{periodo}: " + ", ".join(f"{e.lower()} {x}" for e, x, _ in valores) + f". Estado: {estado}"))
    return [ft.Column(controles, spacing=6, tight=True)]
