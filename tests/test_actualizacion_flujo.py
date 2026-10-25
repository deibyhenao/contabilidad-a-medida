"""Regresión de los callbacks reales del botón, con red e instalación simuladas."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater
import ui_estilo as ui
from ui_actualizaciones import AvisoActualizacion
from main import App


def construir(tmp_path):
    app = App.__new__(App)
    app.cerrando = False
    app._update_lock = threading.Lock()
    app._turno = threading.Lock()
    app._update_cancel = threading.Event()
    app._update_ruta = None
    app._update_publicacion = None
    app._evento_dialogo = None
    eventos, trabajos = [], []

    class Ventana:
        async def destroy(self):
            eventos.append('ventana_cerrada')

    app.page = NS(run_thread=trabajos.append, update=lambda: None, window=Ventana())
    app.ctrl = NS(lector=NS(db_path=tmp_path/'contabilidad.db'), cerrar=lambda: eventos.append('bd_cerrada'))
    app.avisar = lambda mensaje: eventos.append(mensaje)
    app.aviso_actualizacion = AvisoActualizacion(ui.CLARO, app.comprobar_actualizacion,
                                               app.actualizar_aplicacion, app.cancelar_actualizacion)
    contenido = b'MZ' + b'\0' * 58 + (64).to_bytes(4, 'little') + b'PE\0\0' + b'instalador de prueba verificado' * 20000
    pub = updater.Publicacion('1.0.2', 'https://github.com/deibyhenao/contabilidad-a-medida/releases/download/v1.0.2/ContabilidadAMedida-1.0.2-windows-x64-Setup.exe',
                             hashlib.sha256(contenido).hexdigest(), len(contenido), 'prueba')
    return app, trabajos, eventos, pub, contenido


def pulsar_primario(app):
    return app.aviso_actualizacion._acciones.controls[-1].on_click(None)


def test_boton_busca_descarga_y_reinicia_en_orden(tmp_path, monkeypatch):
    app, trabajos, eventos, pub, contenido = construir(tmp_path)
    monkeypatch.setattr(updater, 'comprobar', lambda: pub)
    monkeypatch.setattr(updater.app_paths, 'carpeta_actualizaciones', lambda: tmp_path)
    descargar = updater.descargar
    monkeypatch.setattr(updater, 'descargar', lambda p, progreso, cancelar, **kw: descargar(
        p, progreso, cancelar, carpeta=tmp_path, abrir=lambda _: io.BytesIO(contenido)))
    def instalar(ruta, publicacion, **kw):
        assert kw["db_path"] == app.ctrl.lector.db_path
        assert ruta.read_bytes() == contenido and publicacion == pub
        eventos.append('instalador_preparado')
        return NS(poll=lambda: None, terminate=lambda: eventos.append('cancelado'))
    monkeypatch.setattr(updater, 'preparar_instalacion', instalar)
    app.aviso_actualizacion._cambio.content.content.on_click(None)
    assert app.aviso_actualizacion.estado == 'comprobando'
    trabajos.pop(0)()
    assert app.aviso_actualizacion.estado == 'disponible'
    asyncio.run(pulsar_primario(app))
    assert app.aviso_actualizacion.estado == 'descargando'
    trabajos.pop(0)()
    assert app.aviso_actualizacion.estado == 'listo'
    asyncio.run(pulsar_primario(app))
    assert eventos == ['instalador_preparado', 'bd_cerrada', 'ventana_cerrada']
    assert app.cerrando
    assert not app._turno.locked() and not app._update_lock.locked()


def test_boton_reintenta_error_de_red(tmp_path, monkeypatch):
    app, trabajos, eventos, pub, contenido = construir(tmp_path)
    def fallo():
        raise TimeoutError('fallo simulado')
    monkeypatch.setattr(updater, 'comprobar', fallo)
    app.aviso_actualizacion._cambio.content.content.on_click(None)
    trabajos.pop(0)()
    assert app.aviso_actualizacion.estado == 'error'
    assert not app._update_lock.locked()
    monkeypatch.setattr(updater, 'comprobar', lambda: pub)
    pulsar_primario(app)
    trabajos.pop(0)()
    assert app.aviso_actualizacion.estado == 'disponible'
    assert not eventos


def test_boton_cancelar_no_cierra_ni_instala(tmp_path, monkeypatch):
    app, trabajos, eventos, pub, contenido = construir(tmp_path)
    app._update_publicacion = pub
    app.aviso_actualizacion.mostrar('disponible', version=pub.version)
    descargar = updater.descargar
    monkeypatch.setattr(updater, 'descargar', lambda p, progreso, cancelar, **kw: descargar(
        p, progreso, cancelar, carpeta=tmp_path, abrir=lambda _: io.BytesIO(contenido)))
    asyncio.run(pulsar_primario(app))
    app.aviso_actualizacion._acciones.controls[0].on_click(None)
    trabajos.pop(0)()
    assert app.aviso_actualizacion.estado == 'disponible'
    assert not eventos and not list(tmp_path.iterdir())


def test_boton_reiniciar_espera_operaciones_contables(tmp_path, monkeypatch):
    app, trabajos, eventos, pub, contenido = construir(tmp_path)
    app._update_publicacion = pub
    app._update_ruta = tmp_path/'CAM-ficticio.exe'
    app.aviso_actualizacion.mostrar('listo', version=pub.version)
    app._turno.acquire()
    monkeypatch.setattr(updater, 'preparar_instalacion', lambda *args: (_ for _ in ()).throw(AssertionError('No debe instalar')))
    asyncio.run(pulsar_primario(app))
    assert len(eventos) == 1 and 'termine' in eventos[0]
    assert not app.cerrando and app._turno.locked()
    app._turno.release()
