"""Estrés A10: datos ficticios, sin red; métricas opcionales en A10_METRICAS_DIR."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc

import openpyxl
import pytest

import database
from database import Database
import excel_generator as eg
import fixtures_pdf as pdfs
import pdf_processor as pp
import tax_calculator as tc


def registrar(nombre, datos):
    carpeta = os.environ.get('A10_METRICAS_DIR')
    if carpeta:
        destino = Path(carpeta)
        destino.mkdir(parents=True, exist_ok=True)
        (destino / (nombre + '.json')).write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / 'solo_ficticios.sqlite', multihilo=True) as conexion:
        conexion.carpeta_clientes = tmp_path / 'Clientes'
        yield conexion


def test_excel_error_tardio_no_aplica_filas_anteriores(db, tmp_path):
    cliente = db.crear_cliente('Cliente', 'Solo pruebas', 'A10-EXCEL')
    cid = cliente['id']
    db.guardar_residencia(cid, 2026, pais='Francia')
    codigo = db.upsert('inmuebles', cid, {'direccion': 'Original', 'pct_titularidad': 1})
    ruta = eg.exportar_cliente(db, cid, 2026)
    wb = openpyxl.load_workbook(ruta)
    wb['01_RESIDENCIA_FISCAL']['B2'] = 'Rusia'  # Se procesa antes del inmueble inválido.
    wb['02_INMUEBLES']['B2'] = 'Cambio que debe revertirse'
    wb['02_INMUEBLES']['H3'] = 2  # 200 %, inválido después de la primera fila.
    wb.save(ruta)
    wb.close()
    hash_excel = hashlib.sha256(ruta.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        eg.import_excel_to_db(db, cid)
    assert db.residencia(cid, 2026)['pais'] == 'Francia'
    assert db.get('inmuebles', cid, codigo)['direccion'] == 'Original'
    assert not db.con.in_transaction
    assert hashlib.sha256(ruta.read_bytes()).hexdigest() == hash_excel


def test_pdf_error_tardio_restaura_todo_y_permite_reintentar(db, tmp_path, monkeypatch):
    cid = db.crear_cliente('Cliente', 'PDF', 'A10-PDF')['id']
    ruta = pdfs.recibo_ibi(tmp_path / 'ibi.pdf', ibi=450)
    ext = pp.extraer_local(ruta)
    assert ext.tipo == pp.TipoDocumento.CATASTRO_IBI
    original = db.upsert

    def fallar(tabla, *args, **kwargs):
        if tabla == 'gastos':
            raise OSError(errno.ENOSPC, 'Disco lleno simulado al guardar el gasto')
        return original(tabla, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(db, 'upsert', fallar)
        with pytest.raises(OSError, match='Disco lleno'):
            pp.aplicar(db, cid, ext, ruta, ejercicio=2026)
    for tabla in ('inmuebles', 'catastro', 'gastos', 'documentos'):
        assert db.listar(tabla, cid) == [], tabla
    assert not db.con.in_transaction
    res = pp.aplicar(db, cid, ext, ruta, ejercicio=2026)
    assert not res.duplicado
    assert all(len(db.listar(t, cid)) == 1 for t in ('inmuebles', 'catastro', 'gastos', 'documentos'))
    assert db.listar('gastos', cid)[0]['total'] == 450
    assert len(list(Path(db.get_cliente(cid)['carpeta']).joinpath('Soportes_PDF').glob('*.pdf'))) == 1


@pytest.fixture(scope='module')
def cartera_realista(tmp_path_factory):
    raiz = tmp_path_factory.mktemp('a10_600_clientes')
    ruta = raiz / 'cartera.sqlite'
    inicio = time.perf_counter()
    with Database(ruta, multihilo=True) as d:
        d.carpeta_clientes = raiz / 'Clientes'
        with d.transaccion():
            for i in range(1, 601):
                c = d.crear_cliente('Михаил' if i % 5 == 0 else 'Cliente', f'Prueba{i:04}', f'A10{i:08}')
                d.guardar_residencia(c['id'], 2026, pais='Rusia' if i % 5 == 0 else 'Francia')
            d.con.executemany('INSERT INTO inmuebles(cliente_id,codigo,fecha_adquisicion,pct_titularidad,estado) VALUES(?,?,?,?,?)',
                ((cid, f'INM-{n:03}', '2010-01-01', 1, 'Activo') for cid in range(1, 601) for n in (1, 2)))
            d.con.executemany('INSERT INTO catastro(cliente_id,codigo,inmueble,ejercicio,valor_catastral,anio_revision) VALUES(?,?,?,?,?,?)',
                ((cid, f'CAT-{n}-{ej}', f'INM-{n:03}', ej, 100000, 2010)
                 for cid in range(1, 601) for n in (1, 2) for ej in range(2017, 2027)))
            d.con.executemany('INSERT INTO gastos(cliente_id,codigo,inmueble,ejercicio,categoria,total,origen) VALUES(?,?,?,?,?,?,?)',
                ((cid, f'GAS-{n}-{ej}-{mes}', f'INM-{n:03}', ej, 'Comunidad', 50, 'manual')
                 for cid in range(1, 601) for n in (1, 2) for ej in range(2017, 2027) for mes in range(1, 13)))
            d.con.executemany('INSERT INTO documentos(cliente_id,codigo,ruta,sha256) VALUES(?,?,?,?)',
                ((cid, f'DOC-{n:03}', f'Soportes_PDF/ficticio{n}.pdf', f'{n:064x}')
                 for cid in range(1, 601) for n in range(60)))
        assert d.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    registrar('poblacion_600', {'clientes':600, 'rusia':120, 'inmuebles':1200, 'catastros':12000,
        'gastos':144000, 'documentos_registrados':36000, 'carpetas_reales_clientes':600,
        'segundos_poblacion':round(time.perf_counter()-inicio, 3)})
    return ruta


def test_600_expedientes_recalculo_integridad_y_repeticion(cartera_realista):
    from controlador import Controlador
    c = Controlador(cartera_realista)
    tiempos = []
    try:
        for cid in range(1, 601):
            inicio = time.perf_counter()
            res = c.abrir_cliente(cid, 2026)
            tiempos.append((time.perf_counter()-inicio)*1000)
            assert len(res['informe'].filas_210_imputacion) == 2
            filas = c.db.listar('m210_imputacion', cid)
            assert len(filas) == 2
            assert all(f['cuota'] == (480 if cid % 5 == 0 else 380) for f in filas)
        assert c.db.con.execute('SELECT SUM(cuota) FROM m210_imputacion').fetchone()[0] == 480000
        for cid in range(1, 601, 60):
            fila = c.db.listar('m210_imputacion', cid)[0]
            c.db.upsert('m210_imputacion', cid, {'codigo':fila['codigo'], 'estado':'Presentado', 'cuota':123.45})
            c.abrir_cliente(cid, 2026)
            assert c.db.get('m210_imputacion', cid, fila['codigo'])['cuota'] == 123.45
            assert len(c.db.listar('m210_imputacion', cid)) == 2
        assert c.db.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not c.db.con.in_transaction
    finally:
        c.cerrar()
    registrar('apertura_600', {'expedientes':600, 'segundos_total':round(sum(tiempos)/1000,3),
        'mediana_ms':round(statistics.median(tiempos),3), 'p95_ms':round(sorted(tiempos)[569],3),
        'max_ms':round(max(tiempos),3), 'cuota_sintetica_antes_presentados':480000,
        'presentados_preservados':10, 'nota':'Sin exportar 600 Excel ni renderizar ventanas Flet.'})


@pytest.fixture(scope='module')
def cartera_extrema(tmp_path_factory):
    raiz = tmp_path_factory.mktemp('a10_60000_clientes')
    ruta = raiz / 'extremo.sqlite'
    inicio = time.perf_counter()
    with Database(ruta, multihilo=True) as d:
        with d.transaccion():
            d.con.executemany('INSERT INTO clientes(id,nombre,apellidos,nif,carpeta,creado) VALUES(?,?,?,?,?,?)',
                ((i, 'Михаил' if i % 5 == 0 else 'Cliente', f'Prueba{i:06}', f'EXT{i:08}',
                  str(raiz / f'FICTICIO_{i}'), '2026-01-01') for i in range(1, 60001)))
            d.con.executemany('INSERT INTO documentos(cliente_id,codigo,ruta,sha256) VALUES(?,?,?,?)',
                ((cid, f'DOC-{n:03}', f'Soportes_PDF/{n}.pdf', f'{n:064x}')
                 for cid in range(1, 60001) for n in range(10)))
        assert d.con.execute('SELECT count(*) FROM documentos').fetchone()[0] == 600000
        assert d.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    registrar('poblacion_extrema', {'clientes':60000, 'documentos_registrados':600000,
        'segundos':round(time.perf_counter()-inicio,3), 'sqlite_bytes':ruta.stat().st_size,
        'nota':'Metadatos ficticios: no se crean 600000 PDFs ni 60000 carpetas.'})
    return ruta


def test_volumen_cien_veces_mayor_y_memoria_acotada(cartera_extrema):
    with Database(cartera_extrema, solo_lectura=True) as d:
        tiempos = []
        for offset in (0, 30000, 59950):
            for _ in range(5):
                inicio = time.perf_counter()
                pagina = d.listar_clientes(limite=50, desplazamiento=offset)
                tiempos.append((time.perf_counter()-inicio)*1000)
                assert len(pagina) == 50
        tracemalloc.start()
        try:
            pagina = d.listar_clientes()
            pico = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert len(pagina) == 50 and pico < 1_000_000
        ids = [f['id'] for offset in range(0, 60000, 200)
               for f in d.listar_clientes(limite=200, desplazamiento=offset)]
        assert len(ids) == len(set(ids)) == 60000
    registrar('lectura_extrema', {'mediana_ms':round(statistics.median(tiempos),3),
        'max_ms':round(max(tiempos),3), 'pico_resultado_python_bytes':pico,
        'paginas_200_verificadas':300, 'sin_duplicados_ni_saltos':True})


def test_ocho_lectores_y_un_escritor_wal(cartera_extrema):
    barrera = threading.Barrier(9, timeout=20)

    def lector(indice):
        with Database(cartera_extrema, solo_lectura=True) as d:
            barrera.wait()
            tiempos = []
            for n in range(20):
                inicio = time.perf_counter()
                if n % 4 == 0:
                    filas = d.listar_clientes(busqueda='михаил')
                else:
                    filas = d.listar_clientes(desplazamiento=(indice*500+n*50))
                assert len(filas) == 50
                assert len(d.listar_pagina('documentos', 60000)) == 10
                tiempos.append((time.perf_counter()-inicio)*1000)
            return tiempos

    def escritor():
        with Database(cartera_extrema, multihilo=True) as d:
            barrera.wait()
            for lote in range(20):
                with d.transaccion():
                    for n in range(10):
                        d.upsert('notas', 1, {'nota':f'Escritura {lote}-{n}'}, clave=f'A10|{lote}|{n}')
            return 200

    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=9) as ejecutor:
        lectores = [ejecutor.submit(lector, i) for i in range(8)]
        escritura = ejecutor.submit(escritor)
        tiempos = [t for f in lectores for t in f.result(timeout=120)]
        assert escritura.result(timeout=30) == 200
    with Database(cartera_extrema, solo_lectura=True) as d:
        assert len(d.listar('notas',1)) == 200
        assert d.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    registrar('concurrencia_wal', {'lectores':8, 'consultas_por_lector':40, 'escrituras':200,
        'segundos_total':round(time.perf_counter()-inicio,3),
        'mediana_par_consultas_ms':round(statistics.median(tiempos),3),
        'p95_par_consultas_ms':round(sorted(tiempos)[151],3), 'max_par_consultas_ms':round(max(tiempos),3),
        'nota':'Conexión propia por lector y escritor; búsquedas de contenido intercaladas.'})


def test_ocho_altas_concurrentes_controlador(tmp_path):
    from controlador import Controlador
    c = Controlador(tmp_path / 'concurrencia.sqlite')
    c.cambiar_carpeta_clientes(tmp_path / 'Clientes')
    try:
        def altas(i):
            return [c.crear_cliente('Cliente', f'Hilo{i}', f'A10-HILO-{i}-{n}')['id'] for n in range(50)]
        with ThreadPoolExecutor(max_workers=8) as e:
            ids = [cid for lote in e.map(altas, range(8)) for cid in lote]
        assert len(ids) == len(set(ids)) == 400
        assert c.db.con.execute('SELECT count(*) FROM clientes').fetchone()[0] == 400
        assert c.db.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        c.cerrar()


def test_cierre_brusco_proceso_revierte_lote(db):
    cid = db.crear_cliente('Cliente', 'Interrupción', 'A10-CRASH')['id']
    repo = Path(database.__file__).resolve().parent
    codigo = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from database import Database
d=Database(Path(sys.argv[2]),multihilo=True)
with d.transaccion():
    d.upsert('notas',int(sys.argv[3]),{'codigo':'NOT-CRASH','nota':'No debe confirmarse'})
    os._exit(23)
"""
    p = subprocess.run([sys.executable,'-c',codigo,str(repo),str(db.db_path),str(cid)],capture_output=True,timeout=30)
    assert p.returncode == 23, p.stderr.decode(errors='replace')
    assert db.get('notas',cid,'NOT-CRASH') is None
    assert db.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with db.transaccion():
        db.upsert('notas',cid,{'codigo':'NOT-RECUPERADO','nota':'Funciona después'})
    assert db.get('notas',cid,'NOT-RECUPERADO')


