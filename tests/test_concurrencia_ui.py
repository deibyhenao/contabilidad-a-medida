"""Workers y notificaciones reales sobre un bucle asyncio independiente de Flet."""
import asyncio
from concurrent.futures import Future
import threading
from types import SimpleNamespace as NS

import pytest

from concurrencia import ejecutar_en_ui
from main import App
import main


def test_worker_notifica_en_bucle_y_no_modifica_ui_desde_su_hilo():
    async def escenario():
        loop = asyncio.get_running_loop()
        principal = threading.get_ident()
        vistos = []
        page = NS(session=NS(connection=NS(loop=loop)),
                  run_task=lambda fn: asyncio.run_coroutine_threadsafe(fn(), loop))
        def worker():
            futuro = ejecutar_en_ui(page, lambda: vistos.append(threading.get_ident()))
            futuro.result(timeout=5)
        await asyncio.to_thread(worker)
        assert vistos == [principal]
        ejecutar_en_ui(page, lambda: vistos.append(threading.get_ident()))
        assert vistos == [principal, principal]
    asyncio.run(escenario())


def app_tarea():
    app = App.__new__(App)
    app.cerrando = False
    app._turno = threading.Lock()
    app.cid, app.ejercicio = 7, 2026
    workers, pendientes, aplicados = [], [], []
    def programar(fn):
        pendientes.append(fn)
        return Future()
    app.page = NS(run_task=programar, run_thread=workers.append)
    app.set_ocupado = lambda activo, *args: aplicados.append(('ocupado', activo))
    app._aplicar_resultado = lambda res: aplicados.append(('resultado', res))
    app._pintar_principal = lambda: aplicados.append(('pintar',))
    app.avisar = lambda *args, **kw: aplicados.append(('aviso', args))
    return app, workers, pendientes, aplicados


def vaciar(pendientes):
    while pendientes:
        asyncio.run(pendientes.pop(0)())


def test_resultado_conserva_turno_hasta_aplicarse_en_ui():
    app, workers, pendientes, aplicados = app_tarea()
    hilos = []
    assert app.en_segundo_plano('Exportando', lambda: hilos.append(threading.get_ident()) or {'ok': True})
    worker = threading.Thread(target=workers.pop())
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert hilos and hilos[0] != threading.get_ident()
    assert not aplicados and app._turno.locked()
    # Una segunda acción no se acepta mientras la UI tiene pendiente aplicar la primera.
    assert not app.en_segundo_plano('Otra', lambda: pytest.fail('Trabajo concurrente'))
    vaciar(pendientes)
    assert ('resultado', {'ok': True}) in aplicados
    assert not app._turno.locked()


def test_cerrar_antes_de_notificacion_no_repinta_y_libera_turno():
    app, workers, pendientes, aplicados = app_tarea()
    assert app.en_segundo_plano('Exportando', lambda: {'ok': True})
    workers.pop()()
    app.cerrando = True
    vaciar(pendientes)
    assert not aplicados and not app._turno.locked()


def test_callback_ui_fallido_libera_turno():
    app, workers, pendientes, _ = app_tarea()
    def fallar(_):
        raise RuntimeError('Vista desmontada')
    app._aplicar_resultado = fallar
    assert app.en_segundo_plano('Exportando', lambda: {'ok': True})
    workers.pop()()
    asyncio.run(pendientes.pop(0)())
    with pytest.raises(RuntimeError, match='desmontada'):
        asyncio.run(pendientes.pop(0)())
    assert not app._turno.locked()


def test_actualizacion_notifica_solo_en_ui_y_descarta_al_cerrar():
    app, _, pendientes, _ = app_tarea()
    estados = []
    app.aviso_actualizacion = NS(mostrar=lambda estado, **kw: estados.append(estado))
    app.page.update = lambda: None
    app._mostrar_actualizacion('descargando', progreso=.5)
    assert estados == []
    vaciar(pendientes)
    assert estados == ['descargando']
    app._mostrar_actualizacion('listo')
    app.cerrando = True
    vaciar(pendientes)
    assert estados == ['descargando']


