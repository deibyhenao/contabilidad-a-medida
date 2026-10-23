"""Papelera reversible, ejercicio y procedencia de los documentos del expediente.

Los metadatos permanecen en SQLite, fuera de las plantillas. El historial es por
campo y por cliente; una escritura manual posterior siempre tiene prioridad.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path, PureWindowsPath

EXTENSIONES_SOPORTE = frozenset({".pdf", ".xlsx", ".xls", ".docx", ".csv"})


def crear_esquema(db):
    db.con.execute('''CREATE TABLE IF NOT EXISTS documentos_operaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
        documento TEXT NOT NULL, accion TEXT NOT NULL, detalle TEXT NOT NULL,
        creado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    db.con.execute("""CREATE TABLE IF NOT EXISTS documentos_gestion (
        cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL, en_papelera INTEGER NOT NULL DEFAULT 0,
        ruta_papelera TEXT, extraccion TEXT, actualizado TEXT,
        PRIMARY KEY(cliente_id,codigo),
        FOREIGN KEY(cliente_id,codigo) REFERENCES documentos(cliente_id,codigo) ON DELETE CASCADE)""")
    db.con.execute("""CREATE TABLE IF NOT EXISTS documentos_cambios (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL,
        documento TEXT, tabla TEXT NOT NULL, codigo TEXT NOT NULL, campo TEXT NOT NULL,
        anterior TEXT, posterior TEXT, activo INTEGER NOT NULL DEFAULT 1,
        vigente INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE)""")
    db.con.execute("CREATE INDEX IF NOT EXISTS ix_documentos_cambios_campo ON documentos_cambios(cliente_id,tabla,codigo,campo,id)")
    db.con.execute("CREATE INDEX IF NOT EXISTS ix_documentos_cambios_fuente ON documentos_cambios(cliente_id,documento,id)")
    if "vigente" not in {r["name"] for r in db.con.execute("PRAGMA table_info(documentos_cambios)")}:
        db.con.execute("ALTER TABLE documentos_cambios ADD COLUMN vigente INTEGER NOT NULL DEFAULT 1")
    db.con.execute("""CREATE TABLE IF NOT EXISTS documentos_revision (
        cliente_id INTEGER PRIMARY KEY REFERENCES clientes(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL DEFAULT 0)""")
    db.con.execute("""CREATE TABLE IF NOT EXISTS documentos_archivos (
        cliente_id INTEGER NOT NULL, documento TEXT NOT NULL, origen TEXT NOT NULL,
        destino TEXT NOT NULL, sha256 TEXT NOT NULL,
        PRIMARY KEY(cliente_id,documento),
        FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE)""")


def _json(valor):
    return json.dumps(valor, ensure_ascii=False, default=str, separators=(",", ":"))


@contextmanager
def origen(db, cid):
    """Activa captura por campo solo durante una importación de un cliente."""
    previo = db._documento_origen
    db._documento_origen = (cid, None)
    try:
        yield
    finally:
        db._documento_origen = previo


def vincular_importacion(db, cid, codigo, extraccion):
    db._documento_origen = (cid, codigo)
    datos = extraccion.model_dump(mode="json") if hasattr(extraccion, "model_dump") else extraccion.dict()
    # No almacenar texto completo ni copias de todo el expediente: solo campos del PDF.
    datos.pop("texto", None)
    datos.pop("evidencias", None)
    db.con.execute("INSERT INTO documentos_gestion(cliente_id,codigo,extraccion,actualizado) VALUES(?,?,?,?) "
                   "ON CONFLICT(cliente_id,codigo) DO UPDATE SET extraccion=excluded.extraccion,actualizado=excluded.actualizado",
                   (cid, codigo, _json(datos), datetime.now().isoformat(timespec="seconds")))


def registrar_mutacion(db, tabla, cid, codigo, previa, actual, campos):
    if tabla == "documentos" or db._registrando_documentos or actual is None:
        return
    fuente = db._documento_origen
    documento = fuente[1] if fuente and fuente[0] == cid else None
    if fuente and (fuente[0] != cid or not documento):
        return
    if documento is None:
        campos = set(campos) & {r[0] for r in db.con.execute(
            "SELECT DISTINCT campo FROM documentos_cambios WHERE cliente_id=? AND tabla=? AND codigo=?",
            (cid, tabla, codigo))}
    for campo in sorted(set(campos) - {"codigo", "id", "clave", "cliente_id", "actualizado"}):
        if campo not in actual:
            continue
        antes, despues = (previa or {}).get(campo), actual.get(campo)
        # Ignorar reimportaciones idénticas desde Excel y recálculos, conservando la procedencia.
        # Las correcciones manuales de inmuebles también se protegen con overrides persistentes.
        if antes == despues and previa is not None and documento is None:
            continue
        db.con.execute("INSERT INTO documentos_cambios(cliente_id,documento,tabla,codigo,campo,anterior,posterior) "
                       "VALUES(?,?,?,?,?,?,?)", (cid, documento, tabla, codigo, campo, _json(antes), _json(despues)))


def _documento(db, cid, codigo):
    doc = db.get("documentos", cid, str(codigo))
    if not doc or db.get_cliente(cid) is None:
        raise ValueError("Documento inexistente en este expediente.")
    meta = db.con.execute("SELECT * FROM documentos_gestion WHERE cliente_id=? AND codigo=?", (cid, str(codigo))).fetchone()
    return {**doc, "en_papelera": bool(meta and meta["en_papelera"]),
            "ruta_papelera": meta["ruta_papelera"] if meta else None,
            "extraccion_guardada": meta["extraccion"] if meta else None}


def _ruta(db, cid, relativa, papelera=False):
    from database import ruta_soporte
    cliente = db.get_cliente(cid)
    if not cliente:
        raise ValueError("Cliente inexistente.")
    base = Path(cliente["carpeta"]).absolute()
    # Comprobar cada componente, incluido un posible enlace del propio expediente.
    for parte in (base, *base.parents):
        if parte.exists() and (parte.is_symlink() or getattr(parte.lstat(), "st_file_attributes", 0) & 1024):
            raise ValueError("La carpeta del expediente contiene un enlace o una junction.")
    if not papelera:
        destino = ruta_soporte(base, str(relativa))
    else:
        ruta = Path(str(relativa).replace("\\", "/"))
        if (ruta.is_absolute() or PureWindowsPath(str(relativa)).drive or len(ruta.parts) != 2
                or ruta.parts[0] != ".Papelera_Documentos" or ruta.name in (".", "..")
                or ruta.suffix.casefold() not in EXTENSIONES_SOPORTE or any(c in ruta.name for c in '<>:"|?*')):
            raise ValueError("Ruta de papelera inválida.")
        from rutas_seguras import sanitize_safe_path
        destino = sanitize_safe_path(base, ruta)
    for parte in (destino, destino.parent):
        if parte.exists() and (parte.is_symlink() or getattr(parte.lstat(), "st_file_attributes", 0) & 1024):
            raise ValueError("La ruta del documento contiene un enlace o una junction.")
    return destino


def _huella(ruta):
    h = hashlib.sha256()
    with Path(ruta).open("rb") as entrada:
        for bloque in iter(lambda: entrada.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def _copiar_seguro(origen, destino, huella):
    if not origen.is_file() or _huella(origen) != huella:
        raise ValueError("El documento de origen falta o su contenido ha cambiado; revisa el soporte.")
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        if _huella(destino) != huella:
            raise ValueError("Ya existe un documento diferente en el destino; no se sobrescribe.")
        return False
    creado = False
    try:
        with origen.open("rb") as entrada, destino.open("xb") as salida:
            creado = True
            shutil.copyfileobj(entrada, salida, length=1024 * 1024)
            salida.flush()
            os.fsync(salida.fileno())
        if _huella(destino) != huella:
            raise OSError("No se pudo verificar la copia del documento.")
    except BaseException:
        if creado:
            destino.unlink(missing_ok=True)
        raise
    return True


def recuperar_archivos(db, cid):
    """Completa solo movimientos ya confirmados. Nunca elimina antes del commit externo."""
    if db._nivel_transaccion or db.con.in_transaction:
        return
    for op in list(db.con.execute("SELECT * FROM documentos_archivos WHERE cliente_id=?", (cid,))):
        es_papelera = op["destino"].startswith(".Papelera_Documentos/")
        origen = _ruta(db, cid, op["origen"], papelera=op["origen"].startswith(".Papelera_Documentos/"))
        destino = _ruta(db, cid, op["destino"], papelera=es_papelera)
        _copiar_seguro(origen, destino, op["sha256"]) if origen.exists() else None
        if not destino.is_file() or _huella(destino) != op["sha256"]:
            raise ValueError("Movimiento documental incompleto: falta la copia verificada.")
        if origen.exists():
            if _huella(origen) != op["sha256"]:
                raise ValueError("El original ha cambiado durante la recuperación; se conserva.")
            origen.unlink()
        db.con.execute("DELETE FROM documentos_archivos WHERE cliente_id=? AND documento=?", (cid, op["documento"]))
        db._confirmar()


def listar(db, cid, papelera=False, limite=51, desplazamiento=0):
    db._limites(limite, desplazamiento)
    return [dict(r) for r in db.con.execute(
        "SELECT d.*,COALESCE(g.en_papelera,0) AS en_papelera FROM documentos d "
        "LEFT JOIN documentos_gestion g ON g.cliente_id=d.cliente_id AND g.codigo=d.codigo "
        "WHERE d.cliente_id=? AND COALESCE(g.en_papelera,0)=? ORDER BY d.codigo,d.id LIMIT ? OFFSET ?",
        (cid, int(bool(papelera)), limite, desplazamiento))]


def rutas_retiradas(db, cid):
    return {r[0] for r in db.con.execute("SELECT d.ruta FROM documentos d JOIN documentos_gestion g "
        "ON g.cliente_id=d.cliente_id AND g.codigo=d.codigo WHERE d.cliente_id=? AND g.en_papelera=1", (cid,))}


def revision(db, cid):
    fila = db.con.execute("SELECT revision FROM documentos_revision WHERE cliente_id=?", (cid,)).fetchone()
    return int(fila[0]) if fila else 0


def _revisar(db, cid):
    db.con.execute("INSERT INTO documentos_revision(cliente_id,revision) VALUES(?,1) "
                   "ON CONFLICT(cliente_id) DO UPDATE SET revision=revision+1", (cid,))


def _eventos(db, cid, codigo):
    return [dict(r) for r in db.con.execute(
        "SELECT * FROM documentos_cambios WHERE cliente_id=? AND documento=? AND vigente=1 ORDER BY id", (cid, codigo))]


def _vinculos_legados(db, cid, doc):
    from database import TABLAS
    for tabla, definicion in TABLAS.items():
        campos = {c.campo for c in definicion.columnas_bd}
        if tabla == "documentos":
            continue
        campo_ruta = "ruta" if "ruta" in campos else "carpeta" if tabla == "inmuebles" else None
        if campo_ruta:
            for fila in db.iterar(tabla, cid, **{campo_ruta: doc["ruta"]}):
                yield tabla, fila


def _legado(db, cid, doc, guardar=False):
    """Sin historial solo se invalidan datos vinculados inequívocamente, sin fabricar un antes."""
    from database import TABLAS
    from pdf_processor import extraer_local, TipoDocumento
    vinculos = list(_vinculos_legados(db, cid, doc))
    if not vinculos:
        return []
    ruta = _ruta(db, cid, doc["ruta"])
    ext = extraer_local(ruta) if ruta.is_file() else None
    cambios = []
    for tabla, fila in vinculos:
        if tabla in ("m210_alquiler", "m210_imputacion", "patrimonio_714", "its_718", "modelos_presentados", "obligaciones"):
            continue
        permitidos = set((ext.campos if ext else {}))
        if tabla == "residencias":
            permitidos |= {"certificado", "fecha_certificado", "valido_hasta", "ruta"}
            # País/grupo solo proceden del certificado si la extracción local lo confirma.
            # Una discrepancia con el país del PDF puede ser una corrección manual
            # anterior al historial. Invalidar la justificación y avisar, conservando
            # ese dato ambiguo en vez de borrar una posible corrección del usuario.
            if ext and ext.campos.get("pais") == fila.get("pais"):
                permitidos |= {"pais", "grupo", "gastos_deducibles"}
        else:
            permitidos |= {"ruta"}
            if tabla == "inmuebles":
                permitidos.add("carpeta")
            if tabla in ("gastos", "facturas_emitidas", "facturas_recibidas") and fila.get("origen") == "pdf":
                permitidos |= {c.campo for c in TABLAS[tabla].columnas_bd} - {"codigo"}
        manuales = db.campos_manuales_inmueble(cid, fila["codigo"]) if tabla == "inmuebles" else set()
        for campo in sorted(permitidos - manuales - {"codigo", "ejercicio"}):
            if campo not in fila or fila[campo] in (None, ""):
                continue
            if campo not in {"ruta", "carpeta", "pais", "grupo", "gastos_deducibles"}:
                esperado = (ext.campos if ext else {}).get(campo)
                if campo == "certificado":
                    esperado = "Sí" if ext and ext.tipo == TipoDocumento.CERTIFICADO_RESIDENCIA else None
                if campo == "cuota_iva":
                    esperado = (ext.campos if ext else {}).get("iva")
                if campo == "numero":
                    esperado = (ext.campos if ext else {}).get("n_factura")
                if campo == "categoria":
                    # Las categorías se normalizan; no atribuir una distinta sin prueba.
                    from pdf_processor import categoria_gasto, categoria_residente
                    esperado = categoria_residente((ext.campos if ext else {}), ext.texto if ext else "") if tabla.startswith("facturas_") else categoria_gasto(esperado)
                if esperado != fila[campo]:
                    continue
            cambios.append({"tabla": tabla, "codigo": fila["codigo"], "campo": campo,
                            "anterior": fila[campo], "posterior": None})
            if guardar:
                db.con.execute("INSERT INTO documentos_cambios(cliente_id,documento,tabla,codigo,campo,anterior,posterior) "
                               "VALUES(?,?,?,?,?,?,?)", (cid, doc["codigo"], tabla, fila["codigo"], campo, "null", _json(fila[campo])))
    return cambios


def _ejercicios(db, cid, doc):
    from database import TABLAS, a_fecha
    resultado = {int(doc["ejercicio"])} if doc.get("ejercicio") else set()
    eventos = _eventos(db, cid, doc["codigo"])
    filas = {(e["tabla"], e["codigo"]): db.get(e["tabla"], cid, e["codigo"]) for e in eventos}
    if not eventos:
        filas = {(tabla, fila["codigo"]): fila for tabla, fila in _vinculos_legados(db, cid, doc)}
    for fila in filas.values():
        if fila and fila.get("ejercicio"):
            # La fecha o el año pudieron corregirse a mano después de importar el PDF.
            resultado.add(int(fila["ejercicio"]))
    for ev in eventos:
        if ev["campo"] == "ejercicio":
            for v in (json.loads(ev["anterior"]), json.loads(ev["posterior"])):
                if v is not None:
                    resultado.add(int(v))
    # Fuentes interanuales y residencia pueden influir en todos los años posteriores existentes.
    tipo = str(doc.get("tipo", "")).casefold()
    if any(palabra in tipo for palabra in ("escritura", "residencia", "contrato", "factura")):
        desde = min(resultado, default=date.today().year)
        if "escritura" in tipo or "contrato" in tipo:
            # Registrar una escritura antigua en 2026 no limita su efecto a 2026.
            campos_fecha = {"fecha_adquisicion", "fecha_venta", "fecha_inicio", "fecha_cambio"}
            fechas = [doc.get("fecha_documento")]
            fechas += [fila.get(campo) for fila in filas.values() if fila for campo in campos_fecha]
            fechas += [json.loads(e[k]) for e in eventos if e["campo"] in campos_fecha for k in ("anterior", "posterior")]
            anios = []
            for valor in fechas:
                try:
                    fecha = a_fecha(valor)
                except (ValueError, TypeError):
                    fecha = None
                if fecha:
                    anios.append(fecha.year)
            # Sin fecha fiable, revisar todo el histórico existente sin inventar ejercicios.
            desde = min([desde, *anios]) if anios else 1900
        for tabla, definicion in TABLAS.items():
            if any(c.campo == "ejercicio" for c in definicion.columnas_bd):
                resultado.update(int(r[0]) for r in db.con.execute(
                    f"SELECT DISTINCT ejercicio FROM {tabla} WHERE cliente_id=? AND ejercicio>=?", (cid, desde)) if r[0])
    return resultado


def _bloqueo_presentados(db, cid, ejercicios):
    from tax_calculator import _presentado
    from database import TABLAS
    for tabla in ("m210_alquiler", "m210_imputacion", "patrimonio_714", "its_718", "m303", "m130", "m111_115", "modelos_presentados", "obligaciones"):
        if tabla not in TABLAS:
            continue
        for fila in db.iterar(tabla, cid):
            if (not fila.get("ejercicio") or int(fila["ejercicio"]) in ejercicios) and _presentado(fila):
                raise ValueError("El documento afecta a declaraciones presentadas. Revisa primero su rectificación; no se modifica el histórico.")


def _cambios_previsibles(db, cid, doc, activar):
    """La confirmación usa el mismo resolvedor que la operación, en savepoint revertido."""
    db.con.execute("SAVEPOINT previsualizacion_documental")
    try:
        if not _eventos(db, cid, doc["codigo"]):
            _legado(db, cid, doc, guardar=True)
        eventos = _eventos(db, cid, doc["codigo"])
        claves = sorted({(e["tabla"], e["codigo"], e["campo"]) for e in eventos})
        antes = {(t, c, campo): (db.get(t, cid, c) or {}).get(campo) for t, c, campo in claves}
        _activar_datos(db, cid, doc["codigo"], activar)
        cambios = []
        for tabla, codigo, campo in claves:
            valor = (db.get(tabla, cid, codigo) or {}).get(campo)
            previo = antes[(tabla, codigo, campo)]
            if previo != valor:
                cambios.append({"tabla": tabla, "codigo": codigo, "campo": campo,
                                "anterior": previo, "posterior": valor})
        return cambios
    finally:
        db.con.execute("ROLLBACK TO SAVEPOINT previsualizacion_documental")
        db.con.execute("RELEASE SAVEPOINT previsualizacion_documental")


def previsualizar(db, cid, codigo, accion, ejercicio_destino=None):
    doc = _documento(db, cid, codigo)
    acciones = {"retirar", "restaurar", "cambiar_ejercicio", "sustituir"}
    if accion not in acciones:
        raise ValueError("Acción documental desconocida.")
    avisos, cambios = [], []
    error = None
    ejercicios = _ejercicios(db, cid, doc)
    try:
        if accion == "restaurar" and not doc["en_papelera"]:
            raise ValueError("El documento ya está activo.")
        if accion != "restaurar" and doc["en_papelera"]:
            raise ValueError("Restaura el documento antes de modificarlo.")
        if accion == "cambiar_ejercicio":
            _validar_ejercicio(ejercicio_destino)
            ejercicios.add(int(ejercicio_destino))
        _bloqueo_presentados(db, cid, ejercicios)
        ruta = _ruta(db, cid, doc["ruta_papelera"] if doc["en_papelera"] else doc["ruta"], papelera=doc["en_papelera"])
        if not ruta.is_file():
            raise ValueError("No se encuentra el documento. Recupera el archivo antes de continuar.")
        if doc.get("sha256") and _huella(ruta) != doc["sha256"]:
            raise ValueError("El contenido del documento ha cambiado respecto al documento registrado.")
        eventos = _eventos(db, cid, codigo)
        cambios = _cambios_previsibles(db, cid, doc, accion == "restaurar")
        if not eventos:
            avisos.append("Documento antiguo sin historial de importación: solo se pueden revisar los datos vinculados a este documento. No se conocen los valores anteriores; revisa los campos indicados y los datos manuales tras confirmar.")
            avisos.append("Los datos distintos de los que se pueden comprobar en el documento se conservan por posible corrección manual; revisa especialmente el país y los importes que queden sin soporte.")
        elif len(cambios) < len({(e["tabla"], e["codigo"], e["campo"]) for e in eventos}):
            avisos.append("Se conservan los datos respaldados por otro documento o por una corrección manual posterior. Los importes manuales conservados pueden seguir influyendo en los cálculos; revísalos.")
        if "escritura" in str(doc.get("tipo", "")).casefold():
            avisos.append("La escritura sirve para varios ejercicios. Cambiar su clasificación no cambia la fecha real de adquisición ni de venta.")
        if accion == "cambiar_ejercicio" and "factura" in str(doc.get("tipo", "")).casefold():
            avisos.append("La factura se clasifica por su fecha: también se corregirá el año de la fecha y de sus periodos. Confirma que el PDF respalda esa corrección.")
        if accion == "cambiar_ejercicio" and "certificado" in str(doc.get("tipo", "")).casefold():
            destino = db.get("residencias", cid, str(ejercicio_destino))
            if destino and any(destino.get(k) not in (None, "") for k in ("pais", "certificado", "ruta")) and int(ejercicio_destino) != doc.get("ejercicio"):
                raise ValueError("El ejercicio destino ya tiene residencia fiscal. Revisa ese registro antes de trasladar el certificado.")
    except (ValueError, OSError) as exc:
        error = str(exc)
        avisos.append(error)
    return {"titulo": {"retirar": "Mover documento a la papelera", "restaurar": "Restaurar documento",
                       "cambiar_ejercicio": "Corregir ejercicio del documento", "sustituir": "Sustituir documento"}[accion],
            "descripcion": "Se conservará el documento y se revisarán sus datos asociados respetando las correcciones manuales.",
            "advertencias": avisos, "ejercicio_origen": doc.get("ejercicio"),
            "ejercicio_destino": ejercicio_destino, "cambios": cambios, "confirmable": error is None}


def _validar_ejercicio(ej):
    try:
        valido = not isinstance(ej, bool) and str(ej).strip() == str(int(ej)) and 1900 <= int(ej) <= 2200
    except (TypeError, ValueError, OverflowError):
        valido = False
    if not valido:
        raise ValueError("Indica un ejercicio entero entre 1900 y 2200.")


def _exigir(preview):
    if not preview["confirmable"]:
        raise ValueError(preview["advertencias"][-1])


def _activar_datos(db, cid, codigo, activo):
    """Resuelve la última fuente aún activa, sin pisar una edición manual posterior."""
    from database import TABLAS
    eventos = _eventos(db, cid, codigo)
    claves = {(e["tabla"], e["codigo"], e["campo"]) for e in eventos}
    # Actualizar todas las activaciones antes de resolver cadenas A -> B -> manual.
    db.con.execute("UPDATE documentos_cambios SET activo=? WHERE cliente_id=? AND documento=? AND vigente=1", (int(activo), cid, codigo))
    anterior_guardia = db._registrando_documentos
    db._registrando_documentos = True
    try:
        for tabla, fila_codigo, campo in sorted(claves):
            if tabla not in TABLAS or campo not in {c.campo for c in TABLAS[tabla].columnas_bd} - {"codigo"}:
                continue
            historial = [dict(r) for r in db.con.execute(
                "SELECT * FROM documentos_cambios WHERE cliente_id=? AND tabla=? AND codigo=? AND campo=? ORDER BY id",
                (cid, tabla, fila_codigo, campo))]
            actual = db.get(tabla, cid, fila_codigo)
            if actual is None:
                # Nunca resucitar una fila borrada a mano.
                continue
            if tabla == "inmuebles" and campo in db.campos_manuales_inmueble(cid, fila_codigo):
                continue
            if campo == "ejercicio" and db.con.execute(
                    "SELECT 1 FROM documentos_cambios WHERE cliente_id=? AND tabla=? AND codigo=? "
                    "AND documento IS NULL AND activo=1 AND vigente=1 LIMIT 1", (cid, tabla, fila_codigo)).fetchone():
                # El año identifica también una fila que ahora contiene datos manuales.
                continue
            ultimo_previo = next((e for e in reversed(historial) if e["vigente"] and
                                  (e["activo"] if e["documento"] != codigo else not activo)), None)
            esperado = json.loads(ultimo_previo["posterior"] if ultimo_previo else historial[0]["anterior"])
            # Si cambió sin pasar por upsert (Excel heredado/SQL), se considera manual.
            if actual.get(campo) != esperado:
                continue
            ultimo = next((e for e in reversed(historial) if e["activo"] and e["vigente"]), None)
            valor = json.loads(ultimo["posterior"] if ultimo else historial[0]["anterior"])
            if actual.get(campo) != valor:
                db.con.execute(f"UPDATE {tabla} SET {campo}=? WHERE cliente_id=? AND codigo=?", (valor, cid, fila_codigo))
    finally:
        db._registrando_documentos = anterior_guardia


@contextmanager
def conservar_correcciones(db, cid, codigo, excepto=(), ejercicio_fechas=None):
    """Una reclasificación o sustitución no deshace valores corregidos a mano."""
    protegidos = {}
    eventos = _eventos(db, cid, codigo)
    if not eventos:
        # En documentos antiguos, solo atribuimos al PDF lo verificable. Las
        # discrepancias del registro vinculado se conservan por posible edición.
        doc = _documento(db, cid, codigo)
        atribuibles = {(c["tabla"], c["codigo"], c["campo"]) for c in _legado(db, cid, doc)}
        from database import TABLAS
        metadatos = {"codigo", "ruta", "carpeta", "origen", "ejercicio", "trimestre"}
        for tabla, fila in _vinculos_legados(db, cid, doc):
            for col in TABLAS[tabla].columnas_bd:
                campo, valor = col.campo, fila.get(col.campo)
                if campo in metadatos or campo in excepto or valor in (None, "") or (tabla, fila["codigo"], campo) in atribuibles:
                    continue
                protegidos[(tabla, fila["codigo"], campo)] = valor
    for ev in eventos:
        tabla, cod, campo = ev["tabla"], ev["codigo"], ev["campo"]
        if campo in excepto:
            continue
        actual = db.get(tabla, cid, cod)
        ultimo = db.con.execute("SELECT documento,posterior FROM documentos_cambios WHERE cliente_id=? "
            "AND tabla=? AND codigo=? AND campo=? AND activo=1 AND vigente=1 ORDER BY id DESC LIMIT 1",
            (cid, tabla, cod, campo)).fetchone()
        if actual and ultimo and (ultimo["documento"] is None or actual.get(campo) != json.loads(ultimo["posterior"])):
            valor = actual.get(campo)
            protegidos[(tabla, cod, campo)] = valor
    if ejercicio_fechas:
        for clave, valor in list(protegidos.items()):
            if clave[2] in ("fecha", "periodo_desde", "periodo_hasta") and valor:
                protegidos[clave] = date.fromisoformat(str(valor)).replace(year=ejercicio_fechas).isoformat()
    yield
    for (tabla, cod, campo), valor in protegidos.items():
        actual = db.get(tabla, cid, cod)
        if actual is not None:
            if actual.get(campo) == valor:
                # El nuevo PDF puede coincidir con la corrección. Aun así la intención
                # manual debe seguir siendo la fuente prioritaria al retirarlo después.
                db.con.execute("INSERT INTO documentos_cambios(cliente_id,documento,tabla,codigo,campo,anterior,posterior) "
                    "VALUES(?,NULL,?,?,?,?,?)", (cid, tabla, cod, campo, _json(valor), _json(valor)))
            elif valor is None:
                db.vaciar_campo(tabla, cid, cod, campo)
            else:
                db.upsert(tabla, cid, {"codigo": cod, campo: valor}, respetar_manuales=False)


def _plan_movimiento(db, cid, codigo, retirar):
    doc = _documento(db, cid, codigo)
    origen_rel = doc["ruta"] if retirar else doc["ruta_papelera"]
    origen_archivo = _ruta(db, cid, origen_rel, papelera=not retirar)
    huella = doc.get("sha256") or _huella(origen_archivo)
    destino_rel = f".Papelera_Documentos/{codigo}_{huella[:16]}{origen_archivo.suffix}" if retirar else doc["ruta"]
    return doc, origen_rel, origen_archivo, huella, destino_rel, _ruta(db, cid, destino_rel, papelera=retirar)


@contextmanager
def proteger_movimiento(db, cid, codigo, retirar):
    """Limpia la copia propia si el recálculo revierte la transacción exterior."""
    doc, _, _, huella, _, destino = _plan_movimiento(db, cid, codigo, retirar)
    copias = {}
    try:
        yield copias
    except BaseException:
        if (not db.con.in_transaction and destino in copias and destino.is_file()
                and _documento(db, cid, codigo)["en_papelera"] == doc["en_papelera"]
                and _huella(destino) == huella):
            info = destino.lstat()
            if (info.st_dev, info.st_ino, info.st_ctime_ns) == copias[destino]:
                destino.unlink()
        raise


def _cambiar_estado(db, cid, codigo, retirar, copias=None):
    recuperar_archivos(db, cid)
    _exigir(previsualizar(db, cid, codigo, "retirar" if retirar else "restaurar"))
    doc, origen_rel, origen_archivo, huella, destino_rel, destino = _plan_movimiento(db, cid, codigo, retirar)
    creado = _copiar_seguro(origen_archivo, destino, huella)
    identidad = None
    if creado:
        info = destino.lstat()
        identidad = (info.st_dev, info.st_ino, info.st_ctime_ns)
        if copias is not None:
            copias[destino] = identidad
    try:
        with db.transaccion():
            if not doc.get("sha256"):
                db.upsert("documentos", cid, {"codigo": codigo, "sha256": huella})
            if retirar and not _eventos(db, cid, codigo):
                _legado(db, cid, doc, guardar=True)
            _activar_datos(db, cid, codigo, not retirar)
            db.con.execute("INSERT INTO documentos_gestion(cliente_id,codigo,en_papelera,ruta_papelera,actualizado) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cliente_id,codigo) DO UPDATE SET en_papelera=excluded.en_papelera,"
                "ruta_papelera=excluded.ruta_papelera,actualizado=excluded.actualizado",
                (cid, codigo, int(retirar), destino_rel if retirar else None, datetime.now().isoformat(timespec="seconds")))
            db.con.execute("INSERT INTO documentos_archivos(cliente_id,documento,origen,destino,sha256) VALUES(?,?,?,?,?) "
                "ON CONFLICT(cliente_id,documento) DO UPDATE SET origen=excluded.origen,destino=excluded.destino,sha256=excluded.sha256",
                (cid, codigo, origen_rel, destino_rel, huella))
            _revisar(db, cid)
    except BaseException:
        if creado and destino.is_file() and _huella(destino) == huella:
            info = destino.lstat()
            if (info.st_dev, info.st_ino, info.st_ctime_ns) == identidad:
                destino.unlink()
        raise
    afectados = _ejercicios(db, cid, doc)
    recuperar_archivos(db, cid)
    return afectados


def retirar(db, cid, codigo, *, copias=None):
    return _cambiar_estado(db, cid, codigo, True, copias)


def restaurar(db, cid, codigo, *, copias=None):
    return _cambiar_estado(db, cid, codigo, False, copias)


def cambiar_ejercicio(db, cid, codigo, nuevo_ejercicio):
    _validar_ejercicio(nuevo_ejercicio)
    nuevo_ejercicio = int(nuevo_ejercicio)
    doc = _documento(db, cid, codigo)
    _exigir(previsualizar(db, cid, codigo, "cambiar_ejercicio", nuevo_ejercicio))
    afectados = _ejercicios(db, cid, doc) | {nuevo_ejercicio}
    if doc.get("ejercicio") == nuevo_ejercicio:
        return afectados
    if "escritura" in str(doc.get("tipo", "")).casefold():
        with db.transaccion():
            db.upsert("documentos", cid, {"codigo": codigo, "ejercicio": nuevo_ejercicio})
            _revisar(db, cid)
        return afectados
    from pdf_processor import Extraccion, extraer_local, _aplicar, TipoDocumento
    ruta = _ruta(db, cid, doc["ruta"])
    ext = Extraccion.model_validate_json(doc["extraccion_guardada"]) if doc.get("extraccion_guardada") else extraer_local(ruta)
    if ext.tipo == TipoDocumento.DESCONOCIDO:
        raise ValueError("El documento antiguo no permite identificar sus datos con seguridad. Sustitúyelo tras revisar la extracción.")
    ext.campos["ejercicio"] = nuevo_ejercicio
    factura_existente = None
    inmueble_destino = doc.get("inmueble")
    if ext.tipo == TipoDocumento.FACTURA_GASTO:
        # Partir de las fechas ya revisadas a mano, conservando mes y día.
        tablas_factura = {"gastos", "facturas_recibidas", "facturas_emitidas"}
        filas = {(ev["tabla"], ev["codigo"]) for ev in _eventos(db, cid, codigo) if ev["tabla"] in tablas_factura}
        if not filas:
            filas = {(tabla, fila["codigo"]) for tabla, fila in _vinculos_legados(db, cid, doc) if tabla in tablas_factura}
        if len(filas) > 1:
            raise ValueError("Hay varias facturas vinculadas a este documento. Revisa sus fechas en la ficha antes de trasladarlo.")
        factura_existente = next(iter(filas), None)
        for tabla, cod in filas:
            actual = db.get(tabla, cid, cod)
            if tabla == "gastos" and actual and not inmueble_destino:
                inmueble_destino = actual.get("inmueble")
            for campo in ("fecha", "periodo_desde", "periodo_hasta"):
                if actual and campo in actual:
                    ext.campos[campo] = actual.get(campo)
        if not ext.campos.get("fecha"):
            raise ValueError("Indica la fecha correcta de la factura antes de trasladarla a otro ejercicio.")
        for campo in ("fecha", "periodo_desde", "periodo_hasta"):
            if ext.campos.get(campo):
                fecha = date.fromisoformat(str(ext.campos[campo]))
                try:
                    ext.campos[campo] = fecha.replace(year=nuevo_ejercicio).isoformat()
                except ValueError:
                    raise ValueError("La fecha del documento no existe en el ejercicio destino. Corrígela antes de trasladarlo.") from None
    elif ext.tipo not in (TipoDocumento.CERTIFICADO_RESIDENCIA, TipoDocumento.CATASTRO_IBI,
                          TipoDocumento.VALOR_REFERENCIA, TipoDocumento.MODELO_PRESENTADO, TipoDocumento.MODELO_210):
        raise ValueError("Este documento contiene fechas de vigencia. Corrige sus fechas en los datos del expediente; no basta con cambiar la etiqueta del ejercicio.")
    with db.transaccion(), conservar_correcciones(db, cid, codigo,
            excepto={"ejercicio", "trimestre"},
            ejercicio_fechas=nuevo_ejercicio if ext.tipo == TipoDocumento.FACTURA_GASTO else None):
        if not _eventos(db, cid, codigo):
            _legado(db, cid, doc, guardar=True)
        _activar_datos(db, cid, codigo, False)
        db.con.execute("UPDATE documentos_cambios SET vigente=0 WHERE cliente_id=? AND documento=?", (cid, codigo))
        with origen(db, cid):
            resultado = _aplicar(db, cid, ext, ruta, inmueble_destino, nuevo_ejercicio,
                                 documento_existente=codigo, factura_existente=factura_existente)
        # Mantener el código original: la operación corrige datos, no duplica el soporte.
        datos_documento = {"codigo": codigo, "ejercicio": nuevo_ejercicio}
        if ext.tipo == TipoDocumento.FACTURA_GASTO:
            datos_documento["fecha_documento"] = ext.campos["fecha"]
        db.upsert("documentos", cid, datos_documento)
        _revisar(db, cid)
        afectados |= resultado.ejercicios_afectados
    return afectados