def test_disco_lleno_al_copiar_pdf_no_deja_archivo_parcial(db, tmp_path, monkeypatch):
    c = db.crear_cliente('Cliente', 'Disco', 'A10-DISCO')
    ruta = pdfs.recibo_ibi(tmp_path / 'origen.pdf')
    huella = hashlib.sha256(ruta.read_bytes()).hexdigest()
    original = pp.shutil.copyfileobj

    def fallar(origen, destino, **kwargs):
        destino.write(origen.read(20))
        raise OSError(errno.ENOSPC, 'Disco lleno simulado')

    with monkeypatch.context() as m:
        m.setattr(pp.shutil, 'copyfileobj', fallar)
        with pytest.raises(OSError, match='Disco lleno'):
            pp.archivar_pdf(ruta, Path(c['carpeta']), pp.TipoDocumento.CATASTRO_IBI, None)
    assert pp.shutil.copyfileobj == original
    assert list((Path(c['carpeta'])/'Soportes_PDF').iterdir()) == []
    assert hashlib.sha256(ruta.read_bytes()).hexdigest() == huella
    relativo, copiado = pp.archivar_pdf(ruta, Path(c['carpeta']), pp.TipoDocumento.CATASTRO_IBI, None)
    assert huella == copiado == hashlib.sha256((Path(c['carpeta'])/relativo).read_bytes()).hexdigest()


