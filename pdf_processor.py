"""Extracción revisable de PDFs y aplicación idempotente al modelo de datos.

No usa servicios externos. Los campos extraídos son propuestas: procesar_pdf
permite corregirlos antes de archivar y aplicar. La extracción sola no toca BD.
"""
from __future__ import annotations

import calendar
import hashlib
import math
import re
import shutil
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import fitz
from pydantic import BaseModel, Field

from carteras import RES, cartera_de
from database import TABLAS, Columna, _normalizar, a_fecha, a_num, slug, componente_ruta, subcarpeta_cliente
from database import ESTADOS_COBRO
from rutas_seguras import sanitize_safe_path
from validacion_catastro import PENDIENTE_REVISION, revision_acreditada


class TipoDocumento(str, Enum):
    ESCRITURA_COMPRA = "ESCRITURA_COMPRA"
    ESCRITURA_VENTA = "ESCRITURA_VENTA"
    CATASTRO_IBI = "CATASTRO_IBI"
    VALOR_REFERENCIA = "VALOR_REFERENCIA"
    CONTRATO_ALQUILER = "CONTRATO_ALQUILER"
    MODELO_210 = "MODELO_210"
    CERTIFICADO_RESIDENCIA = "CERTIFICADO_RESIDENCIA"
    MODELO_036_037 = "MODELO_036_037"
    MODELO_PRESENTADO = "MODELO_PRESENTADO"
    FACTURA_GASTO = "FACTURA_GASTO"
    RESUMEN_ALQUILER = "RESUMEN_ALQUILER"
    RESOLUCION_ALQUILER = "RESOLUCION_ALQUILER"
    DESCONOCIDO = "DESCONOCIDO"


class RegistroDocumento(BaseModel):
    tabla: Literal["cobros", "gastos", "catastro"]
    campos: dict[str, Any]
    evidencia: str = Field(min_length=1, max_length=500)


class Extraccion(BaseModel):
    tipo: TipoDocumento
    confianza: float = Field(ge=0, le=1)
    campos: dict[str, Any] = Field(default_factory=dict)
    inquilinos: list[dict] = Field(default_factory=list)
    titulares: list[str] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)
    comentario_guia: str = Field(default="", max_length=4000)
    registros: list[RegistroDocumento] = Field(default_factory=list, max_length=500)
    necesita_ocr: bool = False
    texto: str = ""
    nombre_archivo: str
    motor: str = "local"                                   # "ia" | "local"
    revision_confirmada: bool = False  # Solo la revisión humana; no forma parte del esquema de la API.
    evidencias: dict[str, str] = Field(default_factory=dict)  # campo -> frase del documento (solo IA)
    coste_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    coste_incompleto: bool = False
    modelo_ia: str | None = None  # Modelo de la lectura elegida; no el configurado.
    lecturas_ia: list[dict[str, Any]] = Field(default_factory=list, max_length=2)


class ResultadoAplicacion(BaseModel):
    documento: str
    duplicado: bool
    ruta_relativa: str
    ejercicios_afectados: set[int] = Field(default_factory=set)
    codigos: dict[str, str] = Field(default_factory=dict)
    mensajes: list[str] = Field(default_factory=list)


TIPOS_LEGIBLES = {
    TipoDocumento.ESCRITURA_COMPRA: "Escritura de compra",
    TipoDocumento.ESCRITURA_VENTA: "Escritura de venta",
    TipoDocumento.CATASTRO_IBI: "Catastro / IBI",
    TipoDocumento.VALOR_REFERENCIA: "Valor de referencia",
    TipoDocumento.CONTRATO_ALQUILER: "Contrato de alquiler",
    TipoDocumento.MODELO_210: "Modelo 210 presentado",
    TipoDocumento.CERTIFICADO_RESIDENCIA: "Certificado de residencia fiscal",
    TipoDocumento.MODELO_036_037: "Modelo 036/037",
    TipoDocumento.MODELO_PRESENTADO: "Modelo presentado",
    TipoDocumento.FACTURA_GASTO: "Factura / recibo de gasto",
    TipoDocumento.RESUMEN_ALQUILER: "Relación de ingresos y gastos",
    TipoDocumento.RESOLUCION_ALQUILER: "Resolución de contrato de alquiler",
    TipoDocumento.DESCONOCIDO: "Documento por revisar",
}
AVISO_OCR = "PDF escaneado sin OCR: revisa los campos a mano"
MESES = {m: i for i, m in enumerate(
    ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
     "noviembre", "diciembre"), 1)}
PATRON_FECHA = (r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|"
                r"\d{1,2}\s+de\s+(?:" + "|".join(MESES) + r")\s+de\s+\d{4})\b")
# La alternativa decimal con punto debe preceder a los enteros.
PATRON_IMPORTE = r"[-+]?(?:\d{1,3}(?:[. ]\d{3})+(?:,\d{1,2})?|\d+[.,]\d{1,2}|\d+)"
PATRON_REF = r"(?<![A-Z0-9])" + r"\s*".join(
    [r"\d"] * 7 + [r"[A-Z]"] * 2 + [r"\d"] * 4 + [r"[A-Z]"] + [r"\d"] * 4 + [r"[A-Z]"] * 2
) + r"(?![A-Z0-9])"
PATRON_NIF = r"\b(?:[XYZ]\d{7}|\d{7,8})[A-Z]\b"


def _normal(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)).lower()


def _dato(texto: str, *etiquetas: str) -> str | None:
    """Busca valores etiquetados, preservando tildes del texto original."""
    texto = unicodedata.normalize("NFC", texto)
    alternativas = "|".join(re.escape(_normal(e)) for e in etiquetas)
    patron = rf"(?:^|\n)[ \t]*(?:{alternativas})[ \t]*(?::|=)[ \t]*(?:\n[ \t]*)?([^\n]+)"
    m = re.search(patron, _normal(texto))
    if m:
        return texto[m.start(1):m.end(1)].strip()
    # Segunda opción para prosa: 'precio de compra 180.000,00 euros'.
    m = re.search(rf"\b(?:{alternativas})\b[ \t]+([^\n]+)", _normal(texto))
    return texto[m.start(1):m.end(1)].strip() if m else None


def _fecha(texto: str | None) -> str | None:
    if not texto:
        return None
    m = re.search(PATRON_FECHA, _normal(str(texto)))
    if not m:
        return None
    valor = m.group()
    try:
        if " de " in valor:
            d, mes, y = re.split(r"\s+de\s+", valor)
            return date(int(y), MESES[mes], int(d)).isoformat()
        if re.match(r"\d{4}-", valor):
            y, mes, d = map(int, valor.split("-"))
        else:
            d, mes, y = map(int, re.split(r"[/.-]", valor))
        return date(y, mes, d).isoformat()
    except ValueError:
        return None


def _importe(texto: str | None) -> float | None:
    if not texto:
        return None
    limpio = texto.replace("\u00a0", " ")
    if ":" in limpio:
        tras_dos_puntos = limpio.split(":", 1)[1]
        if re.search(PATRON_IMPORTE, tras_dos_puntos):
            limpio = tras_dos_puntos
    m = re.search(PATRON_IMPORTE, limpio)
    if not m:
        return None
    valor = m.group().replace(" ", "")
    if re.fullmatch(r"[-+]?\d{1,3}(?:\.\d{3})+", valor):
        valor = valor.replace(".", "")
    return round(a_num(valor), 2)


def _entero(texto: str | None) -> int | None:
    m = re.search(r"\d+", texto or "")
    return int(m.group()) if m else None


def _ref(texto: str | None) -> str | None:
    m = re.search(PATRON_REF, texto or "", re.I)
    return re.sub(r"\s", "", m.group()).upper() if m else None


def _sumar_meses(inicio: date, meses: int) -> date:
    indice = inicio.year * 12 + inicio.month - 1 + meses
    y, m = divmod(indice, 12)
    return date(y, m + 1, min(inicio.day, calendar.monthrange(y, m + 1)[1]))


def extraer_texto(ruta: Path) -> tuple[str, bool]:
    if Path(ruta).suffix.lower() != ".pdf":
        from lectores_documentales import extraer_documento
        documento = extraer_documento(Path(ruta))
        return documento.texto, documento.escaneado
    from lectores_documentales import MAX_ARCHIVO_PDF, MAX_PAGINAS, MAX_TEXTO, ErrorDocumento
    if Path(ruta).stat().st_size > MAX_ARCHIVO_PDF:
        raise ErrorDocumento("El PDF supera 32 MB.")
    textos = []
    caracteres = 0
    necesita_ocr = False
    with fitz.open(str(ruta)) as doc:
        if doc.needs_pass or len(doc) > MAX_PAGINAS:
            raise ErrorDocumento("El PDF está cifrado o supera el límite de páginas.")
        for pagina in doc:
            texto = pagina.get_text()
            if len(texto.strip()) < 50:
                try:
                    textpage = pagina.get_textpage_ocr()
                    texto_ocr = pagina.get_text(textpage=textpage)
                    if len(texto_ocr.strip()) >= 50:
                        texto = texto_ocr
                    else:
                        necesita_ocr = True
                except Exception:
                    # Tesseract es opcional. Un fallo OCR no impide revisión manual.
                    necesita_ocr = True
            caracteres += len(texto)
            if caracteres > MAX_TEXTO:
                raise ErrorDocumento("El texto del PDF supera el límite permitido.")
            textos.append(unicodedata.normalize("NFC", texto))
    return "\n".join(textos), necesita_ocr


