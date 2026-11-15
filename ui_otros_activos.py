"""Otros activos del Patrimonio 714 (no residentes): cuentas, criptoactivos y valores.

Solo presenta y recoge datos. La valoración la da ``otros_activos.valorar`` (backend, import diferido: el
módulo puede no existir aún) y el resumen del 714 sale de la fila ``patrimonio_714`` ya recalculada. La UI no
suma importes ni decide sujeción, exención o valoración: eso es del backend y del asesor.

Criterio de diseño (comparación de skills en .tripartito/2026-11-04-otros-activos-714/UI-claude.md):
- Estados con texto + icono, nunca solo color; verde solo si no hay ningún dato pendiente.
- «Estimación» en cuanto el backend avisa o marca provisional.
- Total de la posición frente a unidades y valor unitario, separados y con etiqueta explícita.
- Un resumen de cartera es informativo: se muestra aparte y nunca se suma a sus posiciones.
- Movimiento: fundido corto (160 ms, ease-out) al cambiar de categoría o de tipo; nada se desplaza.
- Normativa del Patrimonio por cliente y ejercicio (UI-normativa-claude.md): desplegable buscable con las 21
  opciones del backend, herencia visible, confirmación solo al guardar un cambio; nunca verde; la UI no calcula.
"""
from __future__ import annotations

import re
import threading
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import flet as ft

import ui_estilo as ui
from concurrencia import ejecutar_en_ui

I = ft.Icons
TABLA = "otros_activos"
SIN_DATO = "Sin dato"
ANIM_CAMBIO = 160  # ms: cambio de categoría o de tipo (frecuente: rápido y solo opacidad)

TIPOS = ("CUENTA", "CRIPTO", "ACCION_COTIZADA", "FONDO", "NO_COTIZADO",
         "RENTA_FIJA_COTIZADA", "RENTA_FIJA_NO_COTIZADA", "RESUMEN_CARTERA")
ETIQUETA_TIPO = {
    "CUENTA": "Cuenta bancaria", "CRIPTO": "Criptoactivo", "ACCION_COTIZADA": "Acciones cotizadas",
    "FONDO": "Fondo de inversión", "NO_COTIZADO": "Participación no cotizada",
    "RENTA_FIJA_COTIZADA": "Renta fija cotizada", "RENTA_FIJA_NO_COTIZADA": "Renta fija no cotizada",
    "RESUMEN_CARTERA": "Resumen de cartera (informativo)",
}
UBICACIONES = ("ESPANA", "EXTRANJERO", "PENDIENTE")
ETIQUETA_UBICACION = {"ESPANA": "En España", "EXTRANJERO": "En el extranjero", "PENDIENTE": "Pendiente de confirmar"}

# (nombre, icono, tipos). «Valores» incluye los resúmenes de cartera, que se pintan aparte.
CATEGORIAS = (
    ("Cuentas", I.ACCOUNT_BALANCE_WALLET_OUTLINED, ("CUENTA",)),
    ("Cripto", I.CURRENCY_BITCOIN, ("CRIPTO",)),
    ("Valores", I.SHOW_CHART, ("ACCION_COTIZADA", "FONDO", "NO_COTIZADO", "RENTA_FIJA_COTIZADA",
                               "RENTA_FIJA_NO_COTIZADA", "RESUMEN_CARTERA")),
)

IMPORTES = ("saldo_31dic", "saldo_medio_4t", "valor_informe", "valor_nominal", "valor_balance",
            "beneficio_1", "beneficio_2", "beneficio_3", "valor_manual")
DECIMALES = ("unidades", "valor_unitario")  # texto decimal: se guardan con punto y sin redondear
# ``manual_override`` NO es un interruptor del formulario: en BD significa «fila protegida por edición humana» y
# lo pone el backend en cada edición. «Usar valor manual» es solo de la UI y depende de ``valor_manual``.
INTERRUPTORES = ("auditoria_favorable", "exento", "revision_confirmada")
USAR_MANUAL = "usar_manual"
AVISO_RESUMEN_SIN_POSICIONES = "Resumen de cartera sin posiciones desglosadas: valoración incompleta."
TEXTOS = ("entidad", "identificador", "descripcion", "cartera_id", "motivo_exencion", "motivo_manual",
          "confirmacion_legal", "observaciones")

ETIQUETA_CAMPO = {
    "entidad": "Entidad o plataforma", "identificador": "IBAN, ISIN o dirección",
    "descripcion": "Descripción", "pct_titularidad": "Titularidad (%)", "cartera_id": "Cartera (agrupación)",
    "saldo_31dic": "Saldo a 31/12 (€)", "saldo_medio_4t": "Saldo medio 4T ajustado (€)",
    "valor_informe": "Valor total de la posición (€)", "unidades": "Unidades",
    "valor_unitario": "Valor unitario a 31/12 (€ por unidad)", "valor_nominal": "Valor nominal (€)",
    "valor_balance": "Valor según balance (€)", "beneficio_1": "Beneficio ejercicio −1 (€)",
    "beneficio_2": "Beneficio ejercicio −2 (€)", "beneficio_3": "Beneficio ejercicio −3 (€)",
    "valor_manual": "Valor manual (€)", "motivo_manual": "Motivo del valor manual",
    "motivo_exencion": "Motivo de la exención", "confirmacion_legal": "Justificación del asesor",
    "observaciones": "Observaciones",
}
AYUDA_CAMPO = {
    "saldo_medio_4t": "Media del 4.º trimestre ajustada (art. 12)",
    "unidades": "Cantidad de la especie; no es un importe",
    "valor_unitario": "Precio de una unidad; no es el total",
    "pct_titularidad": "100 % si es el único titular",
    "cartera_id": "Mismo identificador en el resumen y en sus posiciones",
    "confirmacion_legal": "Ubicación, exención y regla de valoración, con su justificante",
}

# Campos específicos de cada tipo (además de los comunes). El orden es el del formulario.
CAMPOS_TIPO = {
    "CUENTA": ("saldo_31dic", "saldo_medio_4t"),
    "CRIPTO": ("unidades", "valor_unitario", "valor_informe"),
    "ACCION_COTIZADA": ("valor_informe", "unidades", "valor_unitario"),
    "FONDO": ("valor_informe", "unidades", "valor_unitario"),
    "RENTA_FIJA_COTIZADA": ("valor_informe", "unidades", "valor_unitario"),
    "RENTA_FIJA_NO_COTIZADA": ("valor_nominal", "valor_informe"),
    "NO_COTIZADO": ("valor_balance", "auditoria_favorable", "valor_nominal", "beneficio_1", "beneficio_2",
                    "beneficio_3", "valor_informe"),
    "RESUMEN_CARTERA": ("valor_informe",),
}
# Qué significa el «valor total» en cada tipo: siempre el total de la posición, nunca el precio unitario.
AYUDA_TOTAL = {
    "CRIPTO": "Total del informe a 31/12, no el precio por unidad. Si lo dejas vacío, la app usa unidades × valor unitario.",
    "ACCION_COTIZADA": "Total de la posición según la negociación media del 4T, no el precio por acción.",
    "RENTA_FIJA_COTIZADA": "Total de la posición según la negociación media del 4T.",
    "FONDO": "Valor liquidativo total a 31/12 (participaciones × liquidativo).",
    "RENTA_FIJA_NO_COTIZADA": "Solo si no consta el nominal; el nominal tiene preferencia.",
    "NO_COTIZADO": "Valor fiscal acreditado si faltan datos del art. 16.",
    "RESUMEN_CARTERA": "Total que da el informe de la cartera. Es informativo: no se suma a sus posiciones.",
}
# Valor unitario según el tipo: (etiqueta, ayuda). Cotizados = media del 4T (no el precio a 31/12); fondo =
# liquidativo a 31/12. Si falta el total, el backend usa unidades × este valor y avisa si no cuadran.
_AYUDA_UNITARIO_COTIZADO = "Media de negociación del 4T por título, no el precio a 31/12 ni el total"
UNITARIO_TIPO = {
    "CRIPTO": (ETIQUETA_CAMPO["valor_unitario"], AYUDA_CAMPO["valor_unitario"]),
    "ACCION_COTIZADA": ("Cotización media 4T (€ por título)", _AYUDA_UNITARIO_COTIZADO),
    "RENTA_FIJA_COTIZADA": ("Cotización media 4T (€ por título)", _AYUDA_UNITARIO_COTIZADO),
    "FONDO": ("Liquidativo a 31/12 (€ por participación)", "Valor liquidativo de una participación, no el total"),
}


# ------------------------------------------------------------------------------------ backend (diferido)
def backend():
    """``otros_activos`` si ya existe; ``None`` mientras Codex lo construye."""
    try:
        import otros_activos
    except ImportError:
        return None
    return otros_activos


def tabla_disponible() -> bool:
    try:
        from database import TABLAS
    except ImportError:
        return False
    return TABLA in TABLAS