def test_cincuenta_archivados_concurrentes_no_pierden_datos(db, tmp_path):
    c = db.crear_cliente('Cliente', 'Copia simultánea', 'A10-ARCHIVOS')
    fuentes = [pdfs.recibo_ibi(tmp_path / str(i) / 'mismo_nombre.pdf', ibi=400+i) for i in range(50)]
    esperadas = {hashlib.sha256(p.read_bytes()).hexdigest() for p in fuentes}
    with ThreadPoolExecutor(max_workers=8) as e:
        resultados = list(e.map(lambda ruta: pp.archivar_pdf(ruta, Path(c['carpeta']), pp.TipoDocumento.CATASTRO_IBI, date(2026,1,1)), fuentes))
    assert len({r for r,h in resultados}) == len(esperadas) == 50
    for relativo, huella in resultados:
        assert hashlib.sha256((Path(c['carpeta'])/relativo).read_bytes()).hexdigest() == huella


def test_escritura_excel_sin_espacio_preserva_original(db, monkeypatch):
    c = db.crear_cliente('Cliente', 'Excel interrumpido', 'A10-XLSX')
    ruta = eg.exportar_cliente(db, c['id'], 2026)
    original = ruta.read_bytes()

    def interrumpir(wb, destino):
        Path(destino).write_bytes(b'PK-zip-parcial')
        raise OSError(errno.ENOSPC, 'Disco lleno simulado al guardar Excel')

    with monkeypatch.context() as m:
        m.setattr(openpyxl.workbook.workbook.Workbook, 'save', interrumpir)
        with pytest.raises(OSError, match='Disco lleno'):
            eg.exportar_cliente(db, c['id'], 2026)
    assert ruta.read_bytes() == original
    assert not list(ruta.parent.glob('.nr_*.xlsx'))
    assert eg.verificar_estructura(ruta) == []


