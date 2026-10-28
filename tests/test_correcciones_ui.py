"""Correcciones de AUDITORIA_UI.md (26/10/2026): cierre del modal propio con avisos en la pila de Flet,
«Más campos…» durante el guardado, worker único de previsualización, comprobación del Excel con señal y
reintento, acciones nativas accesibles y selector de periodicidad legible sin depender del contraste.
Datos ficticios; backend sustituido por dobles."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import flet as ft
import pytest

import main
import ui_cartera as uc
import ui_estilo as ui
from test_documentos_ui import buscar, recorrer


def textos(control) -> str:
    return " | ".join(str(c.value) for c in recorrer(control) if isinstance(c, ft.Text))


# ------------------------------------------------------------------------------------ 1. modal y aviso
class PilaFlet:
    """Como Page en Flet 1.0.4: SnackBar y AlertDialog comparten pila y pop_dialog cierra el último abierto,
    sea del tipo que sea."""
    def __init__(self):
        self.pila, self.update = [], Mock()
        self._dialogs = NS(controls=self.pila)

    def show_dialog(self, d):
        d.open = True
        self.pila.append(d)

    def pop_dialog(self):
        return ft.Page.pop_dialog(self)  # implementación instalada real, no réplica de su algoritmo


@pytest.fixture
def app_pila(monkeypatch):
    monkeypatch.setattr(ft.AlertDialog, "update", lambda self: None)  # sin sesión real
    monkeypatch.setattr(ft.SnackBar, "update", lambda self: None)
    a = main.App.__new__(main.App)
    a.p, a.cerrando, a.page = ui.CLARO, False, PilaFlet()
    a._dialogos, a._dialogos_vistos = [], set()
    return a


def _snack(pila):
    return next(d for d in pila if isinstance(d, ft.SnackBar))


def test_cerrar_dialogo_con_aviso_encima_cierra_el_modal_y_no_el_aviso(app_pila):
    dlg = app_pila._dialogo("Contrato", ft.Column([]), [])
    app_pila.avisar("Espera a que termine la tarea en curso.")  # SnackBar encima del modal
    app_pila.cerrar_dialogo()
    assert dlg.open is False
    assert _snack(app_pila.page.pila).open is True
    app_pila.cerrar_dialogo()  # segundo clic: no cierra un aviso en ausencia de modal
    assert _snack(app_pila.page.pila).open is True


def test_flet_instalado_apila_y_cierra_snackbar_como_dialogo(app_pila):
    dlg = app_pila._dialogo("Contrato", ft.Column([]), [])
    app_pila.avisar("Aviso ficticio")
    assert app_pila.page.pop_dialog() is _snack(app_pila.page.pila)
    assert dlg.open is True  # causa original comprobada con el método real de Flet
    assert _snack(app_pila.page.pila).open is False


def test_cerrar_dialogo_concreto_no_cierra_el_mas_reciente(app_pila):
    primero = app_pila._dialogo("Contrato", ft.Column([]), [])
    segundo = app_pila._dialogo("Revisar cambios del Excel", ft.Column([]), [])
    app_pila.cerrar_dialogo(dlg=primero)
    assert primero.open is False and segundo.open is True
    app_pila.cerrar_dialogo()
    assert segundo.open is False


def test_dialogo_cerrado_por_flet_no_cuenta_y_on_dismiss_se_conserva(app_pila):
    primero = app_pila._dialogo("Uno", ft.Column([]), [])
    segundo = app_pila._dialogo("Dos", ft.Column([]), [])
    segundo.on_dismiss = descartado = Mock()
    segundo.open = False  # Flet lo cerró por su cuenta
    app_pila.cerrar_dialogo()
    assert primero.open is False
    assert segundo.on_dismiss is descartado


def test_cerrar_antes_de_mostrarse_no_lo_abre_despues(app_pila):
    pendientes = []
    app_pila.page.run_task = lambda corrutina: pendientes.append(corrutina)  # UI asíncrona simulada
    dlg = app_pila._dialogo("Contrato", ft.Column([]), [])
    app_pila.cerrar_dialogo(dlg=dlg)
    import asyncio
    asyncio.run(pendientes.pop()())
    assert dlg not in app_pila.page.pila and not dlg.open


# ------------------------------------------------------------------------------------ 2. Más campos…
@pytest.fixture
def contrato_existente():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cerrando = ui.CLARO, 7, 2025, False
    fila = {"codigo": "CTR-001", "inmueble": "INM-001", "estado": "Vigente", "periodicidad": "Mensual",
            "fecha_inicio": "2025-01-01", "renta_inicial": 800}
    lector = NS(get=Mock(return_value=fila),
                listar=Mock(return_value=[{"codigo": "INM-001", "direccion": "Calle ficticia"}]))
    a.ctrl = NS(lector=lector, editar=Mock(return_value={}), validar=Mock(return_value={}),
                previsualizar_contrato=Mock(return_value={"dias_alquilado": 365}))
    workers = []
    a.page = NS(update=Mock(), run_thread=workers.append)
    a.dlg = object()
    a._dialogo = Mock(return_value=a.dlg)
    a.avisar, a.ocupada, a.cerrar_dialogo, a.dialogo_editar = Mock(), Mock(return_value=False), Mock(), Mock()
    a.en_segundo_plano = Mock(return_value=True)  # el guardado queda en curso hasta que el test lo termina
    a.dialogo_contrato("CTR-001")
    contenido, acciones = a._dialogo.call_args.args[1:3]
    mas = next(x for x in acciones if x.content == "Más campos…")
    return a, contenido, acciones, mas


def test_mas_campos_desactivado_e_ignorado_mientras_se_guarda(contrato_existente):
    a, contenido, acciones, mas = contrato_existente
    buscar(contenido, "Renta mensual (€)").value = "900,00"
    acciones[-1].on_click(None)  # Guardar y recalcular
    a.en_segundo_plano.assert_called_once()
    assert mas.disabled
    mas.on_click(None)
    a.dialogo_editar.assert_not_called()
    a.cerrar_dialogo.assert_not_called()
    a.en_segundo_plano.call_args.kwargs["despues"]()  # termina el guardado
    a.cerrar_dialogo.assert_called_once_with(dlg=a.dlg)
    mas.on_click(None)  # ya cerrado: no abre nada
    a.dialogo_editar.assert_not_called()


def test_guardado_contrato_envia_original_para_detectar_conflictos(contrato_existente):
    a, contenido, acciones, _ = contrato_existente
    buscar(contenido, "Renta mensual (€)").value = "900,00"
    acciones[-1].on_click(None)
    a.en_segundo_plano.call_args.args[1]()
    assert a.ctrl.editar.call_args.kwargs["esperados"]["renta_inicial"] == 800


def test_mas_campos_con_cambios_pendientes_no_descarta_ni_abre_editor(contrato_existente):
    a, contenido, _, mas = contrato_existente
    renta = buscar(contenido, "Renta mensual (€)")
    renta.value = "900,00"
    mas.on_click(None)
    a.dialogo_editar.assert_not_called()
    a.cerrar_dialogo.assert_not_called()
    assert renta.value == "900,00"
    assert "cambios sin guardar" in textos(contenido)


def test_mas_campos_sin_cambios_abre_editor_y_cierra_su_dialogo(contrato_existente):
    a, _, _, mas = contrato_existente
    mas.on_click(None)
    a.cerrar_dialogo.assert_called_once_with(dlg=a.dlg)
    a.dialogo_editar.assert_called_once_with("contratos", "CTR-001")


def test_cancelar_contrato_cierra_su_dialogo_exacto(contrato_existente):
    a, _, acciones, _ = contrato_existente
    next(x for x in acciones if x.content == "Cancelar").on_click(None)
    a.cerrar_dialogo.assert_called_once_with(dlg=a.dlg)


# ------------------------------------------------------------------------------------ 4. previsualización
def test_un_solo_worker_y_solo_la_ultima_solicitud(contrato_existente):
    a, contenido, _, _ = contrato_existente
    workers = a.page.run_thread.__self__  # la lista del fixture (run_thread=workers.append)
    assert len(workers) == 1  # la previa inicial del contrato existente
    campo = buscar(contenido, "Fecha de inicio")
    for fecha in ("01/02/2025", "01/03/2025", "01/04/2025"):
        campo.value = fecha
        campo.on_blur(None)
    assert len(workers) == 1  # sin un hilo por blur
    workers.pop()()
    a.ctrl.previsualizar_contrato.assert_called_once()
    assert a.ctrl.previsualizar_contrato.call_args.args[1]["fecha_inicio"] == "01/04/2025"
    assert "365 días" in textos(contenido)
    campo.on_blur(None)  # el worker terminó: la siguiente solicitud arranca otro
    assert len(workers) == 1


def test_duracion_admite_hasta_1200_meses(contrato_existente):
    _, contenido, _, _ = contrato_existente
    duracion = buscar(contenido, "Duración en meses (ayuda)")
    duracion.value = "1200"
    duracion.on_blur(None)
    assert duracion.error is None
    duracion.value = "1201"
    duracion.on_blur(None)
    assert "1200" in duracion.error


# ------------------------------------------------------------------------------------ 3. Excel ocupado
def app_excel():
    a = main.App.__new__(main.App)
    a.cerrando, a._turno, a.p = False, threading.Lock(), ui.CLARO
    a.cid, a.ejercicio, a.informe = 7, 2026, "INFORME PREVIO"
    llamadas = []
    a.ctrl = NS(sincronizar_excel_si_cambio=lambda cid, ej: llamadas.append((cid, ej)) or {})
    workers = []
    a.page = NS(run_thread=workers.append)
    a._pintar_principal, a.avisar, a._dialogo, a.cerrar_dialogo, a.set_ocupado = (Mock() for _ in range(5))
    a._aplicar_resultado = Mock()
    return a, workers, llamadas


def test_comprobacion_excel_muestra_progreso_y_lo_retira():
    a, workers, _ = app_excel()
    assert a._comprobar_excel("foco")
    a.set_ocupado.assert_called_once_with(True, "Comprobando cambios del Excel…")
    workers.pop()()
    a.set_ocupado.assert_called_with(False)
    assert not a._turno.locked()


def test_excel_omitido_por_turno_ocupado_se_reintenta_una_vez_con_contexto_actual():
    a, workers, llamadas = app_excel()
    assert a.en_segundo_plano("Guardando…", lambda: {})  # otra tarea ocupa el turno
    tarea = workers.pop()
    assert not a._comprobar_excel("foco") and a._excel_pendiente
    a.cid = 8  # el usuario cambió de cliente mientras tanto
    tarea()  # termina la tarea y libera el turno -> reintento
    assert len(workers) == 1 and not a._excel_pendiente
    workers.pop()()
    assert llamadas == [(8, 2026)]
    a._reintentar_excel()  # nada pendiente: no vuelve a lanzar
    assert not workers


def test_reintento_con_turno_otra_vez_ocupado_no_se_encola_en_bucle():
    a, workers, llamadas = app_excel()
    a._turno.acquire()
    a._comprobar_excel("foco")
    a._reintentar_excel()  # el turno sigue ocupado
    assert not a._excel_pendiente and not workers and not llamadas
    a._turno.release()
    a._reintentar_excel()
    assert not workers


# ------------------------------------------------------------------------------------ 5. accesibilidad
def _resumen(**contrato):
    return {"contratos": [{"codigo": "CTR-001", "inmueble": "INM-001", "fecha_inicio": "2025-01-01",
                           "estado": "Vigente", **contrato}],
            "cobros": [{"codigo": "COB-001", "contrato": "CTR-001", "importe_cobrado": 850, "estado": "Cobrado"}],
            "ingresos_devengados": 0, "ingresos_cobrados": 850}


def _alquileres(resumen=None, fallo=None):
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio = ui.CLARO, 7, 2025
    a.ctrl = NS(lector=NS(listar=lambda *x, **k: []),
                resumen_alquileres=Mock(return_value=resumen, side_effect=fallo))
    return a._sec_alquileres()


def test_acciones_de_contratos_y_cobros_nativas_con_foco_y_44px():
    vista = _alquileres(_resumen(renta_inicial=850))
    textuales = {c.content: c for c in recorrer(vista) if isinstance(c, ft.TextButton)}
    for nombre in ("Añadir contrato", "Añadir cobro"):
        assert textuales[nombre].height >= 44 and ft.ControlState.FOCUSED in textuales[nombre].style.side
    iconos = {c.tooltip: c for c in recorrer(vista) if isinstance(c, ft.IconButton)}
    for nombre in ("Añadir inquilino", "Corregir contrato", "Corregir cobro", "Eliminar cobro"):
        assert iconos[nombre].width >= 44 and iconos[nombre].height >= 44
        assert ft.ControlState.FOCUSED in iconos[nombre].style.side


def test_usar_fin_previsto_es_accion_nativa(contrato_existente):
    _, contenido, _, _ = contrato_existente
    usar = next(c for c in recorrer(contenido) if isinstance(c, ft.TextButton)
                and c.content == "Usar este fin previsto")
    assert usar.height >= 44 and ft.ControlState.FOCUSED in usar.style.side


def test_kpi_alquileres_en_responsive_row_y_renta_ausente_sin_dato():
    vista = _alquileres(_resumen())
    assert isinstance(vista.controls[0], ft.ResponsiveRow)
    assert f"{uc.SIN_DATO} mensual" in textos(vista)


def test_resumen_que_falla_se_avisa_en_la_pestana():
    vista = _alquileres(fallo=RuntimeError("fallo ficticio"))
    t = textos(vista)
    assert "No se pudo calcular el resumen de 2025" in t and "Contratos de alquiler" in t


# ------------------------------------------------------------------------------------ selector
def _segmentos(selector):
    return [(any(isinstance(c, ft.Icon) for c in recorrer(s)),
             next(c for c in recorrer(s) if isinstance(c, ft.Text)).weight) for s in selector.controls]


def test_periodicidad_elegida_se_identifica_por_icono_y_negrita():
    fila = uc.selector_periodicidad("Anual", ui.CLARO, Mock())
    selector = next(c for c in recorrer(fila) if isinstance(c, ft.CupertinoSlidingSegmentedButton))
    assert _segmentos(selector) == [(True, ft.FontWeight.W_700), (False, ft.FontWeight.W_400)]
    selector.selected_index = 1
    selector.on_change(NS(control=selector))
    assert _segmentos(selector) == [(False, ft.FontWeight.W_400), (True, ft.FontWeight.W_700)]


def test_periodicidad_rechazada_devuelve_tambien_la_marca():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cerrando = ui.CLARO, 7, 2025, False
    a.ctrl = NS(lector=NS(), establecer_periodicidad_210=Mock(return_value={}))
    a.page = NS(update=Mock())
    a.en_segundo_plano = Mock(return_value=False)  # otra tarea en curso
    from test_ui_cartera import _vista
    tarjeta = a._tarjeta_inmueble(_vista(_periodicidad_210="Anual"))
    selector = next(c for c in recorrer(tarjeta) if isinstance(c, ft.CupertinoSlidingSegmentedButton))
    selector.selected_index = 1
    selector.on_change(NS(control=selector))
    assert selector.selected_index == 0
    assert _segmentos(selector)[0] == (True, ft.FontWeight.W_700)