def clasificar(texto: str, nombre_archivo: str = "") -> tuple[TipoDocumento, float]:
    t = re.sub(r"\s+", " ", _normal(texto))
    nombre = re.sub(r"\s+", " ", _normal(nombre_archivo).replace("_", " "))
    reglas = {
        TipoDocumento.ESCRITURA_COMPRA: [("compraventa", 5), ("notario", 2), ("parte compradora", 2),
                                        ("escritura de compra", 6)],
        TipoDocumento.ESCRITURA_VENTA: [("escritura de venta", 7), ("cliente vendedor", 10),
                                       ("cliente es vendedor", 10)],
        TipoDocumento.CATASTRO_IBI: [("impuesto sobre bienes inmuebles", 9), ("recibo ibi", 8),
                                    ("valor catastral", 4)],
        TipoDocumento.VALOR_REFERENCIA: [("valor de referencia", 6), ("direccion general del catastro", 2)],
        TipoDocumento.CONTRATO_ALQUILER: [("arrendamiento", 6), ("arrendador", 2), ("arrendatario", 3),
                                         ("contrato de alquiler", 8)],
        TipoDocumento.MODELO_210: [("modelo 210", 12), ("impuesto sobre la renta de no residentes", 8)],
        TipoDocumento.CERTIFICADO_RESIDENCIA: [("certificado de residencia fiscal", 12),
                                              ("certificate of residence", 12)],
        TipoDocumento.MODELO_036_037: [("modelo 036", 12), ("modelo 037", 12), ("declaracion censal", 8)],
        TipoDocumento.MODELO_PRESENTADO: [("autoliquidacion presentada", 3)],
        TipoDocumento.FACTURA_GASTO: [
            ("factura", 5), ("recibo", 2), ("nº de factura", 4), ("importe total", 3), ("iva", 1),
            ("periodo de facturacion", 4), ("comunidad de propietarios", 6), ("cuota de comunidad", 6),
            ("poliza", 5), ("prima", 2), ("seguro", 3), ("suministro", 4), ("kwh", 4),
            ("consumo", 2), ("m3", 3), ("electricidad", 4), ("gas natural", 4), ("agua", 3),
            ("prestamo hipotecario", 6), ("intereses", 3), ("reparacion", 4), ("mantenimiento", 3),
            ("presupuesto", 2), ("tasa de basuras", 6), ("tasa de basura", 6),
            ("recogida de residuos", 6), ("honorarios", 4),
        ],
    }
    puntos = {tipo: sum(peso for palabra, peso in claves if palabra in t)
              + sum(peso * 0.25 for palabra, peso in claves if palabra in nombre)
              for tipo, claves in reglas.items()}
    if re.search(r"\bmodelo\s+(?:303|130|131|111|115|390|190|180|347|349|100|714|718)\b", t):
        puntos[TipoDocumento.MODELO_PRESENTADO] += 12
    if re.search(r"\bmodelo\s+(?:036|037)\b", t + " " + nombre):
        puntos[TipoDocumento.MODELO_036_037] += 4
    # Los indicios fiscales explícitos prevalecen sobre recibo/importe/seguro del IBI.
    gasto = puntos[TipoDocumento.FACTURA_GASTO]
    if any(clave in t for clave in ("impuesto sobre bienes inmuebles", "valor catastral")):
        puntos[TipoDocumento.CATASTRO_IBI] = max(puntos[TipoDocumento.CATASTRO_IBI], gasto + 1)
    elif gasto >= 2 and puntos[TipoDocumento.CATASTRO_IBI] >= gasto:
        puntos[TipoDocumento.FACTURA_GASTO] = puntos[TipoDocumento.CATASTRO_IBI] + 1
    # 'parte vendedora' aparece también en una compra: por sí sola no prueba el rol.
    tipo = max(puntos, key=puntos.get)
    puntuacion = puntos[tipo]
    if puntuacion < 2:
        return TipoDocumento.DESCONOCIDO, 0.0
    segundo = sorted(puntos.values(), reverse=True)[1]
    confianza = min(0.98, 0.50 + puntuacion / 40 + min(0.15, (puntuacion - segundo) / 40))
    return tipo, round(confianza, 2)