def test_importacion_excel_confirma_una_vez_y_resiste_repeticion(db):
    cid = db.crear_cliente('Cliente', 'Importación en lote', 'A10-LOTE-EXCEL')['id']
    db.guardar_residencia(cid, 2026, pais='Francia')
    with db.transaccion():
        for n in range(100):
            db.upsert('gastos', cid, {'categoria':'Comunidad','ejercicio':2026,'total':n+0.25,'origen':'excel'})
    ruta = eg.exportar_cliente(db, cid, 2026)
    db.set_config(f'excel_firma|{cid}', '')
    eventos = []
    db.con.set_trace_callback(eventos.append)
    try:
        conteos = eg.import_excel_to_db(db, cid)
    finally:
        db.con.set_trace_callback(None)
    assert conteos['gastos'] == 100
    assert sum(q.strip().upper()=='COMMIT' for q in eventos) == 1
    assert len(db.listar('gastos', cid)) == 100
    assert sum(f['total'] for f in db.listar('gastos', cid)) == 4975
    eg.import_excel_to_db(db, cid)
    assert len(db.listar('gastos', cid)) == 100
    assert eg.verificar_estructura(ruta) == []


def test_pdf_lote_anidado_se_revierte_con_transaccion_exterior(db, tmp_path):
    cid = db.crear_cliente('Cliente', 'Transacción exterior', 'A10-EXTERIOR')['id']
    ruta = pdfs.recibo_ibi(tmp_path/'exterior.pdf')
    with pytest.raises(RuntimeError, match='Cancelar lote exterior'):
        with db.transaccion():
            pp.aplicar(db, cid, pp.extraer_local(ruta), ruta, ejercicio=2026)
            assert len(db.listar('documentos',cid)) == 1
            raise RuntimeError('Cancelar lote exterior')
    for tabla in ('documentos','inmuebles','catastro','gastos'):
        assert db.listar(tabla,cid) == []
    assert db._nivel_transaccion == 0 and not db.con.in_transaction


