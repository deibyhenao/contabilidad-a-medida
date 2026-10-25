"""Recorre el envío real del chat Flet sin servidor, red ni archivos de clientes."""
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import asistente
import ia_extractor
import main


def crear_app(monkeypatch):
    main._cargar_modulos()
    app = main.App.__new__(main.App)
    app.cid, app.ejercicio, app.cerrando = 7, 2026, False
    app._turno = threading.Lock()
    app.informe, app.conversaciones = None, {}
    app.page = SimpleNamespace(run_thread=lambda fn: fn(), update=Mock())
    for nombre in ('avisar', '_pintar_principal', '_bajar_al_final', '_actualizar_chat'):
        setattr(app, nombre, Mock())
    app.ctrl = SimpleNamespace(_lock=threading.RLock(), lector=object(),
        ajustes_ia=lambda: ia_extractor.AjustesIA(activada=True),
        preparar_accion_documental=Mock(return_value={'version': 'v1', 'ruta_origen': 'Soportes_PDF/Antes.pdf', 'ruta_destino': 'Soportes_PDF/Luz.pdf'}),
        ejecutar_accion_documental=Mock(return_value={'documentos_actualizados': True, 'cliente_documentos': 7}))
    monkeypatch.setattr(asistente, 'construir_contexto', lambda *a: ('Contexto ficticio', None))
    def proponer(conv, pregunta, contexto, ajustes, al_texto=None):
        conv.acciones_propuestas = [{'accion': 'renombrar', 'codigo': 'DOC-001', 'nombre': 'Luz.pdf'}]
        conv.contexto, conv.huella = 'PDF antiguo', 'antiguo'
        conv.mensajes = [{'role': 'user', 'content': 'PDF antiguo'}]
        conv.turnos = [asistente.Turno('user', pregunta), asistente.Turno('assistant', 'Propuesta de IA')]
    monkeypatch.setattr(asistente.Conversacion, 'preguntar', proponer)
    return app


def test_chat_propone_confirma_invalida_historial_y_conserva_recibo(monkeypatch):
    app = crear_app(monkeypatch)
    app._enviar_pregunta('Renombra DOC-001 a Luz.pdf')
    app.ctrl.ejecutar_accion_documental.assert_not_called()
    conv = app.conversaciones[7]
    token = re.search(r'/confirmar_documento ([a-f0-9]{24})', conv.turnos[-1].texto)[1]
    assert token not in str(conv.mensajes)  # el token local no viaja al modelo
    app._enviar_pregunta('/confirmar_documento '+token)
    app.ctrl.ejecutar_accion_documental.assert_called_once()
    assert conv.mensajes == [] and conv.contexto == ''
    assert 'PDF y Excel actualizados' in conv.turnos[-1].texto
    solicitud = app._actualizar_chat.call_args.args[0]
    app._aplicar_resultado(solicitud['resultado_correccion'])
    assert app.conversaciones[7] is conv
    assert not app._turno.locked() and not app._chat_ocupado


def test_cambiar_cliente_mientras_claude_propone_no_previsualiza(monkeypatch):
    app = crear_app(monkeypatch)
    original = asistente.Conversacion.preguntar
    def cambiar(conv, *a):
        original(conv, *a)
        app.cid = 8
    monkeypatch.setattr(asistente.Conversacion, 'preguntar', cambiar)
    app._enviar_pregunta('Renombra DOC-001 a Luz.pdf')
    app.ctrl.preparar_accion_documental.assert_not_called()
    app.ctrl.ejecutar_accion_documental.assert_not_called()
    assert 'cambiado' in app.conversaciones[7].ultimo_error
    assert not app._turno.locked()