def extraer_local(ruta: Path) -> Extraccion:
    """Lector local por patrones (sin internet). Respaldo de la IA."""
    ruta = Path(ruta)
    avisos_lector = []
    if ruta.suffix.lower() == ".pdf":
        texto, ocr = extraer_texto(ruta)
    else:
        from lectores_documentales import extraer_documento
        lectura = extraer_documento(ruta)
        texto, ocr, avisos_lector = lectura.texto, lectura.escaneado, lectura.avisos
    tipo, confianza = clasificar(texto, ruta.name)
    campos: dict[str, Any] = {}
    avisos = ([AVISO_OCR] if ocr else []) + avisos_lector
    titulares = []
    inquilinos = []
    referencia = _ref(texto)
    if referencia:
        campos["ref_catastral"] = referencia

    def texto_campo(campo, *etiquetas):
        valor = _dato(texto, *etiquetas)
        if valor:
            campos[campo] = valor

    def importe_campo(campo, *etiquetas):
        valor = _importe(_dato(texto, *etiquetas))
        if valor is not None:
            campos[campo] = valor

    def fecha_campo(campo, *etiquetas):
        valor = _fecha(_dato(texto, *etiquetas))
        if valor:
            campos[campo] = valor

    ejercicio = _entero(_dato(texto, "ejercicio", "año fiscal", "ejercicio fiscal", "tax year"))
    if ejercicio and 1900 <= ejercicio <= 9999:
        campos["ejercicio"] = ejercicio
    if tipo in (TipoDocumento.ESCRITURA_COMPRA, TipoDocumento.ESCRITURA_VENTA,
                TipoDocumento.CATASTRO_IBI, TipoDocumento.CONTRATO_ALQUILER, TipoDocumento.FACTURA_GASTO):
        texto_campo("direccion", "dirección", "dirección del inmueble", "domicilio del inmueble", "finca sita en")
    if tipo in (TipoDocumento.ESCRITURA_COMPRA, TipoDocumento.ESCRITURA_VENTA):
        fecha_campo("fecha_adquisicion" if tipo == TipoDocumento.ESCRITURA_COMPRA else "fecha_venta",
                    "fecha de adquisición" if tipo == TipoDocumento.ESCRITURA_COMPRA else "fecha de venta",
                    "fecha escritura", "fecha de escritura", "fecha")
        importe_campo("precio_escritura" if tipo == TipoDocumento.ESCRITURA_COMPRA else "precio_venta",
                      "precio de compra" if tipo == TipoDocumento.ESCRITURA_COMPRA else "precio de venta",
                      "precio escritura", "precio de compraventa", "precio")
        texto_campo("municipio", "municipio")
        texto_campo("provincia", "provincia")
        cp = _dato(texto, "cp", "código postal")
        m_cp = re.search(r"\b\d{5}\b", cp or "")
        if m_cp:
            campos["cp"] = m_cp.group()
        titular = _dato(texto, "titulares", "titular", "parte compradora", "compradores")
        if titular:
            titulares = [v.strip() for v in re.split(r"\s*(?:;|\s+y\s+)\s*", titular) if v.strip()]
        texto_campo("notario", "notario", "notaria autorizante")
        texto_campo("n_protocolo", "número de protocolo", "nº protocolo", "n protocolo", "protocolo")
        if tipo == TipoDocumento.ESCRITURA_COMPRA:
            campos["tipo_adquisicion"] = "Compraventa"
            importe_campo("valor_referencia", "valor de referencia", "valor referencia")
            importe_campo("itp_iva_ajd", "itp / iva-ajd", "itp", "iva-ajd", "impuesto adquisición")
            pct = _dato(texto, "% titularidad", "porcentaje de titularidad", "titularidad")
            p = _importe(pct)
            if p is not None:
                try:
                    campos["pct_titularidad"] = _normalizar(
                        Columna("pct_titularidad", "H", "pct"),
                        f"{p}%" if "%" in (pct or "") else p,
                    )
                except ValueError:
                    avisos.append("Porcentaje de titularidad fuera de rango: revísalo.")
            if "gananciales" in _normal(texto) or len(titulares) >= 2:
                campos.update(pct_titularidad=0.5, gananciales_forzado=1)
                avisos.append("Dos titulares o gananciales: se propone titularidad del 50 %; confirma el reparto.")
            if any(p in _normal(texto) for p in ("vende", "transmite", "parte vendedora")):
                avisos.append("Confirma si el cliente compra o vende: sin rol explícito se propone escritura de compra.")
        else:
            importe_campo("gastos_venta", "gastos de venta", "gastos venta")
    elif tipo in (TipoDocumento.CATASTRO_IBI, TipoDocumento.VALOR_REFERENCIA):
        if tipo == TipoDocumento.VALOR_REFERENCIA:
            importe_campo("valor_referencia", "valor de referencia", "valor referencia")
        else:
            for campo, etiquetas in {
                "valor_catastral": ("valor catastral total", "valor catastral"),
                "valor_suelo": ("valor del suelo", "valor suelo"),
                "valor_construccion": ("valor de construcción", "valor construcción", "valor construccion"),
                "ibi_ejercicio": ("cuota ibi", "importe ibi", "total a pagar", "cuota tributaria"),
                "tasa_basura": ("tasa de basuras", "tasa de basura", "recogida de residuos"),
            }.items():
                importe_campo(campo, *etiquetas)
            revision = _entero(_dato(texto, "año de revisión", "año revisión", "año última revisión"))
            if revision:
                campos["anio_revision"] = revision
            campos["fuente"] = "IBI Ayuntamiento" if "ibi" in _normal(texto) or "bienes inmuebles" in _normal(texto) else "Catastro"
    elif tipo == TipoDocumento.FACTURA_GASTO:
        normal = _normal(texto)
        texto_campo("concepto", "concepto", "descripción", "detalle del servicio")
        concepto = _normal(campos.get("concepto", ""))
        categoria = _dato(texto, "categoría", "categoria de gasto")
        campos["categoria"] = categoria_gasto(categoria or texto)
        # Estos términos no están todos en el normalizador común; se resuelven aquí.
        hipoteca = bool(re.search(r"\b(?:hipoteca\w*|prestamo hipotecario|intereses)\b", normal))
        if hipoteca:
            campos["categoria"] = categoria_gasto("intereses")
            campos["concepto"] = "Intereses de préstamo hipotecario"
        elif not categoria:
            pistas = (
                (r"\b(?:poliza|prima|seguro)\b", "seguro"),
                (r"\b(?:comunidad de propietarios|cuota de comunidad)\b", "comunidad"),
                (r"\b(?:reparacion|mantenimiento|conservacion)\b", "reparacion"),
                (r"\b(?:tasa de basuras?|recogida de residuos)\b", "basura"),
                (r"\b(?:honorarios|gestoria)\b", "servicios profesionales"),
                (r"\b(?:suministro\w*|kwh|m3|consumo|electricidad|gas natural|agua)\b", "suministros"),
            )
            for contenido in (concepto, normal):
                alias = next((alias for patron, alias in pistas if re.search(patron, contenido)), None)
                if alias:
                    campos["categoria"] = categoria_gasto(alias)
                    break
        if not campos.get("concepto"):
            for patron, descripcion in (
                (r"\bgas natural\b", "Gas"), (r"\b(?:agua|m3)\b", "Agua"),
                (r"\b(?:electricidad|kwh|luz)\b", "Luz"),
                (r"\bimpago\b", "Seguro de impago"),
                (r"\b(?:responsabilidad civil|rc)\b", "Seguro RC"),
                (r"\b(?:hogar|poliza|prima|seguro)\b", "Seguro del hogar"),
                (r"\bcomunidad\b", "Cuota de comunidad"),
                (r"\b(?:reparacion|mantenimiento)\b", "Reparación y mantenimiento"),
            ):
                if re.search(patron, normal):
                    campos["concepto"] = descripcion
                    break
            campos.setdefault("concepto", campos["categoria"])

        texto_campo("proveedor", "proveedor", "emisor", "razón social", "entidad emisora", "acreedor")
        patron_nif = rf"\b(?:[A-HJNP-SUVW]\d{{7}}[0-9A-J]|{PATRON_NIF})\b"
        # Un NIF genérico dentro del bloque Cliente pertenece al destinatario.
        # No convertir una factura recibida en emitida tomando ese NIF como emisor.
        bloque_receptor = re.search(r"(?im)^[ \t]*(?:cliente|destinatario|receptor|facturado a)[ \t]*[:=]", texto)
        texto_emisor = texto[:bloque_receptor.start()] if bloque_receptor else texto
        texto_receptor = texto[bloque_receptor.end():] if bloque_receptor else ""
        nif_etiquetado = (_dato(texto, "nif proveedor", "cif proveedor", "nif del proveedor",
                               "nif emisor", "cif emisor") or _dato(texto_emisor, "cif", "nif"))
        nif = re.search(patron_nif, nif_etiquetado or "", re.I)
        if not nif:
            nif = re.search(patron_nif, texto_emisor, re.I)
        if nif:
            campos["nif_proveedor"] = nif.group().upper()
        if not campos.get("proveedor"):
            # Muchas facturas empiezan por la razón social, sin etiqueta 'Proveedor'.
            for linea in texto.splitlines():
                linea = linea.strip()
                if linea and not re.search(r"\b(?:factura|recibo|fecha|nif|cif|referencia|cliente)\b", _normal(linea)):
                    campos["proveedor"] = linea
                    break
        numero = re.search(
            r"(?:^|\n)[ \t]*(?:n[º°o.]\s*(?:de\s+)?factura|numero de factura|"
            r"factura\s*(?:n[º°o.]|numero))[ \t]*[:=]?[ \t]*(?:\n[ \t]*)?([^\n]+)", normal)
        if numero:
            campos["n_factura"] = texto[numero.start(1):numero.end(1)].strip()
        fecha_campo("fecha", "fecha de factura", "fecha factura", "fecha de emisión", "fecha emisión", "fecha")
        fecha_campo("periodo_desde", "periodo desde", "período desde", "fecha inicio periodo", "desde")
        fecha_campo("periodo_hasta", "periodo hasta", "período hasta", "fecha fin periodo", "hasta")
        periodo = _dato(texto, "periodo de facturación", "período de facturación", "periodo", "período")
        fechas = re.findall(PATRON_FECHA, _normal(periodo or ""))
        if len(fechas) >= 2:
            for campo, valor in zip(("periodo_desde", "periodo_hasta"), fechas[:2]):
                iso = _fecha(valor)
                if iso:
                    campos.setdefault(campo, iso)

        def importe_factura(*etiquetas):
            # Prioridad por etiqueta; nunca confundir el 21 % con la cuota de IVA.
            for etiqueta in etiquetas:
                patron = rf"(?:^|\n)[ \t]*{re.escape(_normal(etiqueta))}(?=[ \t:=\n(]|$)[ \t]*"
                m = re.search(patron, normal)
                if not m:
                    continue
                valor = normal[m.end():].split("\n", 1)[0].strip()
                if not valor or valor in (":", "="):
                    valor = normal[m.end():].lstrip(" :=\t\n").split("\n", 1)[0]
                valor = re.sub(r"\(?\d+(?:[.,]\d+)?\s*%\)?", "", valor)
                # Un campo vacío seguido de otra etiqueta no es un importe.
                numero = re.match(rf"[ \t:=]*(?:eur|€)?[ \t]*({PATRON_IMPORTE})(?![\d.,])", valor)
                if numero:
                    return _importe(numero.group(1))
            return None

        for campo, etiquetas in {
            "base": ("base imponible", "base", "importe neto"),
            "iva": ("cuota de iva", "cuota iva", "importe iva", "iva"),
            "total": ("total factura", "importe total", "total a pagar", "total recibo", "total"),
            "retencion": ("retención irpf", "retencion irpf", "retención", "retencion", "irpf"),
        }.items():
            valor = importe_factura(*etiquetas)
            if valor is not None:
                if campo == "retencion":
                    # Una rectificativa invierte también la retención, aunque se imprima como descuento.
                    referencia = next((campos.get(k) for k in ('base', 'total', 'iva')
                                       if campos.get(k) is not None), 0)
                    valor = -abs(valor) if referencia < 0 else abs(valor)
                campos[campo] = valor
        for campo, patron in (("tipo_iva", r"\biva\s*\(?\s*(\d{1,2}(?:[.,]\d+)?)\s*%"),
                              ("tipo_retencion", r"\b(?:retencion|irpf)[^\n%]{0,20}?(\d{1,2}(?:[.,]\d+)?)\s*%")):
            m = re.search(patron, normal)
            if m:
                campos[campo] = round(float(m.group(1).replace(",", ".")) / 100, 4)
        texto_campo("receptor", "cliente", "destinatario", "receptor", "facturado a")
        nif_receptor = _dato(texto, "nif cliente", "cif cliente", "nif del cliente", "nif destinatario",
                             "nif receptor")
        if not nif_receptor and texto_receptor:
            nif_receptor = _dato(texto_receptor, "cif", "nif")
        m = re.search(patron_nif, nif_receptor or "", re.I)
        if m:
            campos["nif_receptor"] = m.group().upper()
        if hipoteca:
            intereses = importe_factura("importe de intereses", "intereses del periodo", "intereses")
            # _aplicar usa base como alternativa a total: tampoco debe contener la cuota.
            for campo in ("total", "base", "iva"):
                campos.pop(campo, None)
            if intereses is not None:
                campos["total"] = intereses
            else:
                avisos.append("Hipoteca sin importe de intereses identificado: no se propone total; revisa el recibo.")
    elif tipo == TipoDocumento.CONTRATO_ALQUILER:
        fecha_campo("fecha_inicio", "fecha inicio", "fecha de inicio", "inicio del arrendamiento", "desde")
        fecha_campo("fecha_fin_prevista", "fecha fin", "fecha de fin", "fecha fin prevista", "hasta")
        if not campos.get("fecha_fin_prevista") and campos.get("fecha_inicio"):
            duracion = _dato(texto, "duración", "plazo")
            m = re.search(r"(\d+)\s*(mes|ano)", _normal(duracion or ""))
            if m:
                from datetime import timedelta
                meses = int(m.group(1)) * (12 if m.group(2) == "ano" else 1)
                fin = _sumar_meses(a_fecha(campos["fecha_inicio"]), meses) - timedelta(days=1)
                campos["fecha_fin_prevista"] = fin.isoformat()
        importe_campo("renta_inicial", "renta mensual", "renta inicial", "renta", "renta anual")
        periodicidad = _dato(texto, "periodicidad")
        campos["periodicidad"] = next((p for p in ("Mensual", "Trimestral", "Semestral", "Anual")
                                            if _normal(p) in _normal(periodicidad or "")), "Mensual")
        if not periodicidad and _dato(texto, "renta anual"):
            campos["periodicidad"] = "Anual"
        importe_campo("fianza", "fianza")
        texto_campo("tipo", "tipo arrendamiento", "tipo de arrendamiento")
        campos.setdefault("tipo", "Vivienda")
        for linea in texto.splitlines():
            m = re.match(r"\s*(?:arrendatario|inquilino)(?:\s+\d+)?\s*:\s*(.+)", linea, re.I)
            if not m:
                continue
            partes = [p.strip() for p in m.group(1).split(";")]
            nif = re.search(PATRON_NIF, m.group(1), re.I)
            nombre = re.split(r",?\s*(?:NIF|NIE|DNI)\s*[: ]", partes[0], flags=re.I)[0].strip()
            inquilinos.append({"nombre": nombre, "documento": nif.group().upper() if nif else None,
                               "tipo_persona": "Física", "pais": partes[2] if len(partes) > 2 else None})
    elif tipo == TipoDocumento.MODELO_210:
        renta = _dato(texto, "tipo de renta", "tipo renta")
        m = re.search(r"\b(01|02|28|35)\b", renta or "")
        if m:
            campos["tipo_renta"] = m.group()
        for campo, etiquetas in {"base": ("base imponible", "base"), "cuota": ("cuota íntegra", "cuota"),
                                "pagado": ("importe ingresado", "pagado", "resultado a ingresar")}.items():
            importe_campo(campo, *etiquetas)
        texto_campo("nrc", "nrc")
    elif tipo == TipoDocumento.CERTIFICADO_RESIDENCIA:
        texto_campo("pais", "país de residencia fiscal", "país", "country", "residencia fiscal en")
        fecha_campo("fecha_certificado", "fecha certificado", "fecha de expedición", "fecha de emisión", "fecha")
        fecha_campo("valido_hasta", "válido hasta", "valid until")
        texto_campo("nif_extranjero", "nif extranjero", "tax identification number", "tin")
    elif tipo == TipoDocumento.MODELO_036_037:
        fecha_campo("fecha_alta", "fecha de alta", "fecha alta", "inicio actividad")
        texto_campo("epigrafe_iae", "epígrafe iae", "epígrafe", "iae")
        texto_campo("descripcion", "descripción actividad", "actividad", "descripción")
        modelos = _dato(texto, "modelos", "obligaciones tributarias")
        if modelos:
            campos["modelos"] = ",".join(dict.fromkeys(re.findall(r"\b\d{3}\b", modelos)))
        regimen_iva = regimen_iva_normalizado(_dato(texto, "régimen iva", "regimen iva", "régimen de iva"))
        if regimen_iva:
            campos["regimen_iva"] = regimen_iva
        regimen_irpf = regimen_irpf_normalizado(_dato(texto, "régimen irpf", "regimen irpf", "régimen de irpf",
                                                       "método de estimación", "estimación"))
        if regimen_irpf:
            campos["regimen_irpf"] = regimen_irpf
    elif tipo == TipoDocumento.MODELO_PRESENTADO:
        m = re.search(r"\bmodelo\s+(\d{3})\b", texto, re.I)
        if m:
            campos["modelo"] = m.group(1)
        importe_campo("importe", "importe ingresado", "importe", "resultado a ingresar")
        texto_campo("csv", "csv", "código seguro de verificación")

    if tipo in (TipoDocumento.MODELO_210, TipoDocumento.MODELO_PRESENTADO):
        periodo = _dato(texto, "período", "periodo")
        m = re.search(r"\b([1-4]T|0A|anual)\b", periodo or "", re.I)
        if m:
            campos["periodo"] = "0A" if m.group().lower() == "anual" else m.group().upper()
        fecha_campo("fecha_presentacion", "fecha presentación", "fecha de presentación", "presentado el")
    if tipo == TipoDocumento.DESCONOCIDO:
        avisos.append("No se reconoce el tipo de documento: selecciona el tipo y revisa los campos.")
    return Extraccion(tipo=tipo, confianza=confianza, campos=campos, inquilinos=inquilinos, titulares=titulares,
                      avisos=avisos, necesita_ocr=ocr, texto=texto, nombre_archivo=ruta.name)


def regimen_iva_normalizado(texto) -> str | None:
    t = _normal(texto or "")
    for clave, valor in (("recargo", "Recargo de equivalencia"), ("exent", "Exento"),
                         ("simplificado", "Simplificado"), ("general", "General")):
        if clave in t:
            return valor
    return None


