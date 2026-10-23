"""Ciclo documental multiformato con archivos sintéticos y sin acceso a la API."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import acciones_documentales as ad
import asistente
import documentos
import gestion_archivos
from database import Database


@pytest.fixture
def expediente(tmp_path):
    db = Database(tmp_path / "aislada.sqlite")
    db.carpeta_clientes = tmp_path / "Clientes"
    cliente = db.crear_cliente("Cliente", "Ficticio", "MF-001")
    yield db, cliente["id"], Path(cliente["carpeta"])
    db.cerrar()


def registrar(expediente, extension):
    db, cid, base = expediente
    carpeta = base / "Soportes_PDF"
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / ("Soporte" + extension)
    contenido = b"SOPORTE SINTETICO SIN DATOS PERSONALES"
    archivo.write_bytes(contenido)
    db.upsert("documentos", cid, {"codigo": "DOC-001", "tipo": "Otro", "ejercicio": 2025,
               "ruta": "Soportes_PDF/" + archivo.name, "sha256": documentos._huella(archivo)})
    return archivo, contenido


@pytest.mark.parametrize("extension", [".pdf", ".xlsx", ".xls", ".docx", ".csv"])
def test_papelera_restauracion_preservan_extension_contenido_y_registro(expediente, extension):
    db, cid, base = expediente
    archivo, contenido = registrar(expediente, extension)
    documentos.retirar(db, cid, "DOC-001")
    doc = documentos._documento(db, cid, "DOC-001")
    papelera = base / doc["ruta_papelera"]
    assert doc["en_papelera"] and papelera.suffix == extension
    assert papelera.read_bytes() == contenido and not archivo.exists()
    documentos.restaurar(db, cid, "DOC-001")
    assert archivo.read_bytes() == contenido and not papelera.exists()
    assert not documentos._documento(db, cid, "DOC-001")["en_papelera"]


@pytest.mark.parametrize("extension", [".pdf", ".xlsx", ".xls", ".docx", ".csv"])
def test_renombrar_conserva_extension_y_procedencia(expediente, extension):
    db, cid, base = expediente
    archivo, contenido = registrar(expediente, extension)
    nombre = "Resumen corregido" + extension
    with gestion_archivos.proteger(db, cid, "DOC-001", nombre) as copias:
        gestion_archivos.renombrar(db, cid, "DOC-001", nombre, "Descripción revisada", copias=copias)
    documentos.recuperar_archivos(db, cid)
    assert not archivo.exists()
    assert (base / "Soportes_PDF" / nombre).read_bytes() == contenido
    assert db.get("documentos", cid, "DOC-001")["descripcion"] == "Descripción revisada"


@pytest.mark.parametrize("nombre", ["Convertido.pdf", "Convertido.docx", "virus.exe", "../Fuera.xlsx", "CON.xlsx"])
def test_no_convierte_formato_ni_escapa_al_renombrar(expediente, nombre):
    registrar(expediente, ".xlsx")
    db, cid, _ = expediente
    with pytest.raises(ValueError):
        gestion_archivos.preparar(db, cid, "DOC-001", nombre)


@pytest.mark.parametrize("relativa", [".Papelera_Documentos/../fuera.csv", ".Papelera_Documentos/CON.xlsx",
                                      ".Papelera_Documentos/virus.exe", ".Papelera_Documentos/x.csv:ads"])
def test_papelera_rechaza_escapes_dispositivos_y_extensiones(expediente, relativa):
    db, cid, _ = expediente
    with pytest.raises(ValueError):
        documentos._ruta(db, cid, relativa, papelera=True)


def test_propuesta_no_cambia_extension_aunque_preparador_omita_validacion():
    gestor = ad.GestorAcciones(lambda: (1, 2025))
    with pytest.raises(ValueError, match="extensión original"):
        gestor.proponer({"accion": "renombrar", "codigo": "DOC-001", "nombre": "Otro.pdf"}, 1, 2025,
                       lambda *a: {"version": "v", "ruta_origen": "Soportes_PDF/Libro.xlsx", "ruta_destino": "Soportes_PDF/Otro.pdf"})


def test_contexto_lee_texto_local_csv_y_excluye_otros_clientes(expediente, tmp_path):
    _, _, base = expediente
    soportes = base / "Soportes_PDF"
    soportes.mkdir(exist_ok=True)
    (soportes / "rentas.csv").write_text("concepto;importe\nRenta ficticia;1234,56\n", encoding="utf-8")
    (soportes / "ejecutable.exe").write_bytes(b"NO DEBE LEERSE")
    (tmp_path / "ajeno.csv").write_text("SECRETO AJENO", encoding="utf-8")
    texto, numero = asistente._seccion_pdfs(base)
    assert numero == 1 and "Renta ficticia" in texto and "1234,56" in texto
    assert "SECRETO AJENO" not in texto and "NO DEBE LEERSE" not in texto


def test_contexto_acota_lectura_office_y_cache_sin_binarios(tmp_path, monkeypatch):
    import lectores_documentales
    ruta = tmp_path / "doc.docx"
    ruta.write_bytes(b"NO ENVIAR BINARIOS")
    llamadas = []
    def lector(archivo, *, max_caracteres):
        llamadas.append((archivo, max_caracteres))
        return SimpleNamespace(texto="texto " * 2000, avisos=["Vista previa parcial"])
    monkeypatch.setattr(lectores_documentales, "extraer_documento", lector)
    monkeypatch.setattr(asistente, "_CACHE_TEXTO", asistente.OrderedDict())
    texto = asistente.texto_documento(ruta)
    assert len(texto) <= asistente.MAX_POR_PDF and "Vista previa parcial" in texto
    assert "NO ENVIAR BINARIOS" not in texto
    assert asistente.texto_documento(ruta) == texto
    assert llamadas == [(ruta, asistente.MAX_POR_PDF)]


@pytest.mark.parametrize("extension", [".xlsx", ".xls", ".docx", ".csv"])
def test_correccion_ejercicio_soporte_no_pdf_conserva_formato_y_datos(expediente, tmp_path, extension):
    import pdf_processor as pp
    db, cid, base = expediente
    fuente = tmp_path / ("Certificado" + extension)
    fuente.write_bytes(b"DATOS SINTETICOS PREVIAMENTE REVISADOS")
    extraccion = pp.Extraccion(tipo=pp.TipoDocumento.CERTIFICADO_RESIDENCIA, confianza=0.9,
        campos={"pais": "Francia", "ejercicio": 2025}, nombre_archivo=fuente.name)
    resultado = pp.aplicar(db, cid, extraccion, fuente, ejercicio=2025)
    codigo = resultado.documento
    documentos.cambiar_ejercicio(db, cid, codigo, 2026)
    doc = db.get("documentos", cid, codigo)
    assert doc["ejercicio"] == 2026 and Path(doc["ruta"]).suffix == extension
    assert (base / doc["ruta"]).read_bytes() == fuente.read_bytes()
    assert not db.residencia(cid, 2025) and db.residencia(cid, 2026)["pais"] == "Francia"
    documentos.retirar(db, cid, codigo)
    documentos.restaurar(db, cid, codigo)
    assert db.residencia(cid, 2026)["certificado"] == "Sí"
