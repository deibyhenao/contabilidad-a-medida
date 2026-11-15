"""UI de Otros activos (Patrimonio 714, encargo 04/11/2026). Datos ficticios; el backend (tabla, valoración,
recálculo del 714) se sustituye por dobles porque Codex lo construye en paralelo. Sin llamadas a API reales."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import flet as ft
import pytest

import carteras
import main
import ui_estilo as ui
import ui_otros_activos as uoa
from test_documentos_ui import recorrer


def textos(control) -> str:
    return " | ".join(str(c.value) for c in recorrer(control) if isinstance(c, ft.Text))


def _res(valor=1000.0, avisos=(), provisional=None, regla="Regla ficticia"):
    return {"valor_calculado": valor, "valor_cliente": valor, "valor_sujeto": valor, "avisos": list(avisos),
            "provisional": bool(avisos) if provisional is None else provisional, "regla": regla}


class BackendFicticio:
    """Doble de ``otros_activos``: devuelve lo que se le programe por código y registra las llamadas."""

    def __init__(self):
        self.por_codigo, self.llamadas, self.error_validar = {}, [], None
        # Contrato de ``conciliar_carteras``: {codigo_resumen: [avisos]}; solo resúmenes con problema.
        self.conciliacion, self.conciliaciones = {}, []

    def conciliar_carteras(self, filas, valoraciones=None):
        self.conciliaciones.append(([f.get("codigo") for f in filas], valoraciones))
        return {c: list(a) for c, a in self.conciliacion.items()}

    def valorar(self, fila):
        self.llamadas.append(dict(fila))
        return self.por_codigo.get(fila.get("codigo"), _res(float(fila.get("valor_informe") or 0)))

    def validar(self, fila):
        if self.error_validar:
            raise ValueError(self.error_validar)


CUENTA = {"codigo": "OA-001", "ejercicio": 2025, "tipo": "CUENTA", "entidad": "Banco Ficticio",
          "identificador": "ES00 0000", "pct_titularidad": 0.5, "ubicacion": "ESPANA", "saldo_31dic": 1000.0,
          "saldo_medio_4t": 900.0, "estado": "Activo", "revision_confirmada": 1, "confirmacion_legal": "Cert."}
CRIPTO = {"codigo": "OA-002", "ejercicio": 2025, "tipo": "CRIPTO", "entidad": "Exchange Ficticio",
          "unidades": "0.5", "valor_unitario": "40000", "valor_informe": 20000.0, "pct_titularidad": 1,
          "ubicacion": "PENDIENTE", "estado": "Activo", "revision_confirmada": 0}
RESUMEN = {"codigo": "OA-003", "ejercicio": 2025, "tipo": "RESUMEN_CARTERA", "entidad": "Broker Ficticio",
           "valor_informe": 55555.55, "cartera_id": "CART-1", "pct_titularidad": 1, "ubicacion": "ESPANA",
           "estado": "Activo"}
HIJO = {"codigo": "OA-004", "ejercicio": 2025, "tipo": "FONDO", "entidad": "Broker Ficticio",
        "descripcion": "Fondo ficticio", "valor_informe": 30000.0, "cartera_id": "CART-1", "pct_titularidad": 1,
        "ubicacion": "ESPANA", "estado": "Activo", "revision_confirmada": 1, "confirmacion_legal": "Cert."}
BAJA = {**HIJO, "codigo": "OA-005", "descripcion": "Fondo vendido", "estado": "Baja"}
P714 = {"codigo": "714-2025", "ejercicio": 2025, "valor_bienes": 812345.67, "base_liquidable": 112345.67,
        "resultado": 222.22, "estado": "Pendiente"}


@pytest.fixture
def bk(monkeypatch):
    b = BackendFicticio()
    monkeypatch.setattr(uoa, "backend", lambda: b)
    monkeypatch.setattr(uoa, "tabla_disponible", lambda: True)
    return b


def montar(filas=(), filas_714=(P714,)):
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cerrando, a.cartera = ui.CLARO, 7, 2025, False, carteras.NR
    tablas = {"otros_activos": list(filas), "patrimonio_714": list(filas_714)}
    lector = NS(listar=Mock(side_effect=lambda t, cid, **k: [dict(f) for f in tablas.get(t, [])]),
                get=Mock(side_effect=lambda t, cid, c: next((dict(f) for f in tablas.get(t, [])
                                                            if f["codigo"] == c), None)))
    a.ctrl = NS(lector=lector, editar=Mock(return_value={}))
    workers = []
    a.page = NS(update=Mock(), run_thread=workers.append)  # sin run_task: ejecutar_en_ui es síncrono
    a._dialogo, a.avisar, a.ocupada, a.cerrar_dialogo = Mock(), Mock(), Mock(return_value=False), Mock()
    a.elegir_pdfs = Mock()

    def ejecutar(texto, trabajo, exito=None, despues=None, al_fallar=None):
        trabajo()
        if despues:
            despues()
        return True
    a.en_segundo_plano = Mock(side_effect=ejecutar)
    return a, workers


def abrir(a, workers, codigo=None):
    a._dialogo.reset_mock()
    uoa.dialogo_activo(a, codigo)
    contenido = a._dialogo.call_args.args[1]
    datos = a._dialogo.return_value.data
    while workers:  # primera previsualización
        workers.pop(0)()
    return contenido, datos


# ------------------------------------------------------------------------------------ reglas de presentación
def test_verde_solo_sin_avisos_y_con_revision_del_asesor():
    assert uoa.estado_valoracion(CUENTA, _res()) == ("Revisado", "verde")
    assert uoa.estado_valoracion(CUENTA, _res(avisos=["Falta media 4T"]))[0] == "Estimación"
    assert uoa.estado_valoracion(CUENTA, _res(provisional=True))[0] == "Estimación"
    assert uoa.estado_valoracion({**CUENTA, "revision_confirmada": 0}, _res())[1] == "naranja"
    assert uoa.estado_valoracion(RESUMEN, _res()) == ("Informativo", "neutro")
    assert uoa.estado_valoracion(BAJA, _res()) == ("Baja", "neutro")
    assert uoa.estado_valoracion(CUENTA, {"error": "x"})[1] == "rojo"


def test_backend_ausente_o_invalido_no_se_convierte_en_cero(monkeypatch):
    monkeypatch.setattr(uoa, "backend", lambda: None)
    res = uoa.valoracion(CUENTA)
    assert res["provisional"] and res["error"] and "valor_cliente" not in res
    assert uoa.cifra_fila(CUENTA, res) == (uoa.SIN_DATO, "Sin valoración")
    malo = NS(valorar=Mock(side_effect=ValueError("Tipo de activo no válido")))
    monkeypatch.setattr(uoa, "backend", lambda: malo)
    assert uoa.valoracion({"tipo": "X"})["error"] == "Tipo de activo no válido"


def test_resumen_de_cartera_muestra_su_total_como_informativo():
    assert uoa.cifra_fila(RESUMEN, _res(0.0)) == (ui.eur(55555.55), "Total informado · no suma")
    assert uoa.cifra_fila(HIJO, _res(30000.0))[0] == ui.eur(30000)


def test_decimales_sin_redondear_y_con_coma():
    assert uoa.decimal_texto("0,12345678") == "0.12345678"
    assert uoa.decimal_texto("1.234,5") == "1234.5"
    assert uoa.decimal_texto("") is None and uoa.decimal_texto("10") == "10"
    assert uoa.decimal_texto("40.000") == "40000"  # miles, igual que database.a_num
    assert uoa.decimal_texto("0.125") == "0.125"
    with pytest.raises(ValueError):
        uoa.decimal_texto("doce")


def test_resumen_714_sin_fila_no_inventa_ceros():
    assert {v for _, v in uoa.resumen_714([])} == {uoa.SIN_DATO}
    assert dict(uoa.resumen_714([P714]))["Valor de bienes"] == ui.eur(812345.67)


def test_pestana_solo_en_no_residentes_sin_mover_indices():
    assert "Otros activos" in main.SECCIONES and "Otros activos" not in main.SECCIONES_RES
    assert main.SECCIONES.index("Gastos") == 5 and main.SECCIONES.index("Otros activos") == 6


# ------------------------------------------------------------------------------------ sección
def _expand_bajo_wrap(vista) -> list:
    """Hijos directos con expand dentro de Row(wrap=True): Flet los pinta como Expanded bajo Wrap
    (Incorrect use of ParentDataWidget) y Flutter deja la vista en gris sin excepción Python."""
    return [(type(fila).__name__, type(h).__name__) for fila in recorrer(vista)
            if isinstance(fila, ft.Row) and getattr(fila, "wrap", False)
            for h in (fila.controls or []) if getattr(h, "expand", None)]


@pytest.mark.parametrize("disponible", [True, False])
def test_seccion_sin_hijos_expand_bajo_row_wrap(bk, monkeypatch, disponible):
    monkeypatch.setattr(uoa, "tabla_disponible", lambda: disponible)
    bk.por_codigo["OA-002"] = _res(20000.0, avisos=["Pendiente confirmar sujeción"], provisional="Obs")
    a, _ = montar([CUENTA, CRIPTO, RESUMEN, HIJO, BAJA])
    assert _expand_bajo_wrap(uoa.seccion(a)) == []


def test_detector_expand_bajo_wrap_no_es_ciego():
    malo = ft.Column([ft.Container(ft.Row([ft.Column([], expand=True), ft.Text("x")], wrap=True))])
    assert _expand_bajo_wrap(malo) == [("Row", "Column")]


def test_seccion_lista_por_categoria_y_resumen_del_backend(bk):
    bk.por_codigo["OA-002"] = _res(20000.0, avisos=["Pendiente confirmar sujeción"])
    a, _ = montar([CUENTA, CRIPTO, RESUMEN, HIJO, BAJA])
    vista = uoa.seccion(a)
    t = textos(vista)
    a.ctrl.lector.listar.assert_any_call("otros_activos", 7, ejercicio=2025)
    a.ctrl.lector.listar.assert_any_call("patrimonio_714", 7, ejercicio=2025)
    for esperado in (ui.eur(812345.67), ui.eur(112345.67), ui.eur(222.22), "Banco Ficticio", "Revisado",
                     "SEGÚN EL ÚLTIMO RECÁLCULO"):
        assert esperado in t
    assert "Estimación: 1 activo" in t  # la cripto pendiente impide el mensaje verde
    assert "Todos los activos están valorados" not in t
    assert "Exchange Ficticio" not in t  # otra categoría: no se pinta hasta elegirla


def test_cambio_de_categoria_con_fundido_y_resumen_aparte(bk):
    a, _ = montar([CUENTA, CRIPTO, RESUMEN, HIJO, BAJA])
    vista = uoa.seccion(a)
    selector = next(c for c in recorrer(vista) if isinstance(c, ft.CupertinoSlidingSegmentedButton))
    cuerpo = next(c for c in recorrer(vista) if isinstance(c, ft.AnimatedSwitcher)  # no el de la normativa
                  and str(getattr(c.content, "key", "")).startswith("oa-"))
    assert cuerpo.transition == ft.AnimatedSwitcherTransition.FADE and cuerpo.duration <= 200
    selector.selected_index = 2
    selector.on_change(NS(control=selector))
    assert a._oa_categoria == 2
    t = textos(cuerpo)
    assert "RESÚMENES DE CARTERA · INFORMATIVOS" in t and "no se suma" in t
    assert ui.eur(55555.55) in t and ui.eur(30000) in t
    assert ui.eur(85555.55) not in t  # nunca total de la cartera + posiciones
    assert "DADOS DE BAJA · 1" in t and "Fondo vendido" in t
    selector.selected_index = 1
    selector.on_change(NS(control=selector))
    assert "Exchange Ficticio" in textos(cuerpo)


def test_todo_revisado_permite_verde(bk):
    a, _ = montar([CUENTA])
    assert "Todos los activos están valorados y revisados" in textos(uoa.seccion(a))


def test_714_presentado_avisa_sin_bloquear(bk):
    a, workers = montar([CUENTA], [{**P714, "estado": "Presentado"}])
    assert "ya está presentado" in textos(uoa.seccion(a))
    contenido, d = abrir(a, workers, "OA-001")
    assert "ya está presentado" in textos(contenido)
    d["textos"]["observaciones"].value = "Nota ficticia"
    d["guardar"]()
    a.ctrl.editar.assert_called_once()  # el aviso no impide guardar


def test_tabla_no_disponible_no_lee_ni_inventa(monkeypatch):
    monkeypatch.setattr(uoa, "tabla_disponible", lambda: False)
    a, _ = montar([CUENTA])
    t = textos(uoa.seccion(a))
    assert "aún no está disponible" in t and "No se ha registrado ni calculado nada" in t
    a.ctrl.lector.listar.assert_not_called()


def test_boton_cargar_reutiliza_la_ingesta_existente(bk):
    a, _ = montar([])
    vista = uoa.seccion(a)
    boton = next(c for c in recorrer(vista) if isinstance(c, ft.Container) and c.tooltip
                 and "Extractos" in str(c.tooltip))
    boton.on_click(None)
    a.elegir_pdfs.assert_called_once()


# ------------------------------------------------------------------------------------ alta y edición
def test_alta_con_valores_por_defecto_y_sin_esperados(bk):
    a, workers = montar([])
    a._oa_categoria = 1  # en Cripto: el alta propone criptoactivo
    contenido, d = abrir(a, workers)
    assert d["tipo"].value == "CRIPTO" and d["ubicacion"].value == "PENDIENTE"
    assert d["textos"]["pct_titularidad"].value == "100 %"
    t = textos(contenido)
    assert "Total y unidades son datos distintos" in t
    assert d["textos"]["valor_unitario"].label == "Valor unitario a 31/12 (€ por unidad)"
    assert "no el precio por unidad" in d["textos"]["valor_informe"].helper
    d["textos"]["entidad"].value = "Exchange Ficticio"
    d["textos"]["unidades"].value = "0,125"
    d["textos"]["valor_unitario"].value = "40.000,00"
    d["guardar"]()
    d["guardar"]()  # doble clic: un solo guardado
    a.ctrl.editar.assert_called_once()
    (cid, tabla, codigo, datos, ej), k = a.ctrl.editar.call_args
    assert (cid, tabla, codigo, ej, k) == (7, "otros_activos", None, 2025, {"esperados": None})
    assert datos["tipo"] == "CRIPTO" and datos["ubicacion"] == "PENDIENTE" and datos["estado"] == "Activo"
    assert datos["pct_titularidad"] == "100 %" and datos["ejercicio"] == 2025 and datos["origen"] == "app"
    assert datos["revision_confirmada"] == 0 and datos["unidades"] == "0.125"
    assert datos["valor_unitario"] == "40000"
    assert "valor_manual" not in datos and "saldo_31dic" not in datos  # nada inventado


def test_edicion_envia_solo_cambios_y_esperados_previos(bk):
    a, workers = montar([CRIPTO])
    _, d = abrir(a, workers, "OA-002")
    assert d["textos"]["unidades"].value == "0,5"
    d["textos"]["valor_informe"].value = "21.000,00"
    d["guardar"]()
    (cid, tabla, codigo, datos, ej), k = a.ctrl.editar.call_args
    assert codigo == "OA-002" and datos == {"valor_informe": "21.000,00"}
    assert k["esperados"]["valor_informe"] == 20000.0  # la fila leída al abrir: el backend detecta cambios


def test_validacion_del_backend_se_muestra_junto_al_formulario(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    bk.error_validar = "Justifica la exención fiscal."
    d["switches"]["exento"].value = True
    d["guardar"]()
    a.ctrl.editar.assert_not_called()
    assert d["error"].value == "Justifica la exención fiscal."


def test_importe_ilegible_no_se_envia(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    d["textos"]["saldo_31dic"].value = "mil euros"
    d["guardar"]()
    a.ctrl.editar.assert_not_called()
    assert "Saldo a 31/12" in d["error"].value


def test_desactivar_valor_manual_lo_borra_al_guardar(bk):
    a, workers = montar([{**CUENTA, "valor_manual": 5000.0, "motivo_manual": "Certificado", "manual_override": 1}])
    _, d = abrir(a, workers, "OA-001")
    assert d["switches"][uoa.USAR_MANUAL].value is True  # hay valor manual
    d["switches"][uoa.USAR_MANUAL].value = False
    d["switches"][uoa.USAR_MANUAL].on_change(None)
    d["guardar"]()
    datos = a.ctrl.editar.call_args.args[3]
    assert datos["valor_manual"] == "" and datos["motivo_manual"] == ""
    assert "manual_override" not in datos and uoa.USAR_MANUAL not in datos  # la protección la fija el backend


def test_guardado_tras_cambiar_de_expediente_no_escribe(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    d["textos"]["observaciones"].value = "x"
    a.cid = 8
    d["guardar"]()
    a.ctrl.editar.assert_not_called()
    assert "cliente y ejercicio correctos" in d["error"].value


def test_rechazo_por_tarea_en_curso_deja_el_formulario_editable(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    a.en_segundo_plano = Mock(return_value=False)
    d["textos"]["observaciones"].value = "x"
    d["guardar"]()
    assert not d["estado"]["guardando"] and not d["textos"]["observaciones"].read_only


# ------------------------------------------------------------------------------------ titularidad (U2, 05/11/2026)
def test_edicion_titularidad_vaciada_bloquea_previa_y_guardado_sin_convertir_en_100(bk):
    """Antes: la previa calculaba con 100 % y ``cambios`` enviaba "" (el backend rechaza None)."""
    a, workers = montar([CUENTA])  # 50 %
    _, d = abrir(a, workers, "OA-001")
    assert d["textos"]["pct_titularidad"].value == ui.pct(0.5)
    llamadas = len(bk.llamadas)
    d["textos"]["pct_titularidad"].value = ""
    d["textos"]["pct_titularidad"].on_blur(None)
    assert not workers and len(bk.llamadas) == llamadas  # no se valora con un 100 % inventado
    assert d["prev_cliente"].value == uoa.SIN_DATO
    d["guardar"]()
    a.ctrl.editar.assert_not_called()
    assert "Titularidad (%)" in d["error"].value and "100 %" in d["error"].value


def test_edicion_titularidad_vaciada_muestra_el_motivo_en_la_previa(bk):
    a, workers = montar([CUENTA])
    contenido, d = abrir(a, workers, "OA-001")
    d["textos"]["pct_titularidad"].value = "  "
    d["textos"]["pct_titularidad"].on_blur(None)
    assert d["prev_cliente"].value == uoa.SIN_DATO
    assert "Titularidad (%)" in textos(contenido)


def test_edicion_conserva_50_si_no_se_toca_y_envia_el_nuevo_si_se_cambia(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    assert bk.llamadas[-1]["pct_titularidad"] == 0.5  # la previa usa el 50 % existente
    d["textos"]["observaciones"].value = "Revisado"
    d["guardar"]()
    assert a.ctrl.editar.call_args.args[3] == {"observaciones": "Revisado"}  # no reenvía la titularidad
    a2, w2 = montar([CUENTA])
    _, d2 = abrir(a2, w2, "OA-001")
    d2["textos"]["pct_titularidad"].value = "100 %"
    d2["textos"]["pct_titularidad"].on_blur(None)
    w2.pop()()
    assert bk.llamadas[-1]["pct_titularidad"] == 1
    d2["guardar"]()
    assert a2.ctrl.editar.call_args.args[3] == {"pct_titularidad": "100 %"}


def test_alta_con_titularidad_vacia_sigue_proponiendo_100(bk):
    a, workers = montar([])
    _, d = abrir(a, workers)
    d["textos"]["entidad"].value = "Banco Ficticio"
    d["textos"]["pct_titularidad"].value = ""
    d["textos"]["pct_titularidad"].on_blur(None)
    workers.pop()()
    assert bk.llamadas[-1]["pct_titularidad"] == 1  # previa y guardado coinciden: 100 %
    d["guardar"]()
    assert a.ctrl.editar.call_args.args[3]["pct_titularidad"] == "100 %"


def test_backend_real_rechaza_titularidad_vacia_como_la_ui(bk):
    real = pytest.importorskip("otros_activos")
    with pytest.raises(ValueError, match="titularidad"):
        real.validar({**CUENTA, "pct_titularidad": None})


def test_cancelar_con_previsualizacion_pendiente_no_pinta_ni_escribe(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    antes, llamadas = d["prev_cliente"].value, len(bk.llamadas)
    d["textos"]["saldo_31dic"].value = "9.999,00"
    d["textos"]["saldo_31dic"].on_blur(None)
    assert len(workers) == 1  # previa en cola
    d["cerrar"]()
    a.cerrar_dialogo.assert_called_once()
    workers.pop()()  # el worker arranca tras cancelar
    assert len(bk.llamadas) == llamadas and d["prev_cliente"].value == antes
    assert not d["estado"]["activo"]
    d["guardar"]()  # un Enter tardío tras cancelar no guarda
    a.ctrl.editar.assert_not_called()


def test_cancelar_con_valoracion_en_curso_descarta_el_resultado(bk):
    a, workers = montar([CUENTA])
    _, d = abrir(a, workers, "OA-001")
    antes = d["prev_cliente"].value
    original = bk.valorar

    def valorar_y_cancelar(fila):  # el usuario cancela mientras el worker valora
        d["cerrar"]()
        return original({**fila, "valor_informe": 1})
    bk.valorar = valorar_y_cancelar
    d["textos"]["saldo_31dic"].value = "9.999,00"
    d["textos"]["saldo_31dic"].on_blur(None)
    workers.pop()()
    assert d["prev_cliente"].value == antes and not d["estado"]["activo"]
    a.ctrl.editar.assert_not_called()


# ------------------------------------------------------------------------------------ previsualización
def test_previsualizacion_en_worker_y_estimacion(bk):
    bk.por_codigo["OA-002"] = _res(20000.0, avisos=["Pendiente confirmar sujeción en España"])
    a, workers = montar([CRIPTO])
    uoa.dialogo_activo(a, "OA-002")
    d = a._dialogo.return_value.data
    assert not bk.llamadas and len(workers) == 1  # nada calculado en el hilo de UI
    workers.pop()()
    assert d["prev_cliente"].value == ui.eur(20000)
    assert "Estimación" in textos(d["prev_estado"])
    a.ctrl.editar.assert_not_called()


def test_previsualizacion_obsoleta_o_de_otro_expediente_se_descarta(bk):
    a, workers = montar([CRIPTO])
    uoa.dialogo_activo(a, "OA-002")
    d = a._dialogo.return_value.data
    campo = d["textos"]["valor_informe"]
    campo.value = "1,00"
    campo.on_blur(None)  # segunda solicitud antes de que corra el worker
    assert len(workers) == 1  # un solo worker por diálogo
    workers.pop()()
    assert d["prev_cliente"].value == ui.eur(1)
    campo.value = "2,00"
    campo.on_blur(None)
    a.cid = 8  # el usuario cambió de cliente mientras se calculaba
    workers.pop()()
    assert d["prev_cliente"].value == ui.eur(1)


# ------------------------------------------------------------------------------------ baja
def test_baja_con_confirmacion_y_esperados(bk):
    a, _ = montar([CUENTA])
    uoa.confirmar_baja(a, "OA-001")
    titulo, contenido, acciones = a._dialogo.call_args.args[:3]
    assert "OA-001" in titulo and "puedes reactivarlo" in textos(contenido)
    a.ctrl.editar.assert_not_called()  # nada hasta confirmar
    acciones[-1].on_click(None)
    acciones[-1].on_click(None)
    a.ctrl.editar.assert_called_once()
    (cid, tabla, codigo, datos, ej), k = a.ctrl.editar.call_args
    assert (tabla, codigo, datos) == ("otros_activos", "OA-001", {"estado": "Baja"})
    assert k["esperados"]["codigo"] == "OA-001"


def test_baja_tras_cambiar_de_expediente_no_escribe(bk):
    a, _ = montar([CUENTA])
    uoa.confirmar_baja(a, "OA-001")
    acciones = a._dialogo.call_args.args[2]
    a.ejercicio = 2026
    acciones[-1].on_click(None)
    a.ctrl.editar.assert_not_called()
    assert a.avisar.call_args.kwargs.get("error") or a.avisar.call_args.args[1]


def test_reactivar_y_activo_desaparecido(bk):
    a, _ = montar([BAJA])
    uoa.reactivar(a, "OA-005")
    assert a.ctrl.editar.call_args.args[3] == {"estado": "Activo"}
    uoa.confirmar_baja(a, "NO-EXISTE")
    a.avisar.assert_called_with("El activo ya no está disponible.", True)


# ------------------------------------------------------------------------------------ backend real (si existe)
def test_contrato_con_otros_activos_real_si_esta_disponible(monkeypatch):
    real = pytest.importorskip("otros_activos")
    monkeypatch.setattr(uoa, "backend", lambda: real)
    res = uoa.valoracion({**CUENTA, "saldo_medio_4t": None})
    assert set(res) >= {"valor_calculado", "valor_cliente", "valor_sujeto", "avisos", "provisional", "regla"}
    assert uoa.estado_valoracion(CUENTA, res)[0] == "Estimación"  # falta la media 4T: nunca verde
    assert uoa.cifra_fila(RESUMEN, uoa.valoracion(RESUMEN))[1] == "Total informado · no suma"


# ------------------------------------------------------------------------------------ auditoría final 04/11/2026
def test_fila_protegida_sin_valor_manual_arranca_con_interruptor_apagado(bk):
    """manual_override=1 = fila editada por una persona; no implica usar un valor manual."""
    a, workers = montar([{**CUENTA, "manual_override": 1, "valor_manual": None, "motivo_manual": None}])
    _, d = abrir(a, workers, "OA-001")
    assert d["switches"][uoa.USAR_MANUAL].value is False
    assert "manual_override" not in d["switches"]
    d["textos"]["observaciones"].value = "Nota del asesor"
    d["guardar"]()
    datos = a.ctrl.editar.call_args.args[3]
    assert datos == {"observaciones": "Nota del asesor"}  # ni manual_override=0 ni borrados espurios


def test_alta_nunca_envia_manual_override(bk):
    a, workers = montar([])
    _, d = abrir(a, workers)
    d["textos"]["entidad"].value = "Banco Ficticio"
    d["switches"][uoa.USAR_MANUAL].value = True
    d["textos"]["valor_manual"].value = "1.000,00"
    d["textos"]["motivo_manual"].value = "Certificado"
    d["guardar"]()
    datos = a.ctrl.editar.call_args.args[3]
    assert "manual_override" not in datos and uoa.USAR_MANUAL not in datos and datos["motivo_manual"] == "Certificado"


def test_resumen_sin_posiciones_avisa_y_no_permite_verde(bk):
    bk.conciliacion = {"OA-003": [uoa.AVISO_RESUMEN_SIN_POSICIONES]}  # lo que daría el backend para estas filas
    a, _ = montar([CUENTA, RESUMEN])  # RESUMEN de CART-1 sin ninguna posición de CART-1
    vista = textos(uoa.seccion(a))
    assert "Todos los activos están valorados y revisados" not in vista
    a._oa_categoria = 2
    vista = textos(uoa.seccion(a))
    assert "Informativo · revisar" in vista and uoa.AVISO_RESUMEN_SIN_POSICIONES in vista
    assert "Total informado · no suma" in vista


def test_resumen_con_posiciones_sigue_informativo_neutro(monkeypatch):
    """Backend real: el resumen cuadra con su posición (30.000) -> neutro; con solo una baja -> sin posiciones."""
    real = pytest.importorskip("otros_activos")
    monkeypatch.setattr(uoa, "backend", lambda: real)
    cuadrado = {**RESUMEN, "valor_informe": 30000.0}
    assert uoa.resumenes_sin_posiciones([cuadrado, HIJO]) == set()
    assert uoa.resumenes_sin_posiciones([cuadrado, BAJA]) == {"OA-003"}  # una baja no es posición
    res = {f["codigo"]: uoa.valoracion(f) for f in (cuadrado, HIJO)}
    uoa.marcar_resumenes([cuadrado, HIJO], res)
    assert uoa.estado_valoracion(cuadrado, res["OA-003"]) == ("Informativo", "neutro")


def test_aviso_de_resumen_del_backend_no_se_duplica(bk):
    bk.conciliacion = {"OA-003": [uoa.AVISO_RESUMEN_SIN_POSICIONES]}
    res = {"OA-003": {"valor_calculado": 0.0,
                      "avisos": ["Resumen de cartera sin posiciones desglosadas: valoración incompleta"]}}
    uoa.marcar_resumenes([RESUMEN], res)
    uoa.marcar_resumenes([RESUMEN], res)  # repintar no acumula
    assert len(res["OA-003"]["avisos"]) == 1 and res["OA-003"]["provisional"] is True


def test_marcar_resumenes_traslada_el_conciliador_sin_hacer_cuentas(bk):
    """La UI pasa filas y valoraciones al backend y copia sus avisos; no decide el descuadre."""
    descuadre = "El resumen de cartera no coincide con la valoración de sus posiciones: conciliar sin duplicar."
    bk.conciliacion = {"OA-003": [descuadre]}
    res = {f["codigo"]: uoa.valoracion(f) for f in (RESUMEN, HIJO)}
    uoa.marcar_resumenes([RESUMEN, HIJO], res)
    assert bk.conciliaciones[-1] == (["OA-003", "OA-004"], res)
    assert res["OA-003"]["avisos"] == [descuadre] and res["OA-004"]["avisos"] == []
    assert uoa.estado_valoracion(RESUMEN, res["OA-003"]) == ("Informativo · revisar", "naranja")
    assert uoa.resumenes_sin_posiciones([RESUMEN, HIJO]) == set()  # un descuadre no es «sin posiciones»
    bk.conciliacion = {}
    res = {f["codigo"]: uoa.valoracion(f) for f in (RESUMEN, HIJO)}
    uoa.marcar_resumenes([RESUMEN, HIJO], res)  # el backend no avisa: la UI no inventa nada
    assert uoa.estado_valoracion(RESUMEN, res["OA-003"]) == ("Informativo", "neutro")


def test_valoracion_con_error_no_se_pasa_como_importe(bk):
    bk.conciliacion = {"OA-003": [uoa.AVISO_RESUMEN_SIN_POSICIONES]}
    res = {"OA-003": uoa.valoracion(RESUMEN), "OA-004": {"error": "Datos no válidos.", "avisos": [],
                                                         "provisional": True}}
    uoa.marcar_resumenes([RESUMEN, HIJO], res)
    assert bk.conciliaciones[-1][1] is None  # que el backend revalore o avise; nunca un cero de la UI


def test_conciliador_que_falla_no_da_la_cartera_por_cuadrada(bk, monkeypatch):
    def roto(filas, valoraciones=None):
        raise KeyError("OA-004")
    monkeypatch.setattr(bk, "conciliar_carteras", roto)
    res = {"OA-003": {"valor_calculado": 0.0, "avisos": []}}
    uoa.marcar_resumenes([RESUMEN, HIJO], res)
    assert res["OA-003"]["avisos"] == [uoa.AVISO_CONCILIACION_FALLIDA] and res["OA-003"]["provisional"] is True


def test_sin_conciliador_en_backend_usa_el_respaldo_por_cartera(monkeypatch):
    viejo = NS(valorar=lambda fila: _res(float(fila.get("valor_informe") or 0)))  # backend sin el helper
    monkeypatch.setattr(uoa, "backend", lambda: viejo)
    assert uoa.resumenes_sin_posiciones([RESUMEN, HIJO]) == set()
    assert uoa.resumenes_sin_posiciones([RESUMEN, BAJA]) == {"OA-003"}
    res = {"OA-003": {"avisos": []}}
    uoa.marcar_resumenes([RESUMEN], res)
    assert res["OA-003"]["avisos"] == [uoa.AVISO_RESUMEN_SIN_POSICIONES]


def test_backend_real_resumen_por_entidad_con_valor_manual_y_cotizado_unitario(bk, monkeypatch):
    """Backend REAL: resumen sin cartera_id que se concilia por entidad con una posición de valor manual;
    descuadre marcado en la vista y campos unitarios del cotizado visibles con su etiqueta."""
    real = pytest.importorskip("otros_activos")
    monkeypatch.setattr(uoa, "backend", lambda: real)
    resumen = {**RESUMEN, "cartera_id": None, "valor_informe": 50000.0}
    manual = {**HIJO, "cartera_id": None, "valor_informe": 30000.0, "valor_manual": 40000.0,
              "motivo_manual": "Certificado de la gestora"}
    res = {f["codigo"]: uoa.valoracion(f) for f in (resumen, manual)}
    uoa.marcar_resumenes([resumen, manual], res)
    assert uoa.resumenes_sin_posiciones([resumen, manual]) == set()  # la entidad la vincula
    assert any("no coincide" in a for a in res["OA-003"]["avisos"])  # 50.000 frente a 40.000 efectivo
    res = {f["codigo"]: uoa.valoracion(f) for f in ({**resumen, "valor_informe": 40000.0}, manual)}
    uoa.marcar_resumenes([{**resumen, "valor_informe": 40000.0}, manual], res)
    assert res["OA-003"]["avisos"] == []  # cuadra con el valor manual, no con el informe
    a, workers = montar([resumen, manual])
    a._oa_categoria = 2
    vista = textos(uoa.seccion(a))
    assert "Informativo · revisar" in vista and "no coincide con la valoración de sus posiciones" in vista
    cotizado = {"codigo": "OA-006", "ejercicio": 2025, "tipo": "ACCION_COTIZADA", "entidad": "Broker Ficticio",
                "unidades": "100", "valor_unitario": "12.5", "pct_titularidad": 1, "ubicacion": "ESPANA",
                "estado": "Activo", "revision_confirmada": 1, "confirmacion_legal": "Cert."}
    assert uoa.valoracion(cotizado)["valor_calculado"] == 1250.0  # lo calcula el backend, no la UI
    a, workers = montar([cotizado])
    contenido, d = abrir(a, workers, "OA-006")
    assert d["textos"]["valor_unitario"].label == "Cotización media 4T (€ por título)"
    assert any(c is d["textos"]["valor_unitario"] for c in recorrer(contenido))


def test_etiqueta_unitaria_cambia_con_el_tipo(bk):
    a, workers = montar([])
    _, d = abrir(a, workers)
    for tipo, etiqueta in (("FONDO", "Liquidativo a 31/12 (€ por participación)"),
                           ("RENTA_FIJA_COTIZADA", "Cotización media 4T (€ por título)"),
                           ("CRIPTO", "Valor unitario a 31/12 (€ por unidad)")):
        d["tipo"].value = tipo
        d["tipo"].on_select(None)
        while workers:
            workers.pop(0)()
        assert "valor_unitario" in uoa.CAMPOS_TIPO[tipo]
        assert d["textos"]["valor_unitario"].label == etiqueta
        assert any(c is d["textos"]["valor_unitario"] for c in recorrer(d["especificos"].content))


def test_714_provisional_por_observaciones_no_se_pone_en_verde(bk):
    obs = "PROVISIONAL: normativa autonómica sin soporte en el motor; revisar DA 4.ª"
    a, _ = montar([CUENTA], filas_714=({**P714, "observaciones": obs},))
    vista = textos(uoa.seccion(a))
    assert "Todos los activos están valorados y revisados" not in vista
    assert "714 provisional" in vista and "DA 4.ª" in vista
    assert uoa.provisional_714([{**P714, "observaciones": "Revisar bonificación CCAA"}])
    assert uoa.provisional_714([P714]) is None


def test_sin_fila_714_no_hay_verde(bk):
    a, _ = montar([CUENTA], filas_714=())
    assert "Todos los activos están valorados y revisados" not in textos(uoa.seccion(a))


def test_pestanas_con_desplazamiento_horizontal():
    import inspect
    fuente = inspect.getsource(main.App)
    assert "ft.Row([self._selector_secciones()], scroll=ft.ScrollMode.AUTO)" in fuente


def test_controlador_real_conserva_proteccion_y_borra_valor_manual(tmp_path, monkeypatch):
    """Backend real (Controlador + Database en tmp_path, sin API): editar desde la UI deja manual_override=1
    y, al apagar «Usar valor manual», el valor y su motivo desaparecen de la BD."""
    real = pytest.importorskip("otros_activos")
    from controlador import Controlador
    ctrl = Controlador(tmp_path / "ficticia.sqlite")
    try:
        ctrl.db.carpeta_base = ctrl.lector.carpeta_base = tmp_path / "Expedientes"
        cid = ctrl.db.crear_cliente("Prueba", "Activos", "X0000000Z")["id"]
        ctrl.editar(cid, "otros_activos", None, {
            "tipo": "CUENTA", "ejercicio": 2025, "entidad": "Banco Ficticio", "ubicacion": "ESPANA",
            "saldo_31dic": "1000", "saldo_medio_4t": "900", "valor_manual": "5000", "motivo_manual": "Certificado"},
            2025)
        codigo = ctrl.db.listar("otros_activos", cid)[0]["codigo"]
        monkeypatch.setattr(uoa, "backend", lambda: real)
        a, workers = montar()
        a.cid, a.ctrl = cid, ctrl
        _, d = abrir(a, workers, codigo)
        assert d["switches"][uoa.USAR_MANUAL].value is True
        d["switches"][uoa.USAR_MANUAL].value = False
        d["switches"][uoa.USAR_MANUAL].on_change(None)
        while workers:
            workers.pop(0)()
        d["guardar"]()
        assert d["error"].value == ""
        fila = ctrl.db.get("otros_activos", cid, codigo)
        assert fila["valor_manual"] is None and fila["motivo_manual"] is None
        assert fila["manual_override"] == 1  # sigue protegida frente a la ingesta
        assert real.valorar(fila)["valor_cliente"] == 1000.0  # ya no aplica el valor manual
        _, d = abrir(a, workers, codigo)  # protegida pero sin valor manual: interruptor apagado
        assert d["switches"][uoa.USAR_MANUAL].value is False
    finally:
        if hasattr(ctrl, "cerrar"):
            ctrl.cerrar()


def test_revision_extraccion_otros_activos_no_cae_en_gasto(monkeypatch):
    """Regresión: los registros ``otros_activos`` tenían el encabezado «Gasto · … Importe sin indicar»."""
    llamadas = []
    monkeypatch.setattr(uoa, "backend", lambda: llamadas.append("backend"))
    cuenta = main.texto_registro_otro_activo(1, CUENTA)
    assert cuenta.startswith("1. Cuenta bancaria · Banco Ficticio · ES00 0000 · ejercicio 2025")
    assert ui.eur(1000.0) in cuenta and ui.eur(900.0) in cuenta
    cripto = main.texto_registro_otro_activo(2, {k: v for k, v in CRIPTO.items() if k != "valor_informe"})
    assert "Criptoactivo · Exchange Ficticio" in cripto
    assert "0.5 unidades" in cripto and "precio unitario 40000" in cripto
    assert "20000" not in cripto and "Valor sin indicar" not in cripto  # nunca multiplica unidades × precio
    informe = main.texto_registro_otro_activo(3, HIJO)
    assert "Fondo de inversión" in informe and ui.eur(30000.0) in informe
    vacio = main.texto_registro_otro_activo(4, {"tipo": "NUEVO_TIPO"})
    assert vacio == "4. NUEVO_TIPO · ejercicio sin indicar · Valor sin indicar"
    for t in (cuenta, cripto, informe, vacio):
        assert "Gasto" not in t and "Importe sin indicar" not in t
    assert llamadas == []


# ------------------------------------------------------------------------------------ normativa del Patrimonio
CONFIG_CAT = {"normativa": "CATALUNA", "comunidad": "Cataluña", "justificacion": "Inmueble ficticio en Girona",
              "confirmado": True, "version": "v3", "heredada_de": None, "bonificacion_manual": False,
              "motor_general": True, "foral": False}
CONFIG_VACIA = {"normativa": None, "comunidad": None, "justificacion": None, "confirmado": False, "version": "v1",
                "heredada_de": None, "bonificacion_manual": False, "motor_general": False, "foral": False}


def montar_normativa(config=CONFIG_VACIA, filas_714=(P714,), lectura=True, escritura=True):
    """Dobles del contrato del controlador; ``config`` excepción = lectura que falla."""
    a, workers = montar([CUENTA], filas_714)
    if lectura:
        a.ctrl.configuracion_patrimonio = (Mock(return_value=dict(config)) if isinstance(config, dict)
                                           else Mock(side_effect=config))
    if escritura:
        a.ctrl.configurar_patrimonio = Mock(return_value={})
        a.ctrl.usar_bonificacion_automatica = Mock(return_value={})
    return a, workers


def vista_normativa(a, workers):
    vista = uoa.seccion(a)
    while workers:
        workers.pop(0)()
    return vista


def abrir_normativa(a, workers):
    vista = vista_normativa(a, workers)
    boton = next(c for c in recorrer(vista) if isinstance(c, ft.Container)
                 and str(c.tooltip or "").startswith("Normativa del Patrimonio"))
    a._dialogo.reset_mock()
    boton.on_click(None)
    titulo, contenido = a._dialogo.call_args.args[:2]
    return titulo, contenido, a._dialogo.return_value.data


def elegir(d, normativa):
    d["selector"].value = normativa
    d["selector"].on_select(None)


def boton_bonificacion(vista):
    return next((c for c in recorrer(vista) if getattr(c, "key", None) == "nm-bonificacion-auto"), None)


def test_normativa_se_lee_en_worker_y_no_en_el_hilo_de_ui(bk):
    a, workers = montar_normativa(CONFIG_CAT)
    vista = uoa.seccion(a)
    a.ctrl.configuracion_patrimonio.assert_not_called()  # nada leído al construir la vista
    assert "Leyendo la normativa" in textos(vista)
    while workers:
        workers.pop(0)()
    a.ctrl.configuracion_patrimonio.assert_called_once_with(7, 2025)
    t = textos(vista)
    assert "NORMATIVA DEL PATRIMONIO · 2025" in t and "Cataluña" in t and "Elección guardada" in t
    assert "Confirmada" not in t and "Heredada" not in t and "no cambia el 210 ni el 303" in t
    assert "Inmueble ficticio en Girona" in t and "no la del asesor ni la nacionalidad" in t
    assert boton_bonificacion(vista) is None
    assert _expand_bajo_wrap(vista) == []


def test_normativa_obsoleta_o_de_otro_expediente_se_descarta(bk):
    a, workers = montar_normativa(CONFIG_CAT)
    vista = uoa.seccion(a)
    a.cid = 8  # el usuario cambió de cliente mientras se leía
    while workers:
        workers.pop(0)()
    assert "Leyendo la normativa" in textos(vista) and "Cataluña" not in textos(vista)
    a.cid = 7
    vieja = uoa.seccion(a)
    nueva = uoa.seccion(a)  # repintado: la primera tarjeta ya no es la vigente
    while workers:
        workers.pop(0)()
    assert "Leyendo la normativa" in textos(vieja) and "Cataluña" in textos(nueva)


def test_normativa_lectura_fallida_no_inventa_estatal(bk):
    a, workers = montar_normativa(RuntimeError("BD bloqueada"))
    t = textos(vista_normativa(a, workers))
    assert "No se pudo leer la normativa: BD bloqueada" in t
    assert "Estatal" not in t and "Elección guardada" not in t


def test_backend_sin_normativa_no_lanza_worker_ni_rompe(bk):
    a, workers = montar_normativa(lectura=False, escritura=False)
    vista = uoa.seccion(a)
    assert workers == [] and "aún no permite elegir la normativa" in textos(vista)
    a2, w2 = montar_normativa(CONFIG_CAT, escritura=False)  # se lee pero aún no se puede guardar
    vista = vista_normativa(a2, w2)
    assert uoa.SIN_ESCRITURA_NORMATIVA in textos(vista)
    assert not any(str(getattr(c, "tooltip", "") or "").startswith("Normativa del Patrimonio")
                   for c in recorrer(vista))
    uoa.dialogo_normativa(a2, CONFIG_CAT)
    a2.avisar.assert_called_with(uoa.SIN_ESCRITURA_NORMATIVA, True)


def test_opciones_21_del_backend_con_prioridad_y_resto_alfabetico():
    import normativa_patrimonio
    claves = [k for k, _ in uoa.opciones_normativa()]
    assert len(claves) == len(normativa_patrimonio.NOMBRES) == 21 and set(claves) == set(normativa_patrimonio.NOMBRES)
    assert claves[:4] == ["CATALUNA", "VALENCIANA", "MADRID", "ESTATAL"] and claves[-1] == "OTRA"
    resto = [normativa_patrimonio.NOMBRES[k] for k in claves[4:-1]]
    assert resto == sorted(resto, key=uoa._orden_alfabetico)
    assert dict(uoa.opciones_normativa()) == normativa_patrimonio.NOMBRES  # etiquetas del backend, sin copias
    assert uoa.NORMATIVAS == tuple(claves)


def test_estado_de_la_normativa_nunca_verde():
    assert uoa.estado_normativa(CONFIG_CAT) == ("Elección guardada", "guardada")
    assert uoa.estado_normativa({**CONFIG_CAT, "confirmado": False}) == ("Sin confirmar", "naranja")
    for cambio in ({"motor_general": False}, {"motor_general": None}, {"normativa": "PAIS_VASCO", "foral": True},
                   {"normativa": "NAVARRA", "foral": True, "motor_general": True}):
        texto, clave = uoa.estado_normativa({**CONFIG_CAT, **cambio})
        assert clave == "naranja" and "provisional" in texto
    otra = {"normativa": "OTRA", "comunidad": "Aragón", "confirmado": True, "motor_general": False}
    assert uoa.estado_normativa(otra) == ("Elección guardada · pendiente de validación", "naranja")
    assert uoa.estado_normativa(CONFIG_VACIA) == ("Sin elegir", "naranja")
    assert uoa.nombre_normativa({"normativa": "OTRA", "comunidad": "Aragón"}) == "Otra comunidad: Aragón"
    for n in uoa.NORMATIVAS:
        for motor in (True, False):
            assert uoa.estado_normativa({**CONFIG_CAT, "normativa": n, "motor_general": motor})[1] != "verde"
    pastilla = uoa.pastilla_normativa("Elección guardada", "guardada", ui.CLARO)
    assert all(i.color != ui.CLARO.verde for i in recorrer(pastilla) if isinstance(i, ft.Icon))


def test_dialogo_desplegable_con_21_opciones_y_criterio_sin_prometer_cobertura(bk):
    titulo, contenido, d = abrir_normativa(*montar_normativa())
    t = textos(contenido)
    assert titulo == "Normativa del Patrimonio · 2025"
    assert "mayor valor de sus bienes y derechos sujetos en España" in t
    assert "No depende del domicilio del asesor ni de la nacionalidad del cliente" in t
    assert "No cubre todas las comunidades ni todos los ejercicios" in t
    assert "posteriores en borrador" in t and "714 presentados no cambian" in t and "Solo afecta al 714" in t
    assert "mínimo" not in t.lower()  # la UI no calcula ni anuncia mínimos
    assert not any(isinstance(r, ft.Radio) for r in recorrer(contenido))  # 21 radios serían una pared
    assert any(c is d["selector"] for c in recorrer(contenido))
    assert [getattr(o, "key", None) for o in d["selector"].options] == list(uoa.NORMATIVAS)
    assert d["confirmacion"].value is False and d["selector"].value is None


def test_nota_de_cada_opcion_no_promete_calculo_exacto(bk):
    _, _, d = abrir_normativa(*montar_normativa())
    for foral in ("PAIS_VASCO", "NAVARRA"):
        elegir(d, foral)
        assert "foral" in d["nota"].value.lower() and "provisional" in d["nota"].value
    for ciudad in ("CEUTA", "MELILLA"):
        elegir(d, ciudad)
        assert "puntos de conexión" in d["nota"].value and "provisional" in d["nota"].value
    elegir(d, "OTRA")
    assert "provisional" in d["nota"].value
    a2, w2 = montar_normativa({**CONFIG_CAT, "normativa": "NAVARRA", "comunidad": "Comunidad Foral de Navarra",
                               "foral": True})
    t = textos(vista_normativa(a2, w2))
    assert "cálculo provisional" in t and "Régimen foral" in t and "Elección guardada" in t


def test_cancelar_no_escribe(bk):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    elegir(d, "VALENCIANA")
    d["justificacion"].value = "x"
    d["cerrar"]()
    a.ctrl.configurar_patrimonio.assert_not_called()
    a.cerrar_dialogo.assert_called_once()
    d["guardar"]()  # tras cerrar, guardar no hace nada
    a.ctrl.configurar_patrimonio.assert_not_called()


def test_guardar_exige_justificacion_y_confirmacion_y_envia_version(bk):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    d["guardar"]()
    assert d["error"].value == "Elige una normativa de la lista."
    d["selector"].value = "Texto libre escrito en el buscador"
    d["guardar"]()
    assert d["error"].value == "Elige una normativa de la lista."
    elegir(d, "VALENCIANA")
    d["guardar"]()
    assert "justificación" in d["error"].value
    d["justificacion"].value = "  Mayor valor: vivienda ficticia en Alicante  "
    d["guardar"]()
    assert "confirmación del asesor" in d["error"].value
    a.ctrl.configurar_patrimonio.assert_not_called()
    d["confirmacion"].value = True
    d["guardar"]()
    d["guardar"]()  # doble clic: un solo guardado
    a.ctrl.configurar_patrimonio.assert_called_once_with(
        7, 2025, normativa="VALENCIANA", comunidad="Comunitat Valenciana",
        justificacion="Mayor valor: vivienda ficticia en Alicante", confirmado=True, version_esperada="v1")
    assert "recalculando el 714" in a.en_segundo_plano.call_args.args[0]
    assert a.en_segundo_plano.call_args.kwargs["al_fallar"]
    assert d["estado"]["cerrado"] and d["error"].value == ""


def test_madrid_y_resto_envian_el_nombre_del_backend(bk):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    elegir(d, "MADRID")
    d["justificacion"].value, d["confirmacion"].value = "Piso ficticio en Madrid", True
    d["guardar"]()
    kw = a.ctrl.configurar_patrimonio.call_args.kwargs
    assert (kw["normativa"], kw["comunidad"]) == ("MADRID", "Comunidad de Madrid")


def test_otra_comunidad_nombre_libre_obligatorio_y_provisional(bk):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    assert d["otra"].content is not d["comunidad"]  # el campo solo aparece con «Otra»
    elegir(d, "OTRA")
    assert d["otra"].content is d["comunidad"]
    d["justificacion"].value, d["confirmacion"].value = "Finca ficticia", True
    d["guardar"]()
    assert d["error"].value == "Escribe el nombre de la comunidad."
    d["comunidad"].value = " Comunidad ficticia "
    d["guardar"]()
    kw = a.ctrl.configurar_patrimonio.call_args.kwargs
    assert (kw["normativa"], kw["comunidad"]) == ("OTRA", "Comunidad ficticia")
    a2, w2 = montar_normativa({**CONFIG_CAT, "normativa": "OTRA", "comunidad": "Aragón", "motor_general": False})
    t = textos(vista_normativa(a2, w2))
    assert "Otra comunidad: Aragón" in t and "pendiente de validación" in t and uoa.ALCANCE_NORMATIVA in t


def test_estatal_no_envia_comunidad(bk):
    a, workers = montar_normativa(CONFIG_CAT)
    _, _, d = abrir_normativa(a, workers)
    assert d["selector"].value == "CATALUNA" and d["justificacion"].value == "Inmueble ficticio en Girona"
    elegir(d, "ESTATAL")
    d["confirmacion"].value = True
    d["guardar"]()
    kw = a.ctrl.configurar_patrimonio.call_args.kwargs
    assert (kw["normativa"], kw["comunidad"], kw["version_esperada"]) == ("ESTATAL", "", "v3")


def test_sin_cambios_no_escribe_ni_vuelve_a_pedir_confirmacion(bk):
    a, workers = montar_normativa(CONFIG_CAT)
    _, _, d = abrir_normativa(a, workers)
    d["guardar"]()  # sin marcar la casilla: no hay cambio que confirmar
    a.ctrl.configurar_patrimonio.assert_not_called()
    assert d["estado"]["cerrado"] and d["error"].value == ""


def test_confirmar_una_normativa_sin_confirmar_si_escribe(bk):
    a, workers = montar_normativa({**CONFIG_CAT, "confirmado": False})
    t = textos(vista_normativa(a, workers))
    assert "Sin confirmar" in t and "antes de dar por definitiva la cuota" in t
    _, _, d = abrir_normativa(a, workers)
    d["guardar"]()
    assert "confirmación del asesor" in d["error"].value
    d["confirmacion"].value = True
    d["guardar"]()
    assert a.ctrl.configurar_patrimonio.call_args.kwargs["confirmado"] is True


def test_herencia_viva_visible_sin_preguntas_repetidas(bk):
    a, workers = montar_normativa({**CONFIG_CAT, "heredada_de": 2025, "version": "v-2026"})
    a.ejercicio = 2026
    t = textos(vista_normativa(a, workers))
    a.ctrl.configuracion_patrimonio.assert_called_once_with(7, 2026)
    assert "Heredada de 2025" in t and "borradores posteriores sin elección propia" in t
    assert "Elección guardada" in t and "2025 no cambia" in t
    _, contenido, d = abrir_normativa(a, workers)
    assert "Ahora 2026 usa la elección de 2025" in textos(contenido)
    d["guardar"]()  # abrir y guardar sin cambios no crea elección propia ni vuelve a preguntar
    a.ctrl.configurar_patrimonio.assert_not_called()
    assert d["estado"]["cerrado"]


def test_cambiar_un_ano_solo_escribe_ese_ano(bk):
    a, workers = montar_normativa({**CONFIG_CAT, "heredada_de": 2025, "version": "v-2026"})
    a.ejercicio = 2026
    _, _, d = abrir_normativa(a, workers)
    elegir(d, "MADRID")
    d["justificacion"].value, d["confirmacion"].value = "Inmueble ficticio en Madrid", True
    d["guardar"]()
    d["guardar"]()
    a.ctrl.configurar_patrimonio.assert_called_once()
    (cid, ej), kw = a.ctrl.configurar_patrimonio.call_args
    assert (cid, ej, kw["normativa"], kw["version_esperada"]) == (7, 2026, "MADRID", "v-2026")
    a.ctrl.usar_bonificacion_automatica.assert_not_called()


@pytest.mark.parametrize("campo, valor", [("cid", 8), ("ejercicio", 2026)])
def test_normativa_tras_cambiar_de_expediente_no_escribe(bk, campo, valor):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    elegir(d, "CATALUNA")
    d["justificacion"].value, d["confirmacion"].value = "x", True
    setattr(a, campo, valor)
    d["guardar"]()
    a.ctrl.configurar_patrimonio.assert_not_called()
    assert "cliente y ejercicio correctos" in d["error"].value


def test_normativa_con_714_presentado_avisa_sin_bloquear(bk):
    a, workers = montar_normativa(filas_714=({**P714, "estado": "Presentado"},))
    vista = vista_normativa(a, workers)
    assert "Cambiar la normativa recalcula y conserva lo presentado" in textos(vista)
    _, contenido, d = abrir_normativa(a, workers)
    assert "conserva lo presentado" in textos(contenido)
    elegir(d, "ESTATAL")
    d["justificacion"].value, d["confirmacion"].value = "x", True
    d["guardar"]()
    a.ctrl.configurar_patrimonio.assert_called_once()


def test_fallo_al_guardar_normativa_deja_el_formulario_editable(bk):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    a.ctrl.configurar_patrimonio.side_effect = ValueError("La normativa cambió en otra sesión.")

    def ejecutar(texto, trabajo, exito=None, despues=None, al_fallar=None):
        try:
            trabajo()
        except Exception as exc:
            al_fallar(exc)
        return True
    a.en_segundo_plano = Mock(side_effect=ejecutar)
    elegir(d, "CATALUNA")
    d["justificacion"].value, d["confirmacion"].value = "x", True
    d["guardar"]()
    assert not d["estado"]["guardando"] and not d["estado"]["cerrado"]
    assert not d["justificacion"].read_only and not d["selector"].disabled
    assert "cambió en otra sesión" in d["error"].value and "vuelve a abrir" in d["error"].value


def test_normativa_rechazada_por_tarea_en_curso_queda_editable(bk):
    a, workers = montar_normativa()
    _, _, d = abrir_normativa(a, workers)
    a.en_segundo_plano = Mock(return_value=False)
    elegir(d, "CATALUNA")
    d["justificacion"].value, d["confirmacion"].value = "x", True
    d["guardar"]()
    assert not d["estado"]["guardando"] and not d["estado"]["cerrado"] and not d["confirmacion"].disabled
    a.ocupada.return_value = True  # otra tarea en marcha: ni lo intenta
    a.en_segundo_plano.reset_mock()
    d["guardar"]()
    a.en_segundo_plano.assert_not_called()


# ------------------------------------------------------------------------------------ bonificación manual
def test_volver_a_bonificacion_automatica_con_guardas(bk):
    a, workers = montar_normativa({**CONFIG_CAT, "bonificacion_manual": True})
    vista = vista_normativa(a, workers)
    assert "fijada a mano" in textos(vista)
    boton = boton_bonificacion(vista)
    assert boton is not None
    a.ocupada.return_value = True
    boton.on_click(None)
    a.ocupada.return_value = False
    a.cid = 8
    boton.on_click(None)
    a.ctrl.usar_bonificacion_automatica.assert_not_called()
    assert a.avisar.call_args.args[1] is True
    a.cid = 7
    boton.on_click(None)
    boton.on_click(None)  # doble clic: una sola restauración
    a.ctrl.usar_bonificacion_automatica.assert_called_once_with(7, 2025, version_esperada="v3")
    assert "recalculando el 714" in a.en_segundo_plano.call_args.args[0]
    assert a.en_segundo_plano.call_args.kwargs["al_fallar"] and boton.disabled
    a.ctrl.configurar_patrimonio.assert_not_called()


def test_bonificacion_fallo_o_tarea_en_curso_permite_reintentar(bk):
    a, workers = montar_normativa({**CONFIG_CAT, "bonificacion_manual": True})
    boton = boton_bonificacion(vista_normativa(a, workers))
    a.ctrl.usar_bonificacion_automatica.side_effect = ValueError("La configuración cambió")

    def ejecutar(texto, trabajo, exito=None, despues=None, al_fallar=None):
        try:
            trabajo()
        except Exception as exc:
            al_fallar(exc)
            return True
        despues()
        return True
    a.en_segundo_plano = Mock(side_effect=ejecutar)
    boton.on_click(None)
    a.avisar.assert_called_with("La configuración cambió", True)
    boton.on_click(None)
    assert a.ctrl.usar_bonificacion_automatica.call_count == 2 and not boton.disabled
    a.en_segundo_plano = Mock(return_value=False)
    boton.on_click(None)
    boton.on_click(None)
    assert a.en_segundo_plano.call_count == 2  # rechazada: no se queda bloqueada


@pytest.mark.parametrize("config, filas_714", [
    ({**CONFIG_CAT, "bonificacion_manual": True}, ({**P714, "estado": "Presentado"},)),  # conserva su fila
    (CONFIG_CAT, (P714,)),  # bonificación automática: nada que restaurar
])
def test_sin_accion_de_bonificacion_si_presentado_o_automatica(bk, config, filas_714):
    a, workers = montar_normativa(config, filas_714)
    vista = vista_normativa(a, workers)
    assert boton_bonificacion(vista) is None and "fijada a mano" not in textos(vista)
