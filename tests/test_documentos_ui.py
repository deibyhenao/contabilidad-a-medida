"""Construcción y acciones reales de Flet sin servidor, expedientes ni llamadas de IA."""
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import flet as ft
import pytest

import carteras
import main
import pdf_processor as pdf
import ui_estilo as ui


def recorrer(control):
    yield control
    for hijo in getattr(control, "controls", []) or []:
        yield from recorrer(hijo)
    for hijo in getattr(control, "items", []) or []:
        yield from recorrer(hijo)
    contenido = getattr(control, "content", None)
    if isinstance(contenido, ft.Control):
        yield from recorrer(contenido)


def buscar(control, etiqueta):
    return next(c for c in recorrer(control) if getattr(c, "label", None) == etiqueta)


@pytest.fixture
def app():
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio, a.cartera = ui.CLARO, 7, 2026, carteras.NR
    a.page = SimpleNamespace(update=Mock(), pop_dialog=Mock())
    a.ctrl = SimpleNamespace(lector=SimpleNamespace(listar=Mock(return_value=[]),
        get_cliente=Mock(return_value={"nombre": "Cliente", "apellidos": "Ficticio", "nif": "TEST7"})))
    a.avisar, a.ocupada, a._pintar_principal = Mock(), Mock(return_value=False), Mock()
    a.cerrando, a.conversaciones = False, {}
    a._dialogo = Mock()
    a._paginas_detalle = {}
    a._bloque = lambda titulo, filas, vacio="": ft.Column([ft.Text(titulo), *filas, *([] if filas else [ft.Text(vacio)])])

    def ejecutar(_texto, trabajo, *_args, **_kwargs):
        trabajo()
        return True
    a.en_segundo_plano = Mock(side_effect=ejecutar)
    return a


@pytest.mark.parametrize("paleta", [ui.CLARO, ui.OSCURO])
def test_accion_nativa_teclado_y_area_44(paleta):
    accion = ui.accion("Retirar a papelera", lambda e: None, paleta, main.I.DELETE_OUTLINE)
    assert isinstance(accion, ft.TextButton)
    assert accion.height >= 44 and accion.tooltip
    assert accion.style.color == paleta.texto2
    assert ft.ControlState.FOCUSED in accion.style.side


@pytest.mark.parametrize("cartera,tipos", [(carteras.NR, {"No residente"}),
                                           (carteras.RES, {"Autónomo", "Pyme"})])
def test_alta_ofrece_solo_tipos_de_cartera(app, cartera, tipos):
    app.cartera = cartera
    app.dialogo_nuevo_cliente()
    controles = app._dialogo.call_args.args[1]
    selector = buscar(controles, "Tipo de cliente")
    assert {op.key for op in selector.options} == tipos
    assert controles.scroll == ft.ScrollMode.AUTO


def test_alta_rechaza_tipo_inyectado_de_otra_cartera(app):
    app.ctrl.crear_cliente = Mock()
    app.dialogo_nuevo_cliente()
    controles, acciones = app._dialogo.call_args.args[1:3]
    buscar(controles, "Tipo de cliente").value = "Pyme"
    acciones[-1].on_click(None)
    app.ctrl.crear_cliente.assert_not_called()


@pytest.mark.parametrize("papelera,permitidas,ausentes", [
    (False, {"Abrir", "Corregir ejercicio", "Sustituir", "Retirar a papelera"}, {"Restaurar"}),
    (True, {"Restaurar"}, {"Abrir", "Corregir ejercicio", "Sustituir", "Retirar a papelera"}),
])
def test_documentos_separa_activos_y_papelera(app, papelera, permitidas, ausentes):
    app._documentos_papelera = {7: papelera}
    app.ctrl.listar_documentos = Mock(return_value=[{
        "codigo": "DOC-001", "tipo": "Certificado", "ejercicio": 2025, "periodo": "Anual",
        "fecha_documento": "2025-12-31", "ruta": "Soportes_PDF/prueba.pdf", "estado": "Disponible"}])
    vista = app._sec_documentos()
    botones = {c.content: c for c in recorrer(vista) if isinstance(c, (ft.TextButton, ft.PopupMenuItem))}
    assert permitidas <= botones.keys() and not ausentes & botones.keys()
    assert all(botones[t].height >= 44 for t in permitidas)
    app.ctrl.listar_documentos.assert_called_once_with(7, papelera=papelera, limite=51, desplazamiento=0)
    textos = " ".join(str(c.value) for c in recorrer(vista) if isinstance(c, ft.Text))
    assert "2025" in textos and "Anual" in textos and "2025-12-31" in textos
    if not papelera:
        assert inspect.iscoroutinefunction(botones["Sustituir"].on_click)


