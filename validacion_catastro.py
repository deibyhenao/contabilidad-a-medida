"""Controles de procedencia; no cambia las reglas fiscales ni los valores manuales."""
import re
import unicodedata

PENDIENTE_REVISION = "Revisión catastral pendiente de acreditar"


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    return " ".join("".join(c for c in texto if not unicodedata.combining(c)).lower().split())


def revision_acreditada(anio, ejercicio, evidencia, texto, confirmada=False):
    """La fecha del valor o de emisión no acredita una revisión colectiva.

    Una cita de IA debe poder localizarse en el texto fuente. En escaneos sin
    texto verificable se exige la confirmación explícita de una persona.
    """
    if not anio:
        return False
    try:
        anio, ejercicio = int(anio), int(ejercicio)
    except (ValueError, TypeError):
        return False
    if not 1900 <= anio <= ejercicio <= 2200:
        return False
    if confirmada:
        return True
    cita, fuente = normalizar(evidencia), normalizar(texto)
    if not cita or not fuente or cita not in fuente:
        return False
    if re.search(r"\b(?:no|sin|desconocida|desconocido|pendiente)\b", cita):
        return False  # Una negación o ausencia de constancia nunca acredita la revisión.
    if len(set(re.findall(r"\b(?:19|20|21)\d{2}\b", cita))) != 1:
        return False  # Aprobación, vigencia y año del valor pueden ser distintos.
    if re.search(r"\b(?:aprobada|aprobacion|aprobado)\b", cita):
        return False  # Una fecha de aprobación no acredita por sí sola la entrada en vigor.
    coincidencia = re.search(
        r"(?:revision catastral colectiva|valoracion colectiva|ponencia de valores)"
        r"[^.!?;\d]{0,100}(\d{4})\b", cita)
    # El primer año de la misma frase: no saltar desde una ponencia antigua
    # hasta el año del valor o de emisión que figure a continuación.
    return bool(coincidencia and int(coincidencia.group(1)) == anio)


def revision_pendiente(catastro):
    cat = catastro or {}
    if cat.get("pct_imputacion") not in (None, ""):
        return False  # La decisión manual de porcentaje prevalece.
    anio = cat.get("anio_revision")
    try:
        if anio and cat.get("ejercicio") and not 1900 <= int(anio) <= int(cat["ejercicio"]):
            return True
    except (ValueError, TypeError):
        return True
    return not anio and bool(cat.get("revision_pendiente") or
                             PENDIENTE_REVISION in str(cat.get("observaciones") or ""))


def auditar_importaciones_anteriores(db, cid):
    """Previsualización de solo lectura. No corrige valores ni presentaciones.

    Los documentos antiguos sin evidencia conservada requieren revisión humana;
    no se presupone que su revisión catastral sea incorrecta.
    """
    import json
    hallazgos = []
    filas = db.con.execute(
        "SELECT d.codigo, d.ejercicio, g.extraccion FROM documentos d "
        "JOIN documentos_gestion g ON g.cliente_id=d.cliente_id AND g.codigo=d.codigo "
        "WHERE d.cliente_id=? AND g.en_papelera=0", (cid,)).fetchall()
    for fila in filas:
        try:
            ext = json.loads(fila["extraccion"] or "{}")
        except (TypeError, ValueError):
            hallazgos.append(dict(documento=fila["codigo"], motivo="Extracción histórica no interpretable"))
            continue
        if not isinstance(ext, dict):
            hallazgos.append(dict(documento=fila["codigo"], motivo="Extracción histórica sin objeto válido"))
            continue
        if ext.get("tipo") == "CATASTRO_IBI":
            campos = ext.get("campos") or {}
            if not campos.get("ejercicio"):
                hallazgos.append(dict(documento=fila["codigo"], ejercicio=fila["ejercicio"],
                    motivo="Ejercicio asignado sin año explícito en la extracción: cotejar el PDF"))
            if campos.get("anio_revision") and not ext.get("revision_confirmada"):
                hallazgos.append(dict(documento=fila["codigo"], ejercicio=fila["ejercicio"],
                    motivo="Revisar evidencia independiente de la revisión colectiva histórica"))
        for registro in ext.get("registros") or []:
            if registro.get("tabla") == "catastro" and (registro.get("campos") or {}).get("anio_revision"):
                hallazgos.append(dict(documento=fila["codigo"],
                    ejercicio=registro["campos"].get("ejercicio"),
                    motivo="Revisar revisión colectiva del anexo; el año del valor no la acredita"))
    return hallazgos