def test_reserva_escritura_agotada_no_corrompe_y_reintenta(db):
    cid = db.crear_cliente('Cliente', 'Escritura ocupada', 'A10-BUSY')['id']
    with Database(db.db_path, multihilo=True) as otra:
        otra.con.execute('PRAGMA busy_timeout=50')
        with db.transaccion():
            db.upsert('notas',cid,{'codigo':'NOT-PRIMERA','nota':'Confirmada al salir'})
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                with otra.transaccion():
                    pytest.fail('No debe entrar mientras la reserva está ocupada')
            assert otra._nivel_transaccion == 0 and not otra.con.in_transaction
        with otra.transaccion():
            otra.upsert('notas',cid,{'codigo':'NOT-SEGUNDA','nota':'Reintento después de liberar'})
    assert len(db.listar('notas',cid)) == 2
    assert db.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_pdf_quinientas_paginas_no_trunca_texto(tmp_path):
    import fitz
    ruta = tmp_path / 'historico_500_paginas.pdf'
    with fitz.open() as doc:
        for n in range(500):
            pagina = doc.new_page()
            pagina.insert_text((45,60), f'PAGINA-{n:04} Documento sintetico de contabilidad. '
                'Comunidad, seguro e ingresos ficticios para comprobar lectura completa sin OCR.')
        doc.save(str(ruta))
    inicio = time.perf_counter()
    tracemalloc.start()
    try:
        texto, ocr = pp.extraer_texto(ruta)
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert not ocr and texto.count('PAGINA-') == 500
    assert all(f'PAGINA-{n:04}' in texto for n in range(500))
    registrar('pdf_500_paginas', {'paginas':500,'segundos':round(time.perf_counter()-inicio,3),
        'pdf_bytes':ruta.stat().st_size,'caracteres':len(texto),'pico_python_bytes':pico,
        'nota':'PDF digital; no mide OCR ni memoria nativa de PyMuPDF.'})


