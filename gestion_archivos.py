"""Renombrado acotado a Soportes_PDF, con copia verificada y diario recuperable."""
from contextlib import contextmanager
from pathlib import Path

import documentos
from acciones_documentales import validar_nombre


def preparar(db, cid, codigo, nombre):
    validar_nombre(nombre)
    doc = documentos._documento(db, cid, codigo)
    if doc['en_papelera']:
        raise ValueError('Restaura el documento antes de modificarlo.')
    origen = documentos._ruta(db, cid, doc['ruta'])
    relativa = 'Soportes_PDF/' + nombre
    destino = documentos._ruta(db, cid, relativa)
    if not origen.is_file():
        raise ValueError('No se encuentra el PDF de origen.')
    huella = documentos._huella(origen)
    if doc.get('sha256') and doc['sha256'] != huella:
        raise ValueError('El PDF cambió respecto al documento registrado.')
    colision = any(f['codigo'] != codigo and str(f.get('ruta', '')).casefold() == relativa.casefold()
                   for f in db.iterar('documentos', cid, incluir_retirados=True))
    if destino != origen and (destino.exists() or colision):
        raise ValueError('El nombre de destino ya existe; no se sobrescribe ningún archivo.')
    if destino == origen and nombre != Path(doc['ruta']).name:
        raise ValueError('Para cambiar solo mayúsculas, elige primero un nombre diferente.')
    return doc, origen, relativa, destino, huella


@contextmanager
def proteger(db, cid, codigo, nombre):
    doc, origen, _, destino, huella = preparar(db, cid, codigo, nombre)
    copias = {}
    try:
        yield copias
    except BaseException:
        # Solo limpiar NUESTRA copia tras rollback, nunca archivos previos ni cambios confirmados.
        actual = db.get('documentos', cid, codigo)
        if (not db.con.in_transaction and destino in copias and actual and actual['ruta'] == doc['ruta']
                and destino.is_file() and documentos._huella(destino) == huella):
            info = destino.lstat()
            if (info.st_dev, info.st_ino, info.st_ctime_ns) == copias[destino]:
                destino.unlink()
        raise


def renombrar(db, cid, codigo, nombre, descripcion=None, *, copias):
    doc, origen, relativa, destino, huella = preparar(db, cid, codigo, nombre)
    if origen != destino:
        if documentos._copiar_seguro(origen, destino, huella):
            info = destino.lstat()
            copias[destino] = (info.st_dev, info.st_ino, info.st_ctime_ns)
    # El controlador mantiene la transacción exterior y limpia la copia si revierte el recálculo.
    with db.transaccion():
        previo = db._documento_origen
        db._documento_origen = (cid, codigo)
        try:
            for tabla, fila in documentos._vinculos_legados(db, cid, doc):
                campos = {k: relativa for k in ('ruta', 'carpeta') if fila.get(k) == doc['ruta']}
                if campos:
                    db.upsert(tabla, cid, {'codigo': fila['codigo'], **campos}, respetar_manuales=False)
            datos = {'codigo': codigo, 'ruta': relativa, 'sha256': huella}
            if descripcion is not None:
                datos['descripcion'] = descripcion
            db.upsert('documentos', cid, datos, respetar_manuales=False)
            if descripcion == '':
                db.vaciar_campo('documentos', cid, codigo, 'descripcion')
        finally:
            db._documento_origen = previo
        if origen != destino:
            db.con.execute('INSERT INTO documentos_archivos(cliente_id,documento,origen,destino,sha256) '
                           'VALUES(?,?,?,?,?)', (cid, codigo, doc['ruta'], relativa, huella))
        documentos._revisar(db, cid)
    return documentos._ejercicios(db, cid, doc)