def test_debounce_300ms_consulta_paginada_en_worker(tmp_path, monkeypatch):
    app = App.__new__(App)
    app.cerrando, app._busqueda_version, app.cartera = False, 0, 'No residente'
    app.ctrl = NS(lector=NS(db_path=tmp_path/'ficticio.db'))
    app.lista_clientes = NS(opacity=1)
    app.page = NS(update=lambda: None)
    app._refrescar_clientes = lambda filas: None
    app.avisar = lambda *a, **kw: pytest.fail('Busqueda fallida')
    demoras, consultas = [], []
    class Lector:
        def __init__(self, ruta, solo_lectura):
            assert solo_lectura
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def listar_clientes(self, **kw):
            consultas.append((threading.get_ident(), kw))
            return []
    async def dormir(segundos):
        demoras.append(segundos)
    monkeypatch.setattr(main.database, 'Database', Lector)
    monkeypatch.setattr(main.asyncio, 'sleep', dormir)
    asyncio.run(app._on_buscar(NS(control=NS(value='  Михаил  '))))
    assert demoras == [.30]
    hilo, parametros = consultas[0]
    assert hilo != threading.get_ident()
    assert parametros == dict(limite=51, busqueda='михаил', cartera='No residente')


def test_refrescar_lista_restaura_opacidad_tras_busqueda_invalidada():
    import ui_estilo as ui
    app = App.__new__(App)
    app.lista_clientes = NS(opacity=.55, controls=[])
    app.p, app.cid, app.cartera, app.filtro = ui.CLARO, None, main.NR, ''
    app._pagina_clientes = 0
    app._refrescar_clientes([])
    assert app.lista_clientes.opacity == 1


def test_arranque_carga_bd_en_worker_y_monta_solo_en_bucle(monkeypatch):
    import flet as ft
    principal = threading.get_ident()
    montados, io, tareas, vistas, actualizaciones = [], [], [], [], []
    page = NS(platform_brightness=ft.Brightness.LIGHT, window=None,
              add=vistas.append, run_task=tareas.append,
              update=lambda: actualizaciones.append(threading.get_ident()))
    class AppFalsa:
        def __init__(self, _page, _ctrl):
            montados.append(threading.get_ident())
            self.cerrando = False
        def iniciar(self, _pila):
            montados.append(threading.get_ident())
    monkeypatch.setattr(main, 'App', AppFalsa)
    monkeypatch.setattr(main, 'Controlador', lambda: io.append(threading.get_ident()) or object())
    monkeypatch.setattr(main, '_cargar_modulos', lambda: io.append(threading.get_ident()))
    monkeypatch.setattr(main, 'preparar_pagina', lambda *args: None)
    monkeypatch.setenv('SPLASH_MIN', '0')
    main.main(page)
    asyncio.run(tareas.pop()())
    assert montados == [principal, principal]
    assert io and all(h != principal for h in io)
    assert actualizaciones and all(h == principal for h in actualizaciones)
    assert vistas[0].controls == []


def test_arranque_no_repinta_splash_si_se_cerro_durante_carga(monkeypatch):
    import flet as ft
    tareas, actualizaciones = [], []
    page = NS(platform_brightness=ft.Brightness.LIGHT, window=None,
              add=lambda _: None, run_task=tareas.append,
              update=lambda: actualizaciones.append('pintado'))
    class AppFalsa:
        def __init__(self, _page, _ctrl):
            self.cerrando = False
        def iniciar(self, _pila):
            self.cerrando = True
    monkeypatch.setattr(main, 'App', AppFalsa)
    monkeypatch.setattr(main, '_cargar_modulos', lambda: None)
    monkeypatch.setattr(main, 'Controlador', lambda: object())
    monkeypatch.setattr(main, 'preparar_pagina', lambda *args: None)
    monkeypatch.setenv('SPLASH_MIN', '0')
    main.main(page)
    asyncio.run(tareas.pop()())
    assert len(actualizaciones) == 2  # entrada y montaje, sin fundido tras el cierre
