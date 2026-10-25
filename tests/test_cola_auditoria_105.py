"""Regresiones del lote con callbacks UI y proveedor ficticios, sin API real."""
from pathlib import Path
from types import SimpleNamespace as NS
import threading

import pytest

import cola_documentos as cd
import main
import ui_estilo as ui


def app(monkeypatch, proceso):
    a = main.App.__new__(main.App)
    a.cid, a.ejercicio, a.cerrando, a.p = 1, 2025, False, ui.CLARO
    a._turno = threading.Lock()
    a.page = NS(update=lambda: None)
    a.ctrl = NS(procesar_pdf=proceso, estado_ia=lambda: (True, ""),
                ajustes_ia=lambda: NS(modelo="claude-haiku-5-5"))
    a.avisar = lambda *args, **kw: None
    a.set_ocupado = lambda *args: None
    a._pintar_cola = lambda: None
    a._reloj_cola = lambda _: None
    a.en_segundo_plano = lambda texto, trabajo: trabajo() or True
    monkeypatch.setattr(main, "ejecutar_en_ui", lambda page, fn: fn())
    return a


@pytest.mark.parametrize("revisado", [False, True])
def test_coste_del_fallo_se_conserva_sin_sumarlo_dos_veces(monkeypatch, revisado):
    uso = dict(coste_usd=.0123, coste_incompleto=True,
               lecturas_ia=[{"modelo": "claude-haiku-5-5"}])
    def proceso(cid, ruta, ejercicio, confirmar=None):
        if revisado:
            confirmar(NS(**uso))
        error = RuntimeError("Fallo ficticio tras consumo")
        for k, v in uso.items():
            setattr(error, k, v)
        raise error
    a = app(monkeypatch, proceso)
    a.revisar_extraccion = lambda ext, *args: ext
    a.procesar_pdfs([Path("ficticio.pdf")])
    assert not a._cola.activo and a._cola.filas[0].estado == cd.ERROR
    assert a._cola.coste_usd == pytest.approx(.0123)
    assert a._cola.coste_incompleto


@pytest.mark.parametrize("cambio", ["cid", "ejercicio", "cerrando", "conflicto"])
def test_lote_no_lee_el_siguiente_documento_si_contexto_se_invalida(monkeypatch, cambio):
    leidos = []
    def proceso(cid, ruta, ejercicio, confirmar=None):
        leidos.append(ruta)
        if cambio == "conflicto":
            raise main.excel_generator.ExcelConflictoImportacion("Conflicto ficticio")
        setattr(a, cambio, True if cambio == "cerrando" else 2026)
        return {"resultado": NS(documento="DOC-001", duplicado=False)}
    a = app(monkeypatch, proceso)
    if cambio == "conflicto":
        with pytest.raises(main.excel_generator.ExcelConflictoImportacion):
            a.procesar_pdfs([Path("uno.pdf"), Path("dos.pdf")])
    else:
        a.procesar_pdfs([Path("uno.pdf"), Path("dos.pdf")])
    assert leidos == [Path("uno.pdf")]
    assert a._cola.filas[1].estado == cd.EN_COLA


def test_lista_vacia_no_crea_tarjeta_ni_hilo(monkeypatch):
    a = app(monkeypatch, lambda *args: pytest.fail("No hay documento"))
    anterior = a._cola
    a.procesar_pdfs([])
    assert a._cola is anterior


def test_modelo_terminado_local_no_afirma_que_haiku_leyo(monkeypatch):
    a = app(monkeypatch, lambda *args: None)
    a._cola = cd.ColaDocumentos(["local.pdf"])
    a._cola.terminar(0, cd.ARCHIVADO)
    assert a._modelo_cola() == "Lector local"