def test_bandeja_danada_o_cifrada_aisla_errores_y_sigue(tmp_path):
    import fitz
    from controlador import Controlador
    c = Controlador(tmp_path / 'bandeja.sqlite')
    c.cambiar_carpeta_clientes(tmp_path / 'Clientes')
    try:
        cliente = c.crear_cliente('Cliente', 'Documentos dañados', 'A10-DANADOS')
        bandeja = Path(cliente['carpeta']) / database.SUBCARPETA_ENTRADA
        (bandeja / 'vacio.pdf').write_bytes(b'')
        (bandeja / 'roto.pdf').write_bytes(b'%PDF-1.7\ncontenido truncado sin objetos')
        with fitz.open() as doc:
            doc.new_page().insert_text((45,60), 'PDF protegido ficticio de prueba.')
            doc.save(str(bandeja/'cifrado.pdf'), encryption=fitz.PDF_ENCRYPT_AES_256,
                owner_pw='dueno-ficticio', user_pw='clave-ficticia')
        resultados = c.procesar_bandeja(cliente['id'], 2026)
        assert len(resultados) == 3 and all(not r['ok'] for r in resultados)
        assert all(r['ruta_error'].is_file() and r['txt_error'].is_file() for r in resultados)
        assert c.db.listar('documentos',cliente['id']) == []
        assert c.procesar_bandeja(cliente['id'],2026) == []  # No bucle de reintentos.
        assert len(list((bandeja/'_errores').glob('*.pdf'))) == 3
        c.crear_cliente('Cliente', 'Después del error', 'A10-SIGUE')
        assert c.db.con.execute('SELECT count(*) FROM clientes').fetchone()[0] == 2
    finally:
        c.cerrar()


def test_excel_guardado_durante_importacion_no_se_sobrescribe(tmp_path, monkeypatch):
    from controlador import Controlador
    c = Controlador(tmp_path / 'excel_simultaneo.sqlite')
    c.cambiar_carpeta_clientes(tmp_path / 'Clientes')
    try:
        cid = c.crear_cliente('Cliente', 'Excel simultáneo', 'A10-RACE')['id']
        ruta = eg.exportar_cliente(c.db, cid, 2026)
        wb = openpyxl.load_workbook(ruta)
        wb['00_RESUMEN']['B10'] = 'Guardado por usuario mientras se importaba'
        externa = tmp_path / 'version_usuario.xlsx'
        wb.save(externa)
        wb.close()
        manual = externa.read_bytes()
        original = eg.import_excel_to_db

        def importar_y_guardar(*args, **kwargs):
            resultado = original(*args, **kwargs)
            # Reproduce el guardado externo antes de capturar la firma en _importar.
            ruta.write_bytes(manual)
            return resultado

        monkeypatch.setattr(eg, 'import_excel_to_db', importar_y_guardar)
        resultado = c.sincronizar(cid, 2026)
        assert ruta.read_bytes() == manual
        assert resultado['error_excel'] and 'Guardaste' in resultado['error_excel']
        assert resultado['excel'] != ruta and resultado['excel'].is_file()
        monkeypatch.setattr(eg, 'import_excel_to_db', original)
        sincronizado = c.sincronizar(cid, 2026)
        assert sincronizado['error_excel'] is None
        assert c.db.get_cliente(cid)['responsable'] == 'Guardado por usuario mientras se importaba'
    finally:
        c.cerrar()
