"""Auditoría A9. Solo expedientes ficticios; ninguna conexión a Claude ni keyring real.

Pruebas: python -m pytest tests/test_stress_audit.py -q
Medición: python tests/test_stress_audit.py --benchmark --output informe.json
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
if not (REPO / 'database.py').is_file():
    REPO = Path('C:/Users/34603/Desktop/Udea/informatica/Proyectos antigravity/programa1')
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))
import database
from database import Database
import ia_extractor as ia
import pdf_processor as pp
import tax_calculator as tc
import fixtures_pdf as pdfs


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setattr(ia, 'obtener_clave', lambda: None)
    monkeypatch.setattr(ia, '_keyring', lambda: None)
    monkeypatch.setattr(ia, '_cliente', lambda: pytest.fail('Prohibida la API real en A9'))


def poblar(db, raiz, clientes=10_000):
    """Carga masiva controlada: no crea 10.000 carpetas/Excel ni escribe fuera del temporal."""
    db.carpeta_clientes = raiz / 'Clientes'
    filas, residencias = [], []
    for i in range(clientes):
        ruso = i % 5 == 0
        filas.append((i+1, 'Михаил' if ruso else 'Cliente', 'Иванов' if ruso else f'Apellido{i:06}',
                      f'A9{i:08}', 'Rusia' if ruso else 'Francia',
                      str(raiz / 'Clientes' / f'FICTICIO_{i:08}'), '2026-01-01'))
        residencias.append((i+1, '2026', 2026, 'Rusia' if ruso else 'Francia'))
    db.con.executemany('INSERT INTO clientes(id,nombre,apellidos,nif,nacionalidad,carpeta,creado) VALUES(?,?,?,?,?,?,?)', filas)
    db.con.executemany('INSERT INTO residencias(cliente_id,codigo,ejercicio,pais) VALUES(?,?,?,?)', residencias)
    db.con.executemany('INSERT INTO inmuebles(cliente_id,codigo,direccion,ref_catastral) VALUES(?,?,?,?)',
                      ((1, f'INM-{i:04}', f'Calle Ficticia {i}', f'REF{i:018}') for i in range(1000)))
    db.con.executemany('INSERT INTO catastro(cliente_id,codigo,inmueble,ejercicio,valor_catastral) VALUES(?,?,?,?,?)',
                      ((1, f'CAT-{i:04}-{ej}', f'INM-{i:04}', ej, 100000+ej)
                       for i in range(200) for ej in range(2000, 2027)))
    db.con.executemany('INSERT INTO documentos(cliente_id,codigo,ruta,sha256) VALUES(?,?,?,?)',
                      ((1, f'DOC-{i:05}', f'Soportes_PDF/{i}.pdf', f'{i:064x}') for i in range(5000)))
    db.con.commit()


@pytest.fixture(scope='module')
def volumen(tmp_path_factory):
    raiz = tmp_path_factory.mktemp('a9_volumen')
    db = Database(raiz / 'ficticios.sqlite')
    poblar(db, raiz)
    yield db
    db.cerrar()


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / 'ficticio.sqlite') as db:
        db.carpeta_clientes = tmp_path / 'Clientes'
        yield db


def test_poblacion_masiva_20_por_ciento_rusia(volumen):
    total, rusos = volumen.con.execute("SELECT count(*),sum(nacionalidad='Rusia') FROM clientes").fetchone()
    assert total == 10_000 and rusos == 2000


def test_listado_por_defecto_acotado(volumen):
    assert len(volumen.listar_clientes()) == 50


def test_paginacion_sin_saltos_ni_duplicados(volumen):
    esperados = [r[0] for r in volumen.con.execute('SELECT id FROM clientes ORDER BY apellidos,nombre,id')]
    ids = [r['id'] for offset in range(0, 10_000, 200)
           for r in volumen.listar_clientes(limite=200, desplazamiento=offset)]
    assert ids == esperados and len(set(ids)) == 10_000


@pytest.mark.parametrize('limite,offset', [(0,0), (201,0), (-1,0), (10,-1), (True,0), (10,'0')])
def test_limites_invalidos_rechazados(volumen, limite, offset):
    with pytest.raises(ValueError):
        volumen.listar_clientes(limite=limite, desplazamiento=offset)


def test_busqueda_unicode_y_nif_sin_comodines(volumen):
    assert len(volumen.listar_clientes(busqueda='мИХАИЛ иВАНОВ')) == 50
    assert volumen.listar_clientes(busqueda='A900000005')[0]['id'] == 6
    assert volumen.listar_clientes(busqueda="%' OR 1=1 --") == []


def test_indices_y_orden_sin_tabla_temporal(volumen):
    plan = lambda sql, args=(): ' '.join(r['detail'] for r in volumen.con.execute('EXPLAIN QUERY PLAN '+sql, args))
    assert 'INDEX' in plan('SELECT * FROM clientes WHERE nif=?', ('A900000005',))
    orden = plan('SELECT * FROM clientes ORDER BY apellidos,nombre,id LIMIT 50')
    assert 'ix_clientes_orden' in orden and 'TEMP B-TREE' not in orden
    assert 'ref_catastral' in plan('SELECT * FROM inmuebles WHERE cliente_id=? AND ref_catastral=?', (1,'REF'))
    assert 'ix_catastro_inmueble_ejercicio_codigo' in plan(
        'SELECT * FROM catastro WHERE cliente_id=? AND inmueble=? AND ejercicio=?', (1,'INM-0000',2026))
    assert 'sha256' in plan('SELECT codigo FROM documentos WHERE cliente_id=? AND sha256=?', (1,'0'*64))


def test_pagina_historica_y_streaming_completo(volumen):
    pagina = volumen.listar_pagina('documentos', 1, limite=50, desplazamiento=100)
    assert len(pagina) == 50 and pagina[0]['codigo'] == 'DOC-00100'
    assert sum(1 for _ in volumen.iterar('documentos',1,tamano=37)) == 5000
    assert len(volumen.listar('inmuebles',1)) == 1000  # nunca truncar cálculos fiscales


def test_memoria_lista_de_clientes_acotada(volumen):
    tracemalloc.start()
    try:
        filas = volumen.listar_clientes()
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(filas) == 50 and pico < 1_000_000


def test_historicos_no_materializados_en_python(volumen, monkeypatch):
    monkeypatch.setattr(volumen,'listar',lambda *a,**kw: pytest.fail('No cargar historial completo'))
    assert volumen.residencia(1,2027)['pais'] == 'Rusia'
    assert volumen.catastro(1,'INM-0000',2027)['_heredado'] == 2026


def test_solo_lectura_no_migra_ni_escribe(volumen):
    with Database(volumen.db_path, solo_lectura=True) as lector:
        assert len(lector.listar_clientes()) == 50
        with pytest.raises(sqlite3.OperationalError):
            lector.con.execute("INSERT INTO config VALUES('prohibido','1')")


def test_rusia_24_sin_gastos_toda_la_cohorte(volumen):
    inmueble = {'codigo':'INM-001','pct_titularidad':1,'fecha_adquisicion':'2020-01-01'}
    contratos = [{'codigo':'CTR-001','fecha_inicio':'2026-01-01','renta_inicial':1000,
                  'periodicidad':'Mensual','estado':'Vigente'}]
    gastos = [{'categoria':'Comunidad','total':300,'fecha':'2026-02-10','ejercicio':2026}]
    rusos = volumen.con.execute("SELECT pais FROM residencias WHERE pais='Rusia'")
    n = 0
    for fila in rusos:
        r = tc.resolver_residencia(dict(fila))
        resultados = tc.calcular_210_alquiler(inmueble, contratos, {}, gastos, None, r, 2026)
        assert len(resultados) == 4
        assert all(t.ingresos == 3000 and t.gastos == 0 and t.cuota == 720 for t in resultados)
        n += 1
    assert n == 2000


@pytest.mark.parametrize('pais', ['Rusia','Россия','Estados Unidos','Reino Unido'])
def test_extra_ue_extremos(pais):
    res = tc.resolver_residencia({'pais':pais})
    assert res.grupo == tc.GRUPO_EXTRA
    assert tc.tipo_gravamen_210(res.grupo) == .24
    assert tc.gastos_deducibles(res.grupo) is False


@pytest.mark.parametrize('nombre,apellido', [('Михаил','Иванов'),('CON','AUX'),
    ('名'*300,'Иванов:*?<>|/\\'),('José 😀','Núñez " : / \\ ..')])
def test_rutas_internacionales_seguras(db,nombre,apellido):
    cliente = db.crear_cliente(nombre,apellido,'A9-RUTA-01')
    base = Path(cliente['carpeta'])
    assert base.is_dir() and len(base.name) <= 64
    assert not any(c in base.name for c in '<>:"/\\|?*')
    assert len(db.nombre_excel(cliente)) <= 69
    assert db.get_cliente(cliente['id'])['nombre'] == nombre.strip()
    if nombre == 'Михаил':
        assert 'Михаил' in base.name


def test_identificadores_sanitizados_no_comparten_carpeta(db):
    a = db.crear_cliente('Persona','Igual','A/B')
    b = db.crear_cliente('Persona','Igual','A:B')
    assert Path(a['carpeta']).resolve() != Path(b['carpeta']).resolve()


def test_nif_vacio_no_consolida_alta(db):
    with pytest.raises(ValueError):
        db.crear_cliente('Persona','Igual','   ')
    assert db.con.execute('SELECT count(*) FROM clientes').fetchone()[0] == 0


def test_inyeccion_sql_aislada_y_selectores_validados(db):
    cid = db.crear_cliente("Robert'); DROP TABLE clientes;--",'Ficticio',"A9' OR 1=1--")['id']
    otro = db.crear_cliente('Otra','Persona','A9-OTRO')['id']
    cod = db.upsert('inmuebles',cid,{'direccion':"'; DELETE FROM clientes;--"})
    assert db.get('inmuebles',otro,cod) is None
    assert db.get_cliente_por_nif("A9' OR 1=1--")['id'] == cid
    assert db.listar('inmuebles',cid,direccion="x' OR 1=1 --") == []
    for kwargs in ({'tabla':'inmuebles; DROP TABLE clientes'}, {'orden':'codigo; DROP TABLE clientes'},
                   {'filtro_malicioso':'x'}):
        tabla = kwargs.pop('tabla','inmuebles')
        with pytest.raises(KeyError):
            db.listar(tabla,cid,**kwargs)
    assert db.con.execute('SELECT count(*) FROM clientes').fetchone()[0] == 2


@pytest.mark.parametrize('relativa', ['../Otro/Soportes_PDF/secreto.pdf','C:/Otro/secreto.pdf',
    '\\\\servidor\\secreto.pdf','Soportes_PDF/../../secreto.pdf','file:///C:/privado.pdf',
    'Soportes_PDF/factura:oculto.pdf','Soportes_PDF/factura?.pdf'])
def test_abrir_soporte_rechaza_escape(db,relativa):
    c = db.crear_cliente('Cliente','Ficticio','A9-UNO')
    with pytest.raises(ValueError):
        database.ruta_soporte(Path(c['carpeta']),relativa)


def test_archivado_rechaza_junction_otro_cliente(db,tmp_path):
    if os.name != 'nt':
        pytest.skip('Junction real de Windows')
    a = Path(db.crear_cliente('Cliente','A','A9-A')['carpeta'])
    b = Path(db.crear_cliente('Cliente','B','A9-B')['carpeta'])
    enlace, destino = a/'Soportes_PDF', b/'Soportes_PDF'
    enlace.rmdir()
    proceso = subprocess.run(['cmd','/c','mklink','/J',str(enlace),str(destino)],capture_output=True)
    assert proceso.returncode == 0
    try:
        ruta = pdfs.recibo_ibi(tmp_path/'prueba.pdf')
        with pytest.raises(ValueError):
            pp.archivar_pdf(ruta,a,pp.TipoDocumento.CATASTRO_IBI,None)
        assert list(destino.iterdir()) == []
    finally:
        os.rmdir(enlace)  # elimina solo la junction creada por esta prueba


@pytest.mark.parametrize('caso', ['', '{}','null','[]','{"campos":null}','timeout','clave'])
def test_haiku_fallido_permanece_lector_y_alta_manual(db,tmp_path,monkeypatch,caso):
    import anthropic
    import httpx2 as httpx
    monkeypatch.setattr(ia,'obtener_clave',lambda:'credencial-ficticia-A9')
    def crear(**kw):
        assert kw['model'] == 'claude-haiku-5-5' and 'fallbacks' not in kw
        if caso == 'timeout':
            raise TimeoutError('Tiempo agotado ficticio')
        if caso == 'clave':
            req = httpx.Request('POST','https://no-se-contacta.invalid')
            raise anthropic.AuthenticationError('Ficticio',response=httpx.Response(401,request=req),body={})
        return NS(stop_reason='end_turn',content=[NS(type='text',text=caso)])
    monkeypatch.setattr(ia,'_cliente',lambda:NS(messages=NS(create=crear)))
    ext = pp.extraer(pdfs.recibo_ibi(tmp_path/'recibo.pdf',ibi=450),ia.AjustesIA())
    assert ext.motor == 'local' and ext.campos['ibi_ejercicio'] == 450
    assert ext.avisos and 'IA' in ext.avisos[0]
    c = db.crear_cliente('Михаил','Иванов','A9-MANUAL')
    codigo = db.upsert('gastos',c['id'],{'categoria':'IBI','ejercicio':2026,'total':451})
    assert db.get('gastos',c['id'],codigo)['total'] == 451


def test_credencial_entorno_no_se_guarda_en_bd(db,monkeypatch):
    # recuperar la función original que el fixture sustituye, sin leer keyring ni el entorno real
    import importlib.util
    spec = importlib.util.spec_from_file_location('ia_a9_aislada',REPO/'ia_extractor.py')
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    try:
        spec.loader.exec_module(modulo)
        monkeypatch.setattr(modulo,'_keyring',lambda:pytest.fail('No acceder al almacén real'))
        monkeypatch.setenv('ANTHROPIC_API_KEY','credencial-ficticia-solo-A9')
        assert modulo.obtener_clave() == 'credencial-ficticia-solo-A9'
        modulo.AjustesIA().guardar(db)
        assert not any('credencial-ficticia' in str(r['valor']) for r in db.con.execute('SELECT valor FROM config'))
    finally:
        sys.modules.pop(spec.name,None)


def test_unicode_excel_real_y_manual_tras_timeout(tmp_path,monkeypatch):
    from controlador import Controlador
    import openpyxl
    import excel_generator as eg
    monkeypatch.setattr(ia,'obtener_clave',lambda:'credencial-ficticia-A9')
    def agotar():
        raise TimeoutError('Tiempo agotado ficticio')
    monkeypatch.setattr(ia,'_cliente',agotar)
    respaldo = pp.extraer(pdfs.recibo_ibi(tmp_path/'respaldo.pdf'),ia.AjustesIA())
    assert respaldo.motor == 'local'
    ctrl = Controlador(tmp_path/'c.sqlite')
    try:
        ctrl.cambiar_carpeta_clientes(tmp_path/'Clientes')
        c = ctrl.crear_cliente('Михаил','Иванов /:*?','A9-EXCEL',pais='Rusia',ejercicio=2026)
        res = ctrl.editar(c['id'],'inmuebles',None,{'direccion':'Calle Ficticia 10',
            'fecha_adquisicion':'2020-01-01','pct_titularidad':1},2026)
        ruta = ctrl.db.ruta_excel(c)
        assert ruta.is_file() and eg.verificar_estructura(ruta) == []
        wb = openpyxl.load_workbook(ruta,read_only=True)
        try:
            assert len(wb.sheetnames) == 17
        finally:
            wb.close()
        assert ctrl.db.get_cliente(c['id'])['nombre'] == 'Михаил'
    finally:
        ctrl.cerrar()


def app_lista(db):
    import main
    import ui_estilo as ui
    a = main.App.__new__(main.App)
    a.ctrl,a.p,a.cid,a.filtro = NS(lector=db),ui.CLARO,1,''
    a.cerrando,a._pagina_clientes,a._busqueda_version = False,0,0
    a.cartera = 'NR'  # cartera activa de la barra lateral
    a.page,a.lista_clientes = NS(update=lambda:None),NS(controls=[])
    a.avisar = lambda *a,**kw:pytest.fail('Error inesperado en la búsqueda')
    return a


def test_ui_no_construye_miles_de_controles(volumen):
    a = app_lista(volumen)
    a._refrescar_clientes()
    assert len(a.lista_clientes.controls) == 51  # 50 filas y navegación
    a._mover_pagina_clientes(1)
    assert a._pagina_clientes == 1 and len(a.lista_clientes.controls) == 51


def test_busqueda_asincrona_no_bloquea_y_descarta_resultado_antiguo(volumen,monkeypatch):
    a = app_lista(volumen)
    original = Database.listar_clientes
    hilos, pintados = [], []
    principal = threading.get_ident()
    def demorar(self,*args,**kw):
        hilos.append(threading.get_ident())
        time.sleep(.4 if kw.get('busqueda') == 'михаил' else .01)
        return original(self,*args,**kw)
    monkeypatch.setattr(Database,'listar_clientes',demorar)
    a._refrescar_clientes = lambda filas:pintados.append(filas)
    async def ejecutar():
        viejo = asyncio.create_task(a._on_buscar(NS(control=NS(value='михаил'))))
        await asyncio.sleep(.21)
        nuevo = asyncio.create_task(a._on_buscar(NS(control=NS(value='Apellido000001'))))
        pulsos = 0
        while not viejo.done() or not nuevo.done():
            await asyncio.sleep(.01)
            pulsos += 1
        await asyncio.gather(viejo,nuevo)
        return pulsos
    assert asyncio.run(ejecutar()) >= 10
    assert hilos and all(h != principal for h in hilos)
    assert len(pintados) == 1 and pintados[0][0]['apellidos'] == 'Apellido000001'


def test_archivar_rechaza_raiz_alias(db,tmp_path):
    if os.name != 'nt':
        pytest.skip('Junction de Windows')
    c = db.crear_cliente('Cliente','Alias','A9-ALIAS')
    base, alias = Path(c['carpeta']),tmp_path/'AliasCliente'
    proceso = subprocess.run(['cmd','/c','mklink','/J',str(alias),str(base)],capture_output=True)
    assert proceso.returncode == 0
    try:
        # El requisito de aislamiento prohíbe junctions aunque ahora apunten
        # a una carpeta válida: podrían redirigirse a otro expediente.
        with pytest.raises(ValueError):
            pp.archivar_pdf(pdfs.recibo_ibi(tmp_path/'recibo.pdf'),alias,pp.TipoDocumento.CATASTRO_IBI,None)
        assert not list((base/'Soportes_PDF').glob('*.pdf'))
    finally:
        os.rmdir(alias)


def test_colision_concurrente_no_sobrescribe_pdf(db,tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    c = db.crear_cliente('Cliente','Carrera','A9-CARRERA')
    fuentes = [tmp_path/'uno/recibo.pdf',tmp_path/'dos/recibo.pdf']
    for ruta, contenido in zip(fuentes,(b'contenido ficticio A',b'contenido ficticio B')):
        ruta.parent.mkdir()
        ruta.write_bytes(contenido)
    original, barrera = Path.open,threading.Barrier(2)
    vistos,lock = set(),threading.Lock()
    def abrir(self,mode='r',*args,**kwargs):
        if mode == 'xb':
            with lock:
                primera = threading.get_ident() not in vistos
                vistos.add(threading.get_ident())
            if primera:
                barrera.wait(timeout=5)
        return original(self,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'open',abrir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futuros = [pool.submit(pp.archivar_pdf,ruta,Path(c['carpeta']),pp.TipoDocumento.CATASTRO_IBI,None)
                   for ruta in fuentes]
        resultados = [f.result(timeout=10) for f in futuros]
    rutas = [Path(c['carpeta'])/r[0] for r in resultados]
    assert rutas[0] != rutas[1]
    assert {r.read_bytes() for r in rutas} == {p.read_bytes() for p in fuentes}


def test_grafia_windows_no_rechaza_subcarpeta(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows no distingue el casing de estas rutas')
    (tmp_path/'soportes_pdf').mkdir()
    assert database.subcarpeta_cliente(tmp_path).is_dir()


def test_ui_documentos_e_inmuebles_paginados(volumen):
    from controlador import Controlador
    import flet as ft
    a = app_lista(volumen)
    a.ejercicio,a._paginas_detalle = 2026,{}
    a.ctrl.vista_inmuebles = lambda *args,**kwargs:Controlador.vista_inmuebles(NS(lector=volumen),*args,**kwargs)
    a.ctrl.listar_documentos = lambda *args,**kwargs:Controlador.listar_documentos(NS(lector=volumen),*args,**kwargs)
    def acciones_retirar(control):
        total = int(isinstance(control, ft.TextButton) and control.content == 'Retirar a papelera')
        for hijo in getattr(control, 'controls', []) or []:
            total += acciones_retirar(hijo)
        contenido = getattr(control, 'content', None)
        if isinstance(contenido, ft.Control):
            total += acciones_retirar(contenido)
        return total
    assert len(a._sec_inmuebles().controls) == 52  # título, 50 tarjetas y navegación
    documentos = a._sec_documentos()
    assert acciones_retirar(documentos) == 50  # 5.000 soportes en BD, solo una página en UI.
    a._paginas_detalle[(1,2026,'documentos')] = 99
    assert acciones_retirar(a._sec_documentos()) == 50
    a._documentos_papelera = {1: True}
    assert acciones_retirar(a._sec_documentos()) == 0
    # Un borrado puede dejar vacía la última página; siempre debe permitir volver a los restantes.
    a._paginas_detalle[(1,2026,'inmuebles')] = 1000
    assert len(a._sec_inmuebles().controls) == 52
    assert a._numero_pagina('inmuebles') == 0


def test_cabecera_chat_no_escanea_miles_de_archivos(volumen,monkeypatch):
    import asistente
    a = app_lista(volumen)
    a.conversaciones,a._chat_en_curso,a._chat_ocupado = {},None,False
    a.ctrl.estado_ia = lambda:(True,'IA simulada')
    monkeypatch.setattr(asistente,'pdfs_del_cliente',lambda *args:pytest.fail('I/O de directorio en UI'))
    assert len(a._sec_asistente().controls) >= 4


def test_lector_wal_no_necesita_bloqueo_de_escritura(volumen):
    from concurrent.futures import ThreadPoolExecutor
    with Database(volumen.db_path,multihilo=True) as escritor:
        escritor.con.execute('BEGIN IMMEDIATE')
        def leer():
            with Database(volumen.db_path,solo_lectura=True) as lector:
                return len(lector.listar_clientes())
        with ThreadPoolExecutor(max_workers=1) as pool:
            futuro = pool.submit(leer)
            try:
                assert futuro.result(timeout=3) == 50
            finally:
                escritor.con.rollback()


def test_migracion_indices_preserva_registros_y_es_idempotente(tmp_path):
    ruta = tmp_path/'migracion.sqlite'
    with Database(ruta) as d:
        c = d.crear_cliente('Existente','Ficticio','A9-MIGRA')
        d.upsert('inmuebles',c['id'],{'codigo':'INM-001','direccion':'Dato manual'})
        indices = [r['name'] for r in d.con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'")]
        for nombre in indices:
            assert nombre.replace('_','').isalnum()
            d.con.execute(f'DROP INDEX {nombre}')
        d.con.commit()
    for _ in range(2):
        with Database(ruta) as d:
            assert d.get('inmuebles',c['id'],'INM-001')['direccion'] == 'Dato manual'
            assert d.get_cliente(c['id'])['nombre'] == 'Existente'
            assert d.con.execute("SELECT count(*) FROM sqlite_master WHERE name='ix_clientes_orden'").fetchone()[0] == 1


def test_lote_confirma_una_vez_y_oculta_cambios_hasta_commit(db):
    cid = db.crear_cliente('Cliente','Lote','A9-LOTE')['id']
    eventos = []
    db.con.set_trace_callback(eventos.append)
    with db.transaccion():
        for i in range(200):
            db.upsert('notas',cid,{'codigo':f'NOT-{i:04}','nota':'Dato ficticio'})
        with Database(db.db_path,solo_lectura=True) as otra:
            assert otra.con.execute('SELECT count(*) FROM notas').fetchone()[0] == 0
    db.con.set_trace_callback(None)
    assert sum(q.strip().upper()=='COMMIT' for q in eventos) == 1
    assert db.con.execute('SELECT count(*) FROM notas').fetchone()[0] == 200


def test_fallo_de_lote_restaura_datos_y_commit_normal(db):
    cid = db.crear_cliente('Cliente','Rollback','A9-RB')['id']
    db.upsert('notas',cid,{'codigo':'NOT-001','nota':'Manual previo'})
    with pytest.raises(ValueError,match='Fallo ficticio'):
        with db.transaccion():
            db.upsert('notas',cid,{'codigo':'NOT-001','nota':'No consolidar'})
            db.upsert('notas',cid,{'codigo':'NOT-002','nota':'No consolidar'})
            raise ValueError('Fallo ficticio')
    assert db.get('notas',cid,'NOT-001')['nota'] == 'Manual previo'
    assert db.get('notas',cid,'NOT-002') is None
    assert db._nivel_transaccion == 0 and not db.con.in_transaction
    db.upsert('notas',cid,{'codigo':'NOT-003','nota':'Operación posterior'})
    with Database(db.db_path,solo_lectura=True) as otra:
        assert otra.get('notas',cid,'NOT-003')['nota'] == 'Operación posterior'


def test_lote_anidado_fallido_no_borra_el_exterior(db):
    cid = db.crear_cliente('Cliente','Anidado','A9-NEST')['id']
    with db.transaccion():
        db.upsert('notas',cid,{'codigo':'NOT-001','nota':'Exterior'})
        with pytest.raises(RuntimeError):
            with db.transaccion():
                db.upsert('notas',cid,{'codigo':'NOT-002','nota':'Interior fallido'})
                raise RuntimeError('Ficticio')
        db.upsert('notas',cid,{'codigo':'NOT-003','nota':'Exterior posterior'})
    assert [r['codigo'] for r in db.listar('notas',cid)] == ['NOT-001','NOT-003']
    assert db._nivel_transaccion == 0


def test_lote_no_confirma_una_transaccion_ajena(db):
    cid = db.crear_cliente('Cliente','Exterior','A9-EXTERIOR')['id']
    db.con.execute('BEGIN')
    with db.transaccion():
        db.upsert('notas',cid,{'codigo':'NOT-001','nota':'Sin confirmar'})
    assert db.con.in_transaction
    db.con.rollback()
    assert db.listar('notas',cid) == []


def preparar_controlador_lote(tmp_path):
    from controlador import Controlador
    c = Controlador(tmp_path/'lote.sqlite')
    c.cambiar_carpeta_clientes(tmp_path/'Clientes')
    cid = c.crear_cliente('Cliente','Ficticio','A9-CTRL',pais='Rusia',ejercicio=2026)['id']
    cod = c.db.upsert('inmuebles',cid,{'fecha_adquisicion':'2020-01-01','pct_titularidad':1})
    c.db.upsert('catastro',cid,{'inmueble':cod,'ejercicio':2026,'valor_catastral':100000,'anio_revision':2010})
    return c,cid


def test_controlador_agrupa_recalculo_en_un_commit(tmp_path):
    c,cid = preparar_controlador_lote(tmp_path)
    try:
        eventos = []
        c.db.con.set_trace_callback(eventos.append)
        res = c.abrir_cliente(cid,2026)
        c.db.con.set_trace_callback(None)
        assert len(res['informe'].filas_210_imputacion) == 1
        assert sum(q.strip().upper()=='COMMIT' for q in eventos) == 1
        assert c.db.listar('m210_imputacion',cid)[0]['cuota'] == 480
    finally:
        c.cerrar()


def test_controlador_no_deja_recalculo_parcial(tmp_path,monkeypatch):
    c,cid = preparar_controlador_lote(tmp_path)
    try:
        def fallar(d,cid,ej):
            d.upsert('notas',cid,{'codigo':'NOT-FALLO','nota':'No debe quedar'})
            raise RuntimeError('Fallo ficticio en mitad del recálculo')
        monkeypatch.setattr(tc,'recalcular_cliente',fallar)
        with pytest.raises(RuntimeError,match='Fallo ficticio'):
            c.abrir_cliente(cid,2026)
        assert c.db.get('notas',cid,'NOT-FALLO') is None
        assert len(c.db.listar('inmuebles',cid)) == 1
        assert not c.db.con.in_transaction
    finally:
        c.cerrar()


def test_error_exportacion_no_deshace_datos_confirmados(tmp_path,monkeypatch):
    import excel_generator as eg
    c,cid = preparar_controlador_lote(tmp_path)
    try:
        def fallar(*args,**kwargs):
            assert not c.db.con.in_transaction  # Excel fuera de la transacción SQLite
            raise PermissionError('Fallo ficticio al exportar')
        monkeypatch.setattr(eg,'exportar_cliente',fallar)
        with pytest.raises(PermissionError,match='Fallo ficticio'):
            c.sincronizar(cid,2026)
        assert c.db.listar('m210_imputacion',cid)[0]['cuota'] == 480
        assert c.db.get_cliente(cid)
    finally:
        c.cerrar()


def test_error_al_confirmar_lote_hace_rollback(db):
    cid = db.crear_cliente('Cliente','Commit','A9-COMMIT')['id']
    original = db.con
    class ConexionFallo:
        fallo = True
        def __getattr__(self,nombre):
            return getattr(original,nombre)
        def commit(self):
            if self.fallo:
                self.fallo = False
                raise sqlite3.OperationalError('Fallo ficticio al confirmar')
            return original.commit()
    db.con = ConexionFallo()
    with pytest.raises(sqlite3.OperationalError,match='Fallo ficticio'):
        with db.transaccion():
            db.upsert('notas',cid,{'codigo':'NOT-FALLO','nota':'No consolidar'})
    assert db.listar('notas',cid) == []
    assert not db.con.in_transaction and db._nivel_transaccion == 0


def test_error_al_iniciar_lote_no_altera_contador(db):
    original = db.con
    class ConexionFallo:
        def __getattr__(self,nombre):
            return getattr(original,nombre)
        def execute(self,sql,*args):
            if sql == 'BEGIN IMMEDIATE':
                raise sqlite3.OperationalError('Fallo ficticio al reservar escritura')
            return original.execute(sql,*args)
    db.con = ConexionFallo()
    with pytest.raises(sqlite3.OperationalError,match='Fallo ficticio'):
        with db.transaccion():
            pytest.fail('No debe entrar en el lote')
    assert db._nivel_transaccion == 0 and not db.con.in_transaction


def medir(fn,repeticiones=7):
    tiempos = []
    for _ in range(repeticiones):
        t = time.perf_counter()
        resultado = fn()
        tiempos.append((time.perf_counter()-t)*1000)
    tracemalloc.start()
    try:
        resultado = fn()
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {'mediana_ms':round(statistics.median(tiempos),3),'max_ms':round(max(tiempos),3),
            'pico_python_bytes':pico,'filas':len(resultado) if isinstance(resultado,list) else 1,
            'repeticiones':repeticiones}


def benchmark(destino):
    with tempfile.TemporaryDirectory(prefix='contabilidad-a9-') as tmp:
        raiz = Path(tmp)
        with Database(raiz/'benchmark.sqlite') as db:
            poblar(db,raiz)
            import inspect
            paginado = 'limite' in inspect.signature(db.listar_clientes).parameters
            resultados = {
                'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,
                'clientes':10_000,'rusia':2000,'inmuebles_cliente_1':1000,'documentos_cliente_1':5000,
                'listado_inicial':medir(db.listar_clientes),
                'busqueda_unicode':medir(lambda:db.listar_clientes(busqueda='михаил') if paginado else
                    [c for c in db.listar_clientes() if 'михаил' in (c['nombre']+' '+c['apellidos']).lower()]),
                'catastro':medir(lambda:db.catastro(1,'INM-0100',2027)),
                'residencia':medir(lambda:db.residencia(1,2027)),
                'plan_clientes':[dict(r) for r in db.con.execute('EXPLAIN QUERY PLAN SELECT * FROM clientes '
                    'ORDER BY apellidos,nombre,id LIMIT 50')],
                'plan_catastro':[dict(r) for r in db.con.execute('EXPLAIN QUERY PLAN SELECT * FROM catastro '
                    'WHERE cliente_id=1 AND inmueble=\'INM-0100\' AND ejercicio=2026')],
            }
            if paginado:
                resultados['pagina_documentos'] = medir(lambda:db.listar_pagina('documentos',1))
            else:
                resultados['pagina_documentos'] = medir(lambda:db.listar('documentos',1))
        Path(destino).write_text(json.dumps(resultados,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(resultados,ensure_ascii=True,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--benchmark',action='store_true')
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    if args.benchmark:
        benchmark(args.output)