def regimen_irpf_normalizado(texto) -> str | None:
    t = _normal(texto or "")
    for clave, valor in (("simplificada", "Directa simplificada"), ("normal", "Directa normal"),
                         ("objetiva", "Objetiva"), ("modulos", "Objetiva")):
        if clave in t:
            return valor
    return None


_CATEGORIAS_RESIDENTE = (
    (r"\b(?:arrendamiento|alquiler)\b.*\b(?:local|oficina|nave|despacho)\b|\b(?:local|oficina)\b.*\balquiler\b",
     "Alquiler local"),
    (r"\b(?:seguridad social|reta|cuota de autonomos?|tesoreria general)\b", "Seguridad Social"),
    (r"\b(?:material de oficina|papeleria|toner|consumibles)\b", "Material de oficina"),
    (r"\b(?:publicidad|marketing|anuncio)\b", "Publicidad"),
    (r"\b(?:comision\w*|mantenimiento de cuenta|bancari\w+)\b", "Comisiones bancarias"),
    (r"\b(?:taxi|tren|vuelo|billete|hotel|combustible|gasolina|gasoil|peaje|parking)\b", "Viajes y transporte"),
    (r"\b(?:honorarios|asesoria|gestoria|abogad\w*|notari\w*|consultoria)\b", "Servicios profesionales"),
    (r"\b(?:poliza|prima|seguro)\b", "Seguros"),
    (r"\b(?:reparacion|mantenimiento|conservacion)\b", "Reparación/conservación"),
    (r"\b(?:electricidad|kwh|luz|agua|gas natural|telefon\w*|internet|fibra|movil)\b", "Suministros"),
    (r"\b(?:mercancia|compra|existencias|materia prima|pedido)\b", "Compras"),
)


def categoria_residente(c: dict, texto: str) -> str:
    """Categoría de gasto de autónomo/pyme a partir del concepto y del texto de la factura."""
    for contenido in (_normal(str(c.get("concepto") or "")), _normal(texto or "")):
        for patron, categoria in _CATEGORIAS_RESIDENTE:
            if re.search(patron, contenido):
                return categoria
    return "Otros"


def _mismo_nif(a, b) -> bool:
    def limpiar(v):
        normal = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
        # ES solo es el prefijo VAT cuando lo sigue un NIF/NIE español de 9 caracteres.
        return normal[2:] if re.fullmatch(r"ES[A-Z0-9]{9}", normal) else normal
    return bool(limpiar(a)) and limpiar(a) == limpiar(b)


def factura_residente(cliente: dict, c: dict, texto: str, huella: str, ejercicio: int | None = None) -> tuple[str, dict, str]:
    """(tabla, datos, clave) de una factura de autónomo/pyme. Emitida si el emisor es el propio cliente."""
    nombre = _normal(f"{cliente.get('nombre', '')} {cliente.get('apellidos', '')}".strip())
    emisor = _normal(str(c.get("proveedor") or ""))
    emitida = (_mismo_nif(c.get("nif_proveedor"), cliente.get("nif"))
               or (bool(nombre) and nombre in emisor)) and not _mismo_nif(c.get("nif_receptor"), cliente.get("nif"))
    base, iva, total = c.get("base"), c.get("iva"), c.get("total")
    retencion, tipo_iva = c.get("retencion"), c.get("tipo_iva")
    referencia_signo = next((v for v in (base, total, iva) if v not in (None, '')), 0)
    if float(referencia_signo) < 0 and retencion not in (None, ''):
        retencion = -abs(float(retencion))
    if base in (None, "") and total not in (None, ""):
        base = round(float(total) - float(iva or 0) + float(retencion or 0), 2)
    if iva in (None, "") and base not in (None, "") and tipo_iva not in (None, ""):
        iva = round(float(base) * float(tipo_iva), 2)
    if tipo_iva in (None, "") and base not in (None, "", 0) and iva not in (None, ""):
        tipo_iva = round(float(iva) / float(base), 4) if float(base) else None
    comunes = {"fecha": c.get("fecha"), "numero": c.get("n_factura"), "concepto": c.get("concepto"),
               "base": base, "tipo_iva": tipo_iva, "cuota_iva": iva, "tipo_retencion": c.get("tipo_retencion"),
               "retencion": retencion}
    fecha = a_fecha(c.get('fecha'))
    anio_factura = fecha.year if fecha else ejercicio or c.get('ejercicio')
    if emitida:
        datos = {**comunes, "receptor": c.get("receptor"), "nif_receptor": c.get("nif_receptor"),
                 "estado_cobro": "Pendiente"}
        ident = "|".join(str(x or "") for x in (c.get("n_factura"), c.get("nif_receptor")))
        clave = f"FE|{anio_factura}|{ident}" if c.get("n_factura") and anio_factura else f"FE|{huella[:16]}"
        return "facturas_emitidas", datos, clave
    datos = {**comunes, "proveedor": c.get("proveedor"), "nif_proveedor": c.get("nif_proveedor"),
             "categoria": categoria_residente(c, texto), "pct_iva_deducible": 1, "pct_deducible_irpf": 1}
    ident = "|".join(str(x or "") for x in (c.get("nif_proveedor") or c.get("proveedor"), c.get("n_factura")))
    clave = f"FR|{anio_factura}|{ident}" if c.get("n_factura") and anio_factura else f"FR|{huella[:16]}"
    return "facturas_recibidas", datos, clave


