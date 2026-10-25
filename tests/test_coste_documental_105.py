"""Uso real del pipeline con cliente API simulado: dos lecturas, errores y aislamiento."""
import json
from types import SimpleNamespace as NS
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import fixtures_pdf as fx
import ia_extractor as ia
import pdf_processor as pp


def respuesta(tipo="CATASTRO_IBI", confianza=.95):
    return json.dumps({"tipo": tipo, "confianza": confianza, "campos": [], "titulares": [],
                       "gananciales": False, "inquilinos": [], "avisos": [], "registros": []})


@pytest.fixture
def proveedor(tmp_path, monkeypatch):
    pdf = fx._crear(tmp_path / "prueba.pdf", "Documento ficticio de prueba de coste")
    monkeypatch.setattr(ia, "hay_clave", lambda: True)
    def configurar(respuestas):
        pendientes = list(respuestas)
        peticiones = []
        def crear(**kw):
            peticiones.append(kw)
            dato = pendientes.pop(0)
            if isinstance(dato, Exception):
                raise dato
            return NS(model=kw["model"], usage=NS(input_tokens=100, output_tokens=20,
                cache_creation_input_tokens=0, cache_read_input_tokens=0), stop_reason="end_turn",
                content=[NS(type="text", text=dato)])
        monkeypatch.setattr(ia, "_cliente", lambda: NS(messages=NS(create=crear)))
        return pdf, peticiones
    return configurar


@pytest.mark.parametrize("primera,segunda,modelo", [
    (respuesta("DESCONOCIDO", .2), respuesta(), "claude-sonnet-5-5"),
    (respuesta(confianza=.4), respuesta("DESCONOCIDO", .2), "claude-haiku-5-5"),
    ("{json inválido", respuesta(), "claude-sonnet-5-5"),
])
def test_suma_lecturas_aunque_la_primera_sea_invalida_o_se_conserve(proveedor, primera, segunda, modelo):
    pdf, llamadas = proveedor([primera, segunda])
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert len(llamadas) == 2 and ext.coste_usd == pytest.approx(.00042)
    assert not ext.coste_incompleto and ext.modelo_ia == modelo and len(ext.lecturas_ia) == 2
    assert ia._LECTURAS_IA.get() is None


def test_dos_respuestas_invalidas_conservan_consumo_aunque_se_use_lector_local(proveedor):
    pdf, _ = proveedor(["{", "{"])
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert ext.motor == "local" and ext.coste_usd == pytest.approx(.00042)
    assert not ext.coste_incompleto and len(ext.lecturas_ia) == 2


def test_fallo_de_respaldo_sin_uso_marca_coste_incompleto(proveedor):
    pdf, _ = proveedor([respuesta(confianza=.4), RuntimeError("fallo ficticio de transporte")])
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert ext.modelo_ia == "claude-haiku-5-5" and ext.coste_usd == pytest.approx(.00002)
    assert ext.coste_incompleto and len(ext.lecturas_ia) == 2 and ext.lecturas_ia[1]["coste"] is None


def test_consumo_no_se_arrastra_al_documento_siguiente(proveedor, monkeypatch):
    pdf, _ = proveedor([respuesta()])
    primero = pp.extraer(pdf, ia.AjustesIA())
    monkeypatch.setattr(ia, "disponible", lambda _: False)
    segundo = pp.extraer(pdf, ia.AjustesIA())
    assert primero.coste_usd == pytest.approx(.00002)
    assert segundo.coste_usd == 0 and not segundo.coste_incompleto and segundo.lecturas_ia == []


def test_error_que_impide_revision_conserva_uso_sin_ocultar_error(proveedor, monkeypatch):
    pdf, _ = proveedor([respuesta()])
    def fallar(*_):
        raise RuntimeError("fallo de conversión ficticio")
    monkeypatch.setattr(ia, "a_extraccion", fallar)
    with pytest.raises(RuntimeError, match="conversión") as fallo:
        pp.extraer(pdf, ia.AjustesIA())
    assert fallo.value.coste_usd == pytest.approx(.00002)
    assert not fallo.value.coste_incompleto and len(fallo.value.lecturas_ia) == 1
    assert ia._LECTURAS_IA.get() is None


def test_medicion_aislada_entre_hilos():
    barrera = threading.Barrier(2)
    def medir(numero):
        with ia.medir_lecturas_documento() as lecturas:
            ia._registrar_lectura({"coste": numero})
            barrera.wait(timeout=5)
            return lecturas
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(medir, (1, 2))) == [[{"coste": 1}], [{"coste": 2}]]
    assert ia._LECTURAS_IA.get() is None