def test_papelera_no_se_hereda_a_otro_cliente(app):
    app._ver_documentos(True)
    assert app._documentos_papelera == {7: True}
    app.cid = 8
    app.ctrl.listar_documentos = Mock(return_value=[])
    app._sec_documentos()
    assert app.ctrl.listar_documentos.call_args.kwargs["papelera"] is False


def test_retira_solo_despues_confirmar_y_no_doble_click(app):
    app.ctrl.retirar_documento = Mock(return_value={})
    app._confirmar_documento(7, "DOC-001", "retirar", 2026, {"confirmable": True, "version": "vista-1"})
    acciones = app._dialogo.call_args.args[2]
    app.ctrl.retirar_documento.assert_not_called()
    acciones[-1].on_click(None)
    acciones[-1].on_click(None)
    app.ctrl.retirar_documento.assert_called_once_with(7, "DOC-001", 2026, version_esperada="vista-1")
    assert acciones[-1].style.color == app.p.rojo


@pytest.mark.parametrize("cambio", ["cliente", "ejercicio"])
def test_confirmar_cambio_contexto_no_modifica(app, cambio):
    app.ctrl.retirar_documento = Mock()
    app._confirmar_documento(7, "DOC-001", "retirar", 2026, {"confirmable": True})
    if cambio == "cliente":
        app.cid = 8
    else:
        app.ejercicio = 2025
    app._dialogo.call_args.args[2][-1].on_click(None)
    app.ctrl.retirar_documento.assert_not_called()
    app.avisar.assert_called()


def test_cancelar_y_previsualizacion_bloqueada_no_modifican(app):
    app.ctrl.retirar_documento = Mock()
    app._confirmar_documento(7, "DOC-001", "retirar", 2026,
                            {"confirmable": False, "advertencias": ["Declaración presentada; revisión necesaria."]})
    contenido, acciones = app._dialogo.call_args.args[1:3]
    assert len(acciones) == 1 and acciones[0].content == "Cerrar"
    assert contenido.scroll == ft.ScrollMode.AUTO
    acciones[0].on_click(None)
    app.ctrl.retirar_documento.assert_not_called()


def test_preview_sustitucion_usa_efectos_retirada_y_destino_fijo(app, tmp_path):
    app.ctrl.previsualizar_documento = Mock(return_value={"confirmable": True})
    app._previsualizar_documento(7, "DOC-001", "sustituir", 2026, ruta_pdf=tmp_path / "nuevo.pdf")
    app.ctrl.previsualizar_documento.assert_called_once_with(7, "DOC-001", "retirar", ejercicio_destino=None)
    assert app._dialogo.call_args.args[0] == "Sustituir documento"
    assert "nuevo.pdf" in " ".join(str(c.value) for c in recorrer(app._dialogo.call_args.args[1]) if isinstance(c, ft.Text))


def test_revisar_extraccion_certificado_anio_distinto_exige_confirmacion(app):
    ext = pdf.Extraccion(tipo=pdf.TipoDocumento.CERTIFICADO_RESIDENCIA, confianza=.9,
                         nombre_archivo="certificado.pdf", campos={"ejercicio": 2025, "pais": "Francia"})

    def responder(_titulo, contenido, acciones, **_kwargs):
        guardar = acciones[-1]
        guardar.on_click(None)
        assert app.page.pop_dialog.call_count == 0
        casilla = next(c for c in recorrer(contenido) if isinstance(c, ft.Checkbox))
        assert casilla.visible
        casilla.value = True
        guardar.on_click(None)
        guardar.on_click(None)
    app._dialogo.side_effect = responder
    resultado = app.revisar_extraccion(ext, 7, 2026)
    assert resultado.campos["ejercicio"] == 2025
    assert app.page.pop_dialog.call_count == 1


def test_revisar_escritura_cambia_ejercicio_sin_redatar_compra(app):
    ext = pdf.Extraccion(tipo=pdf.TipoDocumento.ESCRITURA_COMPRA, confianza=.9,
                         nombre_archivo="escritura.pdf", campos={"fecha_adquisicion": "2025-03-02"})

    def responder(_titulo, contenido, acciones, **_kwargs):
        assert buscar(contenido, "Ejercicio de registro").value == "2026"
        acciones[-1].on_click(None)
        assert app._evento_dialogo.is_set(), [c.value for c in recorrer(contenido) if isinstance(c, ft.Text)]
    app._dialogo.side_effect = responder
    resultado = app.revisar_extraccion(ext, 7, 2026)
    assert resultado.campos["ejercicio"] == 2026
    assert str(resultado.campos["fecha_adquisicion"]) == "2025-03-02"


def test_revisar_extraccion_descartar_no_modifica(app):
    ext = pdf.Extraccion(tipo=pdf.TipoDocumento.DESCONOCIDO, confianza=.1,
                         nombre_archivo="equivocado.pdf", campos={"ejercicio": 2025})
    app._dialogo.side_effect = lambda _t, _c, acciones, **_kw: acciones[0].on_click(None)
    with pytest.raises(main.CanceladoPorUsuario):
        app.revisar_extraccion(ext, 7, 2026)
    assert ext.campos == {"ejercicio": 2025}


