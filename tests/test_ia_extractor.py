"""Lector IA con la API de Claude simulada (sin red ni clave real) y avisos de pendientes."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import fixtures_pdf as f
import ia_extractor as ia
import pdf_processor as pp
import tax_calculator as tc
from database import Database


class ClienteFalso:
    """Imita anthropic.Anthropic(): guarda la petición y devuelve una respuesta preparada."""

    def __init__(self, datos=None, stop_reason="end_turn", error=None):
        self.peticion = None
        self._datos, self._stop, self._error = datos, stop_reason, error
        self.ruta = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self._create("beta", **kw)))
        self.messages = SimpleNamespace(create=lambda **kw: self._create("normal", **kw))

    def _create(self, ruta, **kw):
        self.peticion, self.ruta = kw, ruta
        if self._error:
            raise self._error
        texto = json.dumps(self._datos) if self._datos is not None else ""
        return SimpleNamespace(stop_reason=self._stop, content=[SimpleNamespace(type="text", text=texto)],
                               usage=SimpleNamespace(input_tokens=12_000, output_tokens=800), model=kw["model"])


RESPUESTA_ESCRITURA = {
    "tipo": "ESCRITURA_COMPRA", "confianza": 0.93,
    "campos": [
        {"campo": "ref_catastral", "valor": "9872023 VH5797S 0001 WX", "evidencia": "referencia catastral 9872023VH5797S0001WX"},
        {"campo": "precio_escritura", "valor": "245000.00", "evidencia": "por el precio de DOSCIENTOS CUARENTA Y CINCO MIL EUROS"},
        {"campo": "fecha_adquisicion", "valor": "2021-03-04", "evidencia": "En Málaga, a cuatro de marzo de dos mil veintiuno"},
        {"campo": "direccion", "valor": "Calle Larios 5, 3ºB", "evidencia": "finca urbana sita en Calle Larios 5"},
        {"campo": "pct_titularidad", "valor": "100", "evidencia": "adquiere el pleno dominio"},
        {"campo": "precio_venta", "valor": "no consta", "evidencia": "-"},
    ],
    "titulares": ["John Smith"], "gananciales": True, "inquilinos": [], "avisos": ["Notaría con sello parcialmente ilegible"],
}


@pytest.fixture
def con_clave(monkeypatch):
    monkeypatch.setattr(ia, "obtener_clave", lambda: "sk-ant-prueba")


def test_peticion_a_claude_bien_formada(con_clave, monkeypatch, tmp_path):
    falso = ClienteFalso(RESPUESTA_ESCRITURA)
    monkeypatch.setattr(ia, "_cliente", lambda: falso)
    pdf = f.escritura_compra(tmp_path / "esc.pdf")
    ia.leer_con_claude(pdf, ia.AjustesIA(modelo="claude-opus-5-5"))
    p = falso.peticion
    assert p["model"] == "claude-opus-5-5"
    assert p["fallbacks"] == "default" and p["betas"] == ["server-side-fallback-2026-07-01"]
    assert p["output_config"]["effort"] == "medium"
    assert p["output_config"]["format"]["type"] == "json_schema"
    doc = p["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert "\n" not in doc["source"]["data"]
    assert "thinking" not in p and "temperature" not in p


def test_haiku_sin_fallback_y_coste(con_clave, monkeypatch, tmp_path):
    falso = ClienteFalso(RESPUESTA_ESCRITURA)
    monkeypatch.setattr(ia, "_cliente", lambda: falso)
    uso = {}
    ia.leer_con_claude(f.escritura_compra(tmp_path / "esc.pdf"), ia.AjustesIA(modelo="claude-haiku-5-5"), uso)
    assert falso.ruta == "normal" and "fallbacks" not in falso.peticion and "betas" not in falso.peticion
    assert falso.peticion["model"] == "claude-haiku-5-5"
    assert uso["coste"] == round((12_000 * 0.10 + 800 * 0.50) / 1e6, 5)
    ia.leer_con_claude(f.escritura_compra(tmp_path / "esc2.pdf"), ia.AjustesIA(modelo="claude-opus-5-5"), uso)
    assert falso.ruta == "beta" and uso["coste"] == round((12_000 * 4 + 800 * 20) / 1e6, 5)


def test_modelo_por_defecto_haiku():
    assert ia.AjustesIA().modelo == "claude-haiku-5-5"


def test_esquema_estricto_valido():
    def revisar(nodo):
        if isinstance(nodo, dict):
            if nodo.get("type") == "object":
                assert nodo["additionalProperties"] is False
                assert set(nodo["required"]) == set(nodo["properties"])
            for v in nodo.values():
                revisar(v)
        elif isinstance(nodo, list):
            for v in nodo:
                revisar(v)
    revisar(ia.ESQUEMA)
    assert set(ia.CAMPOS) >= {c for cs in ia.CAMPOS_POR_TIPO.values() for c in cs}


def test_conversion_a_extraccion():
    ext = ia.a_extraccion(RESPUESTA_ESCRITURA, "esc.pdf")
    assert ext.motor == "ia" and ext.tipo == pp.TipoDocumento.ESCRITURA_COMPRA
    assert ext.campos["ref_catastral"] == "9872023VH5797S0001WX"
    assert ext.campos["precio_escritura"] == 245000.0
    assert ext.campos["fecha_adquisicion"] == "2021-03-04"
    assert ext.campos["pct_titularidad"] == 0.5 and ext.campos["gananciales_forzado"] == 1  # gananciales manda
    assert "precio_venta" not in ext.campos and any("precio_venta" in a for a in ext.avisos)
    assert "DOSCIENTOS" in ext.evidencias["precio_escritura"]


@pytest.mark.parametrize("campo,valor,esperado", [
    ("precio_escritura", "180.000,50", 180000.5), ("pct_titularidad", "50", 0.5),
    ("pct_titularidad", "0.25", 0.25), ("fecha_inicio", "01/07/2026", "2026-07-01"),
    ("ejercicio", "2026", 2026), ("tipo_renta", "2", "02"), ("ref_catastral", "1234567 ab1234c0001de", "1234567AB1234C0001DE"),
])
def test_convertir_valor(campo, valor, esperado):
    assert ia.convertir_valor(campo, valor) == esperado


def test_extraer_usa_ia_y_completa_con_local(con_clave, monkeypatch, tmp_path):
    pdf = f.recibo_ibi(tmp_path / "ibi.pdf", valor_catastral=90_000, ibi=450)
    respuesta = {"tipo": "CATASTRO_IBI", "confianza": 0.9, "titulares": [], "gananciales": False,
                 "inquilinos": [], "avisos": [],
                 "campos": [{"campo": "valor_catastral", "valor": "90000", "evidencia": "Valor catastral 90.000"}]}
    monkeypatch.setattr(ia, "_cliente", lambda: ClienteFalso(respuesta))
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert ext.motor == "ia" and ext.campos["valor_catastral"] == 90000.0
    assert ext.campos["ibi_ejercicio"] == 450  # lo aporta el lector local
    assert ext.evidencias["ibi_ejercicio"] == "Lector local"


def test_fallo_de_api_cae_al_lector_local(con_clave, monkeypatch, tmp_path):
    import anthropic
    import httpx
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(ia, "_cliente", lambda: ClienteFalso(error=error))
    ext = pp.extraer(f.escritura_compra(tmp_path / "esc.pdf"), ia.AjustesIA())
    assert ext.motor == "local" and ext.campos["precio_escritura"] == 180_000
    assert "Sin conexión" in ext.avisos[0]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_y_truncado_caen_al_local(con_clave, monkeypatch, tmp_path, stop):
    monkeypatch.setattr(ia, "_cliente", lambda: ClienteFalso(RESPUESTA_ESCRITURA, stop_reason=stop))
    ext = pp.extraer(f.escritura_compra(tmp_path / "esc.pdf"), ia.AjustesIA())
    assert ext.motor == "local" and "lector local" in ext.avisos[0]


def test_sin_clave_o_desactivada_no_llama(monkeypatch, tmp_path):
    monkeypatch.setattr(ia, "_cliente", lambda: pytest.fail("no debe llamar a la API"))
    pdf = f.escritura_compra(tmp_path / "esc.pdf")
    assert pp.extraer(pdf, ia.AjustesIA()).motor == "local"            # sin clave (conftest)
    monkeypatch.setattr(ia, "obtener_clave", lambda: "sk-ant-prueba")
    assert pp.extraer(pdf, ia.AjustesIA(activada=False)).motor == "local"


def test_ajustes_en_bd(tmp_path):
    db = Database(tmp_path / "t.db")
    assert ia.AjustesIA.desde_db(db) == ia.AjustesIA()
    ia.AjustesIA(activada=False, esfuerzo="high").guardar(db)
    assert ia.AjustesIA.desde_db(db) == ia.AjustesIA(activada=False, esfuerzo="high")
    db.cerrar()


# ---------------------------------------------------------------- pendientes
@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "p.db")
    d.carpeta_clientes = tmp_path / "Clientes"
    yield d
    d.cerrar()


def test_pendientes_documentos_y_declaraciones(db):
    cid = db.crear_cliente("Pepito", "Pérez", "X1234567A")["id"]
    db.guardar_residencia(cid, 2026, pais="Francia")
    inm = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2019-01-01", "pct_titularidad": 1})
    db.upsert("catastro", cid, {"ejercicio": 2025, "inmueble": inm, "valor_catastral": 100_000})
    db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2026-01-01", "renta_inicial": 800})
    tc.recalcular_cliente(db, cid, 2026)
    pend = tc.revisar_pendientes(db, cid, 2026, hoy=date(2026, 10, 25))
    titulos = [p.titulo for p in pend]
    assert "Falta: Escritura de compra" in titulos
    assert "Falta: Catastro / IBI" in titulos            # solo hay el de 2025
    assert "Falta: Certificado de residencia fiscal" in titulos
    assert "Falta: Contrato de alquiler" in titulos      # contrato sin PDF
    assert any(p.titulo.startswith("VENCIDA: 210 Alquiler") for p in pend)  # 3T venció el 20/10
    assert pend[0].nivel == "grave"
    faltas = [d for d in db.listar("documentos", cid) if d["estado"] == "Falta"]
    assert len(faltas) == 4

    # Llega el IBI 2026 y el certificado: sus filas "Falta" desaparecen
    db.upsert("catastro", cid, {"ejercicio": 2026, "inmueble": inm, "valor_catastral": 100_000})
    db.guardar_residencia(cid, 2026, certificado="Sí")
    pend2 = tc.revisar_pendientes(db, cid, 2026, hoy=date(2026, 10, 25))
    assert "Falta: Catastro / IBI" not in [p.titulo for p in pend2]
    assert len([d for d in db.listar("documentos", cid) if d["estado"] == "Falta"]) == 2


def test_presentado_no_aparece_como_pendiente(db):
    cid = db.crear_cliente("Pepito", "Pérez", "X1234567A")["id"]
    db.guardar_residencia(cid, 2026, pais="Francia", certificado="Sí")
    inm = db.upsert("inmuebles", cid, {"fecha_adquisicion": "2019-01-01", "carpeta": "Soportes_PDF/e.pdf"})
    db.upsert("catastro", cid, {"ejercicio": 2026, "inmueble": inm, "valor_catastral": 100_000})
    inf = tc.recalcular_cliente(db, cid, 2026)
    cod = inf.filas_210_imputacion[0]
    db.upsert("m210_imputacion", cid, {"codigo": cod, "estado": "Presentado"})
    obl = db.get_por_clave("obligaciones", cid, f"OBL|210I|2026|{inm}")
    db.upsert("obligaciones", cid, {"codigo": obl["codigo"], "estado": "Presentado"})
    pend = tc.revisar_pendientes(db, cid, 2026, hoy=date(2026, 10, 25))
    assert not any("Imputación" in p.titulo for p in pend)
    assert not any(p.clase == "documento" for p in pend)
