"""Contabilidad a Medida — aplicación de escritorio (Flet) con diseño estilo Apple.

Ejecutar:  .venv\\Scripts\\python.exe main.py          (ventana de escritorio)
           .venv\\Scripts\\python.exe main.py --web    (en el navegador, para revisar el diseño)

La lógica vive en ``controlador.py`` (sin interfaz). Aquí solo hay pantallas, diálogos y el vigilante de la
bandeja ``_ARRASTRA_AQUI``. Todo el trabajo pesado corre fuera del hilo de la interfaz.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import flet as ft

import ui_estilo as ui
import ui_adjuntos as adj
from concurrencia import ejecutar_en_ui
from version import VERSION
from app_paths import AlmacenNoDisponible
from ui_actualizaciones import AvisoActualizacion
import updater
import gastos as reglas_gastos
import carteras
from carteras import CARTERAS, ETIQUETA, NR, RES, cartera_de

__all__ = ["Controlador", "CanceladoPorUsuario", "App", "main"]
RAIZ = Path(__file__).resolve().parent
ICONO_ICO = RAIZ / "assets" / "icono.ico"


def _cargar_modulos() -> None:
    """Importa la lógica (openpyxl, PyMuPDF, SQLite...). Al ejecutar la app se hace DESPUÉS de pintar la
    pantalla de carga, así la ventana aparece antes. Al importar main desde otro módulo, se hace ya."""
    global database, excel_generator, ia_extractor, pdf_processor, tax_calculator, valores_inmuebles
    global CALCULADOS, CAMPOS_PRESENTACION, OPCIONES, CanceladoPorUsuario, Controlador, campos_editables, etiquetas
    global TABLAS, a_fecha
    import database
    import excel_generator
    import ia_extractor
    import pdf_processor
    import tax_calculator
    import valores_inmuebles
    from controlador import (CALCULADOS, CAMPOS_PRESENTACION, OPCIONES, CanceladoPorUsuario, Controlador,
                             campos_editables, etiquetas)
    from database import TABLAS, a_fecha


if __name__ != "__main__":
    _cargar_modulos()

I = ft.Icons
# Etiquetas legibles (con tildes) de los campos del diálogo de revisión; el resto se derivan de la clave
ETIQUETAS_EXTRACCION = {
    "direccion": "Dirección", "cp": "Código postal", "ref_catastral": "Ref. catastral",
    "fecha_adquisicion": "Fecha de adquisición", "precio_escritura": "Precio de escritura",
    "pct_titularidad": "% titularidad", "tipo_adquisicion": "Tipo de adquisición", "n_protocolo": "Nº de protocolo",
    "valor_referencia": "Valor de referencia", "itp_iva_ajd": "ITP / IVA / AJD", "notaria": "Notaría",
    "valor_catastral": "Valor catastral", "valor_suelo": "Valor del suelo", "valor_construccion": "Valor de construcción",
    "anio_revision": "Año de revisión", "ibi_ejercicio": "Cuota IBI", "fecha_inicio": "Fecha de inicio",
    "fecha_fin_prevista": "Fecha de fin prevista", "renta_inicial": "Renta mensual", "nrc": "NRC",
    "fecha_presentacion": "Fecha de presentación", "pais": "País", "fecha_certificado": "Fecha del certificado",
    "valido_hasta": "Válido hasta", "nif_extranjero": "NIF extranjero", "epigrafe_iae": "Epígrafe IAE",
    "descripcion": "Descripción", "csv": "CSV", "tasa_basura": "Tasa de basuras", "categoria": "Categoría",
    "nif_proveedor": "NIF del proveedor", "n_factura": "Nº de factura", "periodo": "Periodo",
    "periodo_desde": "Periodo desde", "periodo_hasta": "Periodo hasta", "iva": "IVA",
}
SECCIONES = ("Resumen", "Pendientes", "Inmuebles", "Alquileres", "Imputación", "Gastos", "Documentos", "Asistente IA")
SECCIONES_RES = ("Resumen", "Pendientes", "Emitidas", "Recibidas", "IVA 303", "IRPF 130",
                 "Retenciones", "Documentos", "Asistente IA")


def _abrir_en_sistema(ruta: Path) -> None:
    if hasattr(os, "startfile") and Path(ruta).exists():
        os.startfile(str(ruta))  # type: ignore[attr-defined]


def _admite_comentario(fn: Callable) -> bool:
    """True si ``procesar_pdf`` acepta ``comentario_guia`` (o **kwargs). Un controlador antiguo o un doble de
    test sin el parámetro sigue funcionando sin comentario."""
    try:
        parametros = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(x.name == "comentario_guia" or x.kind is inspect.Parameter.VAR_KEYWORD for x in parametros)


def _texto_valor(col: database.Columna, valor: Any) -> str:
    """Valor de BD -> texto legible en un campo editable."""
    if valor in (None, ""):
        return ""
    if col.tipo == "date":
        f = a_fecha(valor)
        return f.strftime("%d/%m/%Y") if f else str(valor)
    if col.tipo == "eur":
        return ui.eur(valor).replace(" €", "")
    if col.tipo == "pct":
        return ui.pct(valor)
    return str(valor)


class App:
    def __init__(self, page: ft.Page, ctrl: Optional[Controlador] = None):
        self.page = page
        self.ctrl = ctrl or Controlador()
        self.p = ui.OSCURO if page.platform_brightness == ft.Brightness.DARK else ui.CLARO
        self.cid: Optional[int] = None
        self.ejercicio = date.today().year
        self.informe: Optional[tax_calculator.InformeFiscal] = None
        self.estados_aut: list[str] = []
        self.seccion = 0
        self.cartera = NR
        self._filtros = {c: "" for c in CARTERAS}       # búsqueda recordada por cartera
        self._paginas = {c: 0 for c in CARTERAS}        # página de la lista por cartera
        self._cid_cartera: dict[str, Optional[int]] = {c: None for c in CARTERAS}
        self.filtro = ""
        self._pagina_clientes = 0
        self._busqueda_version = 0
        self._paginas_detalle = {}
        self.ocupado = False
        self.cerrando = False
        self._evento_dialogo: Optional[threading.Event] = None
        self._turno = threading.Lock()  # una sola tarea de fondo a la vez (A4-P03)
        self.conversaciones: dict = {}  # cid -> asistente.Conversacion (vive mientras la app está abierta)
        import chat_correcciones
        self.correcciones_chat = chat_correcciones.GestorCorrecciones(lambda: (self.cid, self.ejercicio))
        self._chat_ocupado = False
        self._chat_en_curso: Optional[dict] = None
        self._columna_ficha: Optional[ft.Column] = None
        self._update_lock = threading.Lock()
        self._update_cancel = threading.Event()
        self._update_publicacion = None
        self._update_ruta = None
        self.aviso_actualizacion = AvisoActualizacion(
            self.p, self.comprobar_actualizacion, self.actualizar_aplicacion, self.cancelar_actualizacion)
        self.aviso_actualizacion.mostrar("reposo", VERSION)
        self.file_picker = ft.FilePicker()
        page.services.append(self.file_picker)

    # ================================================================== arranque
    def iniciar(self, contenedor: Optional[ft.Stack] = None) -> None:
        """Construye la interfaz. Si se da ``contenedor`` (Stack con la pantalla de carga encima), la app se
        coloca debajo; si no, se añade a la página."""
        pg, p = self.page, self.p
        preparar_pagina(pg, p)
        if pg.window:
            pg.window.prevent_close = True
            pg.window.on_event = self._evento_ventana
        pg.on_keyboard_event = self._atajo
        pg.on_platform_brightness_change = self._cambio_tema

        self.barra_progreso = ft.ProgressBar(opacity=0, color=p.acento, bgcolor="transparent", bar_height=2,
                                             animate_opacity=ft.Animation(200, ft.AnimationCurve.EASE_OUT_CUBIC))
        self.lista_clientes = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True,
                                        animate_opacity=ft.Animation(120, ft.AnimationCurve.EASE_OUT_CUBIC))
        self.lbl_carpeta = ui.leyenda(self.ctrl.lector.carpeta_clientes.name, p, p.texto3)
        self.lbl_carpeta.max_lines, self.lbl_carpeta.overflow = 1, ft.TextOverflow.ELLIPSIS
        self.lbl_carpeta.tooltip = str(self.ctrl.lector.carpeta_clientes)
        self.lbl_estado = ui.leyenda("", p, p.texto2)
        self.contenido = ft.AnimatedSwitcher(content=ft.Container(), duration=150, reverse_duration=90,
                                             transition=ft.AnimatedSwitcherTransition.FADE,
                                             switch_in_curve=ft.AnimationCurve.EASE_OUT_CUBIC,
                                             switch_out_curve=ft.AnimationCurve.EASE_OUT_CUBIC)
        self.zona_principal = ft.Container(expand=True)

        lec = self.ctrl.lector
        self.cartera = RES if not lec.contar_clientes(NR) and lec.contar_clientes(RES) else NR
        clientes = lec.listar_clientes(limite=1, cartera=self.cartera)
        if clientes:
            self.cid = clientes[0]["id"]
        raiz = ft.Column([
            self.barra_progreso,
            ft.Row([self._barra_lateral(), self.zona_principal], spacing=0, expand=True,
                   vertical_alignment=ft.CrossAxisAlignment.STRETCH),
        ], spacing=0, expand=True)
        self._raiz = raiz
        if contenedor is not None:
            contenedor.controls.insert(0, raiz)
        else:
            pg.add(raiz)
        self._refrescar_clientes()
        if self.cid:
            self.abrir_cliente(self.cid)
        else:
            self._pintar_principal()
        threading.Thread(target=self._vigilante_bandeja, daemon=True).start()
        if getattr(sys, "frozen", False):
            self.comprobar_actualizacion(automatica=True)
        migracion = getattr(self.ctrl, "migracion", None) or {}
        if getattr(self.ctrl, "aviso_migracion", None):
            self.avisar(self.ctrl.aviso_migracion, True)
        elif migracion.get("movidos"):
            self.avisar(f"Se ordenaron {migracion['movidos']} expedientes en No residentes / Residentes. "
                        "Copia de la base de datos en la carpeta backups.")

    async def _atajo(self, e) -> None:
        """Ctrl+N: nuevo cliente · Ctrl+O: elegir PDFs. Solo con la app libre (sin tareas ni cierre)."""
        if not getattr(e, "ctrl", False) or self.cerrando or self._turno.locked():
            return
        tecla = (e.key or "").upper()
        if tecla == "N":
            self.dialogo_nuevo_cliente()
        elif tecla == "O" and self.cid:
            await self.elegir_pdfs()

    async def _evento_ventana(self, e) -> None:
        # Cierre ordenado (A3-P2): libera un diálogo de revisión en espera y cierra la BD
        if e.type == ft.WindowEventType.CLOSE:
            self.cerrando = True
            if getattr(self, "_update_cancel", None):
                self._update_cancel.set()
            if getattr(self, "correcciones_chat", None):
                self.correcciones_chat.descartar()
                if getattr(self, 'acciones_documentales', None):
                    self.acciones_documentales.descartar()
            if self._evento_dialogo:
                self._evento_dialogo.set()
            # No se espera a una tarea larga (IA, exportación): si la BD está ocupada se cierra el proceso
            # igualmente; SQLite confirma cada escritura, así que no queda nada a medias.
            if self.ctrl._lock.acquire(blocking=False):
                try:
                    self.ctrl.cerrar()
                finally:
                    self.ctrl._lock.release()
            await self.page.window.destroy()

    def _cambio_tema(self, _e=None) -> None:
        """El sistema cambió entre claro y oscuro: se repinta con la otra paleta sin perder cliente ni pestaña."""
        p = ui.OSCURO if self.page.platform_brightness == ft.Brightness.DARK else ui.CLARO
        if p is self.p or self.cerrando or getattr(self, "_raiz", None) is None:
            return
        self.p = p
        preparar_pagina(self.page, p)
        self.barra_progreso.color = p.acento
        self.lbl_estado.color = p.texto2
        self.aviso_actualizacion.cambiar_paleta(p)
        self._raiz.controls[1].controls[0] = self._barra_lateral()
        self.buscar.value = self.filtro
        self._refrescar_clientes()
        self._pintar_principal()

    # ================================================================== actualizaciones
    def _mostrar_actualizacion(self, estado, **kwargs):
        def mostrar():
            if not self.cerrando:
                self.aviso_actualizacion.mostrar(estado, **kwargs)
                self.page.update()
        ejecutar_en_ui(self.page, mostrar)

    def comprobar_actualizacion(self, automatica=False):
        if self.cerrando or not self._update_lock.acquire(blocking=False):
            return
        self._mostrar_actualizacion("comprobando", version=VERSION)
        def tarea():
            try:
                publicacion = updater.comprobar()
                updater.descartar_descarga(self._update_ruta)
                self._update_publicacion = publicacion
                self._update_ruta = None
                self._mostrar_actualizacion("disponible" if publicacion else "actual",
                                             version=publicacion.version if publicacion else VERSION)
                if not publicacion:
                    t = threading.Timer(3.0, self._volver_a_reposo)
                    t.daemon = True
                    t.start()
            except Exception:
                self._mostrar_actualizacion("reposo" if automatica else "error", version=VERSION)
            finally:
                self._update_lock.release()
        try:
            self.page.run_thread(tarea)
        except Exception:
            self._update_lock.release()
            self._mostrar_actualizacion("error")

    def _volver_a_reposo(self) -> None:
        if getattr(self.aviso_actualizacion, "estado", None) == "actual":
            self._mostrar_actualizacion("reposo", version=VERSION)

    def cancelar_actualizacion(self):
        if self.aviso_actualizacion.estado == "descargando":
            self._update_cancel.set()
        elif self.aviso_actualizacion.estado in ("disponible", "listo"):
            self._mostrar_actualizacion("reposo", version=VERSION)

    async def actualizar_aplicacion(self):
        if self.cerrando or self._update_publicacion is None:
            return
        if self._update_ruta is not None:
            # Adquiere el mismo turno que importación, IA, edición y exportación.
            if not self._turno.acquire(blocking=False):
                self.avisar("Espera a que termine la tarea en curso antes de instalar.")
                return
            if not self._update_lock.acquire(blocking=False):
                self._turno.release()
                return
            auxiliar = None
            cierre_iniciado = False
            ruta_bd = self.ctrl.lector.db_path
            try:
                self._mostrar_actualizacion("instalando", version=self._update_publicacion.version)
                auxiliar = await asyncio.to_thread(updater.preparar_instalacion, self._update_ruta, self._update_publicacion,
                                                 db_path=self.ctrl.lector.db_path)
                self.cerrando = True
                self._update_cancel.set()
                if self._evento_dialogo:
                    self._evento_dialogo.set()
                cierre_iniciado = True
                await asyncio.to_thread(self.ctrl.cerrar)
                await self.page.window.destroy()
            except Exception:
                if auxiliar is not None and auxiliar.poll() is None:
                    auxiliar.terminate()
                if cierre_iniciado:
                    # Si el cierre de ventana falla, recupera conexiones antes de habilitar la UI.
                    try:
                        self.ctrl = await asyncio.to_thread(Controlador, ruta_bd)
                    except Exception:
                        self.cerrando = True
                        return
                self.cerrando = False
                self._mostrar_actualizacion("error")
            finally:
                self._update_lock.release()
                self._turno.release()
            return
        if not self._update_lock.acquire(blocking=False):
            return
        self._update_cancel.clear()
        publicacion = self._update_publicacion
        self._mostrar_actualizacion("descargando", version=publicacion.version, progreso=0)
        def tarea():
            ultima = [0.0]
            def progreso(valor):
                ahora = time.monotonic()
                if ahora - ultima[0] >= 0.15 or valor == 1:
                    ultima[0] = ahora
                    self._mostrar_actualizacion("descargando", version=publicacion.version, progreso=valor)
            try:
                ruta = updater.descargar(publicacion, progreso, self._update_cancel, db_path=self.ctrl.lector.db_path)
                self._update_ruta = ruta
                self._mostrar_actualizacion("listo", version=publicacion.version)
            except updater.DescargaCancelada:
                self._mostrar_actualizacion("disponible", version=publicacion.version)
            except Exception:
                self._mostrar_actualizacion("error")
            finally:
                self._update_lock.release()
        try:
            self.page.run_thread(tarea)
        except Exception:
            self._update_lock.release()
            self._mostrar_actualizacion("error")

    # ================================================================== utilidades
    def avisar(self, texto: str, error: bool = False) -> None:
        p = self.p
        self.page.show_dialog(ft.SnackBar(
            content=ft.Row([ft.Icon(I.ERROR_OUTLINE if error else I.CHECK_CIRCLE_OUTLINE, color="#FFFFFF", size=18),
                            ft.Text(texto, color="#FFFFFF", size=13, expand=True)], spacing=10),
            bgcolor=p.rojo if error else "#E61D1D1F", behavior=ft.SnackBarBehavior.FLOATING,
            shape=ft.RoundedRectangleBorder(radius=12), width=520, duration=4000))

    def set_ocupado(self, activo: bool, texto: str = "") -> None:
        self.ocupado = activo
        self.barra_progreso.opacity = 1 if activo else 0
        self.lbl_estado.value = texto if activo else ""
        self.page.update()

    def ocupada(self) -> bool:
        """True (y avisa) si ya hay una tarea en marcha: las acciones nuevas esperan a que termine."""
        if self._turno.locked() or self.cerrando:
            if not self.cerrando:
                self.avisar("Espera a que termine la tarea en curso.")
            return True
        return False

    def en_segundo_plano(self, texto: str, trabajo: Callable[[], dict], exito: Optional[str] = None,
                         despues: Optional[Callable[[], None]] = None) -> bool:
        """Ejecuta ``trabajo`` fuera del hilo de UI, aplica su informe y repinta. Errores -> aviso legible.
        Devuelve False si había otra tarea en marcha (no se encola: el usuario lo repite al terminar)."""
        if self.cerrando or not self._turno.acquire(blocking=False):
            if not self.cerrando:
                self.avisar("Espera a que termine la tarea en curso.")
            return False
        cid, ej = self.cid, self.ejercicio

        def terminar(res, error):
            try:
                if self.cerrando:
                    return
                if error is None:
                    if despues is not None:
                        despues()
                    if (self.cid, self.ejercicio) == (cid, ej):
                        self._aplicar_resultado(res)
                    if res.get("avisos_documentales"):
                        self.avisar(" ".join(res["avisos_documentales"]), error=True)
                    if res.get("error_excel"):
                        self._dialogo_excel_bloqueado(res["error_excel"])
                    elif exito and not res.get("avisos_documentales"):
                        self.avisar(exito)
                elif isinstance(error, CanceladoPorUsuario):
                    self.avisar("Documento descartado.")
                elif isinstance(error, excel_generator.ExcelConflictoImportacion):
                    mensaje = ui.cuerpo(str(error), self.p)
                    mensaje.selectable = True
                    entendido = ui.accion("Entendido", self.cerrar_dialogo, self.p, tipo="primario")
                    entendido.autofocus = True
                    self._dialogo("Revisar cambios del Excel", mensaje, [entendido], icono=I.INFO_OUTLINE)
                else:
                    self.avisar(str(error) or error.__class__.__name__, error=True)
            finally:
                self._turno.release()
                if not self.cerrando:
                    self.set_ocupado(False)
                    self._pintar_principal()

        def tarea():
            res, error = {}, None
            try:
                if not self.cerrando:
                    ejecutar_en_ui(self.page, lambda: self.set_ocupado(True, texto) if not self.cerrando else None)
                    res = trabajo() or {}
            except Exception as exc:
                error = exc
            try:
                # Mantiene el turno hasta aplicar el resultado en el bucle de UI.
                ejecutar_en_ui(self.page, lambda: terminar(res, error))
            except Exception:
                # La sesión pudo desaparecer antes de aceptar la notificación.
                if self._turno.locked():
                    self._turno.release()
                raise
        try:
            self.page.run_thread(tarea)
        except Exception:
            self._turno.release()
            raise
        return True

    def _aplicar_resultado(self, res: dict) -> None:
        if res.get("documentos_actualizados"):
            cid = res.get("cliente_documentos", self.cid)
            if not res.get('conversacion_documental_actualizada'):
                self.conversaciones.pop(cid, None)
            if not res.get('conversacion_documental_actualizada') and getattr(self, "correcciones_chat", None):
                self.correcciones_chat.descartar(cid)
            if not res.get('conversacion_documental_actualizada') and getattr(self, 'acciones_documentales', None):
                self.acciones_documentales.descartar()
        if res.get("informe") is not None:
            self.informe = res["informe"]
            self.estados_aut = res.get("estados_autonomo", [])

    def _dialogo(self, titulo: str, contenido: ft.Control, acciones: list[ft.Control], ancho: int = 520,
                 icono=None) -> ft.AlertDialog:
        p = self.p
        cabecera = ft.Row([ft.Icon(icono, color=p.acento, size=22)] if icono else [], spacing=10)
        cabecera.controls.append(ui.seccion(titulo, p, 19))
        if isinstance(contenido, ft.Column):  # los campos ocupan todo el ancho del diálogo
            contenido.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
            for control in contenido.controls:  # un Dropdown no se estira solo
                if isinstance(control, ft.Dropdown) and not control.width and not control.expand:
                    control.width = ancho
        # Un solo contenedor con scroll; las acciones quedan siempre fuera de él.
        if isinstance(contenido, ft.Column):
            contenido.scroll = None
        dlg = ft.AlertDialog(modal=True, title=cabecera, bgcolor=p.tarjeta, scrollable=True,
                             shape=ft.RoundedRectangleBorder(radius=18),
                             content=ft.Container(contenido, width=ancho), actions=acciones,
                             actions_alignment=ft.MainAxisAlignment.END)
        ejecutar_en_ui(self.page, lambda: self.page.show_dialog(dlg) if not self.cerrando else None)
        return dlg

    def cerrar_dialogo(self, _e=None) -> None:
        self.page.pop_dialog()

    def _dialogo_excel_bloqueado(self, mensaje: str) -> None:
        self._dialogo("Excel abierto en otra aplicación",
                      ui.cuerpo("Cierra el Excel del cliente y pulsa Sincronizar. Los datos están guardados.\n\n"
                                + mensaje, self.p, self.p.texto2),
                      [ui.boton("Entendido", self.cerrar_dialogo, self.p)], icono=I.LOCK_OUTLINE)

    @property
    def cliente(self) -> Optional[dict]:
        return self.ctrl.lector.get_cliente(self.cid) if self.cid else None

    # ================================================================== barra lateral
    def _etiquetas_cartera(self) -> list[ft.Control]:
        p, lec = self.p, self.ctrl.lector
        cantidades = {c: lec.contar_clientes(c) for c in CARTERAS}
        return [ft.Container(ft.Column([
            ft.Text(ETIQUETA[c], size=12, weight=ft.FontWeight.W_600, color=p.texto, font_family=ui.FUENTE),
            ft.Text(f"{cantidades[c]} {'cliente' if cantidades[c] == 1 else 'clientes'}", size=10, color=p.texto2, font_family=ui.FUENTE),
        ], spacing=0, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=6, vertical=3)) for c in CARTERAS]

    def _refrescar_contadores(self) -> None:
        if getattr(self, "selector_cartera", None) is not None:
            self.selector_cartera.controls = self._etiquetas_cartera()

    def _cambiar_cartera(self, e) -> None:
        nueva = CARTERAS[int(e.control.selected_index)]
        if nueva == self.cartera:
            return
        if self.ocupada():
            e.control.selected_index = CARTERAS.index(self.cartera)
            self.page.update()
            return
        self._filtros[self.cartera], self._paginas[self.cartera] = self.filtro, self._pagina_clientes
        if getattr(self, "correcciones_chat", None):
            self.correcciones_chat.descartar()
            if getattr(self, 'acciones_documentales', None):
                self.acciones_documentales.descartar()
        self._cid_cartera[self.cartera] = self.cid
        self.cartera = nueva
        self.filtro, self._pagina_clientes = self._filtros[nueva], self._paginas[nueva]
        self._busqueda_version += 1
        self.buscar.value = self.filtro
        self.lbl_lista.value = ETIQUETA[nueva].upper()
        self.seccion = 0
        cid = self._cid_cartera.get(nueva)
        if cid is None or not self.ctrl.lector.get_cliente(cid):
            primero = self.ctrl.lector.listar_clientes(limite=1, cartera=nueva)
            cid = primero[0]["id"] if primero else None
        self.cid = cid
        self._refrescar_clientes()
        if cid:
            self.abrir_cliente(cid)
        else:
            self.informe = None
            self._pintar_principal()

    def _barra_lateral(self) -> ft.Container:
        p = self.p
        self.buscar = ui.campo("Buscar", p, prefijo=I.SEARCH, hint="Buscar nombre o NIF", on_change=self._on_buscar)
        self.buscar.label = None  # sin etiqueta flotante: el hint basta, como en el buscador de macOS
        self.selector_cartera = ft.CupertinoSlidingSegmentedButton(
            controls=self._etiquetas_cartera(), selected_index=CARTERAS.index(self.cartera), bgcolor=p.campo,
            thumb_color=p.segmento, on_change=self._cambiar_cartera, padding=ft.Padding.all(3))
        self.lbl_lista = ui.leyenda(ETIQUETA[self.cartera].upper(), p, p.texto3, 11)
        return ft.Container(
            width=272, bgcolor=p.barra, padding=ft.Padding.only(left=14, right=14, top=22, bottom=14),
            border=ft.Border.only(right=ft.BorderSide(1, p.separador)),
            content=ft.Column([
                ft.Column([ui.titulo("Contabilidad", p, 22), ui.leyenda("Gestoría · a medida", p),
                           ft.Container(self.aviso_actualizacion, margin=ft.Margin.only(top=4))], spacing=2),
                ft.Container(height=6),
                ft.Row([ft.Container(self.selector_cartera, expand=True)]),
                self.buscar,
                ft.Container(height=2),
                ft.Row([self.lbl_lista,
                        ui.boton_icono(I.ADD_CIRCLE_OUTLINE, self.dialogo_nuevo_cliente, p, "Nuevo cliente")],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                self.lista_clientes,
                ui.separador(p),
                self._fila_carpeta(),
                ui.fila(I.TUNE, p.texto2, "Ajustes e IA", None, p, on_click=self.dialogo_ajustes),
            ], spacing=6, expand=True),
        )

    def _fila_carpeta(self) -> ft.Control:
        """«Carpeta de clientes» con el nombre de la carpeta como subtítulo y la ruta completa en el tooltip."""
        p = self.p
        fila = ui.fila(I.FOLDER_OUTLINED, p.texto2, "Carpeta de clientes", self.lbl_carpeta.value or "—", p,
                       on_click=self.elegir_carpeta_clientes)
        detalle = fila.content.controls[1].controls[1]
        detalle.max_lines, detalle.overflow, detalle.tooltip = 1, ft.TextOverflow.ELLIPSIS, self.lbl_carpeta.tooltip
        self.lbl_carpeta = detalle
        return fila

    async def _on_buscar(self, e) -> None:
        self.filtro = (e.control.value or "").strip().casefold()
        self._pagina_clientes = 0
        self._busqueda_version += 1
        version, filtro, cartera = self._busqueda_version, self.filtro, self.cartera
        await asyncio.sleep(0.30)  # agrupa las pulsaciones; no consulta por cada tecla
        if self.cerrando or version != self._busqueda_version:
            return
        self.lista_clientes.opacity = 0.55
        self.page.update()
        ruta = self.ctrl.lector.db_path
        def consultar():
            with database.Database(ruta, solo_lectura=True) as lector:
                return lector.listar_clientes(limite=51, busqueda=filtro, cartera=cartera)
        try:
            clientes = await asyncio.to_thread(consultar)
        except Exception:
            if not self.cerrando and version == self._busqueda_version:
                self.lista_clientes.opacity = 1
                self.avisar("No se pudo buscar clientes. Inténtalo de nuevo.", True)
            return
        if not self.cerrando and version == self._busqueda_version:
            self._refrescar_clientes(clientes)
            self.lista_clientes.opacity = 1
            self.page.update()

    def _mover_pagina_clientes(self, delta: int) -> None:
        self._busqueda_version += 1
        self._pagina_clientes = max(0, self._pagina_clientes + delta)
        self._refrescar_clientes()
        self.page.update()

    def _refrescar_clientes(self, clientes=None) -> None:
        self.lista_clientes.opacity = 1  # una navegación invalida la búsqueda pendiente
        p = self.p
        pagina = getattr(self, "_pagina_clientes", 0)
        if clientes is None:
            clientes = self.ctrl.lector.listar_clientes(limite=51, desplazamiento=pagina * 50,
                                                       busqueda=self.filtro, cartera=self.cartera)
        if not clientes and pagina:
            self._pagina_clientes = 0  # tras borrar el último cliente de una página
            clientes = self.ctrl.lector.listar_clientes(limite=51, busqueda=self.filtro, cartera=self.cartera)
            pagina = 0
        siguiente = len(clientes) > 50
        clientes = clientes[:50]
        filas = []
        self._filas_cli = {}
        for c in clientes:
            activo = c["id"] == self.cid
            fila_c = ft.Container(content=self._contenido_fila(c, activo), border_radius=12,
                                  padding=ft.Padding.symmetric(horizontal=8, vertical=7))
            reposo, encima = {"bgcolor": p.relleno if activo else "#00000000"}, {"bgcolor": p.relleno if activo else p.hover}
            filas.append(ui.pulsable(fila_c, lambda e, cid=c["id"]: self.abrir_cliente(cid), reposo, encima,
                                     escala=0.98))
            self._filas_cli[c["id"]] = (fila_c, reposo, encima, c)
        if not filas:
            vacia = f"Sin {ETIQUETA[self.cartera].lower()}. Pulsa + para crear el primero."
            filas.append(ui.leyenda(vacia if not self.filtro else "Ningún cliente coincide.", p, p.texto3))
        if pagina or siguiente:
            filas.append(ft.Row([
                ui.boton("Anterior", lambda e: self._mover_pagina_clientes(-1), p, tipo="texto")
                if pagina else ft.Container(),
                ui.leyenda(f"Página {pagina + 1}", p, p.texto3),
                ui.boton("Siguiente", lambda e: self._mover_pagina_clientes(1), p, tipo="texto")
                if siguiente else ft.Container(),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, spacing=2))
        self.lista_clientes.controls = filas
        self._refrescar_contadores()

    def _contenido_fila(self, c: dict, activo: bool) -> ft.Row:
        p = self.p
        return ft.Row([
            ui.avatar(c["nombre"], c["apellidos"], p, activo),
            ft.Column([
                ft.Text(f"{c['nombre']} {c['apellidos']}", size=14, weight=ft.FontWeight.W_600,
                        color="#FFFFFF" if activo else p.texto, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(c["nif"], size=11, color="#FFFFFF" if activo else p.texto2),
            ], spacing=0, expand=True, tight=True),
        ], spacing=10)

    def _marcar_cliente(self, anterior: Optional[int], cid: int) -> None:
        """Cambia la selección en su sitio: solo se repintan dos filas y el fondo pasa con la transición de
        140 ms de ``ui.pulsable``. Si el cliente no está en la página visible, se rehace la lista."""
        filas = getattr(self, "_filas_cli", {})
        if cid not in filas:
            self._refrescar_clientes()
            return
        p = self.p
        for ident, activo in ((anterior, False), (cid, True)):
            if ident is None or ident not in filas:
                continue
            cont, reposo, encima, datos = filas[ident]
            reposo["bgcolor"] = p.relleno if activo else "#00000000"
            encima["bgcolor"] = p.relleno if activo else p.hover
            cont.bgcolor = reposo["bgcolor"]
            cont.content = self._contenido_fila(datos, activo)

    # ================================================================== zona principal
    def abrir_cliente(self, cid: int) -> None:
        if self.ocupada():
            return
        anterior = self.cliente
        nuevo = self.ctrl.lector.get_cliente(cid)
        if cid != self.cid and getattr(self, "correcciones_chat", None):
            self.correcciones_chat.descartar()
            if getattr(self, 'acciones_documentales', None):
                self.acciones_documentales.descartar()
        if anterior and nuevo and cartera_de(anterior.get("tipo_cliente")) != cartera_de(nuevo.get("tipo_cliente")):
            self.seccion = 0  # las pestañas de cada cartera son distintas
        anterior_cid, self.cid = self.cid, cid
        self._marcar_cliente(anterior_cid, cid)
        self.informe = None
        self._pintar_principal()
        self.en_segundo_plano("Cargando expediente…", lambda ej=self.ejercicio: self.ctrl.abrir_cliente(cid, ej))

    def _pintar_principal(self) -> None:
        p = self.p
        cliente = self.cliente
        if not cliente:
            self.zona_principal.content = ui.vacio(
                I.PERSON_ADD_ALT, f"Crea tu primer cliente en {ETIQUETA[self.cartera]}",
                f"Cada cliente tiene su carpeta en {carteras.RAIZ[self.cartera]}, sus PDFs y su Excel.", p,
                ui.boton("Nuevo cliente", self.dialogo_nuevo_cliente, p, I.ADD))
            self.page.update()
            return
        # Un único scroll vertical para toda la ficha (cabecera, PDFs, pestañas y contenido): la rueda del
        # ratón llega hasta el final de cualquier sección. Sin scrolls anidados, que se roban la rueda.
        self._columna_ficha = ft.Column([
            self._cabecera(cliente),
            ft.Container(
                ft.Column([self._zona_pdfs(), self._selector_secciones(), self.contenido], spacing=16),
                padding=ft.Padding.only(left=28, right=28, bottom=40)),
        ], spacing=0, expand=True, scroll=ft.ScrollMode.AUTO)
        self.zona_principal.content = self._columna_ficha
        self.contenido.content = ft.Container(self._contenido_seccion(), key=self._clave_seccion())
        self.page.update()

    def _cabecera(self, cliente: dict) -> ft.Container:
        p = self.p
        inf = self.informe
        res = getattr(inf, "residencia", None) if inf else None
        chips = [ui.pastilla(cliente["nif"], p.texto2, p.campo),
                 ui.pastilla(cliente.get("tipo_cliente") or "No residente", p.acento, p.acento_suave)]
        if inf is not None and hasattr(inf, "regimen_iva"):
            chips.append(ui.pastilla(f"IVA {inf.regimen_iva}", p.texto2, p.campo))
            if inf.tipo_cliente == carteras.TIPO_AUTONOMO:
                chips.append(ui.pastilla(f"IRPF {inf.regimen_irpf}", p.texto2, p.campo))
        if res:
            texto = f"{res.pais or 'Residencia sin registrar'} · {res.grupo} {int(res.tipo * 100)} %"
            color = p.rojo if res.asumida else p.texto2
            chips.append(ui.pastilla(texto, color, p.rojo_suave if res.asumida else p.campo))
        anios = [str(a) for a in range(date.today().year - 4, date.today().year + 1)]
        selector_anio = ui.desplegable("Ejercicio", [(a, a) for a in reversed(anios)], p, str(self.ejercicio),
                                       on_select=self._cambiar_ejercicio, width=110)
        selector_anio.label = None
        acciones = ft.Row([
            ui.boton_icono(I.TABLE_VIEW_OUTLINED, self._abrir_excel, p, "Abrir Excel del cliente", p.texto2),
            ui.boton_icono(I.FOLDER_OPEN_OUTLINED, self._abrir_carpeta, p, "Abrir carpeta del cliente", p.texto2),
            ui.boton_icono(I.SYNC, self.sincronizar, p, "Sincronizar Excel y recalcular", p.texto2),
            ui.boton_icono(I.EDIT_OUTLINED, self.dialogo_editar_cliente, p, "Editar datos del cliente", p.texto2),
            ui.boton_icono(I.PERSON_REMOVE_OUTLINED, self.confirmar_eliminar_cliente, p, "Eliminar cliente",
                           p.texto2, peligro=True),
            ft.Container(width=4),
            ft.Container(selector_anio, tooltip="Ejercicio fiscal"),
        ], spacing=4)
        return ft.Container(
            padding=ft.Padding.only(left=28, right=28, top=24, bottom=16),
            content=ft.Row([
                ft.Column([ui.titulo(f"{cliente['nombre']} {cliente['apellidos']}", p, 30),
                           ft.Row(chips, spacing=6, wrap=True), self.lbl_estado], spacing=8, expand=True),
                acciones,
            ], vertical_alignment=ft.CrossAxisAlignment.START),
        )

    def _cambiar_ejercicio(self, e) -> None:
        if self.ocupada():
            e.control.value = str(self.ejercicio)
            self.page.update()
            return
        if getattr(self, "correcciones_chat", None):
            self.correcciones_chat.descartar()
            if getattr(self, 'acciones_documentales', None):
                self.acciones_documentales.descartar()
        self.ejercicio = int(e.control.value)
        self.en_segundo_plano(f"Calculando {self.ejercicio}…", lambda cid=self.cid, ej=self.ejercicio: self.ctrl.abrir_cliente(cid, ej))

    def _zona_pdfs(self) -> ft.Container:
        p = self.p
        ia_ok, ia_texto = self.ctrl.estado_ia()
        punto = ft.Container(width=8, height=8, border_radius=4, bgcolor=p.verde if ia_ok else p.naranja)
        zona = ft.Container(
            content=ft.Row([
                ft.Container(ft.Icon(I.UPLOAD_FILE, size=22, color=p.acento), width=40, height=40, border_radius=10,
                             bgcolor=p.acento_suave, alignment=ft.Alignment.CENTER),
                ft.Column([
                    ui.cuerpo("Añade facturas, el 036/037 o modelos presentados (PDF, Excel, Word o CSV)"
                              if self._es_residente() else "Añade escrituras, IBI, contratos o modelos (PDF, Excel, Word o CSV)",
                              p, peso=ft.FontWeight.W_600),
                    ft.Row([punto, ui.leyenda(ia_texto, p)], spacing=6),
                ], spacing=2, expand=True, tight=True),
                ui.boton("Elegir documentos", self.elegir_pdfs, p, I.ADD),
                ui.boton("Abrir bandeja", self._abrir_bandeja, p, I.INBOX_OUTLINED, tipo="secundario"),
            ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10), border_radius=14,
            tooltip="Haz clic para elegir hasta 30 documentos y revisarlos antes de procesarlos. Para arrastrar PDF "
                    "desde el Explorador, suéltalos en la carpeta _ARRASTRA_AQUI del cliente (Abrir bandeja).",
        )
        return ui.pulsable(zona, self.elegir_pdfs,
                           {"bgcolor": p.tarjeta, "border": ft.Border.all(1, p.separador)},
                           {"bgcolor": p.tarjeta, "border": ft.Border.all(1, p.borde_activo)}, escala=0.995)

    def _es_residente(self) -> bool:
        c = self.cliente
        return bool(c) and cartera_de(c.get("tipo_cliente")) == RES

    def _nombres_secciones(self) -> list[str]:
        if not self._es_residente():
            return list(SECCIONES)
        nombres = list(SECCIONES_RES)
        if (self.cliente or {}).get("tipo_cliente") != carteras.TIPO_AUTONOMO:
            nombres.remove("IRPF 130")  # una pyme no presenta pagos fraccionados de IRPF
        return nombres

    def _indice_seccion(self, nombre: str) -> int:
        nombres = self._nombres_secciones()
        return nombres.index(nombre) if nombre in nombres else 0

    def _selector_secciones(self) -> ft.Control:
        p = self.p
        nombres = self._nombres_secciones()
        n_pend = len([x for x in (self.informe.pendientes if self.informe else []) if x.nivel != "info"])
        etiquetas_seg = []
        for n in nombres:
            etiqueta: ft.Control = ft.Text(n, size=13, weight=ft.FontWeight.W_600, color=p.texto, no_wrap=True)
            if n == "Pendientes" and n_pend:
                cifra = ft.Container(ft.Text(str(n_pend), size=11, weight=ft.FontWeight.W_700, color=p.rojo),
                                     bgcolor=p.rojo_suave, border_radius=ui.PILDORA,
                                     padding=ft.Padding.symmetric(horizontal=6, vertical=0))
                etiqueta = ft.Row([etiqueta, cifra], spacing=6, tight=True,
                                  vertical_alignment=ft.CrossAxisAlignment.CENTER)
            etiquetas_seg.append(ft.Container(etiqueta, padding=ft.Padding.symmetric(horizontal=8, vertical=4)))
        self.seccion = min(self.seccion, len(nombres) - 1)
        return ft.CupertinoSlidingSegmentedButton(
            controls=etiquetas_seg, selected_index=self.seccion, bgcolor=p.campo, thumb_color=p.segmento,
            on_change=self._cambiar_seccion, padding=ft.Padding.all(3))

    def _cambiar_seccion(self, e) -> None:
        self.seccion = int(e.control.selected_index)
        self.contenido.content = ft.Container(self._contenido_seccion(), key=self._clave_seccion())
        self.page.update()

    def _clave_seccion(self) -> str:
        """Solo cambia (y funde) al cambiar de cliente, ejercicio o pestaña, o al llegar el informe."""
        return f"{self.cid}-{self.ejercicio}-{self.seccion}-{self.informe is None}"

    def _contenido_seccion(self) -> ft.Control:
        if self.informe is None:
            return ft.Container(ft.ProgressRing(width=22, height=22, stroke_width=2, color=self.p.acento),
                                alignment=ft.Alignment.CENTER, padding=60)
        constructores = {
            "Resumen": self._sec_resumen_res if self._es_residente() else self._sec_resumen,
            "Pendientes": self._sec_pendientes, "Inmuebles": self._sec_inmuebles,
            "Alquileres": self._sec_alquileres, "Imputación": self._sec_imputacion, "Gastos": self._sec_gastos,
            "Documentos": self._sec_documentos, "Asistente IA": self._sec_asistente,
            "Emitidas": lambda: self._sec_facturas("facturas_emitidas"),
            "Recibidas": lambda: self._sec_facturas("facturas_recibidas"),
            "IVA 303": self._sec_303, "IRPF 130": self._sec_130, "Retenciones": self._sec_retenciones,
        }
        nombres = self._nombres_secciones()
        self.seccion = min(self.seccion, len(nombres) - 1)
        return constructores[nombres[self.seccion]]()

    # ================================================================== secciones
    def _bloque(self, titulo: str, filas: list[ft.Control], accion: Optional[ft.Control] = None,
                vacio: Optional[str] = None) -> ft.Column:
        p = self.p
        cuerpo_filas: list[ft.Control] = []
        for i, f in enumerate(filas):
            if i:
                cuerpo_filas.append(ft.Container(ui.separador(p), padding=ft.Padding.only(left=58)))
            cuerpo_filas.append(f)
        if not filas and vacio:
            cuerpo_filas.append(ft.Container(ui.leyenda(vacio, p, p.texto3), padding=12))
        return ft.Column([
            ft.Row([ui.seccion(titulo, p), *([accion] if accion else [])],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ui.tarjeta(ft.Column(cuerpo_filas, spacing=0), p, padding=6),
        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _sec_resumen(self) -> ft.Control:
        p, inf, lec = self.p, self.informe, self.ctrl.lector
        pat = inf.patrimonio
        color, fondo = (p.rojo, p.rojo_suave) if pat.obligado else (p.verde, p.verde_suave)
        heroe = ui.tarjeta(ft.Row([
            ft.Container(ft.Icon(I.ACCOUNT_BALANCE_OUTLINED, size=26, color=color), width=52, height=52,
                         border_radius=12, bgcolor=fondo, alignment=ft.Alignment.CENTER),
            ft.Column([
                ui.leyenda(f"IMPUESTO SOBRE EL PATRIMONIO · {inf.ejercicio}", p, color, 11),
                ui.cuerpo(pat.mensaje, p, size=17, peso=ft.FontWeight.W_600),
                ui.leyenda("Valor por inmueble = el mayor entre catastral, escritura, referencia y comprobado, "
                           "por el % de titularidad. Mínimo exento 700.000 €; obligado también si los bienes "
                           "superan 2.000.000 €.", p),
            ], spacing=4, expand=True),
        ], spacing=16), p)
        cuotas_pend = 0.0
        for tabla, codigos, cerrados in (("m210_alquiler", inf.filas_210_alquiler, ("Presentado",)),
                                         ("m210_imputacion", inf.filas_210_imputacion, ("Presentado", "No procede"))):
            for c in codigos:
                fila = lec.get(tabla, self.cid, c) or {}
                if fila.get("estado") not in cerrados:
                    cuotas_pend += float(fila.get("cuota") or 0)
        graves = [x for x in inf.pendientes if x.nivel == "grave"]
        kpis = ft.Row([
            ui.kpi("Inmuebles a 31/12", str(pat.num_inmuebles), p, I.HOME_OUTLINED),
            ui.kpi("Base Patrimonio", ui.eur(pat.valor_bienes), p, I.SAVINGS_OUTLINED),
            ui.kpi("Cuotas 210 por pagar", ui.eur(cuotas_pend), p, I.RECEIPT_LONG_OUTLINED,
                   p.naranja if cuotas_pend else None),
            ui.kpi("Avisos urgentes", str(len(graves)), p, I.NOTIFICATIONS_ACTIVE_OUTLINED,
                   p.rojo if graves else p.verde, "Ver pendientes" if graves else "Todo en orden",
                   on_click=lambda e: self._ir_a_seccion(1)),
        ], spacing=12)
        estados = [ui.fila(I.DESCRIPTION_OUTLINED, p.texto2, e.split(" · ", 1)[-1],
                           e.split(" · ", 1)[0] if " · " in e else None, p) for e in inf.estados[1:]]
        alertas = [ui.fila(I.LIGHTBULB_OUTLINE, ui.colores_estado(a.nivel, p)[0], a.mensaje,
                           a.inmueble or "Cliente", p, fondo_icono=ui.colores_estado(a.nivel, p)[1])
                   for a in inf.alertas]
        return ft.Column([
            heroe, kpis,
            self._bloque("Declaraciones del ejercicio", estados, vacio="Sin declaraciones calculadas todavía."),
            self._bloque("Asesor interanual", alertas, vacio="Sin discrepancias con el año anterior."),
        ], spacing=20)

    def _sec_pendientes(self) -> ft.Control:
        p = self.p
        pend = self.informe.pendientes
        if not pend:
            return ui.vacio(I.TASK_ALT, "Nada pendiente", "Toda la documentación y las declaraciones del "
                            f"ejercicio {self.ejercicio} están al día.", p)
        iconos = {"grave": I.ERROR_OUTLINE, "aviso": I.WARNING_AMBER_ROUNDED, "info": I.SCHEDULE}
        decl, docs = [], []
        for x in pend:
            color, fondo = ui.colores_estado(x.nivel, p)
            if x.clase == "declaracion":
                derecha = [ui.boton("Marcar presentada",
                                    lambda e, c=x.codigo: self.dialogo_presentado("obligaciones", c),
                                    p, tipo="secundario")] if x.codigo else []
                decl.append(ui.fila(iconos[x.nivel], color, x.titulo, x.detalle, p, derecha, fondo_icono=fondo))
            else:
                derecha = [ui.boton("Subir PDF", self.elegir_pdfs, p, I.UPLOAD, tipo="secundario")]
                docs.append(ui.fila(I.DESCRIPTION_OUTLINED, color, x.titulo, x.detalle, p, derecha, fondo_icono=fondo))
        return ft.Column([
            self._bloque("Declaraciones", decl, vacio="Ninguna declaración pendiente."),
            self._bloque("Documentación que falta", docs, vacio="No falta documentación."),
        ], spacing=20)

    def _sec_inmuebles(self) -> ft.Control:
        p = self.p
        vistas = self.ctrl.vista_inmuebles(self.cid, self.ejercicio, limite=51,
                    desplazamiento=self._numero_pagina("inmuebles") * 50)
        if not vistas and self._numero_pagina("inmuebles"):
            self._paginas_detalle[(self.cid, self.ejercicio, "inmuebles")] = 0
            vistas = self.ctrl.vista_inmuebles(self.cid, self.ejercicio, limite=51)
        siguiente = len(vistas) > 50
        vistas = vistas[:50]
        nuevo = ui.boton("Añadir inmueble", lambda e: self.dialogo_editar("inmuebles", None), p, I.ADD, "secundario")
        if not vistas:
            return ui.vacio(I.HOME_WORK_OUTLINED, "Sin inmuebles",
                            "Sube la escritura o el recibo del IBI, o añádelo a mano.", p, nuevo)
        tarjetas: list[ft.Control] = [ft.Row([ui.seccion("Inmuebles", p), nuevo],
                                             alignment=ft.MainAxisAlignment.SPACE_BETWEEN)]
        tarjetas += [self._tarjeta_inmueble(v) for v in vistas]
        tarjetas += self._navegacion_detalle("inmuebles", siguiente)
        return ft.Column(tarjetas, spacing=14)

    def _tarjeta_inmueble(self, v: dict) -> ft.Control:
        p = self.p
        vp = v["_vp"]
        cat = v["_catastro"] or {}
        resumen = valores_inmuebles.resumen(v, cat)
        valores = [("Valor catastral", resumen["catastral"]),
                   ("Valor de adquisición" + (" · parcial" if resumen["parcial"] else ""), resumen["adquisicion"]),
                   ("Valor de referencia · " + resumen["fuente_referencia"], resumen["referencia"])]
        if vp.comprobado:
            valores.append(("Valor comprobado", vp.comprobado))
        celdas = [ft.Container(
            ft.Column([ui.leyenda(nombre, p, p.texto2, 12),
                       ft.Text(ui.eur(valor) if valor is not None else "Sin dato", size=18,
                               weight=ft.FontWeight.W_600, color=p.texto)], spacing=5, tight=True),
            padding=14, border_radius=12, bgcolor=p.campo,
            col={"xs": 12, "sm": 6 if len(valores) == 4 else 4, "lg": 3 if len(valores) == 4 else 4})
            for nombre, valor in valores]
        desglose = ft.ExpansionTile(title=ui.cuerpo("Desglose del valor de adquisición", p),
            controls=[ft.Container(ft.Column([
                ft.Row([ft.Container(ui.cuerpo(etiqueta, p), expand=True),
                        ui.cuerpo(ui.eur(v[campo]) if v.get(campo) is not None else "Sin dato", p)])
                for campo, etiqueta in valores_inmuebles.CAMPOS_COMPRA], spacing=8), padding=12)])
        heredado = f" · Catastro heredado de {cat['_heredado']}" if cat.get("_heredado") else ""
        cod = v["codigo"]
        return ui.tarjeta(ft.Column([
            ft.Row([
                ft.Column([ui.cuerpo(v.get("direccion") or "Inmueble sin dirección", p, size=16,
                                     peso=ft.FontWeight.W_600, max_lines=1),
                           ui.leyenda(f"{cod} · Ref. {v.get('ref_catastral') or 'sin referencia'}{heredado}", p)],
                          spacing=2, expand=True),
                ui.pastilla_estado(v.get("estado") or "Activo", p),
            ]),
            ft.ResponsiveRow(celdas, spacing=10, run_spacing=10),
            ui.leyenda("Importes del inmueble antes de aplicar la titularidad. Adquisición = escritura + gastos registrados.", p),
            ui.leyenda(f"Precio de escritura: {ui.eur(resumen['escritura']) if resumen['escritura'] is not None else 'sin dato'} · "
                       f"Gastos de compra: {ui.eur(resumen['gastos']) if resumen['gastos'] is not None else 'sin dato'}", p),
            *([ui.leyenda("Total parcial: falta registrar el precio de escritura.", p, p.naranja)] if resumen["parcial"] else []),
            ui.leyenda(f"Referencia en adquisición: {ui.eur(resumen['referencia_compra']) if resumen['referencia_compra'] is not None else 'sin dato'} · "
                       f"Referencia Catastro: {ui.eur(resumen['referencia_catastro']) if resumen['referencia_catastro'] is not None else 'sin dato'}", p),
            desglose,
            ui.leyenda(f"Valor usado actualmente en Patrimonio: {ui.eur(vp.valor)} · {vp.fuente}", p),
            ft.Row([
                ui.leyenda(f"Titularidad {ui.pct(v['_tit'])} · computa {ui.eur(v['_computado'])} en Patrimonio", p),
                ft.Container(expand=True),
                ui.interruptor(bool(v.get("gananciales_forzado")),
                               lambda e, c=cod: self._toggle_gananciales(c, e.control.value), p, etiqueta="Gananciales 50 %"),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ui.separador(p),
            ft.Row([
                ui.accion("Precio y gastos de compra", lambda e, c=cod: self.dialogo_adquisicion(c), p, I.RECEIPT_LONG_OUTLINED),
                ui.boton("Editar datos", lambda e, c=cod: self.dialogo_editar("inmuebles", c), p, I.EDIT_OUTLINED,
                         "texto"),
                ui.boton(f"Catastro {self.ejercicio}", lambda e, c=cod, ct=cat: self._editar_catastro(c, ct), p,
                         I.MAP_OUTLINED, "texto"),
                ui.boton("Marcar vendido", lambda e, c=cod: self.dialogo_vendido(c), p, I.SELL_OUTLINED, "texto"),
                ui.boton("Eliminar", lambda e, c=cod: self.confirmar_borrar("inmuebles", c), p, None, "peligro"),
            ], spacing=8, run_spacing=8, wrap=True),
        ], spacing=14), p)

    def dialogo_adquisicion(self, codigo: str) -> None:
        p = self.p
        cid, ejercicio = self.cid, self.ejercicio
        fila = self.ctrl.lector.get("inmuebles", cid, codigo)
        if not fila:
            self.avisar("El inmueble ya no está disponible.", True)
            return
        columnas = {c.campo: c for c in TABLAS["inmuebles"].columnas_bd}
        controles = {campo: ui.campo(etiqueta + " (€)", p, _texto_valor(columnas[campo], fila.get(campo)),
                       ayuda=("Importe de la escritura, sin añadir los gastos de compra." if campo == "precio_escritura"
                              else "Solo si existe este gasto; deja vacío si no consta."))
                     for campo, etiqueta in valores_inmuebles.CAMPOS_COMPRA}
        originales = {campo: ctl.value for campo, ctl in controles.items()}
        total = ui.cuerpo("", p, size=22, peso=ft.FontWeight.W_600)
        error = ui.cuerpo("", p, p.rojo)
        estado = {"guardando": False, "cerrado": False}

        def actualizar(_e=None):
            datos, errores = {}, []
            for campo, ctl in controles.items():
                try:
                    datos[campo] = database._normalizar(columnas[campo], (ctl.value or "").strip())
                    ctl.error = None
                except (ValueError, TypeError, OverflowError):
                    ctl.error = "Usa un importe como 1.234,56"
                    errores.append(ctl.label)
            if errores:
                total.value = "Revisa los importes"
                error.value, error.color = "Revisa: " + ", ".join(errores), p.rojo
            else:
                vista = valores_inmuebles.resumen(datos)
                total.value = ui.eur(vista["adquisicion"]) if vista["adquisicion"] is not None else "Sin importes"
                error.value = "Total parcial: falta el precio de escritura." if vista["parcial"] else ""
                error.color = p.naranja
            if _e is not None:
                self.page.update()
            return not errores

        def cerrar(_e=None):
            if not estado["guardando"] and not estado["cerrado"]:
                estado["cerrado"] = True
                self.cerrar_dialogo()

        def bloquear(valor):
            estado["guardando"] = valor
            for ctl in controles.values():
                ctl.read_only = valor
            cancelar.disabled = confirmar.disabled = valor

        def terminar():
            estado["guardando"] = False
            if not getattr(self, "cerrando", False):
                cerrar()

        def guardar(_e):
            if estado["guardando"] or estado["cerrado"]:
                return
            if self.ocupada():
                self.avisar("Espera a que termine la tarea en curso.")
                return
            if (self.cid, self.ejercicio) != (cid, ejercicio):
                error.value, error.color = "Vuelve a abrir el inmueble en el cliente y ejercicio correctos.", p.rojo
                self.page.update()
                return
            if not actualizar(_e):
                return
            cambios = {c: (ctl.value or "").strip() for c, ctl in controles.items()
                       if (ctl.value or "").strip() != originales[c]}
            if not cambios:
                cerrar()
                return
            bloquear(True)
            self.page.update()
            def trabajo():
                try:
                    return self.ctrl.editar(cid, "inmuebles", codigo, cambios, ejercicio)
                finally:
                    bloquear(False)
            aceptado = self.en_segundo_plano("Guardando precio y gastos de compra…", trabajo,
                "Precio y gastos guardados. Excel actualizado.", despues=terminar)
            if not aceptado:
                bloquear(False)
                self.page.update()

        for ctl in controles.values():
            ctl.on_change = actualizar
        controles["precio_escritura"].autofocus = True
        cancelar = ui.accion("Cancelar", cerrar, p)
        confirmar = ui.accion("Guardar y recalcular", guardar, p, I.CHECK, "primario")
        actualizar()
        self._dialogo("Precio y gastos de compra", ft.Column([
            ui.cuerpo(f"{codigo} · {fila.get('direccion') or 'Inmueble'}", p),
            ui.leyenda("Importes del inmueble antes de titularidad. No sumes el valor de referencia al precio de escritura.", p),
            ft.ResponsiveRow([ft.Container(c, col={"xs": 12, "sm": 6}) for c in controles.values()],
                             spacing=12, run_spacing=12),
            ui.separador(p), ui.leyenda("Valor de adquisición · escritura + gastos registrados", p), total, error,
        ], spacing=12), [cancelar, confirmar], ancho=680, icono=I.RECEIPT_LONG_OUTLINED)

    def _toggle_gananciales(self, cod: str, valor: bool) -> None:
        self.en_segundo_plano("Recalculando con la nueva titularidad…",
                              lambda cid=self.cid, ej=self.ejercicio: self.ctrl.set_gananciales(cid, cod, valor, ej),
                              "Titularidad actualizada y Excel regenerado.")

    def _editar_catastro(self, inmueble: str, cat: dict) -> None:
        if cat and not cat.get("_heredado"):
            self.dialogo_editar("catastro", cat["codigo"])
        else:
            inm = self.ctrl.lector.get("inmuebles", self.cid, inmueble) or {}
            self.dialogo_editar("catastro", None, {"ejercicio": self.ejercicio, "inmueble": inmueble,
                                                   "ref_catastral": inm.get("ref_catastral")})

    def _fila_modelo(self, tabla: str, fila: dict, titulo: str, detalle: str, importe: float) -> ft.Control:
        p = self.p
        derecha: list[ft.Control] = [ft.Text(ui.eur(importe), size=14, weight=ft.FontWeight.W_600, color=p.texto),
                                     ui.pastilla_estado(fila.get("estado"), p)]
        if fila.get("estado") not in ("Presentado", "No procede"):
            derecha.append(ui.boton_icono(I.TASK_ALT, lambda e, c=fila["codigo"]: self.dialogo_presentado(tabla, c),
                                          p, "Marcar presentada"))
        derecha.append(ui.boton_icono(I.EDIT_OUTLINED, lambda e, c=fila["codigo"]: self.dialogo_editar(tabla, c), p,
                                      "Corregir a mano"))
        return ui.fila(I.RECEIPT_OUTLINED, p.acento, titulo, detalle, p, derecha)

    def _sec_alquileres(self) -> ft.Control:
        p, lec = self.p, self.ctrl.lector
        contratos = []
        for c in lec.listar("contratos", self.cid):
            inq = ", ".join(i["nombre"] for i in lec.listar("inquilinos", self.cid, contrato=c["codigo"]) if i.get("nombre"))
            ini, fin = a_fecha(c.get("fecha_inicio")), a_fecha(c.get("fecha_fin_real") or c.get("fecha_fin_prevista"))
            fechas = (f"{ini:%d/%m/%Y}" if ini else "sin inicio") + (f" → {fin:%d/%m/%Y}" if fin else " → indefinido")
            contratos.append(ui.fila(
                I.HANDSHAKE_OUTLINED, p.acento, f"{c['codigo']} · {c.get('inmueble') or '—'} · {inq or 'sin inquilino'}",
                f"{fechas} · {ui.eur(c.get('renta_inicial'))} {(c.get('periodicidad') or 'Mensual').lower()}", p,
                [ui.pastilla_estado(c.get("estado"), p),
                 ui.boton_icono(I.PERSON_ADD_ALT, lambda e, cc=c: self.dialogo_editar(
                     "inquilinos", None, {"contrato": cc["codigo"], "inmueble": cc.get("inmueble")}), p, "Añadir inquilino"),
                 ui.boton_icono(I.EDIT_OUTLINED, lambda e, cc=c["codigo"]: self.dialogo_editar("contratos", cc), p,
                                "Corregir contrato")]))
        modelos = [self._fila_modelo(
            "m210_alquiler", f, f"210 rendimientos {f.get('periodo')} · {f.get('inmueble')}",
            f"Ingresos {ui.eur(f.get('ingresos'))} · gastos {ui.eur(f.get('gastos'))} · base {ui.eur(f.get('base'))}"
            f" · tipo {ui.pct(f.get('tipo'))}", f.get("cuota") or 0)
            for f in lec.listar("m210_alquiler", self.cid, ejercicio=self.ejercicio, orden="periodo")]
        nuevo = ui.boton("Añadir contrato", lambda e: self.dialogo_editar("contratos", None), p, I.ADD, "secundario")
        return ft.Column([
            self._bloque("Contratos de alquiler", contratos, nuevo, "Sube el contrato en PDF o añádelo a mano."),
            self._bloque(f"Modelo 210 de arrendamientos · {self.ejercicio}", modelos,
                         vacio="Sin rentas de alquiler en el ejercicio."),
            ui.leyenda("Los importes del 210 se recalculan solos. Si corriges uno a mano y quieres fijarlo, "
                       "márcalo como Presentado.", p, p.texto3),
        ], spacing=20)

    def _sec_imputacion(self) -> ft.Control:
        p, lec = self.p, self.ctrl.lector
        imput = [self._fila_modelo("m210_imputacion", f, f"210 imputación · {f.get('inmueble')}",
                                   f"{f.get('dias')} días vacíos · VC {ui.eur(f.get('valor_catastral'))} × "
                                   f"{ui.pct(f.get('pct_imputacion'))} × {ui.pct(f.get('pct_titularidad'))}",
                                   f.get("cuota") or 0)
                 for f in lec.listar("m210_imputacion", self.cid, ejercicio=self.ejercicio)]
        patri = [self._fila_modelo("patrimonio_714", f, "Patrimonio 714",
                                   f"Bienes {ui.eur(f.get('valor_bienes'))} · deudas {ui.eur(f.get('deudas'))} · "
                                   f"base liquidable {ui.eur(f.get('base_liquidable'))}", f.get("resultado") or 0)
                 for f in lec.listar("patrimonio_714", self.cid, ejercicio=self.ejercicio)]
        patri += [self._fila_modelo("its_718", f, "Solidaridad grandes fortunas 718",
                                    f"Base {ui.eur(f.get('base'))} · deducción IP {ui.eur(f.get('deducciones'))}",
                                    f.get("resultado") or 0)
                  for f in lec.listar("its_718", self.cid, ejercicio=self.ejercicio)]
        return ft.Column([
            self._bloque(f"Imputación de rentas (inmuebles vacíos) · {self.ejercicio}", imput,
                         vacio="Ningún inmueble estuvo vacío en el ejercicio."),
            self._bloque("Patrimonio e ITS", patri),
            ui.leyenda("Deudas deducibles y bonificación autonómica del 714 se introducen a mano con el lápiz.", p, p.texto3),
        ], spacing=20)

    ICONOS_GASTO = {
        "IBI": I.ACCOUNT_BALANCE_OUTLINED, "Comunidad": I.APARTMENT_OUTLINED, "Seguro": I.SHIELD_OUTLINED,
        "Reparación/conservación": I.BUILD_OUTLINED, "Administración": I.BUSINESS_CENTER_OUTLINED,
        "Agencia": I.STOREFRONT_OUTLINED, "Suministros": I.BOLT_OUTLINED, "Intereses/financiación": I.PERCENT,
        "Amortización": I.TRENDING_DOWN, "Servicios profesionales": I.GAVEL_OUTLINED,
        "Tasa/basura": I.DELETE_SWEEP_OUTLINED, "Otros": I.RECEIPT_LONG_OUTLINED,
    }

    @staticmethod
    def _importe_registrado(g: dict) -> float:
        """Lo que el usuario ve como gasto del ejercicio: el «importe deducible final» si lo escribió; si no,
        total × % inmueble × % alquiler (la titularidad y los días alquilados se aplican en cada 210)."""
        return reglas_gastos.importe_registrado(g)

    def _gastos_del_ejercicio(self) -> list[dict]:
        out = []
        for g in self.ctrl.lector.listar("gastos", self.cid, orden="fecha"):
            fecha = a_fecha(g.get("fecha") or g.get("periodo_desde"))
            if reglas_gastos.ejercicio_registrado(g) == self.ejercicio:
                out.append({**g, "_fecha": fecha, "_importe": self._importe_registrado(g)})
        return out

    def _desglose_gastos(self, gastos: list[dict]) -> ft.Control:
        """Una tarjeta por inmueble con cada partida deducible, su importe y una barra de proporción."""
        p = self.p
        por_inm: dict[str, dict[str, float]] = {}
        for g in gastos:
            cat = g.get("categoria") or "Otros"
            por_inm.setdefault(g.get("inmueble") or "Sin inmueble", {}).setdefault(cat, 0.0)
            por_inm[g.get("inmueble") or "Sin inmueble"][cat] += g["_importe"]
        if not por_inm:
            return ui.tarjeta(ui.vacio(I.RECEIPT_LONG_OUTLINED, "Sin gastos este ejercicio",
                                       "Sube facturas de comunidad, luz, agua, seguro, hipoteca o reparaciones: "
                                       "la IA las clasifica sola.", p), p)
        direcciones = {i["codigo"]: i.get("direccion") or i.get("ref_catastral") or ""
                       for i in self.ctrl.lector.listar("inmuebles", self.cid)}
        tarjetas = []
        for inm, cats in sorted(por_inm.items()):
            total = sum(cats.values()) or 1
            filas = []
            for cat, importe in sorted(cats.items(), key=lambda kv: -kv[1]):
                proporcion = max(1, int(round(importe / total * 1000)))
                barra = ft.Row([
                    ft.Container(height=4, border_radius=2, bgcolor=p.acento, expand=proporcion),
                    ft.Container(height=4, border_radius=2, bgcolor=p.campo, expand=max(1, 1000 - proporcion)),
                ], spacing=0)
                filas.append(ft.Container(ft.Row([
                    ft.Container(ft.Icon(self.ICONOS_GASTO.get(cat, I.RECEIPT_LONG_OUTLINED), size=17, color=p.acento),
                                 width=32, height=32, border_radius=8, bgcolor=p.acento_suave,
                                 alignment=ft.Alignment.CENTER),
                    ft.Column([ft.Row([ui.cuerpo(cat, p, peso=ft.FontWeight.W_600), ft.Container(expand=True),
                                       ft.Text(ui.eur(importe), size=14, weight=ft.FontWeight.W_600, color=p.texto)]),
                               barra], spacing=6, expand=True, tight=True),
                ], spacing=12), padding=ft.Padding.symmetric(horizontal=4, vertical=6)))
            tarjetas.append(ui.tarjeta(ft.Column([
                ft.Row([ft.Column([ui.seccion(inm, p, 15), ui.leyenda(direcciones.get(inm, ""), p)], spacing=0,
                                  expand=True),
                        ft.Column([ui.leyenda("Total registrado", p),
                                   ft.Text(ui.eur(sum(cats.values())), size=20, weight=ft.FontWeight.W_700,
                                           color=p.texto, font_family=ui.FUENTE_TITULO)],
                                  spacing=0, horizontal_alignment=ft.CrossAxisAlignment.END)]),
                ui.separador(p),
                *filas,
            ], spacing=8), p, padding=20))
        return ft.Column(tarjetas, spacing=14)

    def _sec_gastos(self) -> ft.Control:
        p = self.p
        gastos = self._gastos_del_ejercicio()
        filas = []
        for g in gastos:
            fecha = g["_fecha"]
            detalle = " · ".join(x for x in (f"{fecha:%d/%m/%Y}" if fecha else "sin fecha", g.get("proveedor"),
                                             g.get("observaciones")) if x)
            filas.append(ui.fila(
                self.ICONOS_GASTO.get(g.get("categoria") or "Otros", I.PAYMENTS_OUTLINED), p.acento,
                f"{g.get('categoria') or 'Gasto'} · {g.get('inmueble') or '—'}", detalle, p, [
                    ft.Text(ui.eur(g.get("total")), size=14, weight=ft.FontWeight.W_600, color=p.texto),
                    ui.pastilla({"pdf": "PDF", "excel": "Excel"}.get(g.get("origen"), "Manual"), p.texto2, p.campo),
                    *([ui.boton_icono(I.OPEN_IN_NEW, lambda e, r=g["ruta"]: _abrir_en_sistema(
                        Path(self.cliente["carpeta"]) / r), p, "Abrir factura")] if g.get("ruta") else []),
                    ui.boton_icono(I.EDIT_OUTLINED, lambda e, c=g["codigo"]: self.dialogo_editar("gastos", c), p, "Corregir"),
                    ui.boton_icono(I.DELETE_OUTLINE, lambda e, c=g["codigo"]: self.confirmar_borrar("gastos", c), p,
                                   "Eliminar", p.rojo)]))
        nuevo = ui.boton("Añadir gasto", lambda e: self.dialogo_editar(
            "gastos", None, {"ejercicio": self.ejercicio, "fecha": date.today()}), p, I.ADD, "secundario")
        sin_clasificar = [g for g in self.ctrl.lector.listar("gastos", self.cid)
                          if reglas_gastos.ejercicio_registrado(g) is None]
        pendientes = [ui.fila(I.REPORT_GMAILERRORRED, p.rojo,
                             g.get("concepto") or g.get("observaciones") or g["codigo"],
                             "Asigna ejercicio o fecha para incluir este gasto en los cálculos.", p,
                             [ui.boton("Clasificar", lambda e, c=g["codigo"]: self.dialogo_editar("gastos", c),
                                       p, I.EDIT_OUTLINED, "secundario")]) for g in sin_clasificar]
        res = self.ctrl.lector.residencia(self.cid, self.ejercicio) or {}
        deduce = res.get("gastos_deducibles") != "No"
        aviso = [] if deduce else [ui.leyenda(
            f"Residente en {res.get('pais') or 'un país'} fuera de la UE/EEE: tributa al 24 % sobre los ingresos "
            "íntegros y estos gastos no se deducen. Se guardan como soporte y por si cambia la residencia.",
            p, p.naranja)]
        return ft.Column([
            ft.Row([ui.seccion(f"Gastos {'deducibles' if deduce else 'registrados'} · {self.ejercicio}", p), nuevo],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            *aviso,
            self._desglose_gastos(gastos),
            self._bloque("Facturas y recibos", filas, vacio="IBI, basura, comunidad, luz, agua, gas, seguros, "
                                                             "intereses, reparaciones… Solo deducen los residentes UE/EEE."),
            *self._resumen_arrastres(),
            *([self._bloque("Gastos pendientes de clasificar", pendientes)] if pendientes else []),
            ui.leyenda("El ejercicio registrado y el redondeo por gasto coinciden con el resumen Excel. "
                       "El 210 reparte por fechas y aplica los límites fiscales; un importe final manual ya "
                       "incluye la titularidad. Los gastos sin ejercicio ni fecha quedan por clasificar.", p, p.texto3),
        ], spacing=16)

    def _resumen_arrastres(self) -> list[ft.Control]:
        filas = []
        for saldo in self.ctrl.lector.listar("arrastres_gastos", self.cid, ejercicio=self.ejercicio):
            detalle = (f"Aplicado de años anteriores: {ui.eur(saldo.get('aplicado'))} · "
                       f"Nuevo exceso: {ui.eur(saldo.get('generado'))} · "
                       f"Pendiente: {ui.eur(saldo.get('pendiente'))}")
            if saldo.get("estado") == "Revisar":
                detalle += " · Histórico pendiente de revisar"
            filas.append(ui.fila(I.HISTORY, self.p.acento, saldo.get("inmueble") or "Inmueble", detalle, self.p))
        return [self._bloque("Intereses y reparaciones · arrastre fiscal", filas)] if filas else []

    def _numero_pagina(self, tabla: str) -> int:
        return getattr(self, "_paginas_detalle", {}).get((self.cid, self.ejercicio, tabla), 0)

    def _mover_pagina_detalle(self, tabla: str, delta: int) -> None:
        clave = (self.cid, self.ejercicio, tabla)
        self._paginas_detalle[clave] = max(0, self._numero_pagina(tabla) + delta)
        self._pintar_principal()

    def _navegacion_detalle(self, tabla: str, siguiente: bool) -> list[ft.Control]:
        pagina = self._numero_pagina(tabla)
        if not pagina and not siguiente:
            return []
        return [ft.Row([
            ui.boton("Anterior", lambda e: self._mover_pagina_detalle(tabla, -1), self.p, tipo="texto")
            if pagina else ft.Container(),
            ui.leyenda(f"Página {pagina + 1} · hasta 50 registros", self.p),
            ui.boton("Siguiente", lambda e: self._mover_pagina_detalle(tabla, 1), self.p, tipo="texto")
            if siguiente else ft.Container(),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)]

    def _sec_documentos(self) -> ft.Control:
        p, cid, ej = self.p, self.cid, self.ejercicio
        papelera = getattr(self, "_documentos_papelera", {}).get(cid, False)
        clave_pagina = "documentos_papelera" if papelera else "documentos"
        filas = []
        documentos = self.ctrl.listar_documentos(cid, papelera=papelera, limite=51,
                    desplazamiento=self._numero_pagina(clave_pagina) * 50)
        for d in documentos[:50]:
            falta = d.get("estado") == "Falta"
            derecha: list[ft.Control] = [ui.pastilla_estado("En papelera" if papelera else d.get("estado"), p)]
            cod = d["codigo"]
            if papelera:
                derecha.append(ui.accion("Restaurar", lambda e, c=cod: self.dialogo_documento(c, "restaurar", cid, ej),
                                          p, I.RESTORE_FROM_TRASH_OUTLINED))
            else:
                if d.get("ruta"):
                    derecha.append(ui.accion("Abrir", lambda e, c=cod, cliente=cid: self._abrir_documento(cliente, c),
                                              p, I.OPEN_IN_NEW))
                elif falta:
                    derecha.append(ui.accion("Subir PDF", self.elegir_pdfs, p, I.UPLOAD))
                if d.get("ruta"):
                    derecha.extend([
                    ft.PopupMenuButton(content=ft.Row([ft.Icon(I.MORE_HORIZ, color=p.texto2, size=18),
                        ui.cuerpo("Más acciones", p, p.texto2, 13)], spacing=4, tight=True),
                        tooltip="Más acciones del documento", width=126, height=44,
                        icon_color=p.texto2, bgcolor=p.tarjeta, shape=ft.RoundedRectangleBorder(radius=12),
                        items=[ft.PopupMenuItem("Corregir ejercicio", icon=I.EDIT_CALENDAR_OUTLINED,
                            on_click=lambda e, c=cod: self.dialogo_documento(c, "corregir_ejercicio", cid, ej)),
                               ft.PopupMenuItem("Sustituir", icon=I.SWAP_HORIZ, on_click=self._accion_sustituir(cod, cid, ej))]),
                    ui.accion("Retirar a papelera", lambda e, c=cod: self.dialogo_documento(c, "retirar", cid, ej),
                              p, I.DELETE_OUTLINE, tooltip="Retirar el documento y revisar sus datos; se puede restaurar"),
                    ])
            detalle = " · ".join(str(x) for x in (
                cod, f"Ejercicio {d['ejercicio']}" if d.get("ejercicio") else "Sin ejercicio",
                d.get("periodo"), d.get("fecha_documento"), d.get("inmueble")) if x)
            filas.append(ui.tarjeta(ft.Column([
                ft.Row([ft.Icon(I.DELETE_OUTLINE if papelera else I.PICTURE_AS_PDF_OUTLINED,
                                color=p.texto2, size=22),
                        ft.Column([ui.cuerpo(d.get("tipo") or "Documento", p, peso=ft.FontWeight.W_600),
                                   ui.leyenda(detalle, p)], spacing=3, expand=True)], spacing=12),
                *([ui.leyenda(d["descripcion"], p)] if d.get("descripcion") else []),
                *([ui.leyenda("Comentario para la IA: " + d["comentario_guia"], p)] if d.get("comentario_guia") else []),
                ft.Row(derecha, wrap=True, spacing=4, run_spacing=4),
            ], spacing=8), p, padding=16))
        selector = ft.Row([
            ft.SegmentedButton(segments=[ft.Segment("activos", label="Activos", icon=I.DESCRIPTION_OUTLINED),
                                        ft.Segment("papelera", label="Papelera", icon=I.DELETE_OUTLINE)],
                selected=["papelera" if papelera else "activos"], height=44,
                on_change=lambda e: self._ver_documentos("papelera" in e.control.selected),
                style=ft.ButtonStyle(color=p.texto2, bgcolor=p.campo,
                                      shape=ft.RoundedRectangleBorder(radius=12))),
            *([] if papelera else [ui.accion("Subir PDF", self.elegir_pdfs, p, I.UPLOAD)]),
        ], spacing=6, wrap=True)
        ayuda = ("Los documentos retirados se conservan aquí y no se usan en nuevas consultas del asistente."
                 if papelera else "Revisa el ejercicio antes de calcular. Puedes corregir, sustituir o retirar un PDF.")
        vacio = ("La papelera está vacía. Los documentos que retires aparecerán aquí."
                 if papelera else "Todavía no hay documentos activos. Sube un PDF para empezar.")
        bloque = self._bloque(("Papelera del expediente" if papelera else "Documentación del expediente")
                              + f" · Ejercicio abierto {ej}", filas, vacio=vacio)
        return ft.Column([selector, ui.leyenda(ayuda, p), bloque,
                          *self._navegacion_detalle(clave_pagina, len(documentos) > 50)], spacing=14)

    def _ver_documentos(self, papelera: bool) -> None:
        if not hasattr(self, "_documentos_papelera"):
            self._documentos_papelera = {}
        self._documentos_papelera[self.cid] = papelera
        self._pintar_principal()

    def _abrir_documento(self, cid: int, codigo: str) -> None:
        try:
            doc = self.ctrl.obtener_documento(cid, codigo)
            if not doc or doc.get("en_papelera"):
                raise ValueError("El documento ya no está activo. Actualiza la lista.")
            carpeta = self.ctrl.lector.verificar_cartera(self.ctrl.lector.get_cliente(cid))
            ruta = database.ruta_soporte(carpeta, doc.get("ruta") or "")
            if not ruta.is_file():
                raise ValueError("No se encuentra el PDF. Recupera una copia en su carpeta original antes de corregir o retirar el documento.")
            _abrir_en_sistema(ruta)
        except (ValueError, OSError, RuntimeError) as exc:
            self.avisar(str(exc), True)

    def _accion_sustituir(self, codigo: str, cid: int, ejercicio: int):
        async def elegir(_e):
            if (cid, ejercicio) == (self.cid, self.ejercicio):
                await self.elegir_sustituto(codigo)
            else:
                self.avisar("Vuelve a seleccionar el documento en el expediente actual.")
        return elegir

    async def elegir_sustituto(self, codigo: str) -> None:
        cid, ej = self.cid, self.ejercicio
        if self.ocupada():
            return
        archivos = await self.file_picker.pick_files(dialog_title="Elegir PDF de sustitución", allow_multiple=False,
                                                     file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=list(adj.EXTENSIONES))
        rutas = [Path(f.path) for f in (archivos or []) if f.path]
        if rutas and (cid, ej) == (self.cid, self.ejercicio):
            self.dialogo_documento(codigo, "sustituir", cid=cid, ejercicio=ej, ruta_pdf=rutas[0])
        elif rutas:
            self.avisar("Has cambiado de expediente. Vuelve a elegir el documento en el cliente correcto.")

    def dialogo_documento(self, codigo: str, accion: str, cid: Optional[int] = None,
                          ejercicio: Optional[int] = None, ruta_pdf: Optional[Path] = None) -> None:
        """Previsualiza fuera del hilo UI y pide una confirmación con destino fijo."""
        cid = self.cid if cid is None else cid
        ej = self.ejercicio if ejercicio is None else ejercicio
        if self.ocupada():
            return
        if accion == "corregir_ejercicio":
            campo = ui.campo("Ejercicio de destino", self.p, str(ej),
                             ayuda="Revisarás el efecto sobre los datos y las fechas antes de confirmar.")
            error = ui.leyenda("", self.p, self.p.rojo)

            def continuar(_e):
                try:
                    destino = int((campo.value or "").strip())
                    if not 1900 <= destino <= 2200:
                        raise ValueError()
                except ValueError:
                    error.value = "Escribe un ejercicio de cuatro cifras entre 1900 y 2200."
                    self.page.update()
                    return
                self.page.pop_dialog()
                self._previsualizar_documento(cid, codigo, accion, ej, destino)

            campo.autofocus, campo.on_submit = True, continuar
            self._dialogo("Corregir ejercicio", ft.Column([campo, error], scroll=ft.ScrollMode.AUTO, tight=True),
                          [ui.accion("Cancelar", self.cerrar_dialogo, self.p),
                           ui.accion("Revisar cambios", continuar, self.p, tipo="primario")], ancho=480)
        else:
            self._previsualizar_documento(cid, codigo, accion, ej, ruta_pdf=ruta_pdf)

    def _previsualizar_documento(self, cid: int, codigo: str, accion: str, ej: int,
                                 destino: Optional[int] = None, ruta_pdf: Optional[Path] = None) -> None:
        def trabajo():
            vista = self.ctrl.previsualizar_documento(cid, codigo, "retirar" if accion == "sustituir" else accion,
                                                     ejercicio_destino=destino)
            if accion == "sustituir":
                vista = {**vista, "titulo": "Sustituir documento",
                         "descripcion": "El PDF anterior se conservará en la papelera. Revisarás los datos del nuevo PDF antes de guardarlo."}
            if (cid, ej) == (self.cid, self.ejercicio) and not self.cerrando:
                self._confirmar_documento(cid, codigo, accion, ej, vista, destino, ruta_pdf)
            elif not self.cerrando:
                self.avisar("El expediente o el ejercicio ha cambiado. Vuelve a seleccionar el documento.", True)
            return {}
        self.en_segundo_plano("Revisando los datos vinculados al documento…", trabajo)

    def _confirmar_documento(self, cid: int, codigo: str, accion: str, ej: int, vista: dict,
                             destino: Optional[int] = None, ruta_pdf: Optional[Path] = None) -> None:
        p, enviado = self.p, {"valor": False}
        textos = {"retirar": "Retirar a papelera", "restaurar": "Restaurar documento",
                  "corregir_ejercicio": "Confirmar ejercicio", "sustituir": "Sustituir documento"}
        detalles = [ui.cuerpo(vista.get("descripcion") or "Revisa los efectos antes de confirmar.", p)]
        cliente = vista.get("cliente") or {}
        if cliente:
            nombre = " ".join(str(cliente.get(k) or "") for k in ("nombre", "apellidos")).strip()
            detalles.append(ui.cuerpo(f"{nombre} · {cliente.get('nif') or 'sin NIF'} · {codigo}", p,
                                      peso=ft.FontWeight.W_600))
        if ruta_pdf:
            detalles.append(ui.cuerpo(f"Nuevo PDF: {ruta_pdf.name}", p))
        if vista.get("ejercicio_origen") or destino:
            detalles.append(ui.leyenda(f"Ejercicio actual: {vista.get('ejercicio_origen') or 'sin asignar'}"
                                      + (f" → destino: {destino}" if destino else ""), p))
        for cambio in vista.get("cambios", []):
            texto = cambio.get("descripcion") if isinstance(cambio, dict) else str(cambio)
            if not texto and isinstance(cambio, dict):
                tabla, campo = cambio.get("tabla"), cambio.get("campo")
                etiqueta = etiquetas(tabla).get(campo, str(campo).replace("_", " ").capitalize()) if tabla in TABLAS else "Dato vinculado"
                antes, despues = cambio.get("anterior"), cambio.get("posterior")
                if campo in ("ruta", "carpeta"):
                    antes, despues = ("Vinculado" if antes else "Sin soporte"), ("Vinculado" if despues else "Sin soporte")
                texto = f"{etiqueta}: {antes if antes not in (None, '') else 'Sin dato'} → {despues if despues not in (None, '') else 'Sin dato'}"
            detalles.append(ui.leyenda(texto or "Revisión de datos vinculados", p))
        for aviso in vista.get("advertencias", []):
            detalles.append(ft.Container(ui.cuerpo(aviso, p, p.naranja, 13), bgcolor=p.naranja_suave,
                                        border_radius=12, padding=12))
        if accion == "retirar":
            detalles.append(ui.leyenda("El PDF se conserva en la papelera. Podrás restaurarlo desde este expediente.", p))

        def confirmar(_e):
            if enviado["valor"] or self.ocupada():
                return
            if (cid, ej) != (self.cid, self.ejercicio):
                self.page.pop_dialog()
                self.avisar("El expediente o el ejercicio ha cambiado. Revisa de nuevo el documento antes de confirmar.", True)
                return
            def trabajo():
                if accion == "retirar":
                    return self.ctrl.retirar_documento(cid, codigo, ej, version_esperada=vista.get("version"))
                if accion == "restaurar":
                    return self.ctrl.restaurar_documento(cid, codigo, ej, version_esperada=vista.get("version"))
                if accion == "corregir_ejercicio":
                    return self.ctrl.corregir_ejercicio_documento(cid, codigo, destino, ej, version_esperada=vista.get("version"))
                return self.ctrl.sustituir_documento(cid, codigo, ruta_pdf, ej,
                    confirmar=lambda ext: self.revisar_extraccion(ext, cid, ej), version_esperada=vista.get("version"))
            enviado["valor"] = True
            self.page.pop_dialog()
            if not self.en_segundo_plano("Actualizando el expediente…", trabajo,
                                         "Documento actualizado. Revisa los avisos del expediente."):
                enviado["valor"] = False

        acciones = [ui.accion("Cancelar" if vista.get("confirmable") else "Cerrar", self.cerrar_dialogo, p)]
        if vista.get("confirmable"):
            acciones.append(ui.accion(textos[accion], confirmar, p,
                                     I.DELETE_OUTLINE if accion == "retirar" else I.CHECK,
                                     "peligro" if accion == "retirar" else "primario"))
        self._dialogo(vista.get("titulo") or textos[accion],
                      ft.Column(detalles, spacing=12, tight=True, scroll=ft.ScrollMode.AUTO), acciones,
                      ancho=600, icono=I.DELETE_OUTLINE if accion == "retirar" else I.FACT_CHECK_OUTLINED)

    # ================================================================== asistente IA del expediente
    @staticmethod
    def _texto_chat(texto: str) -> str:
        """Conserva formato de texto, sin imágenes ni HTML que soliciten recursos externos."""
        texto = texto.replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"\\*!", lambda _m: "\\!", texto)

    def _burbuja(self, rol: str, texto: str, pensando: bool = False) -> tuple[ft.Control, Optional[ft.Markdown]]:
        """Burbuja de chat. Usuario: azul a la derecha. Asistente: tarjeta a la izquierda con Markdown."""
        p = self.p
        if rol == "user":
            return ft.Row([ft.Container(
                ft.Text(texto, color="#FFFFFF", size=14, font_family=ui.FUENTE, selectable=True),
                bgcolor=p.relleno, border_radius=18, padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                expand=True, margin=ft.Margin.only(left=48))], alignment=ft.MainAxisAlignment.END), None
        md = ft.Markdown(self._texto_chat(texto), selectable=True, auto_follow_links=False,
                         extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                         md_style_sheet=ft.MarkdownStyleSheet(
                             p_text_style=ft.TextStyle(size=14, color=p.texto, font_family=ui.FUENTE, height=1.5),
                             strong_text_style=ft.TextStyle(weight=ft.FontWeight.W_700, color=p.texto),
                             list_bullet_text_style=ft.TextStyle(color=p.texto2)))
        cabecera = ft.Row([ft.Icon(I.AUTO_AWESOME, size=15, color=p.acento),
                           ui.leyenda("Asistente del expediente", p, p.texto2, 11)], spacing=6)
        cuerpo = [cabecera, md]
        if pensando:
            cuerpo.append(ft.Row([ft.ProgressRing(width=12, height=12, stroke_width=2, color=p.texto3),
                                  ui.leyenda("Leyendo el expediente y los PDFs…", p)], spacing=8))
        burbuja = ft.Container(ft.Column(cuerpo, spacing=6, tight=True), bgcolor=p.tarjeta, border_radius=18,
                               border=ft.Border.all(1, p.separador), shadow=ui.sombra_flotante(p),
                               padding=ft.Padding.symmetric(horizontal=18, vertical=14),
                               margin=ft.Margin.only(right=24))
        return burbuja, md

    def _sec_asistente(self) -> ft.Control:
        import asistente
        p, cliente = self.p, self.cliente
        conv = self.conversaciones.get(self.cid)
        ia_ok, ia_texto = self.ctrl.estado_ia()
        aviso_carpeta = ""
        # No recorrer/resolver miles de PDFs desde el bucle de UI. El contexto real se lee en el trabajador.
        n_pdfs = self.ctrl.lector.con.execute("SELECT count(*) FROM documentos WHERE cliente_id=? "
                    "AND ruta IS NOT NULL AND coalesce(estado,'')!='Falta'", (self.cid,)).fetchone()[0]
        cabecera = ui.tarjeta(ft.Row([
            ft.Container(ft.Icon(I.AUTO_AWESOME, size=24, color=p.acento), width=48, height=48, border_radius=12,
                         bgcolor=p.acento_suave, alignment=ft.Alignment.CENTER),
            ft.Column([ui.seccion("Asistente del expediente", p),
                       ui.leyenda(f"Consulta la ficha de {cliente['nombre']} {cliente['apellidos']} y una muestra "
                                  f"del texto de sus PDFs ({n_pdfs} documentos registrados). Consultas de IA y correcciones con confirmación. {ia_texto}.", p)],
                      spacing=2, expand=True),
            *([ui.boton("Nueva conversación", self._nueva_conversacion, p, I.ADD_COMMENT_OUTLINED, "texto")]
              if conv and (conv.turnos or conv.ultimo_error) else []),
        ], spacing=14), p)
        aviso_ia = [] if ia_ok else [ui.tarjeta(ui.vacio(
            I.KEY_OUTLINED, "Activa la IA para conversar",
            "Añade tu clave API de Anthropic en Ajustes. Las correcciones con confirmación funcionan sin conexión.",
            p, ui.boton("Abrir Ajustes", self.dialogo_ajustes, p, I.TUNE)), p)]

        self._chat_lista = ft.Column(spacing=14, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        for t in (conv.turnos if conv else []):
            self._chat_lista.controls.append(self._burbuja(t.rol, t.texto)[0])
        solicitud = self._chat_en_curso
        en_curso = solicitud and (solicitud["cid"], solicitud["ejercicio"]) == (self.cid, self.ejercicio)
        if en_curso:
            self._chat_lista.controls.append(self._burbuja("user", solicitud["pregunta"])[0])
            burbuja, md = self._burbuja("assistant", solicitud["texto"], pensando=True)
            self._chat_lista.controls.append(burbuja)
            solicitud["md"], solicitud["lista"] = md, self._chat_lista
        elif conv and conv.ultimo_error:
            self._chat_lista.controls += [self._burbuja("user", conv.pregunta_fallida)[0],
                self._burbuja("assistant", f"**No se pudo responder.** {conv.ultimo_error}")[0]]
        elif not (conv and conv.turnos):
            chips = [ui.pulsable(ft.Container(
                ft.Row([ft.Icon(I.CHAT_BUBBLE_OUTLINE, size=15, color=p.acento),
                        ft.Text(pregunta, size=13, color=p.texto, font_family=ui.FUENTE, expand=True)], spacing=8),
                border_radius=999, padding=ft.Padding.symmetric(horizontal=14, vertical=9)),
                lambda e, q=pregunta: self._enviar_pregunta(q),
                {"bgcolor": p.tarjeta, "border": ft.Border.all(1, p.separador)},
                {"bgcolor": p.tarjeta, "border": ft.Border.all(1, p.borde_activo)}, escala=0.98)
                for pregunta in (asistente.PREGUNTAS_SUGERIDAS_RES if self._es_residente()
                                 else asistente.PREGUNTAS_SUGERIDAS)]
            self._chat_lista.controls.append(ft.Column([ui.leyenda("Prueba con:", p), *chips], spacing=8))
        self._chat_campo = ui.campo("Pregunta sobre este expediente", p, hint="Ej.: ¿qué gastos faltan del 2T?",
                                    on_submit=lambda e: self._enviar_pregunta(e.control.value), expand=True)
        self._chat_boton = ui.boton("Enviar", lambda e: self._enviar_pregunta(self._chat_campo.value), p, I.ARROW_UPWARD)
        self._chat_campo.disabled = self._chat_ocupado
        self._chat_boton.disabled = self._chat_ocupado
        entrada = ui.tarjeta(ft.Row([self._chat_campo, self._chat_boton], spacing=10,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER), p, padding=12)
        euros = conv.coste * ia_extractor.EUR_POR_USD if conv else 0
        coste = (f"Coste estimado: {euros:.5f} € ({conv.coste:.5f} USD)".replace(".", ",") +
                 (" · estimación incompleta" if conv.coste_incompleto else "")) if conv else ""
        pie = ui.leyenda(coste if conv and (conv.coste or conv.coste_incompleto) else
                         "Para corregir el precio de escritura (sin gastos): /corregir INM-001 valor_adquisicion 250000,00. "
                         "Revisa la propuesta y confirma con /confirmar y su token. "
                         "También puedes pedir: «Renombra DOC-001 a Factura Luz T3.pdf» o «Cambia DOC-001 al ejercicio 2026». "
                         "Las acciones documentales requieren /confirmar_documento y su token.", p, p.texto3)
        avisos = [ui.leyenda(aviso_carpeta, p, p.rojo)] if aviso_carpeta else []
        return ft.Column([cabecera, *aviso_ia, *avisos, self._chat_lista, entrada, pie], spacing=16,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _nueva_conversacion(self, _e=None) -> None:
        if self._chat_ocupado:
            return
        self.conversaciones.pop(self.cid, None)
        self.correcciones_chat.descartar(self.cid)
        if getattr(self, 'acciones_documentales', None):
            self.acciones_documentales.descartar()
        self._pintar_principal()

    def _bajar_al_final(self) -> None:
        col = self._columna_ficha
        if col is None:
            return

        async def bajar():
            try:
                await col.scroll_to(offset=-1, duration=250, curve=ft.AnimationCurve.EASE_OUT_CUBIC)
            except Exception:
                pass
        try:
            self.page.run_task(bajar)
        except Exception:
            pass

    def _actualizar_chat(self, solicitud: dict, final: bool = False) -> None:
        """Las actualizaciones del stream se resuelven en el bucle de UI y solo sobre su vista vigente."""
        async def actualizar():
            if self.cerrando or (self.cid, self.ejercicio) != (solicitud["cid"], solicitud["ejercicio"]):
                return
            if final and solicitud.get("resultado_correccion"):
                self._aplicar_resultado(solicitud["resultado_correccion"])
            if self.seccion != self._indice_seccion("Asistente IA"):
                if final and solicitud.get("resultado_correccion"):
                    self._pintar_principal()
                return
            if final:
                self._pintar_principal()
                self._bajar_al_final()
            elif solicitud.get("lista") is self._chat_lista:
                md = solicitud.get("md")
                if md is not None:
                    md.value = self._texto_chat(solicitud["texto"])
                    try:
                        md.update()
                    except (RuntimeError, AssertionError):
                        pass  # la vista pudo desmontarse al navegar
        if not self.cerrando:
            try:
                self.page.run_task(actualizar)
            except (RuntimeError, AssertionError):
                pass

    def _enviar_pregunta(self, pregunta: Optional[str]) -> None:
        import asistente
        pregunta = (pregunta or "").strip()
        if not pregunta or self.cerrando or not self.cid:
            return
        if len(pregunta) > asistente.MAX_PREGUNTA:
            self.avisar("La pregunta es demasiado larga. Concreta la consulta.", True)
            return
        if not self._turno.acquire(blocking=False):
            self.avisar("Espera a que termine la tarea en curso.")
            return
        cid, ej = self.cid, self.ejercicio
        conv = self.conversaciones.setdefault(cid, asistente.Conversacion(cid))
        solicitud = {"cid": cid, "ejercicio": ej, "pregunta": pregunta, "texto": ""}
        self._chat_en_curso = solicitud
        self._chat_ocupado = True
        pendientes = list(self.informe.pendientes) if self.informe else None

        def tarea():
            ultimo = [0.0]

            def al_texto(texto: str) -> None:
                if self.cerrando:
                    raise asistente.ErrorAsistente("Consulta cancelada al cerrar la aplicación.")
                solicitud["texto"] = texto
                ahora = time.perf_counter()
                if ahora - ultimo[0] > 0.08:  # ~12 repintados/s: fluido sin saturar la interfaz
                    ultimo[0] = ahora
                    self._actualizar_chat(solicitud)

            try:
                import chat_correcciones as cc
                # Exclusivamente el texto escrito por el usuario pasa por este intérprete local.
                gestor = getattr(self, "correcciones_chat", None)
                if gestor is None:
                    gestor = self.correcciones_chat = cc.GestorCorrecciones(lambda: (self.cid, self.ejercicio))
                import acciones_documentales as ad
                gestor_documental = getattr(self, 'acciones_documentales', None)
                if gestor_documental is None:
                    gestor_documental = self.acciones_documentales = ad.GestorAcciones(lambda: (self.cid, self.ejercicio))
                def ejecutar_documental(propuesta):
                    with self.ctrl._lock:
                        if self.cerrando or (self.cid, self.ejercicio) != (cid, ej):
                            raise ValueError('El expediente activo ha cambiado.')
                        res = self.ctrl.ejecutar_accion_documental(propuesta)
                        # Invalidar antes de liberar el turno: ninguna pregunta nueva
                        # puede capturar el contexto anterior, aunque cambie la vista.
                        conv.contexto, conv.huella, conv.modelo = '', '', ''
                        conv.revision_documental = None
                        conv.mensajes.clear()
                        conv.turnos.clear()
                        self.correcciones_chat.descartar(cid)
                        res['conversacion_documental_actualizada'] = True
                        solicitud['resultado_correccion'] = res
                        return res
                respuesta_documental = gestor_documental.procesar(pregunta, cid, ej, ejecutar_documental)
                if respuesta_documental is not None:
                    conv.turnos.extend([asistente.Turno('user', pregunta), asistente.Turno('assistant', respuesta_documental)])
                    conv.ultimo_error, conv.pregunta_fallida = '', ''
                    return
                respuesta_local = gestor.procesar(pregunta, cid, ej,
                    lambda propuesta: self._guardar_correccion_chat(propuesta, solicitud))
                if respuesta_local is not None:
                    if pregunta.lower().startswith("/confirmar "):
                        respuesta_local += "\nPatrimonio recalculado y Excel actualizado."
                    conv.turnos.extend([asistente.Turno("user", pregunta), asistente.Turno("assistant", respuesta_local)])
                    conv.ultimo_error, conv.pregunta_fallida = "", ""
                    return
                # El cierre y las escrituras comparten este bloqueo; la red no lo retiene.
                with self.ctrl._lock:
                    if self.cerrando:
                        return
                    contexto, _ = asistente.construir_contexto(self.ctrl.lector, cid, ej, pendientes)
                    ajustes = self.ctrl.ajustes_ia()
                if self.cerrando:
                    return
                conv.preguntar(pregunta, contexto, ajustes, al_texto)
                for plan in conv.acciones_propuestas:
                    with self.ctrl._lock:
                        propuesta_texto = gestor_documental.proponer(plan, cid, ej, self.ctrl.preparar_accion_documental)
                    conv.turnos[-1].texto = propuesta_texto
                    solicitud['texto'] = propuesta_texto
            except OSError:
                conv.ultimo_error = 'No se pudo acceder a un archivo del expediente. Revisa sus permisos y si está abierto; comprueba el estado antes de repetir la operación.'
                conv.pregunta_fallida = pregunta
            except (asistente.ErrorAsistente, cc.ErrorCorreccion, ValueError) as exc:
                conv.ultimo_error, conv.pregunta_fallida = str(exc), pregunta
            except Exception as exc:  # nunca una traza en pantalla
                conv.ultimo_error = f"Error al preparar la consulta ({exc.__class__.__name__}). Inténtalo de nuevo."
                conv.pregunta_fallida = pregunta
            finally:
                self._chat_ocupado = False
                self._chat_en_curso = None
                self._turno.release()
                self._actualizar_chat(solicitud, final=True)

        try:
            self._pintar_principal()
            self._bajar_al_final()
            self.page.run_thread(tarea)
        except Exception:
            self._chat_ocupado = False
            self._chat_en_curso = None
            self._turno.release()
            conv.ultimo_error = "No se pudo iniciar la consulta. Inténtalo de nuevo."
            conv.pregunta_fallida = pregunta
            if not self.cerrando:
                try:
                    self._pintar_principal()
                    self._chat_campo.value = pregunta
                    self.page.update()
                except (RuntimeError, AssertionError):
                    pass
                self.avisar(conv.ultimo_error, True)

    def _guardar_correccion_chat(self, propuesta, solicitud: dict):
        import chat_correcciones as cc
        with self.ctrl._lock:
            if self.cerrando or (self.cid, self.ejercicio) != (solicitud["cid"], solicitud["ejercicio"]):
                raise cc.ErrorCorreccion("El expediente activo ha cambiado; no se aplica la corrección.")
            try:
                return self.ctrl.aplicar_correccion_chat(propuesta, propuesta.cliente_id, propuesta.ejercicio,
                    al_guardar=lambda resultado: solicitud.update(resultado_correccion=resultado))
            except cc.ErrorCorreccion:
                raise
            except Exception:
                raise cc.GuardadoNoVerificado("No se pudo verificar el guardado completo. Revisa la ficha y el Excel "
                                             "antes de volver a proponer la corrección.") from None

    # ================================================================== residentes (autónomos y pymes)
    def _sec_resumen_res(self) -> ft.Control:
        p, inf = self.p, self.informe
        graves = [x for x in inf.pendientes if x.nivel == "grave"]
        kpis = ft.Row([
            ui.kpi(f"Ingresos {inf.ejercicio}", ui.eur(inf.total_ingresos), p, I.TRENDING_UP),
            ui.kpi("Gastos deducibles", ui.eur(inf.total_gastos), p, I.TRENDING_DOWN),
            ui.kpi("Rendimiento", ui.eur(inf.total_ingresos - inf.total_gastos), p, I.INSIGHTS),
            ui.kpi("Modelos a ingresar", ui.eur(inf.total_a_ingresar), p, I.RECEIPT_LONG_OUTLINED,
                   p.naranja if inf.total_a_ingresar else None),
            ui.kpi("Avisos urgentes", str(len(graves)), p, I.NOTIFICATIONS_ACTIVE_OUTLINED,
                   p.rojo if graves else p.verde, "Ver pendientes" if graves else "Todo en orden",
                   on_click=lambda e: self._ir_a_seccion(1)),
        ], spacing=12)

        def celda(texto, ancho, negrita=False, color=None, derecha=True):
            return ft.Container(ft.Text(texto, size=13, weight=ft.FontWeight.W_600 if negrita else ft.FontWeight.W_400,
                                        color=color or p.texto, font_family=ui.FUENTE,
                                        text_align=ft.TextAlign.RIGHT if derecha else ft.TextAlign.LEFT),
                                expand=ancho, alignment=ft.Alignment.CENTER_RIGHT if derecha else ft.Alignment.CENTER_LEFT)
        cab = ["Trimestre", "Ingresos", "Gastos", "Rendimiento", "IVA 303", "IRPF 130", "111 / 115"]
        anchos = [2, 3, 3, 3, 3, 3, 3]  # proporciones: la tabla ocupa todo el ancho
        filas = [ft.Row([celda(t, w, True, p.texto2, i > 0) for i, (t, w) in enumerate(zip(cab, anchos))], spacing=8)]
        for t in inf.trimestres:
            iva = (ui.eur(t.a_ingresar_303) if not t.a_compensar_303 else f"−{ui.eur(t.a_compensar_303)}")
            filas.append(ft.Row([
                celda(t.periodo, anchos[0], True, derecha=False), celda(ui.eur(t.ingresos), anchos[1]),
                celda(ui.eur(t.gastos), anchos[2]), celda(ui.eur(t.rendimiento), anchos[3]),
                celda(iva, anchos[4], color=p.verde if t.a_compensar_303 else None),
                celda(ui.eur(t.a_ingresar_130) if t.a_ingresar_130 is not None else "—", anchos[5]),
                celda(ui.eur(t.retenciones_111 + t.retenciones_115), anchos[6]),
            ], spacing=8))
        tabla = ui.tarjeta(ft.Column([ui.seccion(f"Trimestres {inf.ejercicio}", p, 15), ui.separador(p), *filas,
                                      ui.leyenda("IVA en verde con signo menos: a compensar en el trimestre siguiente.",
                                                 p, p.texto3)], spacing=8), p)
        act = inf.actividad
        accion = ui.boton("Editar actividad" if act else "Añadir actividad",
                          lambda e: self.dialogo_editar("actividades", act["codigo"] if act else None,
                                                        None if act else {"regimen_iva": "General"}),
                          p, I.EDIT_OUTLINED, "secundario")
        actividad = ui.fila(I.WORK_OUTLINE, p.acento,
                            (act or {}).get("descripcion") or "Actividad sin registrar",
                            f"IAE {(act or {}).get('epigrafe_iae') or '—'} · IVA {inf.regimen_iva}"
                            + (f" · {inf.regimen_irpf}" if inf.tipo_cliente == carteras.TIPO_AUTONOMO else "")
                            + f" · modelos {(act or {}).get('modelos') or 'según régimen'}", p, [accion])
        estados = [ui.fila(I.EVENT_AVAILABLE if "[OK]" in e else I.EVENT_OUTLINED,
                           p.verde if "[OK]" in e else p.naranja, e.split(": ", 1)[0],
                           e.split(": ", 1)[1] if ": " in e else None, p) for e in inf.estados]
        alertas = [ui.fila(I.LIGHTBULB_OUTLINE, ui.colores_estado(a.nivel, p)[0], a.mensaje, "Expediente", p,
                           fondo_icono=ui.colores_estado(a.nivel, p)[1]) for a in inf.alertas]
        return ft.Column([
            kpis, tabla,
            self._bloque("Actividad (036/037)", [actividad]),
            self._bloque("Declaraciones del ejercicio", estados, vacio="Sin declaraciones calculadas todavía."),
            *([self._bloque("Avisos", alertas)] if alertas else []),
        ], spacing=20)

    def _sec_facturas(self, tabla: str) -> ft.Control:
        import fiscal_residentes as fr
        p, lec = self.p, self.ctrl.lector
        emitida = tabla == "facturas_emitidas"
        pagina = self._numero_pagina(tabla)
        filas_bd = lec.listar_pagina(tabla, self.cid, 51, pagina * 50, orden="fecha", ejercicio=self.ejercicio)
        siguiente = len(filas_bd) > 50
        filas = []
        for f in filas_bd[:50]:
            fecha = a_fecha(f.get("fecha"))
            tercero = f.get("receptor") if emitida else f.get("proveedor")
            detalle = " · ".join(x for x in (f"{fecha:%d/%m/%Y}" if fecha else "sin fecha", f.get("trimestre"),
                                             None if emitida else f.get("categoria"), f.get("concepto")) if x)
            hueco = lambda ancho, c=None: ft.Container(c, width=ancho, alignment=ft.Alignment.CENTER_RIGHT)  # noqa: E731
            derecha: list[ft.Control] = [
                hueco(124, ui.pastilla(f"Ret. {ui.eur(fr.retencion(f))}", p.naranja, p.naranja_suave)
                      if fr.retencion(f) else None),
                hueco(124, ui.pastilla(f"IVA {ui.eur(fr.cuota_iva(f))}", p.texto2, p.campo)),
                hueco(110, ft.Text(ui.eur(fr.total_factura(f)), size=14, weight=ft.FontWeight.W_600, color=p.texto)),
                hueco(34, ui.boton_icono(I.OPEN_IN_NEW, lambda e, r=f["ruta"]: self._abrir_soporte(r), p,
                                         "Abrir factura") if f.get("ruta") else None)]
            derecha += [ui.boton_icono(I.EDIT_OUTLINED, lambda e, c=f["codigo"]: self.dialogo_editar(tabla, c), p,
                                       "Corregir"),
                        ui.boton_icono(I.DELETE_OUTLINE, lambda e, c=f["codigo"]: self.confirmar_borrar(tabla, c), p,
                                       "Eliminar", p.rojo)]
            filas.append(ui.fila(I.CALL_MADE if emitida else I.CALL_RECEIVED, p.verde if emitida else p.acento,
                                 f"{f.get('numero') or f['codigo']} · {tercero or 'Sin nombre'}", detalle, p, derecha))
        nuevo = ui.boton("Añadir factura", lambda e: self.dialogo_editar(
            tabla, None, {"fecha": date.today()}), p, I.ADD, "secundario")
        titulo = f"Facturas {'emitidas' if emitida else 'recibidas'} · {self.ejercicio}"
        bloques: list[ft.Control] = []
        if not emitida:
            por_categoria: dict[str, float] = {}
            for f in lec.iterar(tabla, self.cid, ejercicio=self.ejercicio):
                cat = f.get("categoria") or "Otros"
                por_categoria[cat] = por_categoria.get(cat, 0.0) + fr.gasto_irpf(f)
            if por_categoria:
                total = sum(por_categoria.values()) or 1
                barras = [ft.Column([
                    ft.Row([ui.cuerpo(cat, p, peso=ft.FontWeight.W_600), ui.cuerpo(ui.eur(v), p)],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.ProgressBar(value=v / total, color=p.acento, bgcolor=p.campo, bar_height=4, border_radius=2),
                ], spacing=4) for cat, v in sorted(por_categoria.items(), key=lambda kv: -kv[1])]
                bloques.append(ui.tarjeta(ft.Column([ui.seccion("Gasto deducible IRPF por categoría", p, 15), *barras],
                                                    spacing=10), p))
        bloques.append(self._bloque(titulo, filas, accion=nuevo, vacio=(
            "Suelta las facturas en PDF o pulsa «Añadir factura». La app detecta si es emitida o recibida.")))
        bloques += self._navegacion_detalle(tabla, siguiente)
        bloques.append(ui.leyenda("El trimestre y el ejercicio salen de la fecha. Total = base + IVA − retención."
                                  + ("" if emitida else " Gasto IRPF = (base + IVA no deducible) × % deducible."),
                                  p, p.texto3))
        return ft.Column(bloques, spacing=16)

    def _abrir_soporte(self, relativo: str) -> None:
        try:
            ruta = database.ruta_soporte(Path(self.cliente["carpeta"]), relativo)
        except ValueError as exc:
            self.avisar(str(exc), True)
            return
        _abrir_en_sistema(ruta)

    def _filas_modelo_res(self, tabla: str) -> list[dict]:
        orden = {"1T": 1, "2T": 2, "3T": 3, "4T": 4}
        filas = self.ctrl.lector.listar(tabla, self.cid, ejercicio=self.ejercicio)
        return sorted(filas, key=lambda f: (orden.get(f.get("periodo"), 9), f.get("modelo") or ""))

    def _sec_303(self) -> ft.Control:
        p = self.p
        filas = []
        for f in self._filas_modelo_res("m303"):
            detalle = (f"Devengado {ui.eur(f.get('iva_devengado'))} · deducible {ui.eur(f.get('iva_deducible'))}"
                       f" · compensado {ui.eur(f.get('compensacion_anterior'))}"
                       + (f" · a compensar {ui.eur(f.get('a_compensar'))}" if f.get("a_compensar") else ""))
            filas.append(self._fila_modelo("m303", f, f"303 · {f['periodo']} {self.ejercicio}", detalle,
                                           float(f.get("a_ingresar") or 0)))
        return ft.Column([self._bloque(f"IVA · modelo 303 · {self.ejercicio}", filas, vacio=(
            "Sin 303: el régimen de IVA no es el general o aún no hay facturas.")),
            ui.leyenda("IVA de las emitidas − IVA deducible de las recibidas − lo pendiente de compensar. "
                       "Una declaración presentada queda congelada.", p, p.texto3)], spacing=12)

    def _sec_130(self) -> ft.Control:
        p = self.p
        filas = []
        for f in self._filas_modelo_res("m130"):
            detalle = (f"Acumulado: ingresos {ui.eur(f.get('ingresos'))} · gastos {ui.eur(f.get('gastos'))} · "
                       f"rendimiento {ui.eur(f.get('rendimiento'))} · retenciones {ui.eur(f.get('retenciones'))} · "
                       f"pagos anteriores {ui.eur(f.get('pagos_anteriores'))}")
            filas.append(self._fila_modelo("m130", f, f"130 · {f['periodo']} {self.ejercicio}", detalle,
                                           float(f.get("a_ingresar") or 0)))
        return ft.Column([self._bloque(f"IRPF · pago fraccionado 130 · {self.ejercicio}", filas, vacio=(
            "Sin 130: solo para autónomos en estimación directa obligados a presentarlo.")),
            ui.leyenda("20 % del rendimiento acumulado − retenciones − pagos anteriores del año. En la simplificada, "
                       "5 % de difícil justificación (máx. 2.000 €).", p, p.texto3)], spacing=12)

    def _sec_retenciones(self) -> ft.Control:
        p = self.p
        filas = [self._fila_modelo("m111_115", f, f"{f['modelo']} · {f['periodo']} {self.ejercicio}",
                                   f"{f.get('perceptores') or 0} perceptor(es) · base {ui.eur(f.get('base'))}",
                                   float(f.get("retenciones") or 0))
                 for f in self._filas_modelo_res("m111_115")]
        return ft.Column([self._bloque(f"Retenciones · 111 y 115 · {self.ejercicio}", filas, vacio=(
            "Sin retenciones: aparecen al registrar facturas de profesionales o de alquiler de local con "
            "retención.")), ui.leyenda("115: alquiler de local. 111: profesionales y resto.", p, p.texto3)],
            spacing=12)

    def _sec_autonomo(self) -> ft.Control:
        p = self.p
        filas = [ui.fila(I.EVENT_AVAILABLE if "[OK]" in e else I.EVENT_OUTLINED,
                         p.verde if "[OK]" in e else p.naranja, e, None, p) for e in self.estados_aut]
        return self._bloque(f"Calendario fiscal del autónomo · {self.ejercicio}", filas,
                            vacio="Sube el modelo 036/037 para generar el calendario.")

    # ================================================================== acciones
    async def elegir_pdfs(self, _e=None) -> None:
        """Elige documentos (PDF, Excel, Word o CSV) y abre «Revisar documentos». El nombre se conserva por
        compatibilidad con atajos y tests."""
        if not self.cid:
            self.avisar("Selecciona o crea un cliente primero.", True)
            return
        if self.ocupada():
            return
        cid, ej = self.cid, self.ejercicio
        archivos = await self.file_picker.pick_files(dialog_title="Elegir documentos", allow_multiple=True,
                                                     file_type=ft.FilePickerFileType.CUSTOM,
                                                     allowed_extensions=list(adj.EXTENSIONES))
        if not archivos:
            return
        if (cid, ej) != (self.cid, self.ejercicio):
            self.avisar("Has cambiado de expediente o ejercicio. Vuelve a elegir los documentos en el destino correcto.")
            return
        self.revisar_documentos(archivos, cid, ej)

    def revisar_documentos(self, archivos, cid: Optional[int] = None,
                           ejercicio: Optional[int] = None) -> Optional[adj.RevisionDocumentos]:
        """Diálogo previo al procesado: lista, destino fijo y comentario opcional para la IA. Cancelar no importa nada."""
        if getattr(self, "_revision_abierta", False):
            self.avisar("Termina o cancela la revisión de documentos abierta.")
            return None
        cid = self.cid if cid is None else cid
        ej = self.ejercicio if ejercicio is None else ejercicio
        lote = adj.preparar_lote(archivos)
        if not lote.adjuntos:
            self.avisar(" ".join(lote.avisos) or "No hay documentos que procesar.", True)
            return None
        cliente = self.ctrl.lector.get_cliente(cid) or {}
        nombre = " ".join(x for x in (cliente.get("nombre"), cliente.get("apellidos")) if x)

        def vigente() -> bool:
            return not self.cerrando and (self.cid, self.ejercicio) == (cid, ej)

        def procesar(rutas: list[Path], comentario: str) -> Optional[str]:
            if not vigente():
                return "El expediente o el ejercicio ha cambiado. Cancela y vuelve a elegir los documentos."
            if comentario and not _admite_comentario(self.ctrl.procesar_pdf):
                return "Esta versión no puede enviar comentarios a la IA. Borra el comentario para continuar."
            if self._turno.locked():
                return "Hay una tarea en curso. Espera a que termine y pulsa Procesar de nuevo."
            self._revision_abierta = False
            self.page.pop_dialog()
            self.procesar_pdfs(rutas, comentario_guia=comentario)
            return None

        def cancelar_revision(_e=None):
            self._revision_abierta = False
            self.cerrar_dialogo(_e)
        self._revision_abierta = True
        revision = adj.RevisionDocumentos(self.p, lote.adjuntos, adj.Destino(nombre, cliente.get("nif") or "", ej),
                                          procesar, cancelar_revision, destino_vigente=vigente,
                                          refrescar=self.page.update, avisos=lote.avisos)
        dlg = self._dialogo(adj.RevisionDocumentos.TITULO, revision.contenido, revision.acciones, ancho=640,
                            icono=I.FACT_CHECK_OUTLINED)
        if dlg is not None:
            def al_cerrar_revision(e):
                self._revision_abierta = False
                revision.descartar(e)
            dlg.on_dismiss = al_cerrar_revision
        else:
            self._revision_abierta = False
        return revision

    def procesar_pdfs(self, rutas: list[Path], comentario_guia: str = "") -> None:
        """Procesa el lote fuera del hilo UI (PDF, Excel, Word o CSV; el nombre se conserva por compatibilidad).
        Un fallo en un archivo se avisa y el lote sigue; nunca se anuncia como archivado lo que no lo está."""
        cid, ej = self.cid, self.ejercicio
        rutas = [Path(r) for r in rutas]
        comentario_guia = adj.normalizar_comentario(comentario_guia)
        admite = _admite_comentario(self.ctrl.procesar_pdf)
        if comentario_guia and not admite:
            self.avisar("Esta versión no puede enviar comentarios a la IA. No se ha procesado nada.", True)
            return
        extra = {"comentario_guia": comentario_guia} if admite else {}

        def en_ui(fn: Callable[[], None]) -> None:
            ejecutar_en_ui(self.page, lambda: fn() if not self.cerrando else None)

        def trabajo():
            res: dict = {}
            fallos: list[str] = []
            total = len(rutas)
            for i, ruta in enumerate(rutas, 1):
                if self.cerrando:
                    break
                if (self.cid, self.ejercicio) != (cid, ej):
                    en_ui(lambda n=total-i+1: self.avisar(f"Lote detenido por cambio de expediente. Quedan {n} documentos sin procesar."))
                    break
                en_ui(lambda t=f"Leyendo {ruta.name} ({i}/{total})…": self.set_ocupado(True, t))
                try:
                    actual = self.ctrl.procesar_pdf(cid, ruta, ej, confirmar=lambda ext: self.revisar_extraccion(ext, cid, ej),
                                                    **extra)
                except CanceladoPorUsuario:
                    en_ui(lambda n=ruta.name: self.avisar(f"Descartado: {n}"))
                    continue
                except excel_generator.ExcelConflictoImportacion:
                    raise  # afecta a todo el Excel del cliente: se detiene el lote y se muestra la revisión
                except Exception as exc:
                    fallos.append(ruta.name)
                    en_ui(lambda n=ruta.name, m=str(exc) or exc.__class__.__name__:
                          self.avisar(f"No se pudo procesar {n}: {m}", True))
                    continue
                r = (actual or {}).get("resultado")
                if r is None:
                    fallos.append(ruta.name)
                    en_ui(lambda n=ruta.name: self.avisar(f"No se pudo procesar {n}: no se obtuvo ningún resultado.", True))
                    continue
                res = actual
                en_ui(lambda n=ruta.name, r=r: self.avisar(
                    f"Ya estaba archivado como {r.documento}." if getattr(r, "duplicado", False)
                    else f"{n} archivado como {r.documento}. Excel actualizado."))
            if fallos and total > 1:
                nombres = ", ".join(fallos[:3]) + (f" y {len(fallos) - 3} más" if len(fallos) > 3 else "")
                resto = " El resto del lote siguió adelante." if len(fallos) < total else ""
                en_ui(lambda: self.avisar(f"{len(fallos)} de {total} documentos no se pudieron procesar: {nombres}."
                                          + resto, True))
            return res
        self.en_segundo_plano("Procesando documentos…", trabajo)

    def sincronizar(self, _e=None) -> None:
        self.en_segundo_plano("Sincronizando Excel…", lambda cid=self.cid, ej=self.ejercicio: self.ctrl.sincronizar(cid, ej),
                              "Excel sincronizado y obligaciones recalculadas.")

    def _abrir_excel(self, _e=None) -> None:
        ruta = self.ctrl.lector.ruta_excel(self.cliente)
        if ruta.exists():
            _abrir_en_sistema(ruta)
        else:
            self.avisar("El Excel se crea al subir el primer PDF o al sincronizar.", True)

    def _abrir_carpeta(self, _e=None) -> None:
        try:  # solo la carpeta de su cartera, sin seguir enlaces a otro expediente
            _abrir_en_sistema(self.ctrl.lector.verificar_cartera(self.cliente))
        except ValueError as exc:
            self.avisar(str(exc), True)

    def _abrir_bandeja(self, _e=None) -> None:
        bandeja = database.subcarpeta_cliente(Path(self.cliente["carpeta"]), database.SUBCARPETA_ENTRADA)
        bandeja.mkdir(parents=True, exist_ok=True)
        _abrir_en_sistema(bandeja)

    async def elegir_carpeta_clientes(self, _e=None) -> None:
        ruta = await self.file_picker.get_directory_path(dialog_title="Carpeta de clientes",
                                                         initial_directory=str(self.ctrl.lector.carpeta_clientes))
        if ruta:
            self.ctrl.cambiar_carpeta_clientes(Path(ruta))
            self.lbl_carpeta.value, self.lbl_carpeta.tooltip = Path(ruta).name, str(ruta)
            self.page.update()
            self.avisar("Carpeta de clientes actualizada.")

    # ================================================================== diálogos
    def revisar_extraccion(self, ext: pdf_processor.Extraccion, cid: Optional[int] = None,
                           ejercicio: Optional[int] = None) -> pdf_processor.Extraccion:
        """Diálogo de revisión (se llama desde el hilo de trabajo y espera la respuesta del usuario)."""
        p = self.p
        evento = threading.Event()
        self._evento_dialogo = evento
        if self.cerrando:
            self._evento_dialogo = None
            raise CanceladoPorUsuario()
        decision = {"guardar": False, "resuelta": False}
        cid_revision = self.cid if cid is None else cid
        ej_abierto = self.ejercicio if ejercicio is None else ejercicio
        cliente_revision = self.ctrl.lector.get_cliente(self.cid if cid is None else cid) or {}
        identidad = " ".join(x for x in (cliente_revision.get("nombre"), cliente_revision.get("apellidos")) if x)
        destino_revision = ui.tarjeta(ft.Column([
            ui.leyenda("EXPEDIENTE DE DESTINO", p),
            ui.cuerpo(identidad or "Cliente seleccionado", p, peso=ft.FontWeight.W_600),
            ui.leyenda(f"NIF/NIE: {cliente_revision.get('nif') or 'sin indicar'} · Ejercicio abierto: {ej_abierto}", p),
        ], spacing=4, tight=True), p, padding=12)
        comentario = str(getattr(ext, "comentario_guia", "") or "").strip()  # lo adjunta el backend si lo usó
        comentario_revision = [ft.Container(ft.Column([
            ui.leyenda("TU COMENTARIO PARA LA IA", p),
            ui.cuerpo(comentario, p, p.texto2, 13, max_lines=4),
        ], spacing=4, tight=True), bgcolor=p.campo, border_radius=12, padding=12)] if comentario else []
        ejercicio_detectado = ext.campos.get("ejercicio")
        if ext.tipo == pdf_processor.TipoDocumento.FACTURA_GASTO and not ejercicio_detectado and ext.campos.get("fecha"):
            try:
                ejercicio_detectado = a_fecha(ext.campos["fecha"]).year
            except (ValueError, TypeError):
                pass  # La revisión del campo fecha mostrará el error de formato.
        ej_registro = ui.campo("Ejercicio de registro", p, str(ejercicio_detectado or ej_abierto),
                               ayuda="Año al que se vinculan los datos; la fecha del documento se conserva.")
        confirmar_destino = ft.Checkbox(label="Confirmo el ejercicio de registro distinto del que tengo abierto",
                                       value=False, label_style=ft.TextStyle(color=p.texto, size=13),
                                       active_color=p.acento)
        aviso_ejercicio = ui.cuerpo("", p, p.naranja, 13)

        def revisar_ejercicio(_e=None):
            ej_registro.error = None
            distinto = (ej_registro.value or "").strip() != str(ej_abierto)
            confirmar_destino.visible = distinto
            confirmar_destino.value = False
            aviso_ejercicio.value = (f"Estás consultando {ej_abierto}, pero registrarás este documento en "
                                     f"{ej_registro.value or 'un ejercicio sin indicar'}. Comprueba el destino."
                                     if distinto else f"Se registrará en el ejercicio abierto: {ej_abierto}.")
            self.page.update()
        ej_registro.on_change = revisar_ejercicio
        confirmar_destino.visible = str(ejercicio_detectado or ej_abierto) != str(ej_abierto)
        aviso_ejercicio.value = (f"El documento indica {ejercicio_detectado}, distinto del ejercicio abierto {ej_abierto}. "
                                "Confirma el destino o corrígelo antes de guardar."
                                if confirmar_destino.visible else f"Se registrará en el ejercicio abierto: {ej_abierto}.")
        tipos = [(t.value, pdf_processor.TIPOS_LEGIBLES[t]) for t in pdf_processor.TipoDocumento]
        dd_tipo = ui.desplegable("Tipo de documento", tipos, p, ext.tipo.value, expand=True)
        inmuebles = self.ctrl.lector.listar("inmuebles", self.cid if cid is None else cid)
        dd_inm = ui.desplegable("Inmueble", [("AUTO", "Detectar automáticamente")] +
                                [(i["codigo"], f"{i['codigo']} · {i.get('direccion') or i.get('ref_catastral') or ''}")
                                 for i in inmuebles], p, "AUTO", expand=True)
        sw_gan = ui.interruptor(bool(ext.campos.get("gananciales_forzado")), None, p,
                                etiqueta="Gananciales: forzar 50 % de titularidad")
        sw_gan.visible = ext.tipo == pdf_processor.TipoDocumento.ESCRITURA_COMPRA
        def ajustar_tipo(_e=None):
            sw_gan.visible = dd_tipo.value == pdf_processor.TipoDocumento.ESCRITURA_COMPRA.value
            self.page.update()
        dd_tipo.on_select = ajustar_tipo
        claves = [c for c in ext.campos if c not in ("gananciales_forzado", "inmueble", "ejercicio")]
        for sugerido in ia_extractor.CAMPOS_POR_TIPO.get(ext.tipo.value, ()):
            if sugerido not in claves and sugerido != "ejercicio":
                claves.append(sugerido)
        campos: dict[str, ft.TextField] = {}
        controles: list[ft.Control] = []
        for clave in claves:
            tipo = ia_extractor.CAMPOS.get(clave, "t")
            valor = ext.campos.get(clave)
            texto = ""
            if valor not in (None, ""):
                try:
                    texto = (a_fecha(valor).strftime("%d/%m/%Y") if tipo == "f" else
                             ui.eur(valor).replace(" €", "") if tipo == "i" else
                             ui.pct(valor) if tipo == "p" else str(valor))
                except (ValueError, TypeError):
                    texto = str(valor)
            evid = ext.evidencias.get(clave)
            tf = ui.campo(ETIQUETAS_EXTRACCION.get(clave, clave.replace("_", " ").capitalize()), p, texto,
                          ayuda=f"«{evid}»" if evid and evid != "Lector local" else evid,
                          hint="No encontrado: escríbelo si lo ves en el PDF" if not texto else None)
            campos[clave] = tf
            controles.append(ft.Container(tf, col={"xs": 12, "sm": 6}))
        motor = (ui.pastilla("Leído con IA · Claude", p.verde, p.verde_suave) if ext.motor == "ia"
                 else ui.pastilla("Lector local", p.naranja, p.naranja_suave))
        conf_color = p.verde if ext.confianza >= 0.8 else p.naranja if ext.confianza >= 0.5 else p.rojo
        avisos = [ft.Container(ft.Row([ft.Icon(I.WARNING_AMBER_ROUNDED, size=16, color=p.naranja),
                                       ft.Container(ui.cuerpo(a, p, p.naranja, 13), expand=True)], spacing=8),
                               bgcolor=p.naranja_suave, border_radius=12, padding=10) for a in ext.avisos]
        error = ui.leyenda("", p, p.rojo, 13)

        def mostrar_error(mensaje, campo=None):
            error.value = mensaje
            campo = campo if campo is not None else ej_registro
            campo.error, campo.error_max_lines = mensaje, 3
            if hasattr(self.page, "run_task"):
                self.page.run_task(campo.focus)
            self.page.update()

        def guardar(_e):
            if decision["resuelta"]:
                return
            if (self.cid, self.ejercicio) != (cid_revision, ej_abierto):
                mostrar_error("El expediente o el ejercicio ha cambiado. Descarta esta revisión y vuelve a subir el PDF en el destino correcto.")
                return
            try:
                destino = int((ej_registro.value or "").strip())
                if not 1900 <= destino <= 2200:
                    raise ValueError()
            except ValueError:
                mostrar_error("Indica un ejercicio válido de cuatro cifras entre 1900 y 2200.", ej_registro)
                return
            if destino != ej_abierto and not confirmar_destino.value:
                mostrar_error("Confirma el ejercicio de registro o cámbialo al ejercicio abierto.", ej_registro)
                return
            nuevos: dict[str, Any] = {}
            for k, tf in campos.items():
                v = (tf.value or "").strip()
                if not v:
                    continue
                try:
                    nuevos[k] = ia_extractor.convertir_valor(k, v)
                except (ValueError, TypeError):
                    mostrar_error(f"Revisa «{ETIQUETAS_EXTRACCION.get(k, k.replace('_', ' ').capitalize())}»: formato no válido.", tf)
                    return
            if ext.registros and not revisar_movimientos.value:
                mostrar_error("Revisa y confirma los movimientos propuestos antes de guardarlos.")
                return
            tipo_elegido = pdf_processor.TipoDocumento(dd_tipo.value)
            if tipo_elegido == pdf_processor.TipoDocumento.FACTURA_GASTO and nuevos.get("fecha"):
                if a_fecha(nuevos["fecha"]).year != destino:
                    mostrar_error("El ejercicio no coincide con la fecha de la factura. Corrige la fecha o el ejercicio antes de guardar.", ej_registro)
                    return
            ext.tipo = tipo_elegido
            nuevos["ejercicio"] = destino
            if dd_inm.value and dd_inm.value != "AUTO":
                nuevos["inmueble"] = dd_inm.value
            if ext.tipo == pdf_processor.TipoDocumento.ESCRITURA_COMPRA:
                nuevos["gananciales_forzado"] = 1 if sw_gan.value else 0
                if sw_gan.value:
                    nuevos["pct_titularidad"] = 0.5
            ext.campos = nuevos
            decision["guardar"] = True
            decision["resuelta"] = True
            self.page.pop_dialog()
            evento.set()

        def descartar(_e=None, cerrar=True):
            if decision["resuelta"]:
                return
            decision["resuelta"] = True
            if cerrar:
                self.page.pop_dialog()
            evento.set()

        movimientos = []
        revisar_movimientos = ft.Checkbox(label="He revisado los movimientos propuestos", value=False,
                                          visible=bool(ext.registros), active_color=p.acento)
        if ext.registros:
            for numero, registro in enumerate(ext.registros, 1):
                datos_fila = registro.campos
                importe = datos_fila.get("importe_cobrado") if registro.tabla == "cobros" else datos_fila.get("total")
                def fecha_visible(valor):
                    try:
                        return a_fecha(valor).strftime("%d/%m/%Y") if valor else "Sin indicar"
                    except (ValueError, TypeError):
                        return str(valor)
                texto_fila = (f"{numero}. {'Cobro' if registro.tabla == 'cobros' else 'Gasto'} · "
                              f"{fecha_visible(datos_fila.get('periodo_desde'))} — "
                              f"{fecha_visible(datos_fila.get('periodo_hasta'))} · {ui.eur(importe) if importe is not None else 'Importe sin indicar'}")
                movimientos.append(ft.Column([ui.cuerpo(texto_fila, p), ui.leyenda(registro.evidencia, p),
                    *[ui.leyenda(f"{ETIQUETAS_EXTRACCION.get(campo, campo.replace('_', ' ').capitalize())}: {fecha_visible(valor) if campo in {'fecha', 'fecha_cobro'} else ui.eur(valor) if campo in {'base', 'iva'} else valor}", p)
                      for campo, valor in datos_fila.items()
                      if valor not in (None, "") and campo not in {"periodo_desde", "periodo_hasta", "importe_cobrado", "total"}]], spacing=2))
        bloque_movimientos = ft.Column([
            ui.leyenda(f"{len(ext.registros)} MOVIMIENTOS PROPUESTOS", p),
            ui.leyenda("Comprueba importes, periodos y fuentes. Si hay errores, descarta y ajusta el comentario; "
                       "también podrás corregir las filas guardadas en Cobros o Gastos.", p),
            ft.Column(movimientos, spacing=10),
            revisar_movimientos], visible=bool(ext.registros), spacing=8)
        contenido = ft.Column([
            destino_revision,
            *([bloque_movimientos] if ext.registros else []),
            *comentario_revision,
            ft.Row([motor, ui.leyenda(f"Confianza {round(ext.confianza * 100)} %", p, conf_color)], spacing=10),
            *avisos,
            ft.Row([dd_tipo, dd_inm], spacing=10),
            ej_registro,
            aviso_ejercicio,
            confirmar_destino,
            ui.leyenda("La fecha de una escritura indica cuándo se compró el inmueble; no se cambia para consultar otro año. "
                       "Un certificado o una factura puede corresponder a un ejercicio distinto.", p),
            sw_gan,
            ui.separador(p),
            ui.leyenda("Comprueba cada dato. Cuando hay una cita del PDF, se muestra debajo del campo.", p),
            ft.ResponsiveRow(controles, spacing=10, run_spacing=10),
            error,
        ], spacing=12, tight=True, scroll=ft.ScrollMode.AUTO)
        dlg = self._dialogo(f"Revisar · {ext.nombre_archivo}", contenido,
                      [ui.accion("Descartar", descartar, p),
                       ui.accion("Guardar y calcular", guardar, p, I.CHECK, "primario")], ancho=720, icono=I.FACT_CHECK_OUTLINED)
        if dlg is not None:
            dlg.on_dismiss = lambda e: descartar(e, cerrar=False)
        evento.wait()
        self._evento_dialogo = None
        if self.cerrando or not decision["guardar"]:
            raise CanceladoPorUsuario()
        return ext

    def dialogo_editar(self, tabla: str, codigo: Optional[str], iniciales: Optional[dict] = None) -> None:
        """Editor genérico para corregir a mano cualquier fila (o crear una nueva)."""
        if tabla == "documentos" and codigo:
            self.dialogo_documento(codigo, "corregir_ejercicio")
            return
        p = self.p
        fila = dict((self.ctrl.lector.get(tabla, self.cid, codigo) if codigo else None) or {})
        for k, v in (iniciales or {}).items():
            fila.setdefault(k, v)
        nombres = etiquetas(tabla)
        calculados = CALCULADOS.get(tabla, set())
        controles: dict[str, ft.Control] = {}
        originales: dict[str, str] = {}
        bloques: list[ft.Control] = []
        for col in campos_editables(tabla):
            valor = _texto_valor(col, fila.get(col.campo))
            originales[col.campo] = "" if (not codigo and col.campo in (iniciales or {})) else valor
            opciones = OPCIONES.get((tabla, col.campo))
            if col.campo == "gananciales_forzado":
                ctl: ft.Control = ft.Row([ui.cuerpo(nombres[col.campo], p), ft.Container(expand=True),
                                          ui.interruptor(bool(fila.get(col.campo)), None, p)])
                originales[col.campo] = "1" if fila.get(col.campo) else "0"
            elif opciones:
                ops = [("", "—")] + [(o, o) for o in opciones]
                if valor and valor not in opciones:
                    ops.append((valor, valor))
                ctl = ui.desplegable(nombres[col.campo], ops, p, valor or "", expand=True)
            else:
                ayuda = "Se recalcula solo" if col.campo in calculados else (
                    "dd/mm/aaaa" if col.tipo == "date" else "50 % o 0,5" if col.tipo == "pct" else None)
                if tabla == "residencias" and col.campo == "ejercicio" and codigo:
                    ayuda = "Para trasladar un certificado usa Documentos → Corregir ejercicio."
                ctl = ui.campo(nombres[col.campo], p, valor, ayuda=ayuda,
                               solo_lectura=(tabla == "residencias" and col.campo == "ejercicio" and bool(codigo)))
            controles[col.campo] = ctl
            bloques.append(ft.Container(ctl, col={"xs": 12, "sm": 6}))
        error = ui.leyenda("", p, p.rojo, 13)

        def leer(ctl: ft.Control) -> str:
            if isinstance(ctl, ft.Row):  # interruptor
                return "1" if ctl.controls[-1].value else "0"
            return (ctl.value or "").strip()

        def guardar(_e):
            cambios = {c: leer(ctl) for c, ctl in controles.items() if leer(ctl) != originales[c]}
            if not cambios:
                self.page.pop_dialog()
                return
            errores = self.ctrl.validar(tabla, {k: v for k, v in cambios.items() if v != ""})
            if errores:
                error.value = " · ".join(errores.values())
                self.page.update()
                return
            self.page.pop_dialog()
            self.en_segundo_plano("Guardando y recalculando…",
                                  lambda cid=self.cid, ej=self.ejercicio: self.ctrl.editar(cid, tabla, codigo, cambios, ej),
                                  "Cambios guardados. Excel actualizado.")

        hoja = TABLAS[tabla].hoja.split("_", 1)[-1].replace("_", " ").title()
        titulo = f"Corregir · {hoja} {codigo}" if codigo else f"Nuevo · {hoja}"
        aviso = [ui.leyenda("Los campos «Se recalcula solo» los calcula la app. Si pones el estado en Presentado, "
                            "la fila queda fijada.", p, p.texto3)] if calculados else []
        self._dialogo(titulo, ft.Column([*aviso, ft.ResponsiveRow(bloques, spacing=10, run_spacing=12), error],
                                        spacing=12, tight=True),
                      [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"),
                       ui.boton("Guardar", guardar, p, I.CHECK)], ancho=760, icono=I.EDIT_NOTE)

    def dialogo_presentado(self, tabla: str, codigo: str) -> None:
        p = self.p
        fila = self.ctrl.lector.get(tabla, self.cid, codigo) or {}
        importe = (fila.get("cuota") or fila.get("a_ingresar") or fila.get("retenciones") or fila.get("resultado")
                   or fila.get("pagado"))
        f_fecha = ui.campo("Fecha de presentación", p, date.today().strftime("%d/%m/%Y"), "dd/mm/aaaa")
        f_importe = ui.campo("Importe pagado (€)", p, ui.eur(importe).replace(" €", "") if importe else "")
        campo_just = next((c for c in CAMPOS_PRESENTACION.get(tabla, ()) if c in ("nrc", "csv")), None)
        f_just = ui.campo("NRC / CSV del justificante", p) if campo_just else None
        error = ui.leyenda("", p, p.rojo, 13)

        def guardar(_e):
            errores = self.ctrl.validar(tabla, {k: v for k, v in {"fecha_presentacion": f_fecha.value,
                                                                  "pagado": f_importe.value}.items() if v})
            if errores:
                error.value = " · ".join(errores.values())
                self.page.update()
                return
            self.page.pop_dialog()
            self.en_segundo_plano("Marcando como presentada…", lambda cid=self.cid, ej=self.ejercicio: self.ctrl.marcar_presentado(
                cid, tabla, codigo, ej, f_fecha.value or None, f_importe.value or None,
                f_just.value if f_just else None), "Declaración marcada como presentada.")

        self._dialogo("Marcar como presentada", ft.Column(
            [ui.leyenda("Consejo: si subes el justificante en PDF se marca solo, con su NRC.", p),
             f_fecha, f_importe, *([f_just] if f_just else []), error], spacing=12, tight=True),
            [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"), ui.boton("Marcar presentada", guardar, p, I.CHECK)],
            ancho=420, icono=I.TASK_ALT)

    def dialogo_vendido(self, cod: str) -> None:
        p = self.p
        f_fecha = ui.campo("Fecha de venta", p, "", "dd/mm/aaaa")
        f_precio = ui.campo("Precio de venta (€)", p)
        f_gastos = ui.campo("Gastos de venta (€)", p, "", "Notaría, plusvalía, agencia…")
        error = ui.leyenda("", p, p.rojo, 13)

        def guardar(_e):
            errores = self.ctrl.validar("inmuebles", {"fecha_venta": f_fecha.value, "precio_venta": f_precio.value,
                                                      "gastos_venta": f_gastos.value or None})
            if not f_fecha.value or not f_precio.value:
                errores["x"] = "Indica fecha y precio de venta"
            if errores:
                error.value = " · ".join(errores.values())
                self.page.update()
                return
            self.page.pop_dialog()
            self.en_segundo_plano("Registrando la venta…", lambda cid=self.cid, ej=self.ejercicio: self.ctrl.marcar_vendido(
                cid, cod, f_fecha.value, f_precio.value, ej,
                database.a_num(f_gastos.value) if f_gastos.value else None),
                "Venta registrada: revisa el 210 de ganancia en Pendientes.")

        self._dialogo(f"Marcar vendido · {cod}", ft.Column([
            ui.leyenda("Se calcula hasta la fecha de venta, se avisa del 210 de ganancia patrimonial y el inmueble "
                       "deja de computar en años siguientes.", p), f_fecha, f_precio, f_gastos, error],
            spacing=12, tight=True),
            [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"), ui.boton("Registrar venta", guardar, p)],
            ancho=440, icono=I.SELL_OUTLINED)

    def confirmar_borrar(self, tabla: str, codigo: str) -> None:
        p = self.p

        def borrar(_e):
            self.page.pop_dialog()
            self.en_segundo_plano("Eliminando…", lambda cid=self.cid, ej=self.ejercicio: self.ctrl.borrar_registro(cid, tabla, codigo, ej),
                                  f"{codigo} eliminado.")
        self._dialogo(f"¿Eliminar {codigo}?",
                      ui.cuerpo("Se borra de la base de datos y del Excel. No se puede deshacer.", p, p.texto2),
                      [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"),
                       ui.boton("Eliminar", borrar, p, I.DELETE_OUTLINE, "peligro")], ancho=400, icono=I.DELETE_OUTLINE)

    def dialogo_nuevo_cliente(self, _e=None) -> None:
        p = self.p
        cartera_alta, ejercicio_alta = self.cartera, self.ejercicio
        creado = {"valor": False}
        f = {k: ui.campo(t, p) for k, t in (("nombre", "Nombre / razón social"), ("apellidos", "Apellidos"),
                                             ("nif", "NIF / NIE"), ("pais", "País de residencia fiscal"))}
        dd_iva = ui.desplegable("Régimen de IVA", [(r, r) for r in carteras.REGIMENES_IVA], p, "General")
        dd_irpf = ui.desplegable("Régimen de IRPF", [(r, r) for r in carteras.REGIMENES_IRPF], p,
                                 "Directa simplificada")
        ayuda = ui.leyenda("", p)
        error = ui.leyenda("", p, p.rojo, 13)
        error.visible = False  # sin hueco hasta que haya un error que mostrar

        def ajustar(_e=None):
            residente = cartera_de(dd_tipo.value) == RES
            f["pais"].visible = not residente
            dd_iva.visible = residente
            dd_irpf.visible = dd_tipo.value == carteras.TIPO_AUTONOMO
            ayuda.value = (f"Se crea en {carteras.RAIZ[cartera_de(dd_tipo.value)]}. "
                           + ("Sube después el 036/037 y las facturas." if residente else
                              "El país decide el tipo: UE/EEE 19 % con gastos; resto 24 % sin gastos."))
            self.page.update()

        tipos_alta = [t for t in carteras.TIPOS_CLIENTE if cartera_de(t) == cartera_alta]
        dd_tipo = ui.desplegable("Tipo de cliente", [(t, t) for t in tipos_alta], p,
                                 carteras.tipo_por_defecto(cartera_alta), on_select=ajustar)

        def crear(_e):
            if creado["valor"]:
                return
            if self.cartera != cartera_alta or dd_tipo.value not in tipos_alta:
                error.value, error.visible = "La cartera ha cambiado. Cierra este formulario y vuelve a abrir Nuevo cliente.", True
                self.page.update()
                return
            if not (f["nombre"].value or "").strip() or not (f["nif"].value or "").strip():
                error.value, error.visible = "Nombre y NIF/NIE son obligatorios.", True
                self.page.update()
                return
            try:
                creado["valor"] = True
                residente = cartera_de(dd_tipo.value) == RES
                nuevo = self.ctrl.crear_cliente(
                    f["nombre"].value.strip(), (f["apellidos"].value or "").strip(), f["nif"].value.strip(),
                    dd_tipo.value or "No residente", None if residente else (f["pais"].value or "").strip() or None,
                    ejercicio_alta, regimen_iva=dd_iva.value if residente else None,
                    regimen_irpf=dd_irpf.value if dd_tipo.value == carteras.TIPO_AUTONOMO else None)
            except ValueError as exc:
                creado["valor"] = False
                error.value, error.visible = str(exc), True
                self.page.update()
                return
            except Exception:
                creado["valor"] = False
                error.value, error.visible = "No se pudo crear el cliente. Revisa que la carpeta esté disponible y vuelve a intentarlo.", True
                self.page.update()
                return
            self.page.pop_dialog()
            destino = cartera_de(nuevo.get("tipo_cliente"))
            if destino != self.cartera:  # el cliente nace en su cartera: se muestra esa cartera
                self._filtros[self.cartera], self._paginas[self.cartera] = self.filtro, self._pagina_clientes
                self.cartera, self.filtro, self._pagina_clientes = destino, "", 0
                self.selector_cartera.selected_index = CARTERAS.index(destino)
                self.buscar.value = ""
                self.lbl_lista.value = ETIQUETA[destino].upper()
                self.seccion = 0
            self.abrir_cliente(nuevo["id"])
            self.avisar(f"Cliente {nuevo['nombre']} {nuevo['apellidos']} creado en {ETIQUETA[destino]}.")

        ajustar()
        f["nombre"].autofocus = True
        f["nif"].on_submit = crear
        self._dialogo("Nuevo cliente", ft.Column([f["nombre"], f["apellidos"], f["nif"], dd_tipo, f["pais"], dd_iva,
                                                  dd_irpf, ayuda, error], spacing=12, tight=True, scroll=ft.ScrollMode.AUTO),
                      [ui.accion("Cancelar", self.cerrar_dialogo, p), ui.accion("Crear", crear, p, I.ADD, "primario")],
                      ancho=440, icono=I.PERSON_ADD_ALT)

    def _ir_a_seccion(self, indice: int) -> None:
        self.seccion = indice
        self._pintar_principal()

    def confirmar_eliminar_cliente(self, _e=None) -> None:
        p, c = self.p, self.cliente
        if not c or self.ocupada():
            return
        nombre = f"{c['nombre']} {c['apellidos']}"

        def eliminar(_ev):
            if self.cerrando or not self._turno.acquire(blocking=False):
                if not self.cerrando:
                    self.avisar("Espera a que termine la tarea en curso.")
                return
            cid = c["id"]  # el cliente mostrado en la confirmación

            def terminar(destino, restantes, error):
                try:
                    if not self.cerrando:
                        if error is not None:
                            self.avisar(str(error), error=True)
                        else:
                            self.cid = restantes[0]["id"] if restantes else None
                            self.informe = None
                            self.avisar(f"{nombre} eliminado. Su carpeta se guardó en {destino.parent.name}/{destino.name}."
                                        if destino.exists() else f"{nombre} eliminado. No tenía carpeta que archivar.")
                finally:
                    self._turno.release()
                    if not self.cerrando:
                        self.set_ocupado(False)
                        self._refrescar_clientes()
                        if self.cid:
                            self.abrir_cliente(self.cid)
                        else:
                            self._pintar_principal()

            def tarea():
                destino, restantes, error = None, [], None
                try:
                    if not self.cerrando:
                        ejecutar_en_ui(self.page, lambda: self.set_ocupado(True, f"Eliminando a {nombre}…")
                                       if not self.cerrando else None)
                        destino = self.ctrl.eliminar_cliente(cid)
                        restantes = self.ctrl.lector.listar_clientes(limite=1, cartera=self.cartera)
                except Exception as exc:
                    error = exc
                try:
                    ejecutar_en_ui(self.page, lambda: terminar(destino, restantes, error))
                except Exception:
                    if self._turno.locked():
                        self._turno.release()
                    raise
            try:
                self.page.pop_dialog()
                self.page.run_thread(tarea)
            except Exception:
                self._turno.release()
                raise

        self._dialogo(f"¿Eliminar a {nombre}?", ft.Column([
            ui.cuerpo("Desaparece de la lista y de la base de datos.", p),
            ft.Container(ft.Row([ft.Icon(I.INVENTORY_2_OUTLINED, size=18, color=p.texto2),
                                 ft.Container(ui.leyenda(
                                     "Su carpeta con los PDFs y el Excel no se borra: se mueve a «_Eliminados» "
                                     "dentro de la carpeta de clientes, por si la necesitas.", p), expand=True)],
                                spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
                         bgcolor=p.campo, border_radius=12, padding=12),
        ], spacing=12, tight=True),
            [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"),
             ui.boton("Eliminar cliente", eliminar, p, I.DELETE_OUTLINE, "peligro")],
            ancho=440, icono=I.PERSON_REMOVE_OUTLINED)

    def dialogo_editar_cliente(self, _e=None) -> None:
        p, c = self.p, self.cliente
        cid, ej = self.cid, self.ejercicio
        campos_c = (("nombre", "Nombre"), ("apellidos", "Apellidos"), ("pasaporte", "Pasaporte / ID extranjero"),
                    ("nacionalidad", "Nacionalidad"), ("email", "Email"), ("telefono", "Teléfono"),
                    ("responsable", "Responsable del expediente"))
        f = {k: ui.campo(t, p, c.get(k) or "") for k, t in campos_c}
        dd_tipo = ui.desplegable("Tipo de cliente", [(t, t) for t in carteras.TIPOS_CLIENTE], p,
                                 c.get("tipo_cliente") or carteras.TIPO_NR)
        res = self.ctrl.lector.residencia(self.cid, self.ejercicio) or {}
        del_anio = res.get("ejercicio") == self.ejercicio
        f_pais = ui.campo(f"País de residencia {self.ejercicio}", p, (res.get("pais") or "") if del_anio else "")
        dd_cert = ui.desplegable("Certificado de residencia", [("", "—"), ("Sí", "Sí"), ("No", "No"),
                                                               ("Pendiente", "Pendiente")], p,
                                 (res.get("certificado") or "") if del_anio else "")

        def guardar(_e):
            datos = {k: (tf.value or "").strip() for k, tf in f.items()
                     if (tf.value or "").strip() != (c.get(k) or "")}
            pais, cert = (f_pais.value or "").strip(), dd_cert.value or ""
            residencia = {k: v for k, v, anterior in (
                ("pais", pais, (res.get("pais") or "") if del_anio else ""),
                ("certificado", cert, (res.get("certificado") or "") if del_anio else ""),
            ) if v != anterior}
            nuevo_tipo = dd_tipo.value or c.get("tipo_cliente")
            if nuevo_tipo != c.get("tipo_cliente") and cartera_de(nuevo_tipo) != cartera_de(c.get("tipo_cliente")):
                self.page.pop_dialog()
                self._confirmar_cambio_cartera(cid, nuevo_tipo, ej)
                return
            self.page.pop_dialog()

            def trabajo():
                with self.ctrl._lock:
                    self.ctrl._importar(cid)
                    if nuevo_tipo != c.get("tipo_cliente"):
                        self.ctrl.db.actualizar_cliente(cid, tipo_cliente=nuevo_tipo)
                    self.ctrl.db.actualizar_cliente(cid, **datos)
                    if residencia:
                        cambios = dict(residencia)
                        if cambios.get("pais"):
                            cambios["grupo"] = tax_calculator.grupo_territorio(cambios["pais"])
                        codigo = self.ctrl.db.guardar_residencia(cid, ej, **cambios)
                        for campo, valor in residencia.items():
                            if valor == "":
                                self.ctrl.db.vaciar_campo("residencias", cid, codigo, campo)
                    return self.ctrl._recalcular_y_exportar(cid, {ej}, ej)
            self.en_segundo_plano("Guardando datos del cliente…", trabajo, "Datos del cliente actualizados.")
            self._refrescar_clientes()

        self._dialogo("Datos del cliente", ft.Column([
            ft.ResponsiveRow([ft.Container(tf, col={"xs": 12, "sm": 6}) for tf in f.values()], spacing=10, run_spacing=12),
            dd_tipo,
            *([] if cartera_de(c.get("tipo_cliente")) == RES else [
                ui.separador(p), ui.seccion(f"Residencia fiscal {self.ejercicio}", p, 15),
                ft.Row([ft.Container(f_pais, expand=True), ft.Container(dd_cert, expand=True)], spacing=10)]),
        ], spacing=12, tight=True), [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"),
                                     ui.boton("Guardar", guardar, p, I.CHECK)], ancho=640, icono=I.BADGE_OUTLINED)

    def _confirmar_cambio_cartera(self, cid: int, nuevo_tipo: str, ejercicio: int) -> None:
        p = self.p
        destino = cartera_de(nuevo_tipo)

        def mover(_e):
            self.page.pop_dialog()

            def trabajo():
                return self.ctrl.cambiar_tipo_cliente(cid, nuevo_tipo, ejercicio)

            def al_terminar():
                self._filtros[self.cartera], self._paginas[self.cartera] = self.filtro, self._pagina_clientes
                self.cartera, self.filtro, self._pagina_clientes = destino, "", 0
                self.selector_cartera.selected_index = CARTERAS.index(destino)
                self.buscar.value = ""
                self.lbl_lista.value = ETIQUETA[destino].upper()
                self.seccion = 0
                self._refrescar_clientes()
            self.en_segundo_plano(f"Moviendo el expediente a {ETIQUETA[destino]}…", trabajo,
                                  f"Cliente movido a {ETIQUETA[destino]}.", despues=al_terminar)

        self._dialogo("Cambiar de cartera", ft.Column([
            ui.cuerpo(f"El cliente pasará a {ETIQUETA[destino]} como «{nuevo_tipo}».", p),
            ui.leyenda(f"Su carpeta se moverá a {carteras.RAIZ[destino]} con todos sus PDFs. El Excel actual se "
                       "guarda en _backups y se genera el de la nueva plantilla. No se permite si tiene "
                       "declaraciones presentadas.", p),
        ], spacing=10, tight=True), [ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"),
                                     ui.boton("Mover expediente", mover, p, I.DRIVE_FILE_MOVE_OUTLINE)],
            ancho=460, icono=I.SWAP_HORIZ)

    def dialogo_ajustes(self, _e=None) -> None:
        p = self.p
        aj = self.ctrl.ajustes_ia()
        sw = ui.interruptor(aj.activada, None, p)
        hay = ia_extractor.hay_clave()
        f_clave = ui.campo("Clave API de Anthropic", p, "", password=True,
                           hint="Guardada ✓ (escribe otra para cambiarla)" if hay else "sk-ant-…",
                           ayuda="Se guarda en el almacén de credenciales de Windows, no en el proyecto.")
        dd_modelo = ui.desplegable("Modelo", [("claude-opus-5-5", "Claude Opus 5.5 · máxima precisión"),
                                              ("claude-sonnet-5-5", "Claude Sonnet 5.5 · equilibrado"),
                                              ("claude-haiku-5-5", "Claude Haiku 5.5 · más barato")], p, aj.modelo)
        dd_esf = ui.desplegable("Esfuerzo de lectura", [("low", "Bajo · rápido"), ("medium", "Medio · recomendado"),
                                                        ("high", "Alto · documentos difíciles")], p, aj.esfuerzo)

        def guardar(_e):
            nuevo = ia_extractor.AjustesIA(activada=bool(sw.value), modelo=dd_modelo.value or aj.modelo,
                                           esfuerzo=dd_esf.value or aj.esfuerzo)
            try:
                self.ctrl.guardar_ajustes_ia(nuevo, (f_clave.value or "").strip() or None)
            except Exception as exc:
                self.avisar(str(exc), True)
                return
            self.page.pop_dialog()
            self._pintar_principal()
            self.avisar("Ajustes guardados.")

        def borrar(_e):
            self.ctrl.borrar_clave_ia()
            self.page.pop_dialog()
            self._pintar_principal()
            self.avisar("Clave API eliminada de este equipo.")

        self._dialogo("Ajustes e IA", ft.Column([
            ft.Row([ft.Column([ui.cuerpo("Leer los PDFs con IA (Claude)", p, peso=ft.FontWeight.W_600),
                               ui.leyenda("Entiende cualquier formato de notaría, ayuntamiento o contrato, "
                                          "también escaneados.", p)], spacing=2, expand=True), sw]),
            f_clave, dd_modelo, dd_esf,
            ft.Container(ft.Row([ft.Icon(I.SHIELD_OUTLINED, size=18, color=p.texto2),
                                 ft.Container(ui.leyenda(
                                     "Privacidad: cada PDF se envía a la API de Anthropic para leerlo. Revisa tu "
                                     "contrato de encargo de tratamiento (RGPD). Sin clave o con la IA desactivada "
                                     "se usa el lector local, sin internet.", p), expand=True)],
                                spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
                         bgcolor=p.campo, border_radius=12, padding=12),
        ], spacing=14, tight=True), [
            *([ui.boton("Borrar clave", borrar, p, tipo="peligro")] if hay else []),
            ui.boton("Cancelar", self.cerrar_dialogo, p, tipo="texto"), ui.boton("Guardar", guardar, p, I.CHECK)],
            ancho=520, icono=I.TUNE)

    # ================================================================== bandeja vigilada
    def _revisar_lote_bandeja(self, cid: int, ej: int, rutas: list[Path]):
        """El vigilante espera la nota del asesor ANTES de extraer o enviar documentos."""
        evento = threading.Event()
        decision = {"rutas": None, "comentario": "", "cancelado": False, "abierta": False}
        self._evento_dialogo = evento
        def mostrar():
            if self.cerrando or (self.cid, self.ejercicio) != (cid, ej) or getattr(self, "_revision_abierta", False):
                evento.set()
                return
            self._revision_abierta = True
            decision["abierta"] = True
            cliente = self.ctrl.lector.get_cliente(cid) or {}
            nombre = " ".join(str(cliente.get(k) or "") for k in ("nombre", "apellidos")).strip()
            lote = adj.preparar_lote(rutas)
            def aceptar(elegidas, comentario):
                if self.cerrando or (self.cid, self.ejercicio) != (cid, ej):
                    return "El expediente cambió. Cancela y selecciona el destino correcto."
                decision.update(rutas=elegidas, comentario=comentario)
                self.page.pop_dialog()
                evento.set()
            def cancelar(_e=None):
                decision["cancelado"] = True
                self.page.pop_dialog()
                evento.set()
            revision = adj.RevisionDocumentos(self.p, lote.adjuntos, adj.Destino(nombre, cliente.get("nif") or "", ej),
                aceptar, cancelar, destino_vigente=lambda: not self.cerrando and (self.cid, self.ejercicio) == (cid, ej),
                refrescar=self.page.update, avisos=lote.avisos + [
                    "Documentos de la bandeja. Si cancelas, quedan aquí; podrás elegirlos después con Elegir documentos."])
            dlg = self._dialogo("Revisar documentos de la bandeja", revision.contenido, revision.acciones,
                                ancho=640, icono=I.FACT_CHECK_OUTLINED)
            if dlg is not None:
                def descartado(_e):
                    if decision["rutas"] is None:
                        decision["cancelado"] = True
                    evento.set()
                dlg.on_dismiss = descartado
            else:
                evento.set()
        try:
            ejecutar_en_ui(self.page, mostrar)
            while not self.cerrando and not evento.wait(.25):
                pass
            return decision if not self.cerrando else None
        finally:
            if decision["abierta"]:
                self._revision_abierta = False
            if self._evento_dialogo is evento:
                self._evento_dialogo = None

    def _vigilante_bandeja(self) -> None:
        while not self.cerrando:
            time.sleep(2.0)
            cid, ej = self.cid, self.ejercicio
            if self.cerrando or not cid or self._turno.locked() or getattr(self, "_revision_abierta", False):
                continue
            try:
                cliente = self.ctrl.lector.get_cliente(cid)
                bandeja = database.subcarpeta_cliente(Path(cliente["carpeta"]), database.SUBCARPETA_ENTRADA) if cliente else None
                pendientes = sorted((x for x in bandeja.iterdir() if x.is_file() and not x.is_symlink()
                    and x.suffix.lower().lstrip(".") in adj.EXTENSIONES and x.resolve().parent == bandeja), key=lambda x:x.name) if bandeja and bandeja.is_dir() else []
                firma = tuple((x.name, x.stat().st_size, x.stat().st_mtime_ns) for x in pendientes)
                pospuestas = getattr(self, "_bandejas_pospuestas", {})
                hay_pdfs = bool(pendientes) and pospuestas.get(cid) != firma
            except Exception:
                continue
            if not hay_pdfs or not self._turno.acquire(blocking=False):
                continue
            if self.cerrando or (self.cid, self.ejercicio) != (cid, ej):
                self._turno.release()
                continue
            def mostrar_resultado(r, cid=cid, ej=ej):
                if self.cerrando:
                    return
                if r.get("ok"):
                    if (self.cid, self.ejercicio) == (cid, ej):
                        self._aplicar_resultado(r)
                    if r.get("error_excel"):
                        self._dialogo_excel_bloqueado(r["error_excel"])
                    else:
                        self.avisar(f"Bandeja: {r['archivo']} procesado.")
                elif not r.get("cancelado"):
                    self.avisar(f"Bandeja: no se pudo leer {r['archivo']} (movido a _errores).", True)

            def terminar():
                self._turno.release()
                if not self.cerrando:
                    self.set_ocupado(False)
                    self._pintar_principal()

            try:
                seleccion = self._revisar_lote_bandeja(cid, ej, pendientes[:adj.MAX_ARCHIVOS])
                if not seleccion or not seleccion["rutas"]:
                    if seleccion and seleccion["cancelado"]:
                        self._bandejas_pospuestas = {**pospuestas, cid: firma}
                    continue
                ejecutar_en_ui(self.page, lambda: self.set_ocupado(True, "Procesando documentos de la bandeja…")
                               if not self.cerrando else None)
                for r in self.ctrl.procesar_bandeja(cid, ej, confirmar=lambda ext: self.revisar_extraccion(ext, cid, ej),
                                                   comentario_guia=seleccion["comentario"], rutas=seleccion["rutas"]):
                    ejecutar_en_ui(self.page, lambda resultado=r: mostrar_resultado(resultado))
                retiradas = set(pendientes[:adj.MAX_ARCHIVOS]) - set(seleccion["rutas"])
                if retiradas:
                    firma_retiradas = tuple((x.name,x.stat().st_size,x.stat().st_mtime_ns) for x in sorted(retiradas) if x.is_file())
                    self._bandejas_pospuestas = {**pospuestas, cid: firma_retiradas}
            except Exception as exc:
                mensaje = f"Bandeja: {exc}"
                ejecutar_en_ui(self.page, lambda texto=mensaje: self.avisar(texto, True) if not self.cerrando else None)
            finally:
                try:
                    ejecutar_en_ui(self.page, terminar)
                except Exception:
                    if self._turno.locked():
                        self._turno.release()


def preparar_pagina(pg: ft.Page, p) -> None:
    pg.title = "Contabilidad a Medida"
    pg.padding = 0
    pg.spacing = 0
    pg.bgcolor = p.fondo
    pg.theme_mode = ft.ThemeMode.DARK if p.oscuro else ft.ThemeMode.LIGHT
    pg.theme = ft.Theme(font_family=ui.FUENTE, color_scheme_seed=p.acento)
    pg.dark_theme = ft.Theme(font_family=ui.FUENTE, color_scheme_seed=p.acento)
    if pg.window:
        if ICONO_ICO.exists():
            pg.window.icon = str(ICONO_ICO)
        pg.window.width, pg.window.height = 1440, 900
        pg.window.min_width, pg.window.min_height = 1100, 700


def pantalla_carga(p) -> ft.Container:
    """Pantalla de carga: icono con su sombra, nombre y estado. Entra con un fundido y una escala mínima
    (0.96 -> 1, nunca desde 0) y se va con un fundido más rápido que la entrada."""
    contenido = ft.Column([
        ui.icono_app(132),
        ft.Container(height=26),
        ft.Text("Contabilidad", size=46, weight=ft.FontWeight.W_700, color=p.texto, font_family=ui.FUENTE_TITULO,
                style=ft.TextStyle(letter_spacing=-1.2, height=1.0)),
        ft.Text("a tu medida", size=30, weight=ft.FontWeight.W_400, color=p.texto, font_family=ui.FUENTE_TITULO,
                style=ft.TextStyle(letter_spacing=-0.5, height=1.1)),
        ft.Container(height=22),
        ft.Row([ft.ProgressRing(width=14, height=14, stroke_width=2, color=p.texto3),
                ft.Text("Cargando…", size=15, color=p.texto2, font_family=ui.FUENTE)],
               spacing=10, alignment=ft.MainAxisAlignment.CENTER, tight=True),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True, spacing=0,
        opacity=0, scale=0.96, animate_opacity=ft.Animation(320, ft.AnimationCurve.EASE_OUT_QUART),
        animate_scale=ft.Animation(420, ft.AnimationCurve.EASE_OUT_QUART))
    fondo = ft.RadialGradient(center=ft.Alignment(0, -0.2), radius=1.1,
                              colors=["#FFFFFF", "#EEF3FC"] if not p.oscuro else ["#1C1D21", "#121316"])
    return ft.Container(contenido, expand=True, alignment=ft.Alignment.CENTER, gradient=fondo, opacity=1,
                        animate_opacity=ft.Animation(260, ft.AnimationCurve.EASE_OUT_CUBIC))


def main(page: ft.Page) -> None:
    """Pinta la pantalla de carga y vuelve enseguida: Flet solo envía la primera pantalla al terminar esta
    función. Módulos y BD se cargan en un worker; el bucle UI monta y anima la pantalla."""
    p = ui.OSCURO if page.platform_brightness == ft.Brightness.DARK else ui.CLARO
    preparar_pagina(page, p)
    splash = pantalla_carga(p)
    pila = ft.Stack([splash], expand=True)
    page.add(pila)

    async def cargar() -> None:
        app, ctrl = None, None
        try:
            inicio = time.perf_counter()
            await asyncio.sleep(0.05)  # deja llegar el primer fotograma antes de animar
            splash.content.opacity, splash.content.scale = 1, 1
            page.update()
            await asyncio.to_thread(_cargar_modulos)
            ctrl = await asyncio.to_thread(Controlador)
            app = App(page, ctrl)
            app.iniciar(pila)
            page.update()
            # Mínimo ~0,9 s en pantalla para que no parpadee; luego fundido de salida (más corto que la entrada)
            minimo = float(os.environ.get("SPLASH_MIN", "0.9"))  # SPLASH_MIN=8 para revisarla con calma
            await asyncio.sleep(max(0.0, minimo - (time.perf_counter() - inicio)))
            if app.cerrando:
                return
            splash.opacity = 0
            page.update()
            await asyncio.sleep(0.3)
            if app.cerrando:
                return
            if splash in pila.controls:
                pila.controls.remove(splash)
                page.update()
        except Exception as exc:
            if app is not None:
                app.cerrando = True
                if app._evento_dialogo:
                    app._evento_dialogo.set()
                await asyncio.to_thread(app.ctrl.cerrar)
            elif ctrl is not None:
                await asyncio.to_thread(ctrl.cerrar)
            if page.window:
                page.window.prevent_close = False
            splash.opacity = 1
            splash.content = ft.Column([
                ft.Icon(I.ERROR_OUTLINE, size=34, color=p.rojo),
                ui.seccion("No se pudo iniciar la aplicación", p),
                ui.cuerpo(str(exc) if isinstance(exc, AlmacenNoDisponible)
                          else "Cierra esta ventana y vuelve a abrir la aplicación.", p),
                ui.leyenda(f"Error: {exc.__class__.__name__}", p),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True, spacing=12)
            pila.controls = [splash]
            page.update()

    page.run_task(cargar)


if __name__ == "__main__":
    if "--web" in sys.argv:
        ft.run(main, view=ft.AppView.WEB_BROWSER, port=int(os.environ.get("PUERTO", "8550")))
    else:
        ft.run(main)