def test_cierre_externo_revision_desbloquea_hilo_sin_guardar(app):
    ext = pdf.Extraccion(tipo=pdf.TipoDocumento.DESCONOCIDO, confianza=.1,
                         nombre_archivo="equivocado.pdf", campos={})
    class Cerrado:
        @property
        def on_dismiss(self):
            return None
        @on_dismiss.setter
        def on_dismiss(self, callback):
            callback(None)
    app._dialogo.return_value = Cerrado()
    with pytest.raises(main.CanceladoPorUsuario):
        app.revisar_extraccion(ext, 7, 2026)
    app.page.pop_dialog.assert_not_called()


def test_factura_historica_muestra_fecha_y_no_permite_ejercicio_contradictorio(app):
    ext = pdf.Extraccion(tipo=pdf.TipoDocumento.FACTURA_GASTO, confianza=.9,
                         nombre_archivo="factura.pdf", campos={"fecha": "2025-02-10", "base": 100})
    def responder(_titulo, contenido, acciones, **_kwargs):
        ejercicio = buscar(contenido, "Ejercicio de registro")
        assert ejercicio.value == "2025"
        ejercicio.value = "2026"
        acciones[-1].on_click(None)
        assert not app._evento_dialogo.is_set()
        assert "no coincide" in ejercicio.error
        acciones[0].on_click(None)
    app._dialogo.side_effect = responder
    with pytest.raises(main.CanceladoPorUsuario):
        app.revisar_extraccion(ext, 7, 2026)


def test_revisar_pdf_cambio_cliente_no_guarda_y_cancelacion_es_idempotente(app):
    ext = pdf.Extraccion(tipo=pdf.TipoDocumento.DESCONOCIDO, confianza=.1,
                         nombre_archivo="equivocado.pdf", campos={})

    def responder(_titulo, contenido, acciones, **_kwargs):
        textos = " ".join(str(c.value) for c in recorrer(contenido) if isinstance(c, ft.Text))
        assert "Cliente Ficticio" in textos and "TEST7" in textos and "2026" in textos
        app.cid = 8
        acciones[-1].on_click(None)
        assert not app._evento_dialogo.is_set()
        acciones[0].on_click(None)
        acciones[0].on_click(None)
    app._dialogo.side_effect = responder
    with pytest.raises(main.CanceladoPorUsuario):
        app.revisar_extraccion(ext, 7, 2026)
    assert app.page.pop_dialog.call_count == 1


@pytest.mark.parametrize("sustituto", [False, True])
def test_picker_cambio_cliente_no_procesa_documento(app, sustituto):
    async def seleccionar(**_kw):
        app.cid = 8
        return [SimpleNamespace(path="C:/ficticio/nuevo.pdf")]
    app.file_picker = SimpleNamespace(pick_files=seleccionar)
    app.procesar_pdfs, app.dialogo_documento = Mock(), Mock()
    # El selector simulado no suspende. Ejecutar el manejador sin abrir sockets de asyncio en Windows.
    tarea = app.elegir_sustituto("DOC-001") if sustituto else app.elegir_pdfs()
    with pytest.raises(StopIteration):
        tarea.send(None)
    app.procesar_pdfs.assert_not_called()
    app.dialogo_documento.assert_not_called()
    app.avisar.assert_called()


def test_resultado_documental_invalida_solo_chat_cliente_afectado(app):
    app.conversaciones = {7: object(), 8: object()}
    app.correcciones_chat = SimpleNamespace(descartar=Mock())
    app._aplicar_resultado({"documentos_actualizados": True, "cliente_documentos": 7})
    assert 7 not in app.conversaciones and 8 in app.conversaciones
    app.correcciones_chat.descartar.assert_called_once_with(7)



def test_conflicto_excel_muestra_dialogo_legible_y_libera_tarea(app):
    import threading
    app._turno = threading.Lock()
    app.set_ocupado = Mock()
    app.page.run_thread = lambda tarea: tarea()
    def trabajo():
        raise main.excel_generator.ExcelConflictoImportacion('Revisa 01_RESIDENCIA_FISCAL!B2')
    assert main.App.en_segundo_plano(app, 'Sincronizando', trabajo)
    assert not app._turno.locked()
    assert app._dialogo.call_args.args[0] == 'Revisar cambios del Excel'
    assert app._dialogo.call_args.args[1].value == 'Revisa 01_RESIDENCIA_FISCAL!B2'
    assert app._dialogo.call_args.args[1].selectable
    assert app._dialogo.call_args.args[1].color == app.p.texto
    assert isinstance(app._dialogo.call_args.args[2][0], ft.TextButton)
    assert app._dialogo.call_args.args[2][0].autofocus
    app.avisar.assert_not_called()
