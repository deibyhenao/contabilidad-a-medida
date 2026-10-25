"""Regresiones A5: revisión estática de Codex; ejecución pendiente de Claude."""
import asyncio
import datetime as fechas
import inspect
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import database
import main
import ui_estilo as ui


@pytest.fixture
def ctrl(tmp_path):
    c = main.Controlador(tmp_path / "a5.db")
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


def test_a5_destino_existente_no_anida_ni_pisa_archivo(ctrl, monkeypatch):
    cliente = ctrl.crear_cliente("Cliente", "A5", "A5-COLISION")
    carpeta = Path(cliente["carpeta"])
    (carpeta / "manual.txt").write_text("actual", encoding="utf-8")

    class Reloj(fechas.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 17, 12, 0, 0)

    monkeypatch.setattr(fechas, "datetime", Reloj)
    anterior = carpeta.parent / "_Eliminados" / f"{carpeta.name}_20261017_120000"
    anterior.mkdir(parents=True)
    (anterior / "manual.txt").write_text("anterior", encoding="utf-8")
    destino = ctrl.eliminar_cliente(cliente["id"])
    assert destino == anterior.with_name(anterior.name + "_2")
    assert (destino / "manual.txt").read_text(encoding="utf-8") == "actual"
    assert (anterior / "manual.txt").read_text(encoding="utf-8") == "anterior"
    assert not carpeta.exists()


def test_a5_fallo_rename_no_copia_ni_borra_bd(ctrl, monkeypatch):
    cliente = ctrl.crear_cliente("Cliente", "A5", "A5-UNIDAD")
    carpeta = Path(cliente["carpeta"])
    (carpeta / "manual.txt").write_text("conservar", encoding="utf-8")

    def otro_volumen(*_args, **_kwargs):
        raise OSError("traslado entre unidades rechazado")

    monkeypatch.setattr(Path, "rename", otro_volumen)
    with pytest.raises(ValueError, match="misma unidad"):
        ctrl.eliminar_cliente(cliente["id"])
    assert ctrl.db.get_cliente(cliente["id"])
    assert (carpeta / "manual.txt").read_text(encoding="utf-8") == "conservar"
    assert list((carpeta.parent / "_Eliminados").iterdir()) == []


def test_a5_fallo_bd_devuelve_carpeta_y_conserva_config(ctrl):
    cliente = ctrl.crear_cliente("Cliente", "A5", "A5-ROLLBACK")
    cid = cliente["id"]
    carpeta = Path(cliente["carpeta"])
    (carpeta / "manual.txt").write_text("conservar", encoding="utf-8")
    ctrl.db.set_config(f"excel_firma|{cid}", "firma-prueba")
    ctrl.db.con.execute("CREATE TRIGGER impedir_borrado BEFORE DELETE ON clientes "
                        "BEGIN SELECT RAISE(ABORT, 'fallo simulado'); END")
    ctrl.db.con.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fallo simulado"):
        ctrl.eliminar_cliente(cid)
    assert ctrl.db.get_cliente(cid)
    assert ctrl.db.get_config(f"excel_firma|{cid}") == "firma-prueba"
    assert (carpeta / "manual.txt").read_text(encoding="utf-8") == "conservar"
    assert list((carpeta.parent / "_Eliminados").iterdir()) == []


def test_a5_cascada_cubre_todas_las_tablas_y_firma(ctrl):
    cid = ctrl.crear_cliente("Cliente", "A5", "A5-CASCADA")["id"]
    for tabla in database.TABLAS:
        ctrl.db.upsert(tabla, cid, {"codigo": "A5-001"})
    ctrl.db.set_config(f"excel_firma|{cid}", "firma-prueba")
    ctrl.eliminar_cliente(cid)
    assert ctrl.db.get_cliente(cid) is None
    assert ctrl.db.get_config(f"excel_firma|{cid}") is None
    for tabla in database.TABLAS:
        assert ctrl.db.listar(tabla, cid) == [], tabla