def _hash(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


_archivos_nuevos: ContextVar[list | None] = ContextVar("archivos_importacion", default=None)


@contextmanager
def archivos_importacion():
    """Revierte únicamente las copias creadas por este lote, nunca soportes reutilizados."""
    if _archivos_nuevos.get() is not None:
        yield
        return
    creados = []
    token = _archivos_nuevos.set(creados)
    try:
        yield
    except BaseException as exc:
        pendientes = []
        for ruta, carpeta, huella in reversed(creados):
            try:
                segura = sanitize_safe_path(carpeta, ruta)
                if ruta.is_symlink() or segura != ruta:
                    raise ValueError("El soporte cambió de destino.")
                if ruta.exists():
                    if _hash(ruta) != huella:
                        raise ValueError("El soporte fue modificado por otro proceso.")
                    ruta.unlink()
            except (OSError, ValueError):
                pendientes.append(ruta.name)
        if pendientes:
            raise RuntimeError(f"{exc} Los datos se han revertido, pero no se pudo retirar "
                               "una copia de soporte; revisa Soportes_PDF antes de reintentar.") from exc
        raise
    finally:
        _archivos_nuevos.reset(token)


def archivar_pdf(ruta: Path, carpeta_cliente: Path, tipo: TipoDocumento, fecha: date | None,
                 huella_esperada: str | None = None) -> tuple[str, str]:
    ruta = Path(ruta)
    carpeta_cliente = sanitize_safe_path(carpeta_cliente, Path(carpeta_cliente).absolute())
    soportes = subcarpeta_cliente(carpeta_cliente)
    soportes.mkdir(parents=True, exist_ok=True)
    extension = ruta.suffix.lower()
    if extension not in {".pdf", ".xlsx", ".xls", ".docx", ".csv"}:
        raise ValueError("Formato documental no admitido.")
    huella, tamano = _hash(ruta), ruta.stat().st_size
    if huella_esperada is not None and huella != huella_esperada:
        raise ValueError("El PDF cambió después de revisarlo. Vuelve a seleccionarlo y comprueba sus datos.")
    # Mismo contenido renombrado también reutiliza el archivo, sin huérfanos.
    for previo in sorted(soportes.glob("*" )):
        # Los soportes nuevos llevan una huella en su nombre. No releer todos los PDFs
        # anteriores por cada factura; los nombres legados se siguen verificando.
        marca = re.search(r"__h_([a-f0-9]{16})(?:_\d+)?\.[^.]+$", previo.name)
        if marca and marca[1] != huella[:16]:
            continue
        try:
            previo = sanitize_safe_path(soportes, previo)
        except ValueError:
            continue
        if not previo.is_file():
            continue
        if previo.stat().st_size == tamano and _hash(previo) == huella:
            return previo.relative_to(carpeta_cliente).as_posix(), huella
    # Deja margen para extensión y sufijo de colisión bajo MAX_PATH en Windows.
    limite = min(76, 248 - len(str(soportes)) - 1 - len("_9999" + extension))
    if limite < 20:
        raise ValueError("La ruta del expediente es demasiado larga: elige una carpeta base más corta.")
    texto_base = f"{(fecha or date.today()).isoformat()}_{tipo.value}_{componente_ruta(ruta.stem)}"
    if limite >= 32:
        base = componente_ruta(texto_base, limite - 20) + "__h_" + huella[:16]
    else:
        base = "__h_" + huella[:16]
    destino = soportes / f"{base}{extension}"
    numero = 2
    while True:
        destino = sanitize_safe_path(soportes, destino)
        try:
            salida = destino.open("xb")  # exclusivo: no sigue enlaces ni pisa otra importación
            break
        except FileExistsError:
            destino = soportes / f"{base}_{numero}{extension}"
            numero += 1
    try:
        with salida, ruta.open("rb") as origen:
            shutil.copyfileobj(origen, salida, length=1024 * 1024)
        if _hash(destino) != huella:
            raise ValueError("El PDF cambió mientras se archivaba. Vuelve a seleccionarlo y revisa su contenido.")
        shutil.copystat(ruta, destino)
    except Exception:
        destino.unlink(missing_ok=True)  # solo el archivo que acaba de crear esta llamada
        raise
    if _archivos_nuevos.get() is not None:
        _archivos_nuevos.get().append((destino, soportes, huella))
    return destino.relative_to(carpeta_cliente).as_posix(), huella


_CATEGORIAS_ALIAS = {
    "ibi": "IBI", "impuesto sobre bienes inmuebles": "IBI", "comunidad": "Comunidad", "seguro": "Seguro",
    "reparacion": "Reparación/conservación", "conservacion": "Reparación/conservación",
    "mantenimiento": "Reparación/conservación", "administracion": "Administración", "agencia": "Agencia",
    "suministros": "Suministros", "luz": "Suministros", "electricidad": "Suministros", "agua": "Suministros",
    "gas": "Suministros", "intereses": "Intereses/financiación", "hipoteca": "Intereses/financiación",
    "financiacion": "Intereses/financiación", "amortizacion": "Amortización",
    "servicios profesionales": "Servicios profesionales", "gestoria": "Servicios profesionales",
    "abogado": "Servicios profesionales", "basura": "Tasa/basura", "tasa": "Tasa/basura",
}


def categoria_gasto(valor) -> str:
    """Normaliza a una categoría de la lista de la plantilla (08_GASTOS!H). Desconocida -> 'Otros'."""
    from database import CATEGORIAS_GASTO
    texto = _normal(str(valor or ""))
    for cat in CATEGORIAS_GASTO:
        if texto == _normal(cat):
            return cat
    for clave, cat in _CATEGORIAS_ALIAS.items():
        if clave in texto:
            return cat
    return "Otros"


def _permitidos(tabla: str, datos: dict) -> dict:
    campos = {c.campo for c in TABLAS[tabla].columnas_bd}
    return {k: v for k, v in datos.items() if k in campos and k != "codigo"}


def _direccion(texto: str) -> str:
    texto = _normal(texto)
    texto = re.sub(r"\bc[/.]\s*", "calle ", texto)
    texto = re.sub(r"\b(?:avda|av)\.?(?=\s)", "avenida", texto)
    return re.sub(r"[^a-z0-9]", "", texto)


def _inmueble(db, cid, campos, destino=None):
    referencia = re.sub(r"\s", "", str(campos.get("ref_catastral") or "")).upper()
    inmuebles = db.listar("inmuebles", cid)
    if referencia:
        encontrados = [r for r in inmuebles if re.sub(r"\s", "", r.get("ref_catastral") or "").upper() == referencia]
        if len(encontrados) == 1:
            return encontrados[0]["codigo"]
    direccion = _direccion(campos.get("direccion") or "")
    if len(direccion) >= 8:
        encontrados = [r for r in inmuebles if direccion == _direccion(r.get("direccion") or "")]
        if len(encontrados) == 1:
            return encontrados[0]["codigo"]
    if destino and db.get("inmuebles", cid, destino):
        return destino
    return None


def aplicar(db, cid, ext: Extraccion, ruta_pdf: Path, inmueble_destino: str | None = None,
            ejercicio: int | None = None, huella_esperada: str | None = None) -> ResultadoAplicacion:
    """Aplica todos los datos en un lote; un fallo permite reintentar sin altas parciales.

    Un fallo revierte la BD y las copias nuevas. Los soportes anteriores se conservan.
    El controlador mantiene su bloqueo durante esta operación.
    """
    with archivos_importacion(), db.transaccion():
        from documentos import origen
        with origen(db, cid):
            return _aplicar(db, cid, ext, ruta_pdf, inmueble_destino, ejercicio, huella_esperada=huella_esperada)

def _aplicar(db, cid, ext: Extraccion, ruta_pdf: Path, inmueble_destino: str | None = None,
             ejercicio: int | None = None, documento_existente: str | None = None,
             huella_esperada: str | None = None, factura_existente: tuple[str, str] | None = None) -> ResultadoAplicacion:
    cliente = db.get_cliente(cid)
    if cliente is None:
        raise ValueError(f"Cliente inexistente: {cid}")
    c = dict(ext.campos)
    tipo = ext.tipo
    if ext.registros:
        from ia_extractor import CAMPOS_REGISTRO
        permitidas = ({"catastro"} if tipo == TipoDocumento.ESCRITURA_COMPRA else
                      {"cobros", "gastos"} if tipo == TipoDocumento.RESUMEN_ALQUILER else set())
        if any(r.tabla not in permitidas or set(r.campos) - CAMPOS_REGISTRO[r.tabla] for r in ext.registros):
            raise ValueError("El documento contiene tablas o campos incompatibles con su tipo.")
    if tipo == TipoDocumento.FACTURA_GASTO:
        identificados = [c.get("nif_proveedor"), c.get("nif_receptor")]
        if all(identificados) and not any(_mismo_nif(nif, cliente.get("nif")) for nif in identificados):
            raise ValueError("La factura identifica a otras personas y no corresponde al NIF de este cliente. "
                             "Corrige la extracción o súbela al expediente correcto.")
        if not all(identificados):
            ext.avisos = list(ext.avisos) + ["Factura con identificación incompleta: comprueba emisor, destinatario y expediente antes de declarar."]
    if c.get("ref_catastral"):
        c["ref_catastral"] = re.sub(r"\s", "", str(c["ref_catastral"])).upper()
    fechas = ("fecha_adquisicion", "fecha_venta", "fecha_inicio", "fecha_presentacion", "fecha_certificado", "fecha_alta",
              "fecha", "periodo_desde")
    fecha_documento = next((a_fecha(c[f]) for f in fechas if c.get(f)), None)
    if tipo == TipoDocumento.CATASTRO_IBI and not c.get("ejercicio"):
        raise ValueError("Catastro / IBI necesita un ejercicio explícito: comprueba el año del valor en el PDF.")
    ej = int(c.get("ejercicio") or ejercicio or (fecha_documento.year if fecha_documento else date.today().year))
    if tipo == TipoDocumento.FACTURA_GASTO and fecha_documento:
        if c.get("ejercicio") and int(c["ejercicio"]) != fecha_documento.year:
            raise ValueError("El ejercicio de la factura no coincide con su fecha. Revisa ambos datos antes de guardarla.")
        # La selección de la pantalla no cambia la fecha fiscal de una factura histórica.
        ej = fecha_documento.year
    # Valida el ejercicio antes de archivar; una corrección inválida no deja un alta parcial.
    date(ej, 1, 1)
    inmueble = _inmueble(db, cid, c, inmueble_destino or c.get("inmueble"))
    # Evitar crear otra copia de un soporte retirado antes de informar cómo restaurarlo.
    huella = _hash(Path(ruta_pdf))
    if huella_esperada is not None and huella != huella_esperada:
        raise ValueError("El PDF cambió después de revisarlo. Vuelve a seleccionarlo y comprueba sus datos.")
    retirado = db.con.execute("SELECT d.codigo FROM documentos d JOIN documentos_gestion g "
        "ON g.cliente_id=d.cliente_id AND g.codigo=d.codigo "
        "WHERE d.cliente_id=? AND d.sha256=? AND g.en_papelera=1", (cid, huella)).fetchone()
    if retirado:
        raise ValueError("Este PDF está en la papelera. Restáuralo explícitamente antes de volver a importarlo.")
    if documento_existente:
        previo = db.get("documentos", cid, documento_existente)
        if not previo or previo.get("sha256") not in (None, "", huella):
            raise ValueError("El PDF no corresponde al documento que se está corrigiendo.")
        relativo, documento, nuevo = previo["ruta"], documento_existente, True
    else:
        relativo, huella = archivar_pdf(Path(ruta_pdf), Path(cliente["carpeta"]), tipo, fecha_documento,
                                       huella_esperada=huella_esperada or huella)
        documento, nuevo = db.registrar_documento(cid, relativo, TIPOS_LEGIBLES[tipo], huella,
                                             Path(ruta_pdf).name, inmueble=inmueble, ejercicio=ej,
                                             fecha_documento=fecha_documento,
                                             descripcion=ext.nombre_archivo, comentario_guia=ext.comentario_guia,
                                             observaciones="; ".join(ext.avisos) or None)
    if not nuevo:
        previo = db.get("documentos", cid, documento)
        return ResultadoAplicacion(documento=documento, duplicado=True, ruta_relativa=previo["ruta"],
                                   mensajes=[f"Ya archivado como {documento}"])
    from documentos import vincular_importacion
    vincular_importacion(db, cid, documento, ext)
    codigos = {}
    afectados: set[int] = set()
    mensajes = list(ext.avisos)
    contrato = None

    if tipo == TipoDocumento.ESCRITURA_COMPRA:
        datos = _permitidos("inmuebles", c)
        inferir_gananciales = "gananciales_forzado" not in c and (
            len(ext.titulares) >= 2 or "gananciales" in _normal(ext.texto))
        if c.get("gananciales_forzado") or inferir_gananciales:
            datos.update(pct_titularidad=0.5, gananciales_forzado=1)
            if not any("50 %" in m for m in mensajes):
                mensajes.append("Gananciales o dos titulares: se aplica el 50 % de titularidad propuesto.")
        if inmueble:
            datos["codigo"] = inmueble
        datos["carpeta"] = relativo
        # Un estado puesto a mano (Vendido, Baja…) no lo pisa otro PDF (A4-P06); "En revisión" sí pasa a Activo
        previo = db.get("inmuebles", cid, inmueble) if inmueble else None
        if not previo or previo.get("estado") in (None, "", "En revisión"):
            datos["estado"] = "Activo"
        inmueble = db.upsert("inmuebles", cid, datos)
        afectados.add((a_fecha(c.get("fecha_adquisicion")) or date(ej, 1, 1)).year)
        # Un anexo acreditado conserva su propio ejercicio, no el abierto en pantalla.
        referencia = re.sub(r"\s", "", str((db.get("inmuebles", cid, inmueble) or {}).get("ref_catastral") or "")).upper()
        vistos = set()
        for numero, registro in enumerate(ext.registros, 1):
            anexo = dict(registro.campos)
            ref_anexo = re.sub(r"\s", "", str(anexo.pop("ref_catastral", ""))).upper()
            anio = anexo.get("ejercicio")
            if not ref_anexo or ref_anexo != referencia or anio is None:
                raise ValueError("El anexo catastral necesita referencia del inmueble y ejercicio acreditados.")
            anio = int(anio)
            date(anio, 1, 1)
            if anio in vistos or not any(anexo.get(k) is not None for k in
                                      ("valor_catastral", "valor_suelo", "valor_construccion")):
                raise ValueError("El anexo catastral está repetido o no contiene valores acreditados.")
            valores = [a_num(anexo.get(k)) for k in ("valor_catastral", "valor_suelo", "valor_construccion")]
            for campo, valor in zip(("valor_catastral", "valor_suelo", "valor_construccion"), valores):
                if campo in anexo:
                    anexo[campo] = valor
            if any(v is not None and v < 0 for v in valores):
                raise ValueError("El anexo catastral contiene un valor negativo.")
            if valores[0] is not None and any(v is not None and v > valores[0] + .02 for v in valores[1:]):
                raise ValueError("Un componente catastral supera el total. Revisa el anexo.")
            if all(v is not None for v in valores) and abs(valores[0] - valores[1] - valores[2]) > .02:
                raise ValueError("El total catastral no coincide con suelo y construcción. Revisa el anexo.")
            if not revision_acreditada(anexo.get("anio_revision"), anio, registro.evidencia, ext.texto):
                anexo.pop("anio_revision", None)
                anexo["revision_pendiente"] = 1
                mensajes.append(f"Catastro {anio}: {PENDIENTE_REVISION}. El año del valor no acredita la revisión.")
            vistos.add(anio)
            anexo.update(ejercicio=anio, inmueble=inmueble, ruta=relativo,
                         observaciones="Anexo de escritura · " + registro.evidencia +
                         ("; " + PENDIENTE_REVISION if not anexo.get("anio_revision") else ""))
            previo_cat = db.get_por_clave("catastro", cid, f"CAT|{inmueble}|{anio}")
            financieros = ("valor_catastral", "valor_suelo", "valor_construccion")
            if (previo_cat and any(previo_cat.get(k) is not None for k in financieros)
                    and any(k in anexo and previo_cat.get(k) != anexo[k] for k in financieros)):
                mensajes.append(f"Catastro {anio}: se conservan los valores existentes; revisa la diferencia con el anexo.")
                # No mezclar el total anterior con componentes de otra valoración.
                codigos[f"catastro_{numero}"] = previo_cat["codigo"]
                afectados.add(anio)
                continue
            if previo_cat and anexo.get("revision_pendiente"):
                notas = str(previo_cat.get("observaciones") or "")
                db.upsert("catastro", cid, {"codigo": previo_cat["codigo"], "revision_pendiente": 1,
                    "observaciones": notas if PENDIENTE_REVISION in notas else "; ".join(
                        filter(None, [notas, PENDIENTE_REVISION]))})
            codigos[f"catastro_{numero}"] = db.upsert("catastro", cid, anexo, clave=f"CAT|{inmueble}|{anio}",
                                                     solo_si_vacio=tuple(anexo))
            afectados.add(anio)
    elif tipo == TipoDocumento.ESCRITURA_VENTA:
        if inmueble:
            db.upsert("inmuebles", cid, {"codigo": inmueble, "estado": "Vendido",
                                       **{k: c[k] for k in ("fecha_venta", "precio_venta", "gastos_venta") if k in c}})
            afectados.add((a_fecha(c.get("fecha_venta")) or date(ej, 1, 1)).year)
        else:
            mensajes.append("Venta sin inmueble identificado: selecciona el inmueble; no se crea uno nuevo.")
    elif tipo in (TipoDocumento.CATASTRO_IBI, TipoDocumento.VALOR_REFERENCIA):
        if not inmueble:
            inmueble = db.upsert("inmuebles", cid, {"ref_catastral": c.get("ref_catastral"),
                                                   "direccion": c.get("direccion"), "estado": "En revisión"})
            mensajes.append("Se creó un inmueble mínimo En revisión: completa los datos de adquisición y titularidad.")
        datos = _permitidos("catastro", c)
        previo_cat = db.get_por_clave("catastro", cid, f"CAT|{inmueble}|{ej}") or {}
        revision_previa = (not previo_cat.get('revision_pendiente') and
            any(previo_cat.get(k) not in (None, '') for k in ('anio_revision', 'pct_imputacion')))
        if tipo == TipoDocumento.CATASTRO_IBI and not revision_acreditada(
                c.get("anio_revision"), ej, ext.evidencias.get("anio_revision"), ext.texto, ext.revision_confirmada):
            datos.pop("anio_revision", None)
            if revision_previa:
                datos.pop('revision_pendiente', None)
                mensajes.append('Se conserva la revisión catastral o el porcentaje ya confirmado por el asesor.')
            else:
                datos["revision_pendiente"] = 1
                notas = str(previo_cat.get("observaciones") or datos.get("observaciones") or "")
                datos["observaciones"] = notas if PENDIENTE_REVISION in notas else "; ".join(
                    filter(None, [notas, PENDIENTE_REVISION]))
                mensajes.append(PENDIENTE_REVISION + ". Cálculo provisional; revisa Catastro antes de presentar.")
        datos.update(ejercicio=ej, inmueble=inmueble, ruta=relativo)
        financieros = ('valor_catastral', 'valor_suelo', 'valor_construccion', 'valor_referencia')
        conflicto = any(previo_cat.get(k) not in (None, '') and datos.get(k) not in (None, '')
                        and a_num(previo_cat[k]) != a_num(datos[k]) for k in financieros)
        combinados = [a_num(datos.get(k) if datos.get(k) not in (None, '') else previo_cat.get(k))
                      for k in financieros[:3]]
        incoherente = all(v is not None for v in combinados) and abs(combinados[0] - combinados[1] - combinados[2]) > .02
        if previo_cat and (conflicto or incoherente):
            codigo = previo_cat['codigo']
            mensajes.append(f'Catastro {ej}: se conservan los valores existentes; revisa la diferencia con el documento. '
                            'No se mezclan valoraciones ni se sobrescriben datos confirmados.')
        else:
            if incoherente:
                raise ValueError('El total catastral no coincide con suelo y construcción. Revisa el documento.')
            protegidos = ('pct_imputacion', 'anio_revision') if revision_previa else ('pct_imputacion',)
            codigo = db.upsert("catastro", cid, datos, clave=f"CAT|{inmueble}|{ej}", solo_si_vacio=protegidos)
        codigos["catastro"] = codigo
        if tipo == TipoDocumento.CATASTRO_IBI and c.get("ibi_ejercicio") is not None:
            codigos["gasto"] = db.upsert("gastos", cid, {
                "ejercicio": ej, "inmueble": inmueble, "categoria": "IBI", "total": c["ibi_ejercicio"],
                "periodo_desde": date(ej, 1, 1), "periodo_hasta": date(ej, 12, 31),
                "origen": "pdf", "incluido_210": "Pendiente", "ruta": relativo,
            }, clave=f"IBI|{inmueble}|{ej}", solo_si_vacio=("incluido_210",))
        if tipo == TipoDocumento.CATASTRO_IBI and c.get("tasa_basura") not in (None, "", 0):
            codigos["gasto_basura"] = db.upsert("gastos", cid, {
                "ejercicio": ej, "inmueble": inmueble, "categoria": "Tasa/basura", "total": c["tasa_basura"],
                "periodo_desde": date(ej, 1, 1), "periodo_hasta": date(ej, 12, 31),
                "origen": "pdf", "incluido_210": "Pendiente", "ruta": relativo,
            }, clave=f"BASURA|{inmueble}|{ej}", solo_si_vacio=("incluido_210",))
        afectados.add(ej)
    elif tipo == TipoDocumento.RESOLUCION_ALQUILER:
        inicio = a_fecha(c.get("fecha_inicio"))
        fin = a_fecha(c.get("fecha_fin_real"))
        candidatos = [fila for fila in db.listar("contratos", cid)
                      if fila.get("inmueble") == inmueble and a_fecha(fila.get("fecha_inicio")) == inicio]
        if not inmueble or not inicio or not fin or fin < inicio or len(candidatos) != 1:
            raise ValueError("La resolución necesita inmueble, inicio y fin válidos de un contrato único.")
        contrato = candidatos[0]["codigo"]
        anterior_fin = a_fecha(candidatos[0].get("fecha_fin_real"))
        if anterior_fin is not None and anterior_fin != fin:
            raise ValueError("El contrato tiene otra fecha de fin corregida. Revisa el conflicto antes de cambiarla.")
        db.upsert("contratos", cid, {"codigo": contrato, "fecha_fin_real": fin, "estado": "Rescindido anticipadamente"})
        codigos["contrato"] = contrato
        afectados.update(range(inicio.year, max(fin.year, ej) + 1))
        mensajes.append("Resolución revisada: fecha de fin real aplicada al contrato identificado.")
    elif tipo == TipoDocumento.RESUMEN_ALQUILER:
        from ia_extractor import CAMPOS_REGISTRO
        if ext.registros and not inmueble:
            raise ValueError("Selecciona el inmueble para registrar los movimientos de la tabla.")
        claves_filas = set()
        for numero, registro in enumerate(ext.registros, 1):
            tabla = registro.tabla
            if set(registro.campos) - CAMPOS_REGISTRO[tabla]:
                raise ValueError("La tabla contiene campos no permitidos.")
            datos = _permitidos(tabla, registro.campos)
            if tabla == "cobros" and datos.get("estado") not in (None, *ESTADOS_COBRO):
                # La IA puede describir el estado con texto libre («NO», «Contradictorio…»): no es un estado válido
                estado = datos.pop("estado")
                if str(estado).strip():
                    mensajes.append(f"Fila {numero}: estado «{estado}» no válido; queda Pendiente.")
            desde = a_fecha(datos.get("periodo_desde"))
            hasta = a_fecha(datos.get("periodo_hasta"))
            fecha_fila = a_fecha(datos.get("fecha_cobro") if tabla == "cobros" else datos.get("fecha"))
            if tabla == "cobros" and not desde and not hasta:
                if not fecha_fila and not datos.get("periodo_desde") and not datos.get("periodo_hasta"):
                    # Total sin fecha ni periodo (p. ej. «cobrado en 2025» de un Excel del cliente): no es un movimiento
                    # registrable, pero no debe tumbar el documento entero.
                    importe = datos.get("importe_cobrado")
                    mensajes.append(f"Fila {numero}: cobro{f' de {importe:.2f} €' if importe else ''} sin fecha ni periodo; "
                                    "no se registra. Contrástalo con el extracto o anótalo a mano.")
                    continue
                if not fecha_fila or datos.get("periodo_desde") or datos.get("periodo_hasta"):
                    raise ValueError("Un cobro sin periodo necesita una fecha de cobro válida.")
                ejercicio_fila = int(datos.get("ejercicio") or fecha_fila.year)
                if ejercicio_fila != fecha_fila.year:
                    raise ValueError("El ejercicio del cobro no coincide con su fecha.")
                if datos.get("importe_cobrado") is None:
                    raise ValueError("Cada movimiento necesita un importe explícito.")
                datos.update(ejercicio=ejercicio_fila, inmueble=inmueble, ruta=relativo, estado="Pendiente",
                             observaciones="Pendiente de asignar periodo de devengo y contrato. "
                             + (datos.get("observaciones") or "") + " · Fuente: " + registro.evidencia)
                codigos[f"fila_{numero}"] = db.upsert("cobros", cid, datos, clave=f"DOCFILA|{documento}|{numero}")
                afectados.add(ejercicio_fila)
                mensajes.append(f"Cobro {numero} guardado pendiente de asignar periodo; no modifica la renta devengada.")
                continue
            if not desde or not hasta or desde > hasta or desde.year != hasta.year:
                raise ValueError("Cada fila necesita un periodo válido dentro del mismo ejercicio. Revísalo.")
            ejercicio_fila = int(datos.get("ejercicio") or desde.year)
            if ejercicio_fila != desde.year:
                raise ValueError("El ejercicio de una fila no coincide con su periodo.")
            importe = datos.get("importe_cobrado" if tabla == "cobros" else "total")
            if importe is None:
                raise ValueError("Cada movimiento necesita un importe explícito.")
            datos.update(ejercicio=ejercicio_fila, inmueble=inmueble, ruta=relativo)
            datos["observaciones"] = ((datos.get("observaciones") or "") + " · Fuente: " + registro.evidencia).strip(" ·")
            if tabla == "cobros":
                candidatos = [r for r in db.listar("contratos", cid) if r.get("inmueble") == inmueble
                    and a_fecha(r.get("fecha_inicio")) and a_fecha(r["fecha_inicio"]) <= hasta
                    and (a_fecha(r.get("fecha_fin_real") or r.get("fecha_fin_prevista")) or date.max) >= desde]
                if len(candidatos) != 1:
                    raise ValueError("No hay un contrato único para este cobro. Revisa contrato y periodo.")
                contrato_fila = candidatos[0]["codigo"]
                datos["contrato"] = contrato_fila
                # Una fila mensual ya generada admite el cobrado; jamás cambiar su renta devengada.
                clave = (f"COB|{contrato_fila}|{desde.year}-{desde.month:02d}" if desde.day == 1
                         and hasta == date(desde.year, desde.month, calendar.monthrange(desde.year, desde.month)[1])
                         else f"DOCFILA|{documento}|{numero}")
                previo = db.get_por_clave("cobros", cid, clave)
                if previo and previo.get("importe_cobrado") is not None and previo.get("ruta") != relativo:
                    raise ValueError("Ya hay un cobro para ese periodo con otro soporte. Revisa la diferencia antes de sustituirlo.")
            else:
                clave = f"DOCFILA|{documento}|{numero}"
                if datos.get("n_factura"):
                    anteriores = db.con.execute("SELECT * FROM gastos WHERE cliente_id=? AND n_factura=?",
                                               (cid, datos["n_factura"])).fetchall()
                    for anterior in anteriores:
                        previo_gasto = dict(anterior)
                        if previo_gasto.get("clave") == clave:
                            continue  # Revisión del mismo documento, no una segunda factura.
                        proveedor = datos.get("nif_proveedor") or datos.get("proveedor")
                        otro_proveedor = previo_gasto.get("nif_proveedor") or previo_gasto.get("proveedor")
                        if (not proveedor or not otro_proveedor or _normal(proveedor) == _normal(otro_proveedor)):
                            raise ValueError("Esa factura ya consta en otro registro. Revisa los soportes antes de duplicarla.")
                datos["categoria"] = categoria_gasto(datos.get("categoria"))
                datos.update(origen="documento", incluido_210="Pendiente")
            if (tabla, clave) in claves_filas:
                raise ValueError("Hay varios cobros para el mismo mes en este documento. Revisa el desglose antes de guardarlos.")
            claves_filas.add((tabla, clave))
            codigos[f"fila_{numero}"] = db.upsert(tabla, cid, datos, clave=clave)
            afectados.add(ejercicio_fila)
        mensajes.append("Movimientos revisados: los cobros no sustituyen la renta contractual ni una declaración presentada.")
    elif tipo == TipoDocumento.CONTRATO_ALQUILER:
        if inmueble and c.get("fecha_inicio"):
            inicio = a_fecha(c["fecha_inicio"])
            datos = _permitidos("contratos", c)
            datos.update(inmueble=inmueble, estado="Vigente", ruta=relativo)
            # Un contrato ya Finalizado/Rescindido a mano conserva su estado (A4-P06)
            contrato = db.upsert("contratos", cid, datos, clave=f"CTR|{inmueble}|{inicio.isoformat()}",
                                 solo_si_vacio=("estado",))
            codigos["contrato"] = contrato
            for inq in ext.inquilinos:
                datos_inq = _permitidos("inquilinos", inq)
                datos_inq.update(contrato=contrato, inmueble=inmueble, fecha_alta=inicio, ruta=relativo)
                identidad = inq.get("documento") or inq.get("nombre")
                if identidad:
                    db.upsert("inquilinos", cid, datos_inq, clave=f"INQ|{contrato}|{identidad}")
            fin = a_fecha(c.get("fecha_fin_prevista"))
            afectados.update(range(inicio.year, (fin.year if fin else max(inicio.year, ej)) + 1))
        else:
            mensajes.append("Contrato sin inmueble identificado o sin fecha de inicio: revisa y aplica los datos manualmente.")
    elif tipo == TipoDocumento.MODELO_210:
        from tax_calculator import completar_pago_210_nrc, pago_inferido_por_nrc, _anadir_nota_nrc
        renta = str(c.get("tipo_renta") or "").zfill(2)
        periodo = str(c.get("periodo") or "0A")
        if inmueble and renta in ("01", "02", "28", "35"):
            datos = {k: c[k] for k in ("base", "cuota", "pagado", "nrc", "fecha_presentacion") if k in c}
            datos.update(ejercicio=ej, inmueble=inmueble, ruta=relativo, estado="Presentado")
            if renta in ("01", "35"):
                # La calculadora usa 'Anual'; el documento puede denominarlo '0A'.
                from tax_calculator import normalizar_periodo_210, periodo_210_o_none
                periodo = normalizar_periodo_210(periodo)
                anteriores = {periodo_210_o_none(f.get('periodo'))
                              for f in db.listar('m210_alquiler', cid, inmueble=inmueble, ejercicio=ej)
                              if f.get('estado') == 'Presentado'}
                if anteriores and (None in anteriores or
                        (periodo == 'Anual' and anteriores != {'Anual'}) or
                        (periodo != 'Anual' and 'Anual' in anteriores)):
                    raise ValueError("Declaraciones presentadas con periodicidades incompatibles. "
                                     "Revisa el histórico antes de incorporar este justificante.")
                datos["periodo"] = periodo
                codigos["210_alquiler"] = db.upsert("m210_alquiler", cid, datos,
                                                    clave=f"210A|{ej}|{inmueble}|{periodo}", solo_si_vacio=("pagado",))
                completar_pago_210_nrc(db, cid, "m210_alquiler", codigos["210_alquiler"])
            elif renta == "02":
                datos.pop("base", None)
                datos["renta_imputada"] = c.get("base")
                codigos["210_imputacion"] = db.upsert("m210_imputacion", cid, datos,
                                                      clave=f"210I|{ej}|{inmueble}", solo_si_vacio=("pagado",))
                completar_pago_210_nrc(db, cid, "m210_imputacion", codigos["210_imputacion"])
            else:
                previa = db.get_por_clave("obligaciones", cid, f"VENTA|{inmueble}") or {}
                pago = previa.get("pagado") if previa.get("pagado") not in (None, "") else c.get("pagado")
                inferido = pago_inferido_por_nrc({**datos, "pagado": pago})
                detalle = f"Modelo 210 renta 28 presentado. Base {c.get('base')}; cuota {c.get('cuota')}; PDF {relativo}"
                nota = previa.get("notas") or ""
                if detalle not in nota:
                    nota = (nota + "; " if nota else "") + detalle
                if inferido is not None:
                    pago = inferido
                    nota = _anadir_nota_nrc(nota)
                codigos["venta"] = db.upsert("obligaciones", cid, {
                    "ejercicio": ej, "inmueble": inmueble, "tipo": "Otra", "estado": "Presentado",
                    "fecha_presentacion": c.get("fecha_presentacion"), "pagado": pago,
                    "notas": nota,
                }, clave=f"VENTA|{inmueble}", solo_si_vacio=("pagado",))
            afectados.add(ej)
        else:
            mensajes.append("Modelo 210 sin inmueble identificado o tipo de renta 01/02/28/35: revisa los campos.")
    elif tipo == TipoDocumento.CERTIFICADO_RESIDENCIA:
        from tax_calculator import grupo_territorio

        datos = _permitidos("residencias", c)
        datos.pop("ejercicio", None)
        datos.update(certificado="Sí", ruta=relativo)
        if c.get("pais"):
            datos["grupo"] = grupo_territorio(c["pais"])
            datos["gastos_deducibles"] = "Sí" if datos["grupo"] == "UE/EEE" else "No"
        codigos["residencia"] = db.guardar_residencia(cid, ej, **datos)
        afectados.add(ej)
    elif tipo == TipoDocumento.MODELO_036_037:
        datos = _permitidos("actividades", c)
        datos["ruta"] = relativo
        for campo, normalizar in (("regimen_iva", regimen_iva_normalizado), ("regimen_irpf", regimen_irpf_normalizado)):
            if datos.get(campo):
                datos[campo] = normalizar(datos[campo]) or datos[campo]
        codigos["actividad"] = db.upsert("actividades", cid, datos,
                                           clave=f"ACT|{c.get('epigrafe_iae') or '-'}|{c.get('fecha_alta') or ej}")
        if cartera_de(cliente.get("tipo_cliente")) != RES:
            mensajes.append("Alta censal de actividad en un expediente de No residentes: si el cliente es residente, "
                            "cámbialo a Residentes en «Datos del cliente» (su carpeta se moverá de cartera).")
        afectados.add((a_fecha(c.get("fecha_alta")) or date(ej, 1, 1)).year)
    elif tipo == TipoDocumento.FACTURA_GASTO and cartera_de(cliente.get("tipo_cliente")) == RES:
        tabla, datos, clave = factura_residente(cliente, c, ext.texto, huella, ej)
        if factura_existente:
            if factura_existente[0] != tabla or not db.get(tabla, cid, factura_existente[1]):
                raise ValueError("La lectura del PDF no coincide con la clasificación de la factura. Revisa sus datos antes de trasladarla.")
            datos["codigo"] = factura_existente[1]
        elif c.get('n_factura'):
            # Compatibilidad 1.0.6: la clave antigua no incluía el ejercicio.
            # Reutilizarla solo si corresponde al mismo año evita duplicados
            # al releer y evita sobrescribir otra factura de otro ejercicio.
            partes = (c.get('n_factura'), c.get('nif_receptor')) if tabla == 'facturas_emitidas' else (
                c.get('nif_proveedor') or c.get('proveedor'), c.get('n_factura'))
            clave_anterior = ('FE|' if tabla == 'facturas_emitidas' else 'FR|') + '|'.join(str(v or '') for v in partes)
            anterior = db.get_por_clave(tabla, cid, clave_anterior)
            if anterior:
                fecha_anterior = a_fecha(anterior.get('fecha'))
                anio_anterior = fecha_anterior.year if fecha_anterior else anterior.get('ejercicio')
                fecha_nueva = a_fecha(c.get('fecha'))
                anio_nuevo = fecha_nueva.year if fecha_nueva else ej
                if not anio_anterior:
                    raise ValueError('Existe una factura anterior con ese número sin ejercicio. '
                                     'Confirma su fecha antes de importar otra para evitar sobrescribirla.')
                if int(anio_anterior) == int(anio_nuevo):
                    datos['codigo'] = anterior['codigo']
        datos.update(ruta=relativo, origen="pdf")
        codigos["factura"] = db.upsert(tabla, cid, datos, clave=clave, solo_si_vacio=("estado_cobro",))
        fila = db.get(tabla, cid, codigos["factura"]) or {}
        afectados.add(int(fila.get("ejercicio") or ej))
        mensajes.append(f"Factura {'emitida' if tabla == 'facturas_emitidas' else 'recibida'} registrada.")
    elif tipo == TipoDocumento.FACTURA_GASTO:
        if factura_existente and factura_existente[0] != "gastos":
            raise ValueError("La factura no corresponde a esta cartera.")
        if not inmueble:
            activos = [r["codigo"] for r in db.listar("inmuebles", cid) if r.get("estado") not in ("Vendido", "Baja")]
            if len(activos) == 1:  # con un solo inmueble no hay duda posible
                inmueble = activos[0]
        importe = c.get("total") if c.get("total") not in (None, "") else c.get("base")
        if inmueble and importe not in (None, ""):
            datos = _permitidos("gastos", c)
            datos.update(inmueble=inmueble, ejercicio=ej, categoria=categoria_gasto(c.get("categoria")),
                         total=importe, origen="pdf", ruta=relativo)
            if factura_existente:
                if not db.get("gastos", cid, factura_existente[1]):
                    raise ValueError("El gasto vinculado ya no existe en este expediente.")
                datos["codigo"] = factura_existente[1]
            if c.get("concepto"):
                datos["observaciones"] = str(c["concepto"])[:250]
            # Misma factura (proveedor + número) = mismo gasto, aunque el PDF se vuelva a escanear
            id_factura = "|".join(str(c.get(k) or "") for k in ("nif_proveedor", "proveedor", "n_factura")).strip("|")
            clave = f"FAC|{inmueble}|{ej}|{id_factura}" if c.get("n_factura") else f"FAC|{inmueble}|{huella[:16]}"
            if c.get("n_factura") and not factura_existente:
                anterior = db.get_por_clave("gastos", cid, f"FAC|{inmueble}|{id_factura}")
                if anterior:
                    from tax_calculator import ejercicio_registrado
                    anio_anterior = ejercicio_registrado(anterior)
                    if anio_anterior is None:
                        raise ValueError("Hay una factura antigua con el mismo número sin ejercicio identificable. Revísala antes de sustituirla.")
                    if anio_anterior == ej:
                        clave = anterior["clave"]  # Compatibilidad con la clave anterior a 1.0.7.
            codigos["gasto"] = db.upsert("gastos", cid, datos, clave=clave, solo_si_vacio=("incluido_210",))
            afectados.add(ej)
        elif not inmueble:
            mensajes.append("Gasto sin inmueble identificado: elige el inmueble en la revisión y vuelve a subirlo.")
        else:
            mensajes.append("Gasto sin importe: corrígelo a mano en la pestaña Gastos.")
    elif tipo == TipoDocumento.MODELO_PRESENTADO:
        modelo = str(c.get("modelo") or "")
        if modelo:
            periodo = c.get("periodo") or "Anual"
            periodo = "Anual" if periodo == "0A" else periodo
            datos = _permitidos("modelos_presentados", c)
            datos.update(modelo=modelo, ejercicio=ej, periodo=periodo, estado="Presentado", ruta=relativo)
            codigos["modelo"] = db.upsert("modelos_presentados", cid, datos, clave=f"MOD|{modelo}|{ej}|{periodo}")
            afectados.add(ej)
        else:
            mensajes.append("Modelo presentado sin número: completa los campos manualmente.")
    if inmueble:
        codigos["inmueble"] = inmueble
    db.upsert("documentos", cid, {"codigo": documento, "inmueble": inmueble, "contrato": contrato,
                                 "observaciones": "; ".join(mensajes) or None})
    return ResultadoAplicacion(documento=documento, duplicado=False, ruta_relativa=relativo,
                               ejercicios_afectados=afectados, codigos=codigos, mensajes=mensajes)


def _resumen_uso_ia(lecturas: list[dict]) -> dict:
    costes = [u["coste"] for u in lecturas if type(u.get("coste")) in (int, float)
              and math.isfinite(u["coste"]) and u["coste"] >= 0]
    return {"coste_usd": round(sum(costes), 8) if costes or not lecturas else None,
            "coste_incompleto": len(costes) != len(lecturas), "lecturas_ia": [dict(u) for u in lecturas]}


def extraer(ruta: Path, ajustes=None, comentario_guia: str = "") -> Extraccion:
    """Conserva el consumo de todas las lecturas, no solo el de la respuesta elegida."""
    import ia_extractor
    with ia_extractor.medir_lecturas_documento() as lecturas:
        try:
            ext = _extraer_con_respaldo(ruta, ajustes, comentario_guia)
        except Exception as exc:
            # La UI puede conservar coste/uso incluso si el error impide llegar a la revisión.
            try:
                for campo, valor in _resumen_uso_ia(lecturas).items():
                    setattr(exc, campo, valor)
            except Exception:
                pass  # Nunca sustituir el error original por un fallo de instrumentación.
            raise
        for campo, valor in _resumen_uso_ia(lecturas).items():
            setattr(ext, campo, valor)
        return ext


def _extraer_con_respaldo(ruta: Path, ajustes=None, comentario_guia: str = "") -> Extraccion:
    """IA (Claude) si está activada y hay clave; si no o si falla, lector local.

    Sin comentario guía, los campos ausentes en IA y detectados localmente (mismo tipo) se añaden como respaldo.
    Con guía, se respeta la selección de la IA: no reintroducir campos de hojas o zonas que el asesor excluyó.
    """
    import ia_extractor

    ruta = Path(ruta)
    if not isinstance(comentario_guia, str) or len(comentario_guia) > 4000:
        raise ValueError("El comentario debe tener como máximo 4.000 caracteres.")
    from lectores_documentales import FORMATOS
    if ruta.suffix.lower() not in FORMATOS:
        raise ValueError("Formato documental no admitido.")
    local = extraer_local(ruta)
    local.comentario_guia = comentario_guia
    if comentario_guia:
        local.avisos.append("El comentario se conserva, pero el lector local no interpreta instrucciones. Revisa sus propuestas manualmente.")
    if ruta.suffix.lower() != ".pdf":
        local.avisos.append("Tabla documental: confirma la interpretación con IA o completa los datos manualmente; no se infieren importes de columnas ambiguas.")
    if not ia_extractor.disponible(ajustes):
        return local
    try:
        ext = ia_extractor.extraer(ruta, ajustes, **({"comentario_guia": comentario_guia} if comentario_guia else {}))
        ext.comentario_guia = comentario_guia
    except ia_extractor.ErrorIA as exc:
        # Negativa o respuesta fuera del esquema: es justo el caso que el modelo de respaldo puede resolver.
        ext = _lectura_respaldo(ruta, ajustes, comentario_guia) if exc.admite_respaldo else None
        if ext is None:
            local.avisos.insert(0, f"Lectura con IA no disponible ({exc}). Se usó el lector local: revisa los campos.")
            return local
        ext.avisos.insert(0, f"La primera lectura falló ({exc}): releído con {ajustes.respaldo}.")
    else:
        ext = _segunda_lectura(ruta, ajustes, ext, comentario_guia)
    ext.texto = local.texto
    ext.necesita_ocr = False  # la IA lee el PDF como imagen
    if ext.tipo == local.tipo and not comentario_guia:
        for campo, valor in local.campos.items():
            if campo not in ext.campos:
                ext.campos[campo] = valor
                ext.evidencias.setdefault(campo, "Lector local")
        if not ext.inquilinos and local.inquilinos:
            ext.inquilinos = local.inquilinos
        if not ext.titulares and local.titulares:
            ext.titulares = local.titulares
    elif ext.tipo != local.tipo and local.tipo != TipoDocumento.DESCONOCIDO and local.confianza >= 0.8:
        ext.avisos.append(f"El lector local cree que es {TIPOS_LEGIBLES[local.tipo]}: confirma el tipo.")
    return ext


def _lectura_respaldo(ruta: Path, ajustes, comentario_guia: str, avisos: list | None = None) -> Extraccion | None:
    """Lectura con el modelo de respaldo, o None si no hay respaldo o falla (el fallo se anota en ``avisos``)."""
    import dataclasses
    import ia_extractor
    respaldo = getattr(ajustes, "respaldo", "") if ajustes is not None else ""
    if not respaldo or respaldo == getattr(ajustes, "modelo", None):
        return None
    try:
        otra = ia_extractor.extraer(ruta, dataclasses.replace(ajustes, modelo=respaldo, respaldo="", permitir_respaldo_servidor=False,
                                                                cachear=False),
                                    **({"comentario_guia": comentario_guia} if comentario_guia else {}))
    except Exception as exc:  # incluido un esquema inválido: la segunda lectura nunca hace perder la primera
        if avisos is not None:
            detalle = str(exc) if isinstance(exc, ia_extractor.ErrorIA) else exc.__class__.__name__
            avisos.append(f"La segunda lectura con {respaldo} no estuvo disponible ({detalle}): revisa los datos.")
        return None
    otra.comentario_guia = comentario_guia
    return otra


def _segunda_lectura(ruta: Path, ajustes, ext: Extraccion, comentario_guia: str) -> Extraccion:
    """Enrutado de calidad: si el modelo barato no reconoce el documento o duda, se relee SOLO ese archivo con el
    modelo de respaldo (Sonnet) y se queda la lectura mejor. Un fallo de la segunda lectura no pierde la primera."""
    import ia_extractor
    respaldo = getattr(ajustes, "respaldo", "") if ajustes is not None else ""
    dudosa = ext.tipo == TipoDocumento.DESCONOCIDO or float(ext.confianza or 0) < ia_extractor.UMBRAL_RESPALDO
    if not dudosa or not respaldo or respaldo == getattr(ajustes, "modelo", None):
        return ext
    otra = _lectura_respaldo(ruta, ajustes, comentario_guia, ext.avisos)
    if otra is None:
        return ext
    mejor = (otra.tipo != TipoDocumento.DESCONOCIDO and (ext.tipo == TipoDocumento.DESCONOCIDO
             or float(otra.confianza or 0) >= float(ext.confianza or 0)))
    elegida = otra if mejor else ext
    elegida.comentario_guia = comentario_guia
    elegida.avisos.insert(0, f"Primera lectura dudosa (confianza {float(ext.confianza or 0):.0%}): releído con "
                             f"{respaldo}; " + ("se usa esa lectura." if mejor else "se mantiene la primera."))
    return elegida


def preparar_pdf(db, ruta_pdf, confirmar=None, comentario_guia: str = "") -> tuple[Extraccion, str]:
    """Extrae y revisa sin abrir una transacción de escritura durante la red o el diálogo."""
    import ia_extractor

    huella_revisada = _hash(Path(ruta_pdf))
    ext = extraer(Path(ruta_pdf), ia_extractor.AjustesIA.desde_db(db), **({"comentario_guia": comentario_guia} if comentario_guia else {}))
    if confirmar is not None:
        ext = confirmar(ext)
        if not isinstance(ext, Extraccion):
            raise TypeError("confirmar debe devolver una Extraccion")
    ext.comentario_guia = comentario_guia
    if _hash(Path(ruta_pdf)) != huella_revisada:
        raise ValueError("El PDF cambió durante la revisión. Selecciónalo de nuevo para comprobar sus datos.")
    return ext, huella_revisada


def procesar_pdf(db, cid, ruta_pdf, confirmar=None, inmueble_destino=None, ejercicio=None, comentario_guia: str = "") -> ResultadoAplicacion:
    """Extrae, permite revisión y aplica. El controlador coordina recálculo y exportación."""
    ext, huella = preparar_pdf(db, ruta_pdf, confirmar=confirmar, comentario_guia=comentario_guia)
    return aplicar(db, cid, ext, Path(ruta_pdf), inmueble_destino, ejercicio, huella_esperada=huella)