def valoracion(fila: dict) -> dict:
    """Resultado del backend para mostrar. Un error de validación o un backend ausente no se convierte en un
    cero: se devuelve como estimación con el motivo."""
    modulo = backend()
    if modulo is None:
        return {"error": "Valoración no disponible en esta versión.", "avisos": [], "provisional": True}
    try:
        res = dict(modulo.valorar(dict(fila)))
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": str(exc) or "Datos no válidos.", "avisos": [], "provisional": True}
    res.setdefault("avisos", [])
    res["provisional"] = bool(res.get("provisional") or res["avisos"])
    return res


# ------------------------------------------------------------------------------------ formato puro
def _ausente(v: Any) -> bool:
    return v is None or v == ""


def importe(v: Any) -> str:
    return SIN_DATO if _ausente(v) else ui.eur(v)


def decimal_texto(texto: Any) -> Optional[str]:
    """«0,125» o «1.234,5» -> «0.125» / «1234.5». Sin redondear (unidades de cripto). ``None`` si vacío.
    Lanza ValueError si no es un número."""
    s = str(texto or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"[-+]?[1-9]\d{0,2}(?:\.\d{3})+", s):  # «40.000» = miles, igual que database.a_num
        s = s.replace(".", "")
    try:
        n = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError("número no válido") from exc
    if not n.is_finite():
        raise ValueError("número no válido")
    return format(n.normalize(), "f") if n == n.to_integral() else s


def es_baja(f: dict) -> bool:
    return (f.get("estado") or "Activo") == "Baja"


def es_resumen(f: dict) -> bool:
    return f.get("tipo") == "RESUMEN_CARTERA"


def categoria_de(tipo: Optional[str]) -> int:
    return next((i for i, (_, _, tipos) in enumerate(CATEGORIAS) if tipo in tipos), len(CATEGORIAS) - 1)


def de_categoria(filas: list[dict], indice: int) -> list[dict]:
    tipos = CATEGORIAS[indice][2]
    return [f for f in filas if f.get("tipo") in tipos]


def estado_valoracion(fila: dict, res: dict) -> tuple[str, str]:
    """(texto, clave de color). Verde («Revisado») solo sin avisos, no provisional y con revisión del asesor."""
    if es_baja(fila):
        return "Baja", "neutro"
    if es_resumen(fila):  # nunca suma ni se pone en verde; si le faltan posiciones o hay avisos, se revisa
        return ("Informativo · revisar", "naranja") if (res.get("avisos") or res.get("error")) else ("Informativo", "neutro")
    if res.get("error"):
        return "Revisar datos", "rojo"
    if res.get("provisional") or res.get("avisos"):
        return "Estimación", "naranja"
    if fila.get("revision_confirmada") in (1, "1", True):
        return "Revisado", "verde"
    return "Estimación", "naranja"


