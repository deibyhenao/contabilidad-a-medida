"""Enrutado Haiku -> Sonnet y tablas de cobros de clientes (prueba con un expediente real, sin llamar a la API)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures_pdf as fx  # noqa: E402
import ia_extractor as ia  # noqa: E402
import pdf_processor as pp  # noqa: E402
from pdf_processor import Extraccion, TipoDocumento  # noqa: E402


def _ext(tipo, confianza, **kw):
    return Extraccion(tipo=tipo, confianza=confianza, nombre_archivo="x.pdf", motor="ia", **kw)


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(ia, "hay_clave", lambda: True)
    return fx._crear(tmp_path / "doc.pdf", "Documento ficticio sin datos")


def _simular(monkeypatch, respuestas):
    llamadas = []

    def extraer(ruta, ajustes, comentario_guia=""):
        llamadas.append(ajustes.modelo)
        r = respuestas[ajustes.modelo]
        if isinstance(r, Exception):
            raise r
        return r.model_copy(deep=True)
    monkeypatch.setattr(ia, "extraer", extraer)
    return llamadas


def test_haiku_con_confianza_media_no_paga_sonnet(pdf, monkeypatch):
    # Prueba real: Haiku 0,70 y Sonnet no aportó nada; por debajo de 0,5 sí se relee
    llamadas = _simular(monkeypatch, {"claude-haiku-5-5": _ext(TipoDocumento.RESUMEN_ALQUILER, 0.70)})
    assert pp.extraer(pdf, ia.AjustesIA()).tipo == TipoDocumento.RESUMEN_ALQUILER
    assert llamadas == ["claude-haiku-5-5"]


def test_relectura_con_sonnet_no_paga_escritura_de_cache(pdf, monkeypatch):
    vistos = []
    _simular(monkeypatch, {"claude-haiku-5-5": _ext(TipoDocumento.DESCONOCIDO, 0.3),
                           "claude-sonnet-5-5": _ext(TipoDocumento.CATASTRO_IBI, 0.9)})
    simulado = ia.extraer
    monkeypatch.setattr(ia, "extraer", lambda r, a, **kw: (vistos.append(a.cachear), simulado(r, a, **kw))[1])
    pp.extraer(pdf, ia.AjustesIA())
    assert vistos == [True, False]


def test_pdf_largo_se_recorta_bajo_el_tramo_caro(tmp_path):
    import fitz
    doc = fitz.open()
    for n in range(81):
        doc.new_page().insert_text((72, 72), "Precio de compraventa 250.000 euros, referencia catastral"
                                   if n == 40 else f"Pagina {n} sin datos")
    datos = doc.tobytes()
    reducido, aviso = ia._pdf_para_ia(datos)
    with fitz.open(stream=reducido, filetype="pdf") as r:
        textos = [p.get_text() for p in r]
    assert len(textos) == ia.MAX_PAGINAS_IA and "40 de 81" in aviso
    assert "Pagina 0 " in textos[0] and "Pagina 80" in textos[-1] and any("compraventa" in t for t in textos)
    corto = fitz.open(); corto.new_page()
    pequeno = corto.tobytes()
    assert ia._pdf_para_ia(pequeno) == (pequeno, "")


def test_haiku_seguro_no_relee(pdf, monkeypatch):
    llamadas = _simular(monkeypatch, {"claude-haiku-5-5": _ext(TipoDocumento.CATASTRO_IBI, 0.95)})
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert llamadas == ["claude-haiku-5-5"] and ext.tipo == TipoDocumento.CATASTRO_IBI


def test_haiku_no_reconoce_y_sonnet_lo_lee(pdf, monkeypatch):
    llamadas = _simular(monkeypatch, {"claude-haiku-5-5": _ext(TipoDocumento.DESCONOCIDO, 0.4),
                                      "claude-sonnet-5-5": _ext(TipoDocumento.RESUMEN_ALQUILER, 0.55)})
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert llamadas == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert ext.tipo == TipoDocumento.RESUMEN_ALQUILER and "releído con claude-sonnet-5-5" in ext.avisos[0]


def test_sin_respaldo_o_si_falla_se_queda_la_primera(pdf, monkeypatch):
    llamadas = _simular(monkeypatch, {"claude-haiku-5-5": _ext(TipoDocumento.DESCONOCIDO, 0.4),
                                      "claude-sonnet-5-5": ia.ErrorIA("Sin conexión")})
    assert pp.extraer(pdf, ia.AjustesIA(respaldo="")).tipo == TipoDocumento.DESCONOCIDO
    assert llamadas == ["claude-haiku-5-5"]
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert ext.tipo == TipoDocumento.DESCONOCIDO and any("segunda lectura" in a for a in ext.avisos)


def test_haiku_falla_y_sonnet_lo_lee(pdf, monkeypatch):
    llamadas = _simular(monkeypatch, {"claude-haiku-5-5": ia.ErrorIA("respuesta fuera del esquema", admite_respaldo=True),
                                      "claude-sonnet-5-5": _ext(TipoDocumento.CATASTRO_IBI, 0.9)})
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert llamadas == ["claude-haiku-5-5", "claude-sonnet-5-5"] and ext.tipo == TipoDocumento.CATASTRO_IBI
    assert "releído con claude-sonnet-5-5" in ext.avisos[0]


def test_error_inesperado_en_la_segunda_lectura_conserva_la_primera(pdf, monkeypatch):
    _simular(monkeypatch, {"claude-haiku-5-5": _ext(TipoDocumento.DESCONOCIDO, 0.4),
                           "claude-sonnet-5-5": RuntimeError("esquema inválido")})
    ext = pp.extraer(pdf, ia.AjustesIA())
    assert ext.tipo == TipoDocumento.DESCONOCIDO and any("segunda lectura" in a for a in ext.avisos)


def test_respaldo_se_guarda_en_ajustes(tmp_path):
    from database import Database
    db = Database(tmp_path / "t.db")
    assert ia.AjustesIA.desde_db(db).respaldo == "claude-sonnet-5-5"
    ia.AjustesIA(respaldo="").guardar(db)
    assert ia.AjustesIA.desde_db(db).respaldo == ""
    db.set_config("ia_modelo_respaldo", "modelo-inexistente")
    assert ia.AjustesIA.desde_db(db).respaldo == "claude-sonnet-5-5"


def test_excel_con_total_sin_fecha_no_tumba_el_documento(tmp_path):
    from controlador import Controlador
    ctrl = Controlador(tmp_path / "t.db")
    try:
        ctrl.cambiar_carpeta_clientes(tmp_path)
        cid = ctrl.crear_cliente("Prueba", "Excel", "X0000003T", pais="Rusia", ejercicio=2025)["id"]
        inm = ctrl.db.upsert("inmuebles", cid, {"direccion": "Calle Ficticia 3", "ref_catastral": "0000000AA0000A0003AA",
                                                "fecha_adquisicion": "2020-01-01", "pct_titularidad": 1})
        ext = _ext(TipoDocumento.RESUMEN_ALQUILER, 0.85, campos={"ejercicio": 2025, "ref_catastral": "0000000AA0000A0003AA"},
                   registros=[pp.RegistroDocumento(tabla="cobros", evidencia="E10: 7710",
                                                   campos={"ejercicio": 2025, "importe_cobrado": 7710.0, "estado": "NO"})])
        archivo = fx._crear(tmp_path / "cobros.pdf", "Resumen ficticio")
        r = pp.aplicar(ctrl.db, cid, ext, archivo, inmueble_destino=inm, ejercicio=2025)
        assert any("7710.00 € sin fecha ni periodo" in m for m in r.mensajes)
        assert any("«NO» no válido" in m for m in r.mensajes)
        assert not [c for c in ctrl.db.listar("cobros", cid) if c.get("importe_cobrado") == 7710]
    finally:
        ctrl.cerrar()
