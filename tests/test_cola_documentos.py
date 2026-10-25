"""Estados de la vista «Leyendo N documentos» (sin Flet ni API)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cola_documentos as cd  # noqa: E402


class Reloj:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_recorrido_completo_de_un_lote():
    reloj = Reloj()
    cola = cd.ColaDocumentos(["escritura.pdf", "cobros.xlsx", "contrato.pdf"], reloj=reloj)
    assert cola.titulo() == "Leyendo 3 documentos" and cola.progreso == 0 and cola.activo
    assert [f.extension for f in cola.filas] == ["PDF", "XLSX", "PDF"]
    cola.leyendo(0)
    assert cola.progreso == pytest.approx(0.5 / 3)
    cola.en_revision(0, 0.0111)
    cola.terminar(0, cd.ARCHIVADO, "DOC-003")
    cola.leyendo(1); cola.en_revision(1, 0.0013); cola.en_revision(1, 0.0223)   # Haiku + relectura Sonnet
    cola.terminar(1, cd.DESCARTADO)
    reloj.t = 141.0
    cola.leyendo(2); cola.terminar(2, cd.ERROR, "PDF dañado")
    assert not cola.activo and cola.progreso == 1 and cola.tiempo() == "0:41"
    reloj.t = 500.0
    assert cola.tiempo() == "0:41"                                   # el reloj se para al terminar
    assert cola.coste_usd == pytest.approx(0.0347)
    assert cola.titulo() == "1 de 3 archivados"
    assert cola.resumen() == "1 archivado · 1 descartado · 1 con error"


def test_lote_detenido_deja_lo_pendiente_sin_procesar():
    cola = cd.ColaDocumentos(["a.pdf", "b.pdf", "c.pdf"], reloj=Reloj())
    cola.leyendo(0); cola.terminar(0, cd.DUPLICADO, "DOC-001"); cola.leyendo(1)
    cola.detener()
    assert [f.estado for f in cola.filas] == [cd.DUPLICADO, cd.EN_COLA, cd.EN_COLA]
    assert cola.resumen() == "1 ya estaba · 2 sin procesar" and cola.coste_usd is None


def test_estado_final_invalido_y_singular():
    cola = cd.ColaDocumentos(["solo.csv"], reloj=Reloj())
    assert cola.titulo() == "Leyendo 1 documento"
    with pytest.raises(ValueError):
        cola.terminar(0, cd.LEYENDO)


def _app_vista(cola):
    from types import SimpleNamespace as NS
    import main
    import ui_estilo as ui
    a = main.App.__new__(main.App)
    aj = NS(modelo="claude-haiku-5-5")
    a.ctrl, a.p, a._cola = NS(estado_ia=lambda: (True, ""), ajustes_ia=lambda: aj), ui.CLARO, cola
    return a


def _textos(control, salida=None):
    salida = [] if salida is None else salida
    if getattr(control, "value", None) and isinstance(control.value, str):
        salida.append(control.value)
    for hijo in ("content", "controls"):
        valor = getattr(control, hijo, None)
        for c in (valor if isinstance(valor, list) else [valor] if valor is not None else []):
            _textos(c, salida)
    return salida


def test_tarjeta_del_lote_en_curso_y_terminada():
    import ui_estilo as ui
    cola = cd.ColaDocumentos(["escritura.pdf", "cobros.xlsx"], reloj=Reloj())
    a = _app_vista(cola)
    cola.leyendo(0)
    textos = _textos(a._vista_cola())
    assert "Leyendo 2 documentos" in textos and "Claude Haiku" in textos and "Leyendo…" in textos
    cola.en_revision(0, 0.0111); cola.terminar(0, cd.ARCHIVADO, "DOC-003"); cola.terminar(1, cd.ERROR, "dañado")
    vista = a._vista_cola()
    textos = _textos(vista)
    assert "1 de 2 archivados" in textos and "DOC-003" in textos and "Error" in textos
    assert any("Coste IA ≈ 0,0111 $" in t for t in textos)
    barra = vista.content.controls[1]
    assert barra.value == 1 and barra.color == ui.CLARO.naranja         # fallo parcial: naranja, no rojo


def test_coste_parcial_y_modelos_realmente_usados():
    cola = cd.ColaDocumentos(["a.pdf", "b.xlsx"], reloj=Reloj())
    a = _app_vista(cola)
    assert "Claude Haiku" in _textos(a._vista_cola())                  # antes de leer: el configurado
    cola.en_revision(0, 0.0013, False, ["claude-haiku-5-5", "claude-sonnet-5-5"])
    cola.en_revision(1, None, True, ["claude-haiku-5-5"])
    textos = _textos(a._vista_cola())
    assert "Claude Haiku + Sonnet" in textos
    assert any(t.startswith("Coste IA ≥ 0,0013 $") and t.endswith("(parcial)") for t in textos)


def test_segundo_lote_rechazado_no_borra_la_cola_activa():
    import threading
    from types import SimpleNamespace as NS
    activa = cd.ColaDocumentos(["a.pdf"], reloj=Reloj())
    a = _app_vista(activa)
    avisos = []
    a.cerrando, a._turno, a.cid, a.ejercicio = False, threading.Lock(), 1, 2025
    a.ctrl.procesar_pdf = lambda cid, ruta, ejercicio, confirmar=None, comentario_guia="": None
    a._turno.acquire()
    a.avisar = lambda texto, error=False: avisos.append(texto)
    a.procesar_pdfs([Path("otro.pdf")])
    assert a._cola is activa and avisos == ["Espera a que termine la tarea en curso."]
