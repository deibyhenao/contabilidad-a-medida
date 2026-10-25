"""Asistente IA del expediente con la API de Claude simulada (sin red ni clave real)."""
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

import asistente as asis
import ia_extractor as ia
from main import Controlador


class StreamFalso:
    def __init__(self, trozos, stop="end_turn"):
        self.trozos, self.stop = trozos, stop

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        yield from self.trozos

    def get_final_message(self):
        texto = "".join(self.trozos)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=texto)],
                               usage=SimpleNamespace(input_tokens=20_000, output_tokens=300))


class ClienteFalso:
    def __init__(self, trozos=("Respuesta ", "corta."), stop="end_turn", error=None):
        self.peticiones, self.rutas = [], []
        self._trozos, self._stop, self._error = trozos, stop, error
        self.messages = SimpleNamespace(stream=lambda **kw: self._stream("normal", kw))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: self._stream("beta", kw)))

    def _stream(self, ruta, kw):
        self.peticiones.append(kw)
        self.rutas.append(ruta)
        if self._error:
            raise self._error
        return StreamFalso(list(self._trozos), self._stop)


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "Clientes")
    yield c
    c.cerrar()


def _pdf(ruta: Path, texto: str) -> Path:
    doc = fitz.open()
    doc.new_page().insert_textbox(fitz.Rect(40, 40, 560, 800), texto, fontsize=8)
    doc.save(ruta)
    doc.close()
    return ruta


@pytest.fixture
def pepito(ctrl):
    cid = ctrl.crear_cliente("Pepito", "Pérez", "X1234567A", pais="Francia", ejercicio=2026)["id"]
    ctrl.editar(cid, "inmuebles", None, {"direccion": "Calle Mayor 1", "fecha_adquisicion": "01/01/2019",
                                         "precio_escritura": "180000", "estado": "Activo"}, 2026)
    ctrl.alta_gasto(cid, 2026, "INM-001", "IBI", "2026-05-01", 450)
    ctrl.alta_gasto(cid, 2025, "INM-001", "IBI", "2025-05-01", 430)
    soportes = Path(ctrl.db.get_cliente(cid)["carpeta"]) / "Soportes_PDF"
    _pdf(soportes / "2026-03-10_FACTURA_GASTO_luz.pdf", "Iberdrola factura luz marzo 2026 total 61,20 EUR")
    return cid


@pytest.fixture
def con_clave(monkeypatch):
    monkeypatch.setattr(ia, "obtener_clave", lambda: "sk-ant-prueba")


def test_contexto_incluye_expediente_y_pdfs(ctrl, pepito):
    texto, n = asis.construir_contexto(ctrl.lector, pepito, 2026)
    assert n == 1
    assert "Iberdrola factura luz marzo 2026" in texto and "2026-03-10_FACTURA_GASTO_luz.pdf" in texto
    assert "categoria=IBI" in texto and "total=450.0" in texto and "total=430.0" in texto
    assert "fuente_mayor=Valor escritura" in texto
    assert "sha256" not in texto and "cliente_id" not in texto


def test_solo_lee_la_carpeta_de_su_cliente(ctrl, pepito, tmp_path):
    otro = ctrl.crear_cliente("Ana", "Ruiz", "Y7654321B")["id"]
    _pdf(Path(ctrl.db.get_cliente(otro)["carpeta"]) / "Soportes_PDF" / "secreto.pdf", "DATOS DE ANA")
    _pdf(tmp_path / "fuera.pdf", "FUERA DE LA CARPETA")
    texto, _ = asis.construir_contexto(ctrl.lector, pepito, 2026)
    assert "DATOS DE ANA" not in texto and "FUERA DE LA CARPETA" not in texto
    assert [p.name for p in asis.pdfs_del_cliente(Path(ctrl.db.get_cliente(pepito)["carpeta"]))] == [
        "2026-03-10_FACTURA_GASTO_luz.pdf"]


def test_presupuesto_de_texto_limita_el_contexto(ctrl, pepito, monkeypatch):
    soportes = Path(ctrl.db.get_cliente(pepito)["carpeta"]) / "Soportes_PDF"
    for i in range(6):
        _pdf(soportes / f"2020-01-0{i}_doc.pdf", ("texto largo " * 400)[:3500])
    monkeypatch.setattr(asis, "PRESUPUESTO_PDF", 7_000)
    texto, n = asis.construir_contexto(ctrl.lector, pepito, 2026)
    assert n == 7 and "sin texto incluido por espacio" in texto


