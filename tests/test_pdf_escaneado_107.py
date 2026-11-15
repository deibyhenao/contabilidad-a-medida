"""PDF largo con anexos escaneados: el recorte para la IA no los descarta todos (sin API)."""
import fitz

import ia_extractor as ia


def _pdf(total, escaneadas):
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), 0)
    pix.set_rect(pix.irect, (120, 120, 120))
    imagen = pix.tobytes("png")
    for n in range(total):
        pagina = doc.new_page()
        if n in escaneadas:
            pagina.insert_image(fitz.Rect(50, 50, 300, 300), stream=imagen)
        else:
            pagina.insert_text((72, 72), f"Pagina {n} clausula sin datos fiscales relevantes del documento")
    datos = doc.tobytes()
    doc.close()
    return datos


def test_reserva_paginas_escaneadas_y_avisa_de_las_omitidas():
    escaneadas = set(range(40, 70))                       # anexo de 30 páginas solo imagen en la zona intermedia
    reducido, aviso = ia._pdf_para_ia(_pdf(81, escaneadas))
    with fitz.open(stream=reducido, filetype="pdf") as r:
        assert r.page_count == ia.MAX_PAGINAS_IA
        con_imagen = sum(1 for p in r if p.get_images())
    assert con_imagen == ia.CUPO_ESCANEADAS_IA
    assert "Quedaron fuera 25 páginas escaneadas" in aviso and "Páginas originales" in aviso


def test_sin_escaneadas_no_cambia_el_aviso():
    _, aviso = ia._pdf_para_ia(_pdf(60, set()))
    assert "escaneadas (solo imagen)" not in aviso