def pastilla_valoracion(texto: str, clave: str, p: ui.Paleta) -> ft.Container:
    color, fondo, icono = {
        "verde": (p.verde, p.verde_suave, I.CHECK_CIRCLE),
        "naranja": (p.naranja, p.naranja_suave, I.HOURGLASS_BOTTOM),
        "rojo": (p.rojo, p.rojo_suave, I.ERROR_OUTLINE),
    }.get(clave, (p.texto2, p.campo, None))
    c = ui.pastilla(texto, color, fondo)
    if icono:
        c.content = ft.Row([ft.Icon(icono, size=13, color=color), c.content], spacing=4, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return c


def presentado_714(filas_714: list[dict]) -> bool:
    return any(str(f.get("estado") or "").strip().lower().startswith("presentado") for f in filas_714)


def provisional_714(filas_714: list[dict]) -> Optional[str]:
    """Observación del recálculo si el 714 es provisional o pide revisión (p. ej. motor autonómico sin soporte:
    la DA 4.ª permite la normativa autonómica a todo no residente, así que el grupo UE/extra-UE no lo cierra)."""
    for f in filas_714:
        texto = " · ".join(str(f.get(c) or "") for c in ("estado", "observaciones")).strip(" ·")
        if "provisional" in texto.lower() or "revisar" in texto.lower():
            return str(f.get("observaciones") or f.get("estado") or "").strip() or "Pendiente de revisión."
    return None


AVISO_CONCILIACION_FALLIDA = ("No se pudo conciliar el resumen con sus posiciones: revisa los datos de las "
                              "posiciones de esta cartera.")


def _sin_posiciones_local(filas: list[dict]) -> set:
    """Respaldo si el backend aún no trae ``conciliar_carteras``: solo cuenta filas por cartera, no suma."""
    carteras = {str(f.get("cartera_id")).strip() for f in filas
                if not es_baja(f) and not es_resumen(f) and str(f.get("cartera_id") or "").strip()}
    return {f.get("codigo") for f in filas if es_resumen(f) and not es_baja(f)
            and str(f.get("cartera_id") or "").strip() not in carteras}


def _conciliacion(filas: list[dict], valoraciones: Optional[dict] = None) -> Optional[dict]:
    """``otros_activos.conciliar_carteras`` (reglas de cartera/entidad, ejercicio e importes efectivos: todo
    backend). ``None`` si el backend no lo trae; ante datos que no puede conciliar devuelve el aviso a cada resumen
    vivo en lugar de dar la cartera por cuadrada."""
    funcion = getattr(backend(), "conciliar_carteras", None)
    if funcion is None:
        return None
    try:
        return {c: list(a) for c, a in (funcion([dict(f) for f in filas], valoraciones) or {}).items()}
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return {f.get("codigo"): [AVISO_CONCILIACION_FALLIDA] for f in filas if es_resumen(f) and not es_baja(f)}


def resumenes_sin_posiciones(filas: list[dict]) -> set:
    """Códigos de resúmenes vivos sin posiciones según el backend (respaldo local si no está)."""
    avisos = _conciliacion(filas)
    if avisos is None:
        return _sin_posiciones_local(filas)
    return {c for c, lista in avisos.items() if any("sin posiciones" in str(a).lower() for a in lista)}


def marcar_resumenes(filas: list[dict], valoraciones: dict) -> None:
    """Pasa a la valoración de cada resumen los avisos de ``conciliar_carteras`` (ausencia de posiciones o
    descuadre) sin duplicarlos. La UI no suma ni compara importes: solo traslada el resultado del backend."""
    # Solo valoraciones completas: una posición con error no tiene importe y el backend la revalora o avisa.
    completas = {c: r for c, r in valoraciones.items() if "valor_calculado" in r and not r.get("error")}
    avisos_backend = _conciliacion(filas, completas if len(completas) == len(valoraciones) else None)
    if avisos_backend is None:
        avisos_backend = {c: [AVISO_RESUMEN_SIN_POSICIONES] for c in _sin_posiciones_local(filas)}
    for codigo, nuevos in avisos_backend.items():
        res = valoraciones.setdefault(codigo, {"avisos": []})
        avisos = list(res.get("avisos") or [])
        for aviso in nuevos:
            ya = (aviso in avisos or ("sin posiciones" in str(aviso).lower()
                                      and any("sin posiciones" in str(a).lower() for a in avisos)))
            if not ya:
                avisos.append(aviso)
        res["avisos"], res["provisional"] = avisos, True


AVISO_PRESENTADO = ("El 714 de {ej} ya está presentado. La app conserva lo presentado: guardar aquí recalcula "
                    "los activos y deja la diferencia para revisar (posible complementaria). No bloquea el expediente.")


def detalle_fila(f: dict, res: dict) -> str:
    """Línea secundaria: tipo · identificador · ubicación · titularidad · cartera."""
    partes = [ETIQUETA_TIPO.get(f.get("tipo"), f.get("tipo") or SIN_DATO)]
    if f.get("identificador"):
        partes.append(str(f["identificador"]))
    partes.append(ETIQUETA_UBICACION.get(f.get("ubicacion") or "PENDIENTE", str(f.get("ubicacion"))))
    if not _ausente(f.get("pct_titularidad")):
        partes.append(f"titularidad {ui.pct(f['pct_titularidad'])}")
    if f.get("cartera_id"):
        partes.append(f"cartera {f['cartera_id']}")
    return " · ".join(partes)


def cifra_fila(f: dict, res: dict) -> tuple[str, str]:
    """(valor principal, etiqueta). Un resumen enseña su total como informativo; el resto, el valor del cliente
    que da el backend. Nunca se suma nada aquí."""
    if es_resumen(f):
        return importe(f.get("valor_informe")), "Total informado · no suma"
    if es_baja(f):
        return "—", "Dado de baja"
    if res.get("error"):
        return SIN_DATO, "Sin valoración"
    etiqueta = "Estimación del cliente" if res.get("provisional") else "Valor del cliente"
    return importe(res.get("valor_cliente")), etiqueta


def resumen_714(filas_714: list[dict]) -> list[tuple[str, str]]:
    """Cifras del 714 tal cual las dejó el último recálculo del backend."""
    f = filas_714[0] if filas_714 else {}
    return [("Valor de bienes", importe(f.get("valor_bienes"))),
            ("Base liquidable", importe(f.get("base_liquidable"))),
            ("Resultado", importe(f.get("resultado")))]


# ------------------------------------------------------------------------------------ sección
def seccion(app) -> ft.Control:
    p, cid, ej = app.p, app.cid, app.ejercicio
    cargar = ui.boton("Cargar documentos", app.elegir_pdfs, p, I.UPLOAD_FILE, tipo="secundario",
                      tooltip="Extractos, certificados fiscales o informes de cartera (PDF, Word o Excel)")
    nuevo = ui.boton("Nuevo activo", lambda _e: dialogo_activo(app, None), p, I.ADD)
    cabecera = ft.Row([
        ft.Column([ui.seccion(f"Otros activos · {ej}", p),
                   ui.leyenda("Cuentas, criptoactivos y valores a 31/12. La valoración la calcula la app con los "
                              "datos que registres; tú confirmas ubicación, exenciones y regla.", p)],
                  spacing=2, expand=True, tight=True),
        cargar, nuevo,
    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)  # sin wrap: Expanded es ilegal bajo Wrap
    if not tabla_disponible():
        return ft.Column([cabecera, ui.tarjeta(ui.vacio(
            I.CONSTRUCTION_OUTLINED, "Otros activos aún no está disponible",
            "Esta versión todavía no guarda otros activos. No se ha registrado ni calculado nada.", p), p)],
            spacing=16)
    lec = app.ctrl.lector
    filas = list(lec.listar(TABLA, cid, ejercicio=ej))
    filas_714 = list(lec.listar("patrimonio_714", cid, ejercicio=ej))
    valoraciones = {f.get("codigo"): valoracion(f) for f in filas}
    marcar_resumenes(filas, valoraciones)
    indice = getattr(app, "_oa_categoria", 0)
    indice = indice if 0 <= indice < len(CATEGORIAS) else 0

    cuerpo = ft.AnimatedSwitcher(
        _lista_categoria(app, filas, valoraciones, indice), duration=ANIM_CAMBIO, reverse_duration=110,
        transition=ft.AnimatedSwitcherTransition.FADE, switch_in_curve=ft.AnimationCurve.EASE_OUT_CUBIC,
        switch_out_curve=ft.AnimationCurve.EASE_OUT_CUBIC)

    def cambiar(e):
        app._oa_categoria = int(e.control.selected_index)
        cuerpo.content = _lista_categoria(app, filas, valoraciones, app._oa_categoria)
        app.page.update()

    segmentos = []
    for i, (nombre, icono, _) in enumerate(CATEGORIAS):
        n = len([f for f in de_categoria(filas, i) if not es_baja(f) and not es_resumen(f)])
        segmentos.append(ft.Container(ft.Row([
            ft.Icon(icono, size=15, color=p.texto2),
            ft.Text(nombre, size=13, weight=ft.FontWeight.W_600, color=p.texto, no_wrap=True),
            ft.Text(str(n), size=12, color=p.texto3),
        ], spacing=6, tight=True), padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            tooltip=f"{nombre}: {n} activos"))
    selector = ft.CupertinoSlidingSegmentedButton(controls=segmentos, selected_index=indice, bgcolor=p.campo,
                                                  thumb_color=p.segmento, on_change=cambiar,
                                                  padding=ft.Padding.all(3))
    return ft.Column([cabecera, _tarjeta_resumen(p, ej, filas, filas_714, valoraciones),
                      tarjeta_normativa(app, filas_714),
                      ft.Row([selector], wrap=True), cuerpo], spacing=16)


def _tarjeta_resumen(p: ui.Paleta, ej: int, filas: list[dict], filas_714: list[dict],
                     valoraciones: dict) -> ft.Control:
    vivos = [f for f in filas if not es_baja(f) and not es_resumen(f)]
    pendientes = [f for f in vivos if estado_valoracion(f, valoraciones.get(f.get("codigo"), {}))[1] != "verde"]
    pendientes += [f for f in filas if es_resumen(f) and not es_baja(f)
                   and estado_valoracion(f, valoraciones.get(f.get("codigo"), {}))[1] != "neutro"]
    obs_714 = provisional_714(filas_714)
    celdas = [ft.Container(ft.Column([ui.leyenda(e, p, p.texto2, 12),
                                      ft.Text(v, size=17, color=p.texto3 if v == SIN_DATO else p.texto,
                                              weight=ft.FontWeight.W_400 if v == SIN_DATO else ft.FontWeight.W_600,
                                              font_family=ui.FUENTE)], spacing=3, tight=True),
                           padding=12, border_radius=12, bgcolor=p.campo, col={"xs": 6, "md": 3})
              for e, v in resumen_714(filas_714)]
    estado_714 = (filas_714[0].get("estado") if filas_714 else None) or SIN_DATO
    celdas.append(ft.Container(ft.Column([ui.leyenda("Estado del 714", p, p.texto2, 12),
                                          ui.pastilla_estado(estado_714, p)], spacing=6, tight=True),
                               padding=12, border_radius=12, bgcolor=p.campo, col={"xs": 6, "md": 3}))
    notas: list[ft.Control] = []
    if not filas_714:
        notas.append(ui.leyenda(f"Sin cálculo del 714 para {ej}. Sincroniza el expediente para recalcularlo.",
                                p, p.texto3))
    if pendientes:
        n = len(pendientes)
        notas.append(ft.Row([ft.Icon(I.HOURGLASS_BOTTOM, size=15, color=p.naranja),
                             ui.leyenda(f"Estimación: {n} {'activo tiene' if n == 1 else 'activos tienen'} datos o "
                                        "confirmaciones pendientes. El 714 no se da por cerrado.", p, p.naranja)],
                            spacing=6, wrap=True))
    if obs_714:
        notas.append(ft.Row([ft.Icon(I.HOURGLASS_BOTTOM, size=15, color=p.naranja),
                             ui.leyenda(f"714 provisional: {obs_714}", p, p.naranja)], spacing=6, wrap=True))
    if vivos and filas_714 and not pendientes and not obs_714:
        notas.append(ft.Row([ft.Icon(I.CHECK_CIRCLE, size=15, color=p.verde),
                             ui.leyenda("Todos los activos están valorados y revisados por el asesor.", p, p.verde)],
                            spacing=6, wrap=True))
    if presentado_714(filas_714):
        notas.append(ft.Row([ft.Icon(I.LOCK_OUTLINE, size=15, color=p.texto2),
                             ui.leyenda(AVISO_PRESENTADO.format(ej=ej), p, p.texto2)], spacing=6, wrap=True))
    return ui.tarjeta(ft.Column([
        ui.leyenda(f"PATRIMONIO 714 · {ej} · SEGÚN EL ÚLTIMO RECÁLCULO", p, p.texto3, 11),
        ft.ResponsiveRow(celdas, spacing=8, run_spacing=8), *notas,
    ], spacing=10, tight=True), p, padding=16)


def _lista_categoria(app, filas: list[dict], valoraciones: dict, indice: int) -> ft.Control:
    p = app.p
    nombre = CATEGORIAS[indice][0]
    propias = de_categoria(filas, indice)
    activos = [f for f in propias if not es_baja(f) and not es_resumen(f)]
    resumenes = [f for f in propias if es_resumen(f) and not es_baja(f)]
    bajas = [f for f in propias if es_baja(f)]
    bloques: list[ft.Control] = []

    def tarjeta(filas_bloque: list[dict]) -> ft.Control:
        cuerpo: list[ft.Control] = []
        for i, f in enumerate(filas_bloque):
            if i:
                cuerpo.append(ft.Container(ui.separador(p), padding=ft.Padding.only(left=58)))
            cuerpo.append(_fila(app, f, valoraciones.get(f.get("codigo"), {})))
        return ui.tarjeta(ft.Column(cuerpo, spacing=0), p, padding=6)

    if activos:
        bloques.append(tarjeta(activos))
    elif not resumenes:
        bloques.append(ui.tarjeta(ui.vacio(CATEGORIAS[indice][1], f"Sin {nombre.lower()} en {app.ejercicio}",
                                           "Carga el certificado o extracto, o añádelo a mano con «Nuevo activo».",
                                           p), p))
    if resumenes:
        bloques.append(ft.Column([
            ui.leyenda("RESÚMENES DE CARTERA · INFORMATIVOS", p, p.texto3, 11),
            ui.leyenda("El total del resumen no se suma: computan sus posiciones registradas una a una.", p),
            tarjeta(resumenes)], spacing=6, tight=True))
    if bajas:
        bloques.append(ft.Column([ui.leyenda(f"DADOS DE BAJA · {len(bajas)}", p, p.texto3, 11),
                                  ft.Container(tarjeta(bajas), opacity=0.7)], spacing=6, tight=True))
    return ft.Column(bloques, spacing=14, key=f"oa-{indice}")


def _fila(app, f: dict, res: dict) -> ft.Control:
    p = app.p
    codigo = f.get("codigo")
    texto, clave = estado_valoracion(f, res)
    valor, etiqueta = cifra_fila(f, res)
    titulo = " · ".join(x for x in (f.get("entidad"), f.get("descripcion")) if x) or codigo or SIN_DATO
    detalle = detalle_fila(f, res)
    if res.get("regla") and not es_resumen(f) and not es_baja(f):
        detalle += f"\n{res['regla']}"
    if res.get("error"):
        detalle += f"\n{res['error']}"
    if es_resumen(f) and not es_baja(f) and res.get("avisos"):
        detalle += "\n" + " ".join(str(a) for a in res["avisos"])
    if es_baja(f):
        accion = ui.accion_icono(I.UNARCHIVE_OUTLINED, lambda _e, c=codigo: reactivar(app, c), p,
                                 f"Reactivar {codigo}", p.texto2)
    else:
        accion = ui.accion_icono(I.ARCHIVE_OUTLINED, lambda _e, c=codigo: confirmar_baja(app, c), p,
                                 f"Dar de baja {codigo}", p.texto2, peligro=True)
    derecha = [
        ft.Column([ft.Text(valor, size=15, weight=ft.FontWeight.W_600,
                           color=p.texto3 if valor in (SIN_DATO, "—") or es_resumen(f) else p.texto,
                           font_family=ui.FUENTE),
                   ui.leyenda(etiqueta, p, p.texto3, 11)],
                  spacing=0, tight=True, horizontal_alignment=ft.CrossAxisAlignment.END),
        pastilla_valoracion(texto, clave, p),
        ui.accion_icono(I.EDIT_OUTLINED, lambda _e, c=codigo: dialogo_activo(app, c), p, f"Editar {codigo}",
                        p.texto2),
        accion,
    ]
    icono = CATEGORIAS[categoria_de(f.get("tipo"))][1]
    c = ui.fila(icono, p.acento, titulo, detalle, p, derecha=derecha, fondo_icono=p.acento_suave)
    avisos = [a for a in res.get("avisos") or [] if not es_baja(f)]
    c.tooltip = f"{titulo}. {texto}" + (". " + " ".join(avisos) if avisos else "")
    return c


# ------------------------------------------------------------------------------------ normativa del Patrimonio
# Contrato con el controlador (se busca con getattr: puede faltar en versiones antiguas):
#   configuracion_patrimonio(cid, ejercicio) -> {normativa, comunidad, justificacion, confirmado, version,
#       heredada_de: int | None, bonificacion_manual: bool, motor_general: bool, foral: bool}
#   configurar_patrimonio(cid, ejercicio, normativa=, comunidad=, justificacion=, confirmado=, version_esperada=)
#   usar_bonificacion_automatica(cid, ejercicio, version_esperada)
# Las dos escrituras devuelven el resultado estándar (recalculan y exportan). La UI no calcula cuota, escala,
# mínimo ni bonificación: muestra lo que dice el backend. Solo afecta al 714 (ni al 210 ni al 303).
try:
    from normativa_patrimonio import NOMBRES as _NOMBRES_BACKEND
except ImportError:  # versión sin el módulo: solo lo imprescindible
    _NOMBRES_BACKEND = {"ESTATAL": "Estatal (no residente)", "OTRA": "Otra comunidad"}
NOMBRES_NORMATIVA: dict = dict(_NOMBRES_BACKEND)
ETIQUETA_NORMATIVA = NOMBRES_NORMATIVA  # nombre anterior, por compatibilidad
PRIORIDAD_NORMATIVA = ("CATALUNA", "VALENCIANA", "MADRID", "ESTATAL")  # las más usadas en la cartera
FORALES = ("PAIS_VASCO", "NAVARRA")
CONEXION_PROPIA = ("CEUTA", "MELILLA")


def _orden_alfabetico(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(texto).casefold()) if not unicodedata.combining(c))


def opciones_normativa() -> list:
    """(clave, nombre del backend): Cataluña, Valencia, Madrid y Estatal primero; el resto en orden alfabético
    sin acentos; «Otra comunidad» al final."""
    primero = [k for k in PRIORIDAD_NORMATIVA if k in NOMBRES_NORMATIVA]
    resto = sorted((k for k in NOMBRES_NORMATIVA if k not in primero and k != "OTRA"),
                   key=lambda k: _orden_alfabetico(NOMBRES_NORMATIVA[k]))
    final = ["OTRA"] if "OTRA" in NOMBRES_NORMATIVA else []
    return [(k, NOMBRES_NORMATIVA[k]) for k in (*primero, *resto, *final)]


NORMATIVAS = tuple(k for k, _ in opciones_normativa())
EXPLICACION_NORMATIVA = (
    "Un no residente puede optar por la normativa de la comunidad autónoma donde radique el mayor valor de sus "
    "bienes y derechos sujetos en España. No depende del domicilio del asesor ni de la nacionalidad del cliente.")
ALCANCE_NORMATIVA = ("La app solo calcula la normativa que tiene para cada ejercicio; si no la tiene, el 714 queda "
                     "provisional y lo indica. No cubre todas las comunidades ni todos los ejercicios.")
ALCANCE_GUARDADO = ("Se guarda para {ej} y la heredan los ejercicios posteriores en borrador que no tengan elección "
                    "propia. Los ejercicios anteriores y los 714 presentados no cambian. Solo afecta al 714.")
HEREDADA = "Heredada de {anno}"
HEREDADA_ALCANCE = ("Se aplica a {ej} y a los borradores posteriores sin elección propia. «Cambiar» guarda una "
                    "elección propia desde {ej}; {anno} no cambia.")
HEREDADA_DIALOGO = ("Ahora {ej} usa la elección de {anno}. Si guardas un cambio, {ej} tendrá elección propia y "
                    "{anno} no cambia. Si no cambias nada, no se guarda nada.")
SIN_CONFIRMAR_NORMATIVA = "Sin confirmar: confírmala antes de dar por definitiva la cuota del 714."
FORAL_NORMATIVA = ("Régimen foral: la app no lo calcula; requiere puntos de conexión y reglas propias. El 714 queda "
                   "provisional.")
CONEXION_NORMATIVA = "Requiere puntos de conexión y reglas propias que la app no aplica: el 714 queda provisional."
SOLO_714 = "Solo afecta al Patrimonio 714; no cambia el 210 ni el 303."
BONIF_MANUAL = ("La bonificación del 714 de {ej} está fijada a mano: la app no la sustituye mientras no vuelvas a "
                "la automática.")
AVISO_PRESENTADO_NORMATIVA = ("El 714 de {ej} ya está presentado. Cambiar la normativa recalcula y conserva lo "
                              "presentado; la diferencia queda para revisar (posible complementaria).")
SIN_ESCRITURA_NORMATIVA = "Esta versión aún no permite guardar la normativa. No se ha guardado nada."


def _fundido(contenido: ft.Control) -> ft.AnimatedSwitcher:
    return ft.AnimatedSwitcher(contenido, duration=ANIM_CAMBIO, reverse_duration=110,
                               transition=ft.AnimatedSwitcherTransition.FADE,
                               switch_in_curve=ft.AnimationCurve.EASE_OUT_CUBIC,
                               switch_out_curve=ft.AnimationCurve.EASE_OUT_CUBIC)


def motor_disponible(normativa: Optional[str], ejercicio) -> Optional[bool]:
    """Si el backend tiene regla general para esa normativa y ejercicio (consulta, no cálculo). ``None`` si no
    se puede saber. Forales, Ceuta, Melilla y «Otra» nunca cuentan como cubiertas."""
    if normativa in FORALES or normativa in CONEXION_PROPIA or normativa == "OTRA":
        return False
    try:
        import normativa_patrimonio
        return normativa_patrimonio.regla(normativa, int(ejercicio)) is not None
    except Exception:
        return None


def nota_opcion(normativa: Optional[str], ejercicio) -> str:
    if normativa not in NOMBRES_NORMATIVA:
        return "Escribe para buscar o despliega la lista: primero Cataluña, Valencia, Madrid y Estatal."
    if normativa == "OTRA":
        return "Escribe su nombre. La app no la calcula: el 714 queda provisional y pendiente de validación."
    if normativa in FORALES:
        return FORAL_NORMATIVA
    if normativa in CONEXION_PROPIA:
        return CONEXION_NORMATIVA
    motor = motor_disponible(normativa, ejercicio)
    if motor is True:
        return f"La app tiene el cálculo general de esta normativa para {ejercicio}."
    if motor is False:
        return f"La app no tiene esta normativa para {ejercicio}: el 714 queda provisional."
    return "Si la app no tiene esta normativa para el ejercicio, el 714 queda provisional."


def estado_normativa(config: dict) -> tuple[str, str]:
    """(texto, clave). Nunca verde: «Elección guardada» (neutro) dice que el asesor eligió, no que el cálculo
    fiscal esté aprobado. Foral, sin motor general, «Otra» o dato ausente del backend -> provisional (naranja)."""
    c = config or {}
    normativa = c.get("normativa")
    if normativa not in NOMBRES_NORMATIVA:
        return "Sin elegir", "naranja"
    if not c.get("confirmado"):
        return "Sin confirmar", "naranja"
    if normativa == "OTRA":
        return "Elección guardada · pendiente de validación", "naranja"
    if c.get("foral") or normativa in FORALES or c.get("motor_general") is not True:
        return "Elección guardada · cálculo provisional", "naranja"
    return "Elección guardada", "guardada"


def pastilla_normativa(texto: str, clave: str, p: ui.Paleta) -> ft.Container:
    if clave != "guardada":
        return pastilla_valoracion(texto, clave, p)
    c = ui.pastilla(texto, p.texto2, p.campo)  # neutro a propósito: no es un visto bueno fiscal
    c.content = ft.Row([ft.Icon(I.CHECK, size=13, color=p.texto2), c.content], spacing=4, tight=True,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return c


def nombre_normativa(config: dict) -> str:
    normativa = (config or {}).get("normativa")
    if normativa == "OTRA":
        return f"Otra comunidad: {str(config.get('comunidad') or '').strip() or 'sin nombre'}"
    return NOMBRES_NORMATIVA.get(normativa, "Normativa sin elegir")


def _heredada(config: dict) -> Optional[int]:
    anno = (config or {}).get("heredada_de")
    return anno if isinstance(anno, int) and not isinstance(anno, bool) else None


def tarjeta_normativa(app, filas_714: list[dict]) -> ft.Control:
    """Normativa elegida para el cliente y el ejercicio. Se lee en un worker; un resultado de otro
    expediente o de una tarjeta ya sustituida se descarta."""
    p, cid, ej = app.p, app.cid, app.ejercicio
    presentado = presentado_714(filas_714)
    turno = getattr(app, "_oa_normativa_turno", 0) + 1
    app._oa_normativa_turno = turno
    cuerpo = _fundido(ft.Row([ft.ProgressRing(width=14, height=14, stroke_width=2, color=p.texto3),
                              ui.leyenda("Leyendo la normativa…", p, p.texto3)], spacing=8, key="nm-cargando"))
    leer = getattr(app.ctrl, "configuracion_patrimonio", None)

    def aviso(texto: str, color: str, icono) -> ft.Row:
        return ft.Row([ft.Icon(icono, size=15, color=color), ui.leyenda(texto, p, color)], spacing=6, wrap=True)

    def pintar(config: Optional[dict], fallo: Optional[str]) -> None:
        if (getattr(app, "cerrando", False) or getattr(app, "_oa_normativa_turno", None) != turno
                or (app.cid, app.ejercicio) != (cid, ej)):
            return
        if fallo:
            cuerpo.content = ft.Column([aviso(f"No se pudo leer la normativa: {fallo}", p.rojo, I.ERROR_OUTLINE)],
                                       key="nm-error", tight=True)
        else:
            cuerpo.content = _contenido_normativa(app, config or {}, presentado, aviso)
        app.page.update()

    if leer is None:
        cuerpo.content = ft.Column([aviso("Esta versión aún no permite elegir la normativa del Patrimonio. "
                                          "No se ha guardado nada.", p.texto3, I.INFO_OUTLINE)],
                                   key="nm-no-disponible", tight=True)
    else:
        def trabajo() -> None:
            config, fallo = None, None
            try:
                config = dict(leer(cid, ej) or {})
            except Exception as exc:  # nunca un hilo muerto con la tarjeta cargando
                fallo = str(exc) or exc.__class__.__name__
            ejecutar_en_ui(app.page, lambda: pintar(config, fallo))
        try:
            app.page.run_thread(trabajo)
        except Exception as exc:
            pintar(None, str(exc) or exc.__class__.__name__)
    return ui.tarjeta(ft.Column([ui.leyenda(f"NORMATIVA DEL PATRIMONIO · {ej}", p, p.texto3, 11), cuerpo],
                                spacing=10, tight=True), p, padding=16)


def _contenido_normativa(app, config: dict, presentado: bool, aviso) -> ft.Control:
    p, ej = app.p, app.ejercicio
    texto, clave = estado_normativa(config)
    normativa = config.get("normativa")
    justificacion = str(config.get("justificacion") or "").strip()
    conocida = normativa in NOMBRES_NORMATIVA
    confirmada = conocida and bool(config.get("confirmado"))
    anno = _heredada(config)
    derecha: list[ft.Control] = [pastilla_normativa(texto, clave, p)]
    if getattr(app.ctrl, "configurar_patrimonio", None) is not None:
        derecha.append(ui.boton("Cambiar" if confirmada else "Confirmar normativa" if conocida else "Elegir normativa",
                                lambda _e: dialogo_normativa(app, config, presentado), p, I.GAVEL_OUTLINED,
                                tipo="secundario", tooltip=f"Normativa del Patrimonio de {ej}"))
    lineas: list[ft.Control] = [
        ft.Row([ft.Column([ui.cuerpo(nombre_normativa(config), p, peso=ft.FontWeight.W_600),
                           ui.leyenda(justificacion or "Sin justificación del asesor.", p,
                                      p.texto2 if justificacion else p.texto3)],
                          spacing=2, tight=True, expand=True), *derecha],
               spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),  # sin wrap: hay un expand
    ]
    if anno is not None:
        lineas.append(aviso(f"{HEREDADA.format(anno=anno)} · {HEREDADA_ALCANCE.format(ej=ej, anno=anno)}",
                            p.texto2, I.HISTORY))
    lineas.append(ui.leyenda("Comunidad donde radica el mayor valor de los bienes y derechos sujetos en España; no "
                             "la del asesor ni la nacionalidad del cliente. " + SOLO_714, p, p.texto3))
    if conocida and not confirmada:
        lineas.append(aviso(SIN_CONFIRMAR_NORMATIVA, p.naranja, I.HOURGLASS_BOTTOM))
    if confirmada and (config.get("foral") or normativa in FORALES):
        lineas.append(aviso(FORAL_NORMATIVA, p.naranja, I.HOURGLASS_BOTTOM))
    elif confirmada and normativa in CONEXION_PROPIA:
        lineas.append(aviso(CONEXION_NORMATIVA, p.naranja, I.HOURGLASS_BOTTOM))
    if clave != "guardada":
        lineas.append(aviso(ALCANCE_NORMATIVA, p.naranja, I.HOURGLASS_BOTTOM))
    if presentado:
        lineas.append(aviso(AVISO_PRESENTADO_NORMATIVA.format(ej=ej), p.texto2, I.LOCK_OUTLINE))
    elif config.get("bonificacion_manual"):  # presentado: conserva su fila, no se ofrece la acción
        bonificacion = _accion_bonificacion(app, config, aviso)
        if bonificacion is not None:
            lineas.append(bonificacion)
    if getattr(app.ctrl, "configurar_patrimonio", None) is None:
        lineas.append(aviso(SIN_ESCRITURA_NORMATIVA, p.texto3, I.INFO_OUTLINE))
    return ft.Column(lineas, spacing=8, tight=True, key=f"nm-{normativa}-{config.get('version')}")


def _accion_bonificacion(app, config: dict, aviso) -> Optional[ft.Control]:
    """Acción discreta «Volver a bonificación automática»: worker, versión leída y guardas de expediente."""
    p, cid, ej = app.p, app.cid, app.ejercicio
    restaurar_bk = getattr(app.ctrl, "usar_bonificacion_automatica", None)
    if restaurar_bk is None:
        return None
    version = config.get("version")
    estado = {"en_curso": False, "hecho": False}

    def liberar() -> None:
        estado["en_curso"] = False

    def terminar() -> None:
        estado["en_curso"], estado["hecho"] = False, True
        boton.disabled = True

    def fallar(exc: Exception) -> None:
        liberar()
        app.avisar(str(exc) or "No se pudo volver a la bonificación automática. No se ha cambiado nada.", True)

    def restaurar(_e=None) -> None:
        if estado["en_curso"] or estado["hecho"] or app.ocupada():
            return
        if (app.cid, app.ejercicio) != (cid, ej):
            app.avisar("Has cambiado de expediente o ejercicio. No se ha cambiado la bonificación.", True)
            return
        estado["en_curso"] = True
        if not app.en_segundo_plano("Volviendo a la bonificación automática y recalculando el 714…",
                                    lambda: restaurar_bk(cid, ej, version_esperada=version),
                                    "Bonificación automática restaurada. Patrimonio 714 recalculado.",
                                    despues=terminar, al_fallar=fallar):
            liberar()

    boton = ui.accion("Volver a bonificación automática", restaurar, p, I.RESTART_ALT)
    boton.key = "nm-bonificacion-auto"
    return ft.Column([aviso(BONIF_MANUAL.format(ej=ej), p.texto2, I.EDIT_NOTE_OUTLINED), ft.Row([boton])],
                     spacing=4, tight=True)


def _buscable(selector: ft.Control) -> None:
    """Desplegable con búsqueda si la versión de Flet lo trae (sin dependencias nuevas); si no, lista normal."""
    for atributo, valor in (("editable", True), ("enable_filter", True), ("menu_height", 320)):
        if hasattr(selector, atributo):
            try:
                setattr(selector, atributo, valor)
            except Exception:
                pass


def dialogo_normativa(app, config: dict, presentado: bool = False) -> None:
    """Elegir normativa en un desplegable buscable. Cancelar no escribe; guardar un cambio exige justificación y
    confirmación del asesor y envía la versión leída para que el backend rechace un cambio concurrente. Guardar
    sin cambios cierra sin escribir ni volver a preguntar (también si la elección es heredada)."""
    p, cid, ej = app.p, app.cid, app.ejercicio
    escribir = getattr(app.ctrl, "configurar_patrimonio", None)
    if escribir is None:
        app.avisar(SIN_ESCRITURA_NORMATIVA, True)
        return
    config = dict(config or {})
    inicial = config.get("normativa") if config.get("normativa") in NOMBRES_NORMATIVA else None
    anno = _heredada(config)
    estado = {"guardando": False, "cerrado": False, "dlg": None}
    selector = ui.desplegable("Normativa aplicable", opciones_normativa(), p, inicial)
    _buscable(selector)
    nota = ui.leyenda(nota_opcion(inicial, ej), p, p.texto3)
    comunidad = ui.campo("Nombre de la comunidad", p,
                         str(config.get("comunidad") or "") if inicial == "OTRA" else "",
                         "Pendiente de validación: la app no la calcula")
    justificacion = ui.campo("Justificación del asesor", p, str(config.get("justificacion") or ""),
                             "Bienes y derechos sujetos de mayor valor en esa comunidad y su justificante")
    justificacion.multiline, justificacion.min_lines, justificacion.max_lines = True, 2, 5
    # Siempre desmarcada: confirmar es un acto de cada cambio guardado, no un estado heredado.
    confirmacion = ft.Checkbox(label="Confirmo, como asesor, la normativa elegida y su justificación",
                               value=False, active_color=p.acento)
    error = ui.leyenda("", p, p.rojo, 13)
    otra = _fundido(comunidad if inicial == "OTRA" else ft.Container(key="nm-sin-otra"))

    def cambiar(_e=None) -> None:
        otra.content = comunidad if selector.value == "OTRA" else ft.Container(key="nm-sin-otra")
        nota.value = nota_opcion(selector.value, ej)
        error.value = ""
        app.page.update()
    selector.on_select = cambiar

    def bloquear(valor: bool) -> None:
        estado["guardando"] = valor
        comunidad.read_only = justificacion.read_only = valor
        for ctl in (selector, confirmacion, cancelar, confirmar):
            ctl.disabled = valor

    def cerrar(_e=None) -> None:
        if not estado["guardando"] and not estado["cerrado"]:
            estado["cerrado"] = True
            app.cerrar_dialogo(dlg=estado["dlg"])

    def terminar() -> None:
        estado["guardando"] = False
        cerrar()

    def fallar(exc: Exception) -> None:
        if not estado["cerrado"]:
            error.value = (f"{exc} Si otra persona cambió la normativa, cierra y vuelve a abrir."
                           if str(exc) else "No se pudo guardar. No se ha cambiado nada.")
            app.page.update()

    def mostrar(texto: str) -> None:
        error.value = texto
        app.page.update()

    def guardar(_e=None) -> None:
        if estado["guardando"] or estado["cerrado"] or app.ocupada():
            return
        if (app.cid, app.ejercicio) != (cid, ej):
            return mostrar("Vuelve a abrir la normativa en el cliente y ejercicio correctos. No se ha guardado nada.")
        normativa = selector.value
        if normativa not in NOMBRES_NORMATIVA:
            return mostrar("Elige una normativa de la lista.")
        nombre = (str(comunidad.value or "").strip() if normativa == "OTRA"
                  else "" if normativa == "ESTATAL" else NOMBRES_NORMATIVA[normativa])
        motivo = str(justificacion.value or "").strip()
        if normativa == "OTRA" and not nombre:
            return mostrar("Escribe el nombre de la comunidad.")
        sin_cambios = (normativa == config.get("normativa") and config.get("confirmado")
                       and nombre == str(config.get("comunidad") or "").strip()
                       and motivo == str(config.get("justificacion") or "").strip())
        if sin_cambios:  # nada que confirmar: no se vuelve a preguntar ni se escribe
            cerrar()
            return
        if not motivo:
            return mostrar("Escribe la justificación del asesor.")
        if not confirmacion.value:
            return mostrar("Marca la confirmación del asesor para guardar.")
        error.value = ""
        bloquear(True)
        app.page.update()
        version = config.get("version")

        def trabajo():
            try:
                return escribir(cid, ej, normativa=normativa, comunidad=nombre, justificacion=motivo,
                                confirmado=True, version_esperada=version)
            finally:  # si falla, el formulario vuelve a ser editable con lo escrito
                ejecutar_en_ui(app.page, lambda: bloquear(False) if not estado["cerrado"] else None)
        if not app.en_segundo_plano("Guardando la normativa y recalculando el 714…", trabajo,
                                    "Normativa guardada. Patrimonio 714 recalculado.", despues=terminar,
                                    al_fallar=fallar):
            bloquear(False)
            app.page.update()

    cancelar = ui.accion("Cancelar", cerrar, p)
    confirmar = ui.accion("Guardar y recalcular", guardar, p, I.CHECK, "primario")
    contenido = ft.Column([
        *([ft.Row([ft.Icon(I.LOCK_OUTLINE, size=15, color=p.naranja),
                   ui.leyenda(AVISO_PRESENTADO_NORMATIVA.format(ej=ej), p, p.naranja)], spacing=6, wrap=True)]
          if presentado else []),
        *([ft.Row([ft.Icon(I.HISTORY, size=15, color=p.texto2),
                   ui.leyenda(HEREDADA_DIALOGO.format(ej=ej, anno=anno), p, p.texto2)], spacing=6, wrap=True)]
          if anno is not None else []),
        ui.cuerpo(EXPLICACION_NORMATIVA, p, p.texto2),
        selector, nota, otra,
        ft.Row([ft.Icon(I.INFO_OUTLINE, size=15, color=p.texto3),
                ui.leyenda(ALCANCE_GUARDADO.format(ej=ej) + " " + ALCANCE_NORMATIVA, p, p.texto3)],
               spacing=6, wrap=True),
        ui.leyenda("REVISIÓN DEL ASESOR", p, p.texto3, 11),
        justificacion, confirmacion, error,
    ], spacing=12, tight=True, scroll=ft.ScrollMode.AUTO)
    estado["dlg"] = app._dialogo(f"Normativa del Patrimonio · {ej}", contenido, [cancelar, confirmar], ancho=600,
                                 icono=I.GAVEL_OUTLINED)
    if estado["dlg"] is not None:  # para tests y depuración
        estado["dlg"].data = {"selector": selector, "grupo": selector, "nota": nota, "comunidad": comunidad,
                              "justificacion": justificacion, "confirmacion": confirmacion, "error": error,
                              "guardar": guardar, "cerrar": cerrar, "estado": estado, "otra": otra}


# ------------------------------------------------------------------------------------ baja / reactivar
def _guardar(app, codigo: Optional[str], cambios: dict, previo: Optional[dict], texto: str, exito: str,
             despues: Optional[Callable[[], None]] = None, al_fallar=None) -> bool:
    cid, ej = app.cid, app.ejercicio
    return app.en_segundo_plano(texto, lambda: app.ctrl.editar(cid, TABLA, codigo, cambios, ej, esperados=previo),
                                exito, despues=despues, al_fallar=al_fallar)


def confirmar_baja(app, codigo: str) -> None:
    p = app.p
    previo = app.ctrl.lector.get(TABLA, app.cid, codigo)
    if not previo:
        app.avisar("El activo ya no está disponible.", True)
        return
    cid, ej = app.cid, app.ejercicio
    hecho = {"v": False}

    def dar_de_baja(_e):
        if hecho["v"]:
            return
        if (app.cid, app.ejercicio) != (cid, ej):
            app.cerrar_dialogo(_e)
            app.avisar("Has cambiado de expediente o ejercicio. No se ha dado de baja nada.", True)
            return
        hecho["v"] = True
        app.cerrar_dialogo(_e)
        _guardar(app, codigo, {"estado": "Baja"}, previo, "Dando de baja y recalculando…",
                 f"{codigo} dado de baja. Patrimonio recalculado.")
    nombre = previo.get("entidad") or previo.get("descripcion") or codigo
    app._dialogo(f"¿Dar de baja {codigo}?", ft.Column([
        ui.cuerpo(f"{nombre} deja de computar en {ej}. El registro se conserva y puedes reactivarlo.", p, p.texto2),
        *([ui.leyenda(AVISO_PRESENTADO.format(ej=ej), p, p.naranja)]
          if presentado_714(list(app.ctrl.lector.listar("patrimonio_714", cid, ejercicio=ej))) else []),
    ], spacing=10, tight=True),
        [ui.accion("Cancelar", app.cerrar_dialogo, p),
         ui.accion("Dar de baja", dar_de_baja, p, I.ARCHIVE_OUTLINED, "peligro")], ancho=440,
        icono=I.ARCHIVE_OUTLINED)


def reactivar(app, codigo: str) -> None:
    previo = app.ctrl.lector.get(TABLA, app.cid, codigo)
    if not previo:
        app.avisar("El activo ya no está disponible.", True)
        return
    _guardar(app, codigo, {"estado": "Activo"}, previo, "Reactivando y recalculando…",
             f"{codigo} reactivado. Patrimonio recalculado.")


# ------------------------------------------------------------------------------------ diálogo alta / edición
def _texto_inicial(campo: str, valor: Any) -> str:
    if _ausente(valor):
        return ""
    if campo in IMPORTES:
        return ui.eur(valor).replace(" €", "")
    if campo == "pct_titularidad":
        return ui.pct(valor)
    if campo in DECIMALES:
        return str(valor).replace(".", ",")
    return str(valor)


def _normalizar(campo: str, texto: str):
    """Texto del formulario -> valor para la previsualización (mismo normalizador que la BD)."""
    if texto == "":
        return None
    if campo in DECIMALES:
        return decimal_texto(texto)
    import database
    tipo = "pct" if campo == "pct_titularidad" else "eur" if campo in IMPORTES else "txt"
    return database._normalizar(database.Columna(campo, None, tipo), texto)


def dialogo_activo(app, codigo: Optional[str]) -> None:
    """Alta (codigo=None) o corrección. Guarda con ``ctrl.editar(..., esperados=previo)``: si la fila cambió
    entretanto (por ejemplo en el Excel) el backend lo rechaza sin sobrescribir."""
    p, lec = app.p, app.ctrl.lector
    cid, ej = app.cid, app.ejercicio
    previo = dict((lec.get(TABLA, cid, codigo) if codigo else None) or {})
    if codigo and not previo:
        app.avisar("El activo ya no está disponible.", True)
        return
    nuevo = not codigo
    tipo_ini = previo.get("tipo") or CATEGORIAS[getattr(app, "_oa_categoria", 0) % len(CATEGORIAS)][2][0]
    presentado = presentado_714(list(lec.listar("patrimonio_714", cid, ejercicio=ej)))

    tipo = ui.desplegable("Tipo de activo", [(t, ETIQUETA_TIPO[t]) for t in TIPOS], p, tipo_ini, expand=True)
    ubicacion = ui.desplegable("Ubicación fiscal", [(u, ETIQUETA_UBICACION[u]) for u in UBICACIONES], p,
                               previo.get("ubicacion") or "PENDIENTE", expand=True)
    textos: dict[str, ft.TextField] = {}
    for campo in (*TEXTOS, *IMPORTES, *DECIMALES, "pct_titularidad"):
        valor = _texto_inicial(campo, previo.get(campo))
        if nuevo and campo == "pct_titularidad":
            valor = "100 %"
        textos[campo] = ui.campo(ETIQUETA_CAMPO[campo], p, valor, AYUDA_CAMPO.get(campo))
    for campo in ("confirmacion_legal", "observaciones", "motivo_exencion", "motivo_manual"):
        textos[campo].multiline, textos[campo].min_lines, textos[campo].max_lines = True, 1, 4
    switches = {c: ui.interruptor(previo.get(c) in (1, "1", True), None, p) for c in INTERRUPTORES}
    # Solo UI: arranca según haya valor manual, nunca según la protección ``manual_override`` de la BD.
    switches[USAR_MANUAL] = ui.interruptor(not _ausente(previo.get("valor_manual")), None, p)
    originales = {c: ("" if nuevo else str(ctl.value or "").strip()) for c, ctl in textos.items()}
    for c in DECIMALES:  # se comparan ya normalizados, como se guardan
        try:
            originales[c] = decimal_texto(originales[c]) or ""
        except ValueError:
            pass
    originales.update({c: ("" if nuevo else ("1" if switches[c].value else "0")) for c in INTERRUPTORES})
    originales.update({"tipo": "" if nuevo else (previo.get("tipo") or ""),
                       "ubicacion": "" if nuevo else (previo.get("ubicacion") or "")})

    error = ui.leyenda("", p, p.rojo, 13)
    prev_calculado = ft.Text(SIN_DATO, size=17, weight=ft.FontWeight.W_600, color=p.texto, font_family=ui.FUENTE)
    prev_cliente = ft.Text(SIN_DATO, size=17, weight=ft.FontWeight.W_600, color=p.texto, font_family=ui.FUENTE)
    prev_sujeto = ft.Text(SIN_DATO, size=17, weight=ft.FontWeight.W_600, color=p.texto, font_family=ui.FUENTE)
    prev_estado = ft.Container()
    prev_regla = ui.leyenda("", p, p.texto3)
    prev_avisos = ft.Column(spacing=4, tight=True)
    estado = {"version": 0, "guardando": False, "cerrado": False, "dlg": None, "pendiente": None, "activo": False}
    cola = threading.Lock()

    def leer() -> dict[str, str]:
        datos = {c: str(ctl.value or "").strip() for c, ctl in textos.items()}
        datos.update({c: ("1" if s.value else "0") for c, s in switches.items()})
        datos["tipo"], datos["ubicacion"] = tipo.value or "", ubicacion.value or "PENDIENTE"
        return datos

    def fila_prevista(datos: dict[str, str]) -> dict:
        """Fila con los valores del formulario normalizados; ValueError con el campo si alguno no se entiende."""
        fila = {k: v for k, v in previo.items() if not str(k).startswith("_")}
        fila.update(ejercicio=ej, tipo=datos["tipo"], ubicacion=datos["ubicacion"],
                    estado=previo.get("estado") or "Activo")
        for c in textos:
            try:
                fila[c] = _normalizar(c, datos[c])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{ETIQUETA_CAMPO[c]}: {exc}") from exc
        if fila.get("pct_titularidad") is None:
            if not nuevo:  # editar: vaciarla no equivale a 100 % (``cambios`` enviaría "" y el backend lo rechaza)
                raise ValueError(f"{ETIQUETA_CAMPO['pct_titularidad']}: indícala; si es el único titular, "
                                 "escribe 100 %.")
            fila["pct_titularidad"] = 1  # alta: mismo 100 % por defecto que envía ``cambios``
        fila.update({c: int(datos[c]) for c in INTERRUPTORES})
        if datos[USAR_MANUAL] != "1":
            fila["valor_manual"] = fila["motivo_manual"] = None
        return fila

    # ---------------------------------------------------- campos según tipo (fundido corto al cambiar)
    def bloque(*ctls) -> ft.ResponsiveRow:
        return ft.ResponsiveRow([ft.Container(c, col={"xs": 12, "sm": 6}) for c in ctls], spacing=10, run_spacing=12)

    def fila_switch(campo: str, texto: str) -> ft.Row:
        return ft.Row([ui.cuerpo(texto, p), ft.Container(expand=True), switches[campo]],
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def campos_tipo(t: str) -> ft.Control:
        ctls: list[ft.Control] = []
        for c in CAMPOS_TIPO.get(t, ()):
            if c in switches:
                ctls.append(fila_switch(c, "Balance auditado con opinión favorable"))
            else:
                ctls.append(textos[c])
        if "valor_informe" in CAMPOS_TIPO.get(t, ()):
            textos["valor_informe"].label = ("Total de la cartera (€)" if t == "RESUMEN_CARTERA"
                                             else ETIQUETA_CAMPO["valor_informe"])
            textos["valor_informe"].helper = AYUDA_TOTAL.get(t, "Importe total de la posición, no el unitario.")
        if t in UNITARIO_TIPO:
            textos["valor_unitario"].label, textos["valor_unitario"].helper = UNITARIO_TIPO[t]
        filas = [ft.ResponsiveRow([ft.Container(c, col={"xs": 12, "sm": 6}) for c in ctls], spacing=10,
                                  run_spacing=12)]
        if t == "CRIPTO":
            filas.insert(0, ui.leyenda("Total y unidades son datos distintos: escribe el total de la posición en "
                                       "euros, o unidades y valor unitario. Si das los tres, la app los contrasta.",
                                       p, p.texto2))
        return ft.Column([ui.leyenda(f"VALORACIÓN · {ETIQUETA_TIPO.get(t, t).upper()}", p, p.texto3, 11), *filas],
                         spacing=8, tight=True, key=f"campos-{t}")

    especificos = ft.AnimatedSwitcher(campos_tipo(tipo_ini), duration=ANIM_CAMBIO, reverse_duration=110,
                                      transition=ft.AnimatedSwitcherTransition.FADE,
                                      switch_in_curve=ft.AnimationCurve.EASE_OUT_CUBIC,
                                      switch_out_curve=ft.AnimationCurve.EASE_OUT_CUBIC)
    motivo_exencion = ft.Container(textos["motivo_exencion"], visible=bool(switches["exento"].value))
    manual = ft.Container(bloque(textos["valor_manual"], textos["motivo_manual"]),
                          visible=bool(switches[USAR_MANUAL].value))
    aviso_manual = ui.leyenda("Al desactivarlo se borra el valor manual y su motivo al guardar.", p, p.texto3)
    aviso_manual.visible = False

    # ---------------------------------------------------- previsualización en un único worker
    def pintar(version: int, res: dict, fallo: Optional[str]) -> None:
        if (estado["cerrado"] or getattr(app, "cerrando", False) or version != estado["version"]
                or (app.cid, app.ejercicio) != (cid, ej)):
            return  # resultado viejo o de otro expediente: se descarta
        if fallo:
            res = {"error": fallo, "avisos": [], "provisional": True}
        texto_estado, clave = estado_valoracion({"tipo": tipo.value, "estado": previo.get("estado"),
                                                 "revision_confirmada": int(bool(switches["revision_confirmada"].value))},
                                                res)
        prev_estado.content = pastilla_valoracion(texto_estado, clave, p)
        if res.get("error"):
            prev_calculado.value = prev_cliente.value = prev_sujeto.value = SIN_DATO
            prev_regla.value, prev_regla.color = res["error"], p.rojo
        else:
            prev_calculado.value = importe(res.get("valor_calculado"))
            prev_cliente.value = importe(res.get("valor_cliente"))
            prev_sujeto.value = ("Pendiente" if ubicacion.value == "PENDIENTE"
                                 else importe(res.get("valor_sujeto")))
            prev_regla.value, prev_regla.color = res.get("regla") or "", p.texto3
        prev_avisos.controls = [ft.Row([ft.Icon(I.WARNING_AMBER_ROUNDED, size=15, color=p.naranja),
                                        ui.leyenda(str(a), p, p.naranja)], spacing=6, wrap=True)
                                for a in res.get("avisos") or []]
        app.page.update()

    def calcular() -> None:
        while True:
            with cola:
                solicitud, estado["pendiente"] = estado["pendiente"], None
                if solicitud is None or estado["cerrado"] or getattr(app, "cerrando", False):
                    estado["activo"] = False
                    return
            version, fila = solicitud
            res, fallo = {}, None
            try:
                res = valoracion(fila)
            except Exception as exc:  # nunca un hilo muerto con el diálogo esperando
                fallo = str(exc) or exc.__class__.__name__
            try:
                ejecutar_en_ui(app.page, lambda v=version, r=res, f=fallo: pintar(v, r, f))
            except Exception:
                with cola:
                    estado["activo"], estado["pendiente"] = False, None
                raise

    def previsualizar(_e=None) -> None:
        if estado["cerrado"] or estado["guardando"]:
            return
        estado["version"] += 1
        try:
            fila = fila_prevista(leer())
        except ValueError as exc:
            pintar(estado["version"], {}, str(exc))
            return
        prev_regla.value, prev_regla.color = "Calculando…", p.texto3
        app.page.update()
        with cola:
            estado["pendiente"] = (estado["version"], fila)
            if estado["activo"]:
                return
            estado["activo"] = True
        try:
            app.page.run_thread(calcular)
        except Exception:
            with cola:
                estado["activo"], estado["pendiente"] = False, None
            raise

    def cambiar_tipo(_e=None) -> None:
        especificos.content = campos_tipo(tipo.value or tipo_ini)
        previsualizar()

    def cambiar_switch(_e=None) -> None:
        motivo_exencion.visible = bool(switches["exento"].value)
        manual.visible = bool(switches[USAR_MANUAL].value)
        aviso_manual.visible = (not manual.visible and not nuevo
                                and not _ausente(previo.get("valor_manual")))
        previsualizar()

    tipo.on_select = cambiar_tipo
    ubicacion.on_select = previsualizar
    for c in (*INTERRUPTORES, USAR_MANUAL):
        switches[c].on_change = cambiar_switch
    for c in (*IMPORTES, *DECIMALES, "pct_titularidad"):
        textos[c].on_blur = textos[c].on_submit = previsualizar

    # ---------------------------------------------------- guardar
    def cambios() -> dict:
        actuales = leer()
        if actuales.pop(USAR_MANUAL) != "1":  # el interruptor no es columna; manual_override lo fija el backend
            actuales["valor_manual"] = actuales["motivo_manual"] = ""
        for c in DECIMALES:  # se guardan con punto, sin redondear
            actuales[c] = decimal_texto(actuales[c]) or ""
        if nuevo:
            # Solo lo escrito y los valores por defecto del contrato; un interruptor apagado no se envía.
            datos = {k: v for k, v in actuales.items() if v != "" and not (k in INTERRUPTORES and v == "0")}
            datos.setdefault("pct_titularidad", "100 %")
            datos.update(ejercicio=ej, estado="Activo", origen="app",
                         revision_confirmada=int(actuales["revision_confirmada"]))
        else:
            datos = {k: v for k, v in actuales.items() if v != originales.get(k, "")}
        for c in INTERRUPTORES:
            if c in datos:
                datos[c] = int(datos[c])
        return datos

    def bloquear(valor: bool) -> None:
        estado["guardando"] = valor
        for ctl in textos.values():
            ctl.read_only = valor
        for ctl in (tipo, ubicacion, *switches.values(), cancelar, confirmar):
            ctl.disabled = valor

    def cerrar(_e=None) -> None:
        if not estado["guardando"] and not estado["cerrado"]:
            estado["cerrado"] = True
            app.cerrar_dialogo(dlg=estado["dlg"])

    def terminar() -> None:
        estado["guardando"] = False
        cerrar()

    def guardar(_e=None) -> None:
        if estado["guardando"] or estado["cerrado"] or app.ocupada():
            return
        if (app.cid, app.ejercicio) != (cid, ej):
            error.value = "Vuelve a abrir el activo en el cliente y ejercicio correctos."
            app.page.update()
            return
        try:
            datos = cambios()
            fila = fila_prevista(leer())
        except ValueError as exc:
            error.value = str(exc)
            app.page.update()
            return
        if not datos:
            cerrar()
            return
        modulo = backend()
        if modulo is not None:  # misma validación que hará el backend, mostrada junto al formulario
            try:
                modulo.validar(fila)
            except ValueError as exc:
                error.value = str(exc)
                app.page.update()
                return
        error.value = ""
        bloquear(True)
        app.page.update()

        def trabajo():
            try:
                return app.ctrl.editar(cid, TABLA, codigo, datos, ej, esperados=previo if codigo else None)
            finally:  # si falla, el formulario vuelve a ser editable con lo escrito
                ejecutar_en_ui(app.page, lambda: bloquear(False) if not estado["cerrado"] else None)
        if not app.en_segundo_plano("Guardando activo y recalculando el 714…", trabajo,
                                    "Activo guardado. Patrimonio 714 recalculado.", despues=terminar):
            bloquear(False)
            app.page.update()

    cancelar = ui.accion("Cancelar", cerrar, p)
    confirmar = ui.accion("Guardar y recalcular", guardar, p, I.CHECK, "primario")
    textos["entidad"].autofocus = True

    def cifra(etiqueta: str, ctl: ft.Text) -> ft.Container:
        return ft.Container(ft.Column([ui.leyenda(etiqueta, p), ctl], spacing=2, tight=True),
                            col={"xs": 12, "sm": 4})

    previa = ft.Container(ft.Column([
        ft.Row([ui.leyenda(f"VALORACIÓN DE LA APP · {ej} · SIN GUARDAR", p, p.texto3, 11),
                ft.Container(expand=True), prev_estado], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ft.ResponsiveRow([cifra("Valor de la posición", prev_calculado),
                          cifra("Del cliente (× titularidad)", prev_cliente),
                          cifra("Sujeto en España", prev_sujeto)], spacing=10, run_spacing=8),
        prev_regla, prev_avisos,
    ], spacing=8, tight=True), padding=14, border_radius=12, bgcolor=p.campo)

    contenido = ft.Column([
        *([ft.Row([ft.Icon(I.LOCK_OUTLINE, size=15, color=p.naranja),
                   ui.leyenda(AVISO_PRESENTADO.format(ej=ej), p, p.naranja)], spacing=6, wrap=True)]
          if presentado else []),
        bloque(tipo, ubicacion),
        bloque(textos["entidad"], textos["identificador"]),
        bloque(textos["descripcion"], textos["pct_titularidad"]),
        especificos,
        textos["cartera_id"],
        ui.leyenda("EXENCIÓN Y AJUSTES", p, p.texto3, 11),
        fila_switch("exento", "Exento"), motivo_exencion,
        fila_switch(USAR_MANUAL, "Usar un valor manual del asesor"), manual, aviso_manual,
        ui.leyenda("REVISIÓN DEL ASESOR", p, p.texto3, 11),
        textos["confirmacion_legal"],
        fila_switch("revision_confirmada", "Ubicación, exención y valoración confirmadas"),
        textos["observaciones"],
        previa, error,
    ], spacing=12, tight=True)
    titulo = f"Corregir activo · {codigo}" if codigo else "Nuevo activo"
    estado["dlg"] = app._dialogo(titulo, contenido, [cancelar, confirmar], ancho=760,
                                 icono=I.ACCOUNT_BALANCE_WALLET_OUTLINED)
    previsualizar()
    # Para tests y depuración: acceso a los controles sin recorrer el árbol.
    if estado["dlg"] is not None:
        estado["dlg"].data = {"textos": textos, "switches": switches, "tipo": tipo, "ubicacion": ubicacion,
                              "guardar": guardar, "cerrar": cerrar, "error": error, "estado": estado,
                              "prev_cliente": prev_cliente, "prev_estado": prev_estado, "especificos": especificos}
