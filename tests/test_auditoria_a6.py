"""Regresiones A6: respuestas externas defectuosas y coherencia de importes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import fixtures_pdf as f
import ia_extractor as ia
import main
import pdf_processor as pp


def respuesta(**cambios):
    datos = dict(tipo="ESCRITURA_COMPRA", confianza=0.9, campos=[], titulares=[],
                 gananciales=False, inquilinos=[], avisos=[])
    datos.update(cambios)
    return datos


def simular(monkeypatch, datos, stop="end_turn"):
    def crear(**_kw):
        return SimpleNamespace(stop_reason=stop,
                               content=[SimpleNamespace(type="text", text=json.dumps(datos))],
                               usage=None, model=ia.MODELO_DEFECTO)
    monkeypatch.setattr(ia, "obtener_clave", lambda: "clave-simulada")
    monkeypatch.setattr(ia, "_cliente", lambda: SimpleNamespace(messages=SimpleNamespace(create=crear)))


@pytest.mark.parametrize("datos", [
    [], None, respuesta(campos=[None]), respuesta(confianza="incorrecta"),
    respuesta(inquilinos=[None]), respuesta(avisos="AVISO_PRIVADO"),
    respuesta(gananciales="false"), respuesta(titulares=[123]),
    respuesta(confianza=float("nan")), respuesta(confianza=2),
])
def test_respuesta_invalida_vuelve_al_lector_local(monkeypatch, tmp_path, datos):
    simular(monkeypatch, datos)
    ext = pp.extraer(f.escritura_compra(tmp_path / "entrada.pdf"), ia.AjustesIA())
    assert ext.motor == "local"
    assert ext.campos["precio_escritura"] == 180000
    assert "lector local" in ext.avisos[0]
    assert "AVISO_PRIVADO" not in ext.avisos[0]


def test_contexto_incompleto_no_se_acepta(monkeypatch, tmp_path):
    simular(monkeypatch, respuesta(), stop="model_context_window_exceeded")
    ext = pp.extraer(f.escritura_compra(tmp_path / "entrada.pdf"), ia.AjustesIA())
    assert ext.motor == "local"


def test_no_acepta_campos_de_venta_en_una_compra():
    datos = respuesta(campos=[dict(campo="precio_venta", valor="250000", evidencia="Otro importe")])
    ext = ia.a_extraccion(datos, "compra.pdf")
    assert "precio_venta" not in ext.campos
    assert any("precio_venta" in aviso for aviso in ext.avisos)


@pytest.mark.parametrize("valor", ["NaN", "Infinity", "-Infinity"])
def test_importe_no_finito_se_rechaza(valor):
    with pytest.raises(ValueError):
        ia.convertir_valor("total", valor)


def test_hipoteca_sin_intereses_no_recupera_el_capital_de_base(monkeypatch, tmp_path):
    datos = respuesta(tipo="FACTURA_GASTO", campos=[
        dict(campo="categoria", valor="Intereses/financiación", evidencia="Préstamo hipotecario"),
        dict(campo="base", valor="750.00", evidencia="Amortización de capital 750 EUR"),
    ])
    simular(monkeypatch, datos)
    ruta = f._crear(tmp_path / "hipoteca.pdf", "RECIBO DE PRESTAMO HIPOTECARIO\nAmortizacion de capital: 750,00 EUR")
    ext = pp.extraer(ruta, ia.AjustesIA())
    assert ext.campos.get("total") is None
    assert ext.campos.get("base") is None
    assert any("intereses" in aviso.lower() for aviso in ext.avisos)


def test_limite_pdf_se_comprueba_antes_de_leerlo(monkeypatch, tmp_path):
    ruta = tmp_path / "grande.pdf"
    with ruta.open("wb") as archivo:
        archivo.truncate(ia.LIMITE_BYTES_PDF + 1)
    def no_leer(_ruta):
        pytest.fail("Un PDF que excede el límite no debe cargarse en memoria")
    monkeypatch.setattr(Path, "read_bytes", no_leer)
    with pytest.raises(ia.ErrorIA, match="32 MB"):
        ia.leer_con_claude(ruta, ia.AjustesIA())


def test_total_cero_se_conserva_en_el_desglose_visual():
    assert main.App._importe_registrado({"total": 0, "base": 100}) == 0


def test_cero_manual_tambien_prevalece():
    assert main.App._importe_registrado({"importe_deducible": 0, "total": 100}) == 0
