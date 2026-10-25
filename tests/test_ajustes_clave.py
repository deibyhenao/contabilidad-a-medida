"""Persistencia y gestión de claves ficticias: sin credenciales reales ni red."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import flet as ft
import ia_extractor as ia
import ui_estilo as ui
from test_documentos_ui import app, recorrer, buscar

OBTENER_REAL = ia.obtener_clave
CLAVE_A = 'clave-ficticia-sin-acceso-AAAA'
CLAVE_B = 'clave-ficticia-sin-acceso-BBBB'


@pytest.fixture
def almacen(monkeypatch):
    valores={}
    kr=SimpleNamespace(
        set_password=lambda servicio,usuario,valor:valores.update({(servicio,usuario):valor}),
        get_password=lambda servicio,usuario:valores.get((servicio,usuario)),
        delete_password=lambda servicio,usuario:valores.pop((servicio,usuario),None))
    monkeypatch.setattr(ia,'_keyring',lambda:kr)
    monkeypatch.setattr(ia,'obtener_clave',OBTENER_REAL)
    return kr


def configurar(app):
    app.ctrl.ajustes_ia=Mock(return_value=ia.AjustesIA())
    def guardar(ajustes,clave):
        if clave:
            ia.guardar_clave(clave)
    app.ctrl.guardar_ajustes_ia=Mock(side_effect=guardar)
    app.ctrl.borrar_clave_ia=Mock(side_effect=ia.borrar_clave)
    app.dialogo_ajustes()
    return app._dialogo.call_args.args[1:3]


def accion(contenido,nombre):
    return next(c for c in recorrer(contenido) if isinstance(c,ft.TextButton) and c.content==nombre)


@pytest.mark.parametrize('paleta',[ui.CLARO,ui.OSCURO])
def test_guardar_reabrir_cambiar_y_conservar_sin_reeescribir(app,almacen,paleta):
    app.p=paleta
    contenido,acciones=configurar(app)
    assert not any(c.value=='Clave guardada' for c in recorrer(contenido) if isinstance(c,ft.Text))
    buscar(contenido,'Clave API de Anthropic').value=CLAVE_A
    acciones[-1].on_click(None)
    assert ia.obtener_clave()==CLAVE_A
    contenido,acciones=configurar(app)
    assert buscar(contenido,'Clave guardada').value=='•••• AAAA'
    assert not buscar(contenido,'Clave API de Anthropic').visible
    assert CLAVE_A not in [getattr(c,'value',None) for c in recorrer(contenido)]
    acciones[-1].on_click(None)  # Cambiar otros ajustes no borra ni reescribe la clave.
    app.ctrl.guardar_ajustes_ia.assert_called_once()
    assert app.ctrl.guardar_ajustes_ia.call_args.args[1] is None
    assert ia.obtener_clave()==CLAVE_A
    contenido,acciones=configurar(app)
    accion(contenido,'Cambiar clave').on_click(None)
    buscar(contenido,'Clave API de Anthropic').value=CLAVE_B
    acciones[-1].on_click(None)
    contenido,_=configurar(app)
    assert buscar(contenido,'Clave guardada').value=='•••• BBBB'
    assert ia.obtener_clave()==CLAVE_B
    assert contenido.scroll==ft.ScrollMode.AUTO


def test_mostrar_ocultar_y_cancelar_limpia_secreto(app,almacen):
    ia.guardar_clave(CLAVE_A)
    contenido,acciones=configurar(app)
    boton=accion(contenido,'Mostrar clave')
    boton.on_click(None)
    assert buscar(contenido,'Clave guardada').value==CLAVE_A
    boton.on_click(None)
    assert buscar(contenido,'Clave guardada').value=='•••• AAAA'
    boton.on_click(None)
    accion(contenido,'Cambiar clave').on_click(None)
    buscar(contenido,'Clave API de Anthropic').value=CLAVE_B
    acciones[-2].on_click(None)
    assert buscar(contenido,'Clave API de Anthropic').value==''
    assert buscar(contenido,'Clave guardada').value=='•••• AAAA'
    assert ia.obtener_clave()==CLAVE_A


def test_eliminar_y_reabrir_sin_clave(app,almacen):
    ia.guardar_clave(CLAVE_A)
    _,acciones=configurar(app)
    acciones[0].on_click(None)
    assert ia.obtener_clave()==CLAVE_A
    accion(app._dialogo.call_args.args[1],'Confirmar eliminación').on_click(None)
    assert ia.obtener_clave() is None
    contenido,_=configurar(app)
    assert buscar(contenido,'Clave API de Anthropic').visible
    assert any(c.value=='Sin clave configurada' for c in recorrer(contenido) if isinstance(c,ft.Text))


def test_error_guardado_no_confirma_exito(app,almacen,monkeypatch):
    almacen.set_password=lambda *a:None
    contenido,acciones=configurar(app)
    buscar(contenido,'Clave API de Anthropic').value=CLAVE_A
    acciones[-1].on_click(None)
    app.page.pop_dialog.assert_not_called()
    assert any('No se pudo comprobar' in str(c.value) for c in recorrer(contenido) if isinstance(c,ft.Text))
    assert ia.obtener_clave() is None


def test_error_eliminar_no_confirma_exito(app,almacen):
    ia.guardar_clave(CLAVE_A)
    almacen.delete_password=lambda *a:None
    contenido,acciones=configurar(app)
    acciones[0].on_click(None)
    accion(contenido,'Confirmar eliminación').on_click(None)
    app.page.pop_dialog.assert_not_called()
    assert ia.obtener_clave()==CLAVE_A
    assert any('No se pudo eliminar' in str(c.value) for c in recorrer(contenido) if isinstance(c,ft.Text))


def test_clave_entorno_identificada_no_ofrece_cambio_o_borrado(app,almacen,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY',CLAVE_B)
    contenido,acciones=configurar(app)
    textos=[c.content for c in recorrer(contenido) if isinstance(c,ft.TextButton)]
    assert 'Cambiar clave' not in textos
    assert all(c.content!='Eliminar clave' for c in acciones)
    assert buscar(contenido,'Clave guardada').value=='•••• BBBB'
    with pytest.raises(ia.ErrorIA,match='variables de entorno'):
        ia.borrar_clave()


def test_entorno_vacio_no_oculta_almacen(almacen,monkeypatch):
    ia.guardar_clave(CLAVE_A)
    monkeypatch.setenv('ANTHROPIC_API_KEY','   ')
    assert ia.estado_clave()['origen']=='Windows'
    assert ia.obtener_clave()==CLAVE_A


def test_errores_backend_no_exponen_clave(almacen):
    def fallar(*a):raise RuntimeError(CLAVE_A)
    almacen.set_password=fallar
    with pytest.raises(ia.ErrorIA) as error:
        ia.guardar_clave(CLAVE_A)
    assert CLAVE_A not in str(error.value)


def test_error_generico_no_expone_clave_y_oculta_anterior(app,almacen):
    ia.guardar_clave(CLAVE_A)
    contenido,acciones=configurar(app)
    accion(contenido,'Mostrar clave').on_click(None)
    app.ctrl.guardar_ajustes_ia.side_effect=RuntimeError(CLAVE_A)
    acciones[-1].on_click(None)
    assert buscar(contenido,'Clave guardada').value=='•••• AAAA'
    assert CLAVE_A not in [getattr(c,'value',None) for c in recorrer(contenido)]


def test_cancelar_eliminacion_conserva_clave(app,almacen):
    ia.guardar_clave(CLAVE_A)
    contenido,acciones=configurar(app)
    acciones[0].on_click(None)
    accion(contenido,'Conservar clave').on_click(None)
    assert ia.obtener_clave()==CLAVE_A
    app.page.pop_dialog.assert_not_called()