def _app_borrado():
    app = object.__new__(main.App)
    cola, borrados, dialogo = [], [], {}
    cliente = {"id": 1, "nombre": "Cliente", "apellidos": "A5"}
    app.p, app.cid, app.ejercicio = ui.CLARO, 1, 2026
    app.cartera = "NR"
    app.cerrando, app._turno = False, threading.Lock()
    app.page = SimpleNamespace(run_thread=cola.append, pop_dialog=lambda: None)
    app.ctrl = SimpleNamespace(
        lector=SimpleNamespace(get_cliente=lambda _cid: cliente, listar_clientes=lambda **_kw: []),
        eliminar_cliente=lambda cid: borrados.append(cid) or Path("_Eliminados/cliente"),
    )
    app.avisar = lambda *_args, **_kwargs: None
    app.set_ocupado = lambda *_args: None
    app._refrescar_clientes = lambda: None
    app._pintar_principal = lambda: None
    app._dialogo = lambda _titulo, _contenido, acciones, **_kw: dialogo.update(acciones=acciones)
    app.confirmar_eliminar_cliente()
    return app, cola, borrados, dialogo["acciones"][-1]


def test_a5_confirmacion_fija_cliente_y_reserva_turno_antes_del_hilo():
    app, cola, borrados, boton = _app_borrado()
    app.cid = 2  # una selección distinta nunca cambia el objetivo confirmado
    boton.on_click(None)
    assert app._turno.locked()
    cola.pop()()
    assert borrados == [1]
    assert not app._turno.locked()


def test_a5_borrado_encolado_no_empieza_tras_cerrar():
    app, cola, borrados, boton = _app_borrado()
    boton.on_click(None)
    app.cerrando = True
    app.set_ocupado = lambda *_args: pytest.fail("actualización tras cerrar")
    cola.pop()()
    assert borrados == []
    assert not app._turno.locked()


def test_a5_fallo_al_programar_borrado_libera_turno():
    app, _cola, borrados, boton = _app_borrado()

    def fallar(_trabajo):
        raise RuntimeError("sesión cerrada")

    app.page.run_thread = fallar
    with pytest.raises(RuntimeError, match="sesión cerrada"):
        boton.on_click(None)
    assert borrados == []
    assert not app._turno.locked()


def test_a5_error_de_carga_sustituye_splash_por_mensaje(monkeypatch):
    vistas, cola = [], []
    page = SimpleNamespace(platform_brightness=main.ft.Brightness.LIGHT, window=None,
                           add=vistas.append, run_task=cola.append, update=lambda: None)
    monkeypatch.setattr(main, "preparar_pagina", lambda *_args: None)
    monkeypatch.setattr(main.time, "sleep", lambda _segundos: None)

    def fallar():
        raise ImportError("detalle interno")

    monkeypatch.setattr(main, "_cargar_modulos", fallar)
    main.main(page)
    asyncio.run(cola.pop()())
    splash = vistas[0].controls[0]
    assert splash.opacity == 1
    assert splash.content.controls[1].value == "No se pudo iniciar la aplicación"
    assert splash.content.controls[-1].value == "Error: ImportError"


def test_a5_pulsacion_larga_restaura_escala_sin_activar_accion():
    clics = []
    c = ui.boton("Prueba", lambda e: clics.append(e), ui.CLARO)
    c.on_tap_down(None)
    assert c.scale == 0.97
    c.on_long_press(None)
    assert c.scale == 1
    assert clics == []


def test_a5_manejador_async_se_espera_y_hover_salida_restaura():
    clics = []

    async def accion(e):
        clics.append(e)

    c = ui.boton("Prueba", accion, ui.CLARO)
    assert inspect.iscoroutinefunction(c.on_click)
    c.on_tap_down(None)
    asyncio.run(c.on_click("evento"))
    assert clics == ["evento"] and c.scale == 1
    c.on_hover(SimpleNamespace(data=False))
    assert c.bgcolor == ui.CLARO.acento and c.scale == 1


def test_a5_error_al_marcar_ocupado_no_retiene_turno():
    app, _cola, _borrados, _boton = _app_borrado()
    cola, trabajos = [], []
    app.page.run_thread = cola.append

    def actualizar(activo, _texto=""):
        if activo:
            raise RuntimeError("fallo al actualizar")

    app.set_ocupado = actualizar
    assert app.en_segundo_plano("Prueba", lambda: trabajos.append(True))
    cola.pop()()
    assert trabajos == [] and not app._turno.locked()