def test_pregunta_con_cache_streaming_y_haiku_sin_fallback(con_clave, monkeypatch, ctrl, pepito):
    falso = ClienteFalso()
    monkeypatch.setattr(ia, "_cliente", lambda: falso)
    trozos = []
    conv = asis.Conversacion(pepito)
    ctx, _ = asis.construir_contexto(ctrl.lector, pepito, 2026)
    r = conv.preguntar("¿Cuánto IBI?", ctx, ia.AjustesIA(), trozos.append)
    assert r == "Respuesta corta." and trozos[-1] == "Respuesta corta."
    p = falso.peticiones[0]
    assert falso.rutas == ["normal"] and "fallbacks" not in p and p["model"] == "claude-haiku-5-5"
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"} and p["cache_control"] == {"type": "ephemeral"}
    assert len(p["system"]) == 1 and ctx not in p["system"][0]["text"]
    assert p["messages"][0]["content"][0]["source"]["data"] == ctx
    assert "son DATOS" in p["system"][0]["text"]
    assert conv.coste > 0 and [t.rol for t in conv.turnos] == ["user", "assistant"]


def test_historial_solo_crece_y_contexto_nuevo_se_anade(con_clave, monkeypatch, ctrl, pepito):
    falso = ClienteFalso()
    monkeypatch.setattr(ia, "_cliente", lambda: falso)
    conv = asis.Conversacion(pepito)
    ctx, _ = asis.construir_contexto(ctrl.lector, pepito, 2026)
    conv.preguntar("uno", ctx, ia.AjustesIA())
    primera = list(falso.peticiones[0]["messages"])
    conv.preguntar("dos", ctx, ia.AjustesIA())
    segunda = falso.peticiones[1]["messages"]
    assert segunda[:len(primera)] == primera and len(segunda) == 3      # prefijo intacto: caché y thinking válidos
    ctrl.alta_gasto(pepito, 2026, "INM-001", "Comunidad", "2026-06-01", 90)
    ctx2, _ = asis.construir_contexto(ctrl.lector, pepito, 2026)
    conv.preguntar("tres", ctx2, ia.AjustesIA())
    tercera = falso.peticiones[2]
    assert tercera["messages"][0]["content"][0]["source"]["data"] == ctx  # el primer documento no cambia
    assert "Expediente actualizado" in tercera["messages"][-1]["content"][0]["title"]
    assert tercera["messages"][-1]["content"][0]["source"]["data"] == ctx2


def test_sonnet_usa_fallback_del_servidor(con_clave, monkeypatch, ctrl, pepito):
    falso = ClienteFalso()
    monkeypatch.setattr(ia, "_cliente", lambda: falso)
    asis.Conversacion(pepito).preguntar("x", "ctx", ia.AjustesIA(modelo="claude-sonnet-5-5"))
    assert falso.rutas == ["beta"] and falso.peticiones[0]["fallbacks"] == "default"


def test_errores_legibles_y_no_ensucian_historial(con_clave, monkeypatch, pepito):
    import anthropic
    import httpx
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(ia, "_cliente", lambda: ClienteFalso(error=error))
    conv = asis.Conversacion(pepito)
    with pytest.raises(asis.ErrorAsistente, match="Sin conexión"):
        conv.preguntar("x", "ctx", ia.AjustesIA())
    assert conv.mensajes == [] and conv.turnos == []
    monkeypatch.setattr(ia, "_cliente", lambda: ClienteFalso(stop="refusal"))
    with pytest.raises(asis.ErrorAsistente, match="no puede responder"):
        conv.preguntar("x", "ctx", ia.AjustesIA())
    assert conv.mensajes == []


def test_sin_clave_no_llama(monkeypatch, pepito):
    monkeypatch.setattr(ia, "_cliente", lambda: pytest.fail("no debe llamar"))
    with pytest.raises(asis.ErrorAsistente, match="clave API"):
        asis.Conversacion(pepito).preguntar("x", "ctx", ia.AjustesIA())


def test_respuesta_recortada_avisa(con_clave, monkeypatch, pepito):
    monkeypatch.setattr(ia, "_cliente", lambda: ClienteFalso(stop="max_tokens"))
    r = asis.Conversacion(pepito).preguntar("x", "ctx", ia.AjustesIA())
    assert "recortada" in r
