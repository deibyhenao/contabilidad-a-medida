"""Regresiones de transporte PDF: recursos compartidos, paginación y límites; sin red."""
import json
import random
from types import SimpleNamespace as NS

import fitz
import pytest

import ia_extractor as ia


def test_recorte_no_duplica_imagenes_compartidas():
    with fitz.open() as doc:
        pix = fitz.Pixmap(fitz.csRGB, 400, 400, random.Random(105).randbytes(400 * 400 * 3), False)
        xref = 0
        for _ in range(81):
            xref = doc.new_page().insert_image(fitz.Rect(0, 0, 400, 400), pixmap=pix, xref=xref)
        fuente = doc.tobytes()
    reducido, aviso = ia._pdf_para_ia(fuente)
    with fitz.open(stream=reducido, filetype="pdf") as doc:
        assert len(doc) == 40
        assert len({image[0] for page in doc for image in page.get_images()}) == 1
    assert len(reducido) < len(fuente) * 2
    assert "Lectura parcial" in aviso and "no se evalúan visualmente" in aviso


@pytest.mark.parametrize("paginas", [40, 41, 81])
def test_recorte_conserva_orden_y_correspondencia(paginas):
    with fitz.open() as doc:
        for n in range(paginas):
            doc.new_page().insert_text((72, 72), f"ORIGINAL {n + 1}" + (" valor catastral euros" if n == 40 else ""))
        fuente = doc.tobytes()
    reducido, aviso = ia._pdf_para_ia(fuente)
    with fitz.open(stream=reducido, filetype="pdf") as doc:
        numeros = [int(page.get_text().split()[1]) for page in doc]
    assert len(numeros) == min(paginas, 40)
    assert numeros == sorted(set(numeros))
    if paginas > 40:
        assert ", ".join(map(str, numeros)) in aviso
    else:
        assert reducido == fuente and aviso == ""


def test_peticion_codificada_se_rechaza_antes_de_abrir_red(tmp_path, monkeypatch):
    ruta = tmp_path / "grande.pdf"
    ruta.write_bytes(b"ficticio")
    monkeypatch.setattr("lectores_documentales.extraer_documento", lambda _: NS(avisos=[], texto=""))
    monkeypatch.setattr(ia, "_pdf_para_ia", lambda _: (b"x" * 12000, ""))
    # PDF bajo 16KB, pero base64 + esquema excede el límite.
    monkeypatch.setattr(ia, "LIMITE_BYTES_PDF", 16000)
    monkeypatch.setattr(ia, "_cliente", lambda: pytest.fail("No se debe abrir red"))
    with pytest.raises(ia.ErrorIA, match="petición codificada"):
        ia.leer_con_claude(ruta, ia.AjustesIA())


def test_pdf_preparado_demasiado_grande_no_abre_red(tmp_path, monkeypatch):
    ruta = tmp_path / "grande.pdf"
    ruta.write_bytes(b"ficticio")
    monkeypatch.setattr("lectores_documentales.extraer_documento", lambda _: NS(avisos=[], texto=""))
    monkeypatch.setattr(ia, "_pdf_para_ia", lambda _: (b"x" * 17, ""))
    monkeypatch.setattr(ia, "LIMITE_BYTES_PDF", 16)
    monkeypatch.setattr(ia, "_cliente", lambda: pytest.fail("No se debe abrir red"))
    with pytest.raises(ia.ErrorIA, match="PDF preparado"):
        ia.leer_con_claude(ruta, ia.AjustesIA())


def test_modelo_recibe_paginacion_original_del_recorte(tmp_path, monkeypatch):
    ruta = tmp_path / "parcial.pdf"
    ruta.write_bytes(b"ficticio")
    aviso = "Lectura parcial. Páginas originales, en el orden enviado: 1, 42, 81."
    monkeypatch.setattr("lectores_documentales.extraer_documento", lambda _: NS(avisos=[], texto=""))
    monkeypatch.setattr(ia, "_pdf_para_ia", lambda _: (b"pdf reducido", aviso))
    peticiones = []
    def crear(**peticion):
        peticiones.append(peticion)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"avisos": []}))])
    monkeypatch.setattr(ia, "_cliente", lambda: NS(messages=NS(create=crear)))
    respuesta = ia.leer_con_claude(ruta, ia.AjustesIA())
    assert aviso in respuesta["avisos"]
    textos = [bloque["text"] for bloque in peticiones[0]["messages"][0]["content"] if bloque["type"] == "text"]
    assert any(aviso in texto and "Cita las páginas originales" in texto for texto in textos)
