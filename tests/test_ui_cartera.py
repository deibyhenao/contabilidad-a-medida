"""Interfaz de cartera (encargo UI 26/10/2026): ocupación, comparativa de valores, periodicidad del 210,
contratos con previsualización y detección de cambios del Excel. Datos siempre ficticios; el backend se
sustituye por dobles porque Codex implementa los métodos nuevos en paralelo."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import ANY, Mock, call

import flet as ft
import pytest

import excel_generator
import main
import tax_calculator as tc
import ui_cartera as uc
import ui_estilo as ui
from test_documentos_ui import buscar, recorrer


def textos(control) -> str:
    return " | ".join(str(c.value) for c in recorrer(control) if isinstance(c, ft.Text))


# ------------------------------------------------------------------------------------ formato puro
def test_ocupacion_compra_a_mitad_de_anio_separa_devengado_y_cobrado():
    v = {"fecha_adquisicion": "2025-07-01", "_dias_propiedad": 184, "_dias_alquilado": 122, "_dias_vacio": 62,
         "_ingresos_devengados": 4000.0, "_ingresos_cobrados": None}
    filas = {e: x for e, x, _ in uc.ocupacion(v, 2025)}
    assert filas["Fecha de compra"] == "01/07/2025"
    assert filas["Días de propiedad 2025"] == "184 días"
    assert filas["Días alquilados"] == "122 días" and filas["Días vacíos"] == "62 días"
    assert filas["Renta devengada 2025"] == ui.eur(4000)
    assert filas["Cobros registrados 2025"] == uc.SIN_DATO  # un cobro ausente no es 0 ni la renta devengada


def test_ausente_es_sin_dato_y_cero_es_cero():
    filas = {e: x for e, x, _ in uc.ocupacion({}, 2025)}
    assert set(filas.values()) == {uc.SIN_DATO}
    filas = {e: x for e, x, _ in uc.ocupacion({"_dias_vacio": 0, "_ingresos_cobrados": 0}, 2025)}
    assert filas["Días vacíos"] == "0 días" and filas["Cobros registrados 2025"] == ui.eur(0)


def test_referencia_del_ejercicio_no_hereda_otro_anio_como_propio():
    v = {"fecha_adquisicion": "2022-03-10", "_anio_compra": 2022, "_valor_referencia_compra": 180000,
         "_valor_referencia_actual": None}
    cat = {"valor_catastral": 90000, "valor_referencia": 200000, "_heredado": 2024}
    filas = {e: (x, n) for e, x, n in uc.comparativa_valores(v, {"catastral": 90000, "referencia_catastro": 200000},
                                                             cat, 2025)}
    assert filas["Referencia en adquisición (2022):"][0] == ui.eur(180000)
    valor, nota = filas["Referencia Catastro 2025:"]
    assert valor == uc.SIN_DATO and "2024" in nota and "2025" in nota
    assert filas["Precio de escritura:"][0] == uc.SIN_DATO


@pytest.mark.parametrize("estado", ["PRESENTADO[OK]", "Presentado", "presentado ok"])
def test_presentado_canonico_con_icono(estado):
    assert uc.estado_visible(estado) == "Presentado"
    pastilla = ui.pastilla_estado(estado, ui.CLARO)
    assert "Presentado" in textos(pastilla) and "[OK]" not in textos(pastilla)
    assert any(isinstance(c, ft.Icon) for c in recorrer(pastilla))


def test_estados_no_presentados_no_cambian():
    assert uc.estado_visible("Pendiente") == "Pendiente" and uc.estado_visible(None) is None
    assert uc.inmuebles_con_presentado([{"inmueble": "INM-1", "estado": "PRESENTADO[OK]"},
                                        {"inmueble": "INM-2", "estado": "Pendiente"}]) == {"INM-1"}


# ------------------------------------------------------------------------------------ tarjeta de inmueble
@pytest.fixture
def app():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cerrando = ui.CLARO, 7, 2025, False
    a.ctrl = NS(lector=NS(), establecer_periodicidad_210=Mock(return_value={}))
    a.page = NS(update=Mock())
    a.en_segundo_plano = Mock(side_effect=lambda texto, trabajo, *x, **k: (trabajo(), True)[1])
    return a


def _vista(**extra):
    datos = {"codigo": "INM-001", "direccion": "Calle ficticia 1", "precio_escritura": 100000}
    cat = {"valor_catastral": 50000}
    return {**datos, "_catastro": cat, "_vp": tc.valor_patrimonial(datos, cat), "_tit": 1, "_computado": 100000,
            **extra}


def test_tarjeta_muestra_ocupacion_y_comparativa(app):
    v = _vista(fecha_adquisicion="2025-01-01", _dias_propiedad=365, _dias_alquilado=90, _dias_vacio=275,
               _ingresos_devengados=3000, _ingresos_cobrados=2500, _periodicidad_210="Anual")
    t = textos(app._tarjeta_inmueble(v))
    for esperado in ("365 días", "90 días", "275 días", "Renta devengada 2025", ui.eur(3000),
                     "Cobros registrados 2025", ui.eur(2500), "Valor catastral 2025:", "Referencia Catastro 2025:",
                     "Precio de escritura:", "Referencia en adquisición (2025):"):
        assert esperado in t


def test_tarjeta_sin_campos_nuevos_no_inventa_ceros_ni_selector(app):
    tarjeta = app._tarjeta_inmueble(_vista())
    assert "Sin dato" in textos(tarjeta) and "0 días" not in textos(tarjeta)
    assert not any(isinstance(c, ft.CupertinoSlidingSegmentedButton) for c in recorrer(tarjeta))


def _selector(tarjeta):
    return next(c for c in recorrer(tarjeta) if isinstance(c, ft.CupertinoSlidingSegmentedButton))


def test_periodicidad_trimestral_se_guarda_en_worker_con_contexto_capturado(app):
    selector = _selector(app._tarjeta_inmueble(_vista(_periodicidad_210="Anual")))
    assert selector.selected_index == 0 and not selector.disabled
    selector.selected_index = 1
    selector.on_change(NS(control=selector))
    app.ctrl.establecer_periodicidad_210.assert_called_once_with(7, "INM-001", 2025, "Trimestral",
                                                                  al_recalcular=ANY)


def test_periodicidad_bloqueada_con_210_presentado_o_historico(app):
    assert _selector(app._tarjeta_inmueble(_vista(_periodicidad_210="Anual"), True)).disabled
    app.ejercicio = 2023
    assert _selector(app._tarjeta_inmueble(_vista(_periodicidad_210="Trimestral"))).disabled


def test_periodicidad_ocupada_vuelve_al_valor_guardado(app):
    app.en_segundo_plano = Mock(return_value=False)
    selector = _selector(app._tarjeta_inmueble(_vista(_periodicidad_210="Anual")))
    selector.selected_index = 1
    selector.on_change(NS(control=selector))
    assert selector.selected_index == 0
    app.ctrl.establecer_periodicidad_210.assert_not_called()


# ------------------------------------------------------------------------------------ desglose del 210
ANUAL = [{"codigo": "210-A", "periodo": "Anual", "ingresos": 3000, "gastos": 500, "base": 2500, "tipo": 0.19,
          "cuota": 475, "estado": "Pendiente"}]
TRIMESTRES = ([{"codigo": f"210-{i}T", "periodo": f"{i}T", "ingresos": 750, "gastos": 0, "base": 750, "tipo": 19,
                "cuota": 142.5, "estado": "Pendiente"} for i in (4, 2, 1, 3)]
              + [{**ANUAL[0]},  # periodo de la periodicidad anterior: obsoleto, no se enseña como pendiente
                 {"codigo": "210-X", "periodo": "1T", "estado": "No procede"}])


def test_desglose_pasa_de_una_fila_anual_a_cuatro_trimestres():
    assert [f["periodo"] for f in uc.filas_desglose_210({"_desglose_210": ANUAL}, "Anual")] == ["Anual"]
    filas = uc.filas_desglose_210({"_desglose_210": TRIMESTRES}, "Trimestral")
    assert [f["periodo"] for f in filas] == ["1T", "2T", "3T", "4T"]
    assert all(f["estado"] != "No procede" for f in filas)


def test_desglose_en_tarjeta_sin_sumas_y_con_titularidad(app):
    t = textos(app._tarjeta_inmueble(_vista(_periodicidad_210="Trimestral", _desglose_210=TRIMESTRES)))
    assert "Importes del contribuyente según titularidad" in t
    for esperado in ("1T", "2T", "3T", "4T", ui.eur(142.5), ui.eur(0), "Tipo 19 %"):
        assert esperado in t
    assert ui.eur(570) not in t and ui.eur(475) not in t  # ni total calculado en UI ni fila anual obsoleta
    t = textos(app._tarjeta_inmueble(_vista(_periodicidad_210="Anual", _desglose_210=ANUAL)))
    assert ui.eur(475) in t and "Tipo 19 %" in t and "4T" not in t


def test_desglose_ausente_no_se_inventa_y_dato_ausente_es_sin_dato(app):
    assert uc.filas_desglose_210(_vista(), "Anual") is None
    assert "Importes del contribuyente" not in textos(app._tarjeta_inmueble(_vista(_periodicidad_210="Anual")))
    t = textos(app._tarjeta_inmueble(_vista(_periodicidad_210="Anual",
                                            _desglose_210=[{"periodo": "Anual", "cuota": 0}])))
    assert uc.SIN_DATO in t and ui.eur(0) in t


# ------------------------------------------------------------------------------------ cambio con al_recalcular
def app_periodicidad(backend):
    """App con el en_segundo_plano real: el worker queda en ``workers`` y la UI es síncrona (sin run_task)."""
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cerrando = ui.CLARO, 7, 2025, False
    a._turno, a.informe = threading.Lock(), "INFORME PREVIO"
    a.ctrl = NS(lector=NS(), establecer_periodicidad_210=Mock(side_effect=backend))
    workers = []
    a.page = NS(update=Mock(), run_thread=workers.append)
    a.set_ocupado, a._pintar_principal, a._reintentar_excel, a.avisar = Mock(), Mock(), Mock(), Mock()
    return a, workers


def _pulsar(a, codigo="INM-001", indice=1):
    selector = _selector(a._tarjeta_inmueble(_vista(codigo=codigo, _periodicidad_210="Anual", _desglose_210=ANUAL)))
    selector.selected_index = indice
    selector.on_change(NS(control=selector))
    return selector


def test_callback_pinta_calculo_antes_del_excel_y_mantiene_turno():
    visto = {}

    def backend(cid, inm, ej, modo, al_recalcular=None):
        al_recalcular({"informe": "INFORME TRIMESTRAL"})  # tras COMMIT, antes de exportar
        visto.update(informe=a.informe, turno=a._turno.locked(), ocupado=a.set_ocupado.call_args,
                     pintado=a._pintar_principal.call_count)
        # Repintado durante el guardado: muestra lo calculado (trimestral) y sigue bloqueado.
        visto["tarjeta"] = a._tarjeta_inmueble(_vista(_periodicidad_210="Trimestral", _desglose_210=TRIMESTRES))
        visto["otro"] = _pulsar(a, "INM-002")  # doble clic / otro inmueble mientras se guarda el Excel
        return {"informe": "INFORME TRIMESTRAL"}
    a, workers = app_periodicidad(backend)
    selector = _pulsar(a)
    assert selector.disabled and selector.data.visible  # feedback inmediato, antes del worker
    assert "Calculando" in textos(selector.data)
    workers.pop()()
    assert visto["informe"] == "INFORME TRIMESTRAL" and visto["turno"] and visto["pintado"] == 1
    assert visto["ocupado"] == call(True, "Guardando Excel…")
    durante = _selector(visto["tarjeta"])
    assert durante.disabled and durante.selected_index == 1 and "Guardando Excel…" in textos(visto["tarjeta"])
    assert all(f"{i}T" in textos(visto["tarjeta"]) for i in range(1, 5))
    assert visto["otro"].selected_index == 0 and not visto["otro"].disabled
    a.ctrl.establecer_periodicidad_210.assert_called_once()
    # Al acabar el Excel: turno libre, barra apagada, nada pendiente y selector reconstruido.
    assert not a._turno.locked() and a.set_ocupado.call_args == call(False)
    assert a._periodicidad_en_curso() is None
    assert not _selector(a._tarjeta_inmueble(_vista(_periodicidad_210="Trimestral"))).disabled


def test_callback_de_otro_expediente_se_descarta():
    visto = {}

    def backend(cid, inm, ej, modo, al_recalcular=None):
        a.cid = 8  # el usuario cambió de cliente mientras se calculaba
        al_recalcular({"informe": "INFORME DEL 7"})
        visto.update(pintado=a._pintar_principal.call_count, informe=a.informe, turno=a._turno.locked())
        return {"informe": "INFORME DEL 7"}
    a, workers = app_periodicidad(backend)
    _pulsar(a)
    workers.pop()()
    assert visto == {"pintado": 0, "informe": "INFORME PREVIO", "turno": True}
    assert a.informe == "INFORME PREVIO" and not a._turno.locked()
    a.avisar.assert_called_once()  # solo el aviso de éxito: el backend no falló


def test_callback_tardio_no_reactiva_ocupado():
    guardado = {}

    def backend(cid, inm, ej, modo, al_recalcular=None):
        guardado["cb"] = al_recalcular  # el aviso llega al bucle de UI después del final
        return {"informe": "INFORME FINAL"}
    a, workers = app_periodicidad(backend)
    _pulsar(a)
    workers.pop()()
    a.set_ocupado.reset_mock()
    a._pintar_principal.reset_mock()
    guardado["cb"]({"informe": "INFORME VIEJO"})
    a.set_ocupado.assert_not_called()
    a._pintar_principal.assert_not_called()
    assert a.informe == "INFORME FINAL" and not a._turno.locked()


def test_error_de_excel_aplica_calculo_confirmado_y_callback_tardio_no_lo_reactiva():
    guardado = {}
    def backend(cid, inm, ej, modo, al_recalcular=None):
        guardado['cb'] = al_recalcular
        raise main.CalculoConfirmadoSinExcel(cid, ej, {'informe': 'CALCULO CONFIRMADO'}, PermissionError())
    a, workers = app_periodicidad(backend)
    _pulsar(a)
    workers.pop()()
    assert a.informe == 'CALCULO CONFIRMADO' and not a._turno.locked()
    assert 'no se pudo completar' in a.avisar.call_args.args[0]
    a.set_ocupado.reset_mock()
    guardado['cb']({'informe': 'CALCULO CONFIRMADO'})
    a.set_ocupado.assert_not_called()


def test_error_visual_del_avance_no_impide_exportar_ni_liberar_turno(caplog):
    exportado = []
    def backend(cid, inm, ej, modo, al_recalcular=None):
        al_recalcular({'informe': 'CALCULO'})
        exportado.append(True)
        return {'informe': 'FINAL'}
    a, workers = app_periodicidad(backend)
    a._pintar_principal.side_effect = [RuntimeError('detalle no publicable'), None]
    _pulsar(a)
    workers.pop()()
    assert exportado == [True] and a.informe == 'FINAL' and not a._turno.locked()
    assert 'No se pudo mostrar el avance' in caplog.text
    assert 'detalle no publicable' not in caplog.text


def test_error_del_backend_restaura_y_libera():
    def backend(*x, **k):
        raise ValueError("La periodicidad difiere de un modelo ya presentado.")
    a, workers = app_periodicidad(backend)
    _pulsar(a)
    workers.pop()()
    a.avisar.assert_called_with("La periodicidad difiere de un modelo ya presentado.", error=True)
    assert not a._turno.locked() and a._periodicidad_en_curso() is None
    a._pintar_principal.assert_called()  # reconstruye el selector con lo guardado


def test_rechazo_por_ocupado_restaura_selector_sin_pendiente():
    a, workers = app_periodicidad(lambda *x, **k: {})
    a._turno.acquire()
    selector = _pulsar(a)
    assert selector.selected_index == 0 and not selector.disabled and not selector.data.visible
    assert a._periodicidad_en_curso() is None and not workers
    a.ctrl.establecer_periodicidad_210.assert_not_called()
    a.avisar.assert_called_once()


# ------------------------------------------------------------------------------------ alquileres
def test_alquileres_resumen_del_anio_con_cobros_y_devengo():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio = ui.CLARO, 7, 2025
    resumen = {
        "contratos": [{"codigo": "CTR-001", "inmueble": "INM-001", "fecha_inicio": "2025-01-01",
                       "fecha_fin_prevista": "2025-06-30", "renta_inicial": 850, "estado": "Vigente",
                       "_dias_alquilado": 181, "_ingresos_devengados": 5100}],
        "cobros": [{"codigo": "COB-001", "contrato": "CTR-001", "periodo_desde": "2025-01-01",
                    "periodo_hasta": "2025-01-31", "fecha_cobro": "2025-01-05", "importe_cobrado": 850,
                    "estado": "Cobrado"}],
        "ingresos_devengados": 5100, "ingresos_cobrados": 850}
    a.ctrl = NS(lector=NS(listar=lambda *x, **k: []), resumen_alquileres=Mock(return_value=resumen))
    t = textos(a._sec_alquileres())
    for esperado in ("Renta devengada 2025", "Cobros registrados 2025", "181 días alquilado", ui.eur(5100),
                     "Cobros · 2025", "(fin previsto)", ui.eur(850)):
        assert esperado in t
    a.ctrl.resumen_alquileres.assert_called_once_with(7, 2025)


def test_alquileres_sin_backend_nuevo_sigue_con_lista_basica():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio = ui.CLARO, 7, 2025
    a.ctrl = NS(lector=NS(listar=lambda *x, **k: []))
    t = textos(a._sec_alquileres())
    assert "Contratos de alquiler" in t and "Renta devengada" not in t


@pytest.fixture
def contrato():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cerrando = ui.CLARO, 7, 2025, False
    lector = NS(get=Mock(return_value=None),
                listar=Mock(return_value=[{"codigo": "INM-001", "direccion": "Calle ficticia"}]))
    a.ctrl = NS(lector=lector, editar=Mock(return_value={}), validar=Mock(return_value={}),
                previsualizar_contrato=Mock(return_value={"dias_alquilado": 122, "ingresos_devengados": 4000,
                                                          "fecha_fin_prevista": "2025-12-31",
                                                          "avisos": ["Aviso ficticio"]}))
    workers = []
    a.page = NS(update=Mock(), pop_dialog=Mock(), run_thread=workers.append)  # sin run_task: UI síncrona
    a._dialogo, a.avisar, a.ocupada, a.cerrar_dialogo = Mock(), Mock(), Mock(return_value=False), Mock()
    def ejecutar(texto, trabajo, *x, **k):
        trabajo()
        if k.get("despues"):
            k["despues"]()
        return True
    a.en_segundo_plano = Mock(side_effect=ejecutar)
    a.dialogo_contrato(None)
    contenido, acciones = a._dialogo.call_args.args[1:3]
    buscar(contenido, "Inmueble").value = "INM-001"
    buscar(contenido, "Fecha de inicio").value = "01/09/2025"
    buscar(contenido, "Renta mensual (€)").value = "1.000,00"
    buscar(contenido, "Duración en meses (ayuda)").value = "4"
    return a, workers, contenido, acciones


def test_previsualizacion_en_worker_sin_guardar(contrato):
    a, workers, contenido, _ = contrato
    buscar(contenido, "Duración en meses (ayuda)").on_blur(None)
    assert a.ctrl.previsualizar_contrato.call_count == 0  # todavía en cola: no en el hilo de UI
    workers.pop()()
    cid, datos, ej = a.ctrl.previsualizar_contrato.call_args.args
    assert (cid, ej) == (7, 2025) and datos["duracion_meses"] == 4 and datos["inmueble"] == "INM-001"
    t = textos(contenido)
    assert "122 días" in t and ui.eur(4000) in t and "31/12/2025" in t and "Aviso ficticio" in t
    a.ctrl.editar.assert_not_called()


def test_previsualizacion_obsoleta_se_descarta(contrato):
    a, workers, contenido, _ = contrato
    campo = buscar(contenido, "Fecha de inicio")

    def prever(_cid, datos, _ej):
        if a.ctrl.previsualizar_contrato.call_count == 1:
            campo.value = "01/10/2025"
            campo.on_blur(None)  # el usuario cambia algo mientras se calcula la primera
            return {"dias_alquilado": 1}
        return {"dias_alquilado": 122}
    a.ctrl.previsualizar_contrato.side_effect = prever
    campo.on_blur(None)
    assert len(workers) == 1
    workers.pop(0)()   # el mismo worker calcula la primera (obsoleta) y después la pendiente
    assert not workers  # sin un hilo por blur
    assert a.ctrl.previsualizar_contrato.call_count == 2
    assert "1 día" not in textos(contenido)
    assert "122 días" in textos(contenido)


def test_guardado_por_controlador_sin_duracion_y_sin_doble_clic(contrato):
    a, _, _, acciones = contrato
    acciones[-1].on_click(None)
    acciones[-1].on_click(None)
    a.ctrl.editar.assert_called_once()
    cid, tabla, codigo, cambios, ej = a.ctrl.editar.call_args.args
    assert (cid, tabla, codigo, ej) == (7, "contratos", None, 2025)
    assert "duracion_meses" not in cambios
    assert cambios["fecha_inicio"] == "01/09/2025" and cambios["estado"] == "Vigente"
    assert cambios["periodicidad"] == "Mensual" and cambios["renta_inicial"] == "1.000,00"


def test_contrato_nuevo_exige_inmueble_y_fecha(contrato):
    a, _, contenido, acciones = contrato
    buscar(contenido, "Fecha de inicio").value = ""
    acciones[-1].on_click(None)
    a.ctrl.editar.assert_not_called()
    assert "fecha de inicio" in textos(contenido)


# ------------------------------------------------------------------------------------ Excel al volver
def app_excel(res=None, error=None):
    a = main.App.__new__(main.App)
    a.cerrando, a._turno, a.p = False, threading.Lock(), ui.CLARO
    a.cid, a.ejercicio, a.informe = 7, 2026, "INFORME PREVIO"
    llamadas = []
    def sincronizar(cid, ej):
        llamadas.append((cid, ej))
        if error is not None:
            raise error
        return res or {}
    a.ctrl = NS(sincronizar_excel_si_cambio=sincronizar)
    workers = []
    a.page = NS(run_thread=workers.append)
    a._pintar_principal, a.avisar, a._dialogo, a.cerrar_dialogo = Mock(), Mock(), Mock(), Mock()
    a.set_ocupado = Mock()  # la comprobación muestra la barra de progreso (A-UI 4)
    return a, workers, llamadas


def test_excel_sin_cambios_no_repinta_ni_bloquea():
    a, workers, llamadas = app_excel()
    assert a._comprobar_excel("foco")
    workers.pop()()
    assert llamadas == [(7, 2026)]
    a._pintar_principal.assert_not_called()
    a.avisar.assert_not_called()
    assert not a._turno.locked()
    assert not a._comprobar_excel("pestaña")  # intervalo mínimo entre comprobaciones


def test_excel_con_cambios_actualiza_informe_sin_tocar_conversaciones():
    a, workers, _ = app_excel({"informe": "INFORME NUEVO", "excel": "ficticio.xlsx"})
    a.conversaciones = {7: "conversación"}
    a._comprobar_excel("foco")
    workers.pop()()
    assert a.informe == "INFORME NUEVO" and a.conversaciones == {7: "conversación"}
    a._pintar_principal.assert_called_once()


def test_excel_resultado_tardio_de_otro_cliente_se_descarta():
    a, workers, _ = app_excel({"informe": "INFORME DEL 7"})
    a._comprobar_excel("foco")
    a.cid = 8
    workers.pop()()
    assert a.informe == "INFORME PREVIO"
    a._pintar_principal.assert_not_called()
    assert not a._turno.locked()


def test_excel_conflicto_avisa_una_vez():
    a, workers, _ = app_excel(error=excel_generator.ExcelConflictoImportacion("Conflicto ficticio"))
    for _ in range(2):
        a._excel_ultima = float("-inf")
        a._comprobar_excel("foco")
        workers.pop()()
    assert a._dialogo.call_count == 1
    assert not a._turno.locked()


def test_excel_no_comprueba_con_tarea_en_curso_ni_sin_backend():
    a, workers, llamadas = app_excel()
    a._turno.acquire()
    assert not a._comprobar_excel("foco") and not workers and not llamadas
    a._turno.release()
    a.ctrl = NS()
    assert not a._comprobar_excel("foco")


def test_excel_error_tardio_no_abre_dialogo_en_otro_expediente():
    a, workers, _ = app_excel(error=excel_generator.ExcelConflictoImportacion("Conflicto ficticio"))
    a._comprobar_excel("foco")
    a.cid = 8
    workers.pop()()
    a._dialogo.assert_not_called()
    a.avisar.assert_not_called()
    assert not a._turno.locked()


def test_previa_no_se_aplica_si_usuario_cambia_de_expediente(contrato):
    a, workers, contenido, _ = contrato
    buscar(contenido, "Fecha de inicio").on_blur(None)
    a.cid = 8
    workers.pop()()
    assert "122 días" not in textos(contenido)


def test_referencia_actual_no_se_etiqueta_como_catastro_heredado():
    v = {"fecha_adquisicion": "2022-01-01", "_valor_referencia_actual": 210000}
    cat = {"valor_catastral": 90000, "_heredado": 2024}
    filas = {e: (x, n) for e, x, n in uc.comparativa_valores(v, {"catastral": 90000}, cat, 2025)}
    assert filas["Referencia Catastro 2025:"] == (ui.eur(210000), None)
    assert "2024" in filas["Valor catastral 2025:"][1]
