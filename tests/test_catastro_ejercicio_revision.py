"""Regresión de años de Catastro, procedencia y presentación provisional."""
from pathlib import Path
import pytest
import ia_extractor as ia
import pdf_processor as pp
import tax_calculator as tax
from database import Database
from controlador import Controlador
from validacion_catastro import PENDIENTE_REVISION, revision_acreditada, revision_pendiente


@pytest.fixture
def expediente(tmp_path):
    db = Database(tmp_path/'prueba.db')
    db.carpeta_clientes = tmp_path/'Clientes'
    c = db.crear_cliente('Persona', 'Ficticia', 'X1234567A')
    inm = db.upsert('inmuebles', c['id'], dict(ref_catastral='REF-FICTICIA',
        fecha_adquisicion='2024-01-01', precio_escritura=100000, pct_titularidad=1))
    ruta = tmp_path/'catastro.csv'
    ruta.write_text('Año valor 2026. Valor catastral 90000', encoding='utf-8')
    yield db, c['id'], inm, ruta, Path(c['carpeta'])
    db.cerrar()


def propuesta(ruta, **campos):
    return pp.Extraccion(tipo=pp.TipoDocumento.CATASTRO_IBI, confianza=.99,
        campos=dict(ref_catastral='REF-FICTICIA', valor_catastral=90000, **campos),
        evidencias={'anio_revision':'Año valor 2026'}, texto=ruta.read_text(encoding='utf-8'),
        nombre_archivo=ruta.name, motor='ia')


def test_sin_ejercicio_no_usa_abierto_ni_archiva(expediente):
    db,cid,inm,ruta,carpeta = expediente
    with pytest.raises(ValueError, match='ejercicio explícito'):
        pp.aplicar(db,cid,propuesta(ruta,anio_revision=2026),ruta,ejercicio=2025)
    assert not db.listar('catastro',cid)
    assert not db.listar('documentos',cid)
    assert not list((carpeta/'Soportes_PDF').glob('*'))
    assert ruta.exists()


def test_anio_valor_no_es_revision_y_conserva_ejercicio(expediente):
    db,cid,inm,ruta,_ = expediente
    out=pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026,anio_revision=2026),ruta,ejercicio=2025)
    fila=db.listar('catastro',cid)[0]
    assert fila['ejercicio']==2026 and fila['anio_revision'] is None
    assert not db.listar('catastro',cid,ejercicio=2025)
    assert revision_pendiente(fila) and any(PENDIENTE_REVISION in m for m in out.mensajes)


@pytest.mark.parametrize('cita,texto,anio,ej,confirmada,esperado',[
    ('Ponencia de valores de 2010. Año valor 2026.','Ponencia de valores de 2010. Año valor 2026.',2026,2026,False,False),
    ('Ponencia de valores aprobada en 2010 con entrada en vigor en 2012','Ponencia de valores aprobada en 2010 con entrada en vigor en 2012',2010,2026,False,False),
    ('Ponencia de valores aprobada en 2010','Ponencia de valores aprobada en 2010',2010,2026,False,False),
    ('Revisión catastral colectiva no acreditada para el ejercicio 2026','Revisión catastral colectiva no acreditada para el ejercicio 2026',2026,2026,False,False),
    ('Ponencia de valores sin constancia de entrada en vigor en 2026','Ponencia de valores sin constancia de entrada en vigor en 2026',2026,2026,False,False),
    ('Año valor 2026','Año valor 2026',2026,2026,False,False),
    ('Valoración colectiva 2019','Valoración colectiva 2019',2019,2026,False,True),
    ('Ponencia de valores: 2020','Ponencia de valores: 2020',2020,2026,False,True),
    ('Valoración colectiva 2019','Año valor 2026',2019,2026,False,False),
    ('Revisión catastral colectiva 2027','Revisión catastral colectiva 2027',2027,2026,True,False),
    ('','',2019,2026,True,True),
    ('','',None,2026,True,False),
])
def test_acreditacion(cita,texto,anio,ej,confirmada,esperado):
    assert revision_acreditada(anio,ej,cita,texto,confirmada)==esperado


def test_confirmacion_humana_explicita(expediente):
    db,cid,inm,ruta,_=expediente
    ext=propuesta(ruta,ejercicio=2026,anio_revision=2019)
    ext.revision_confirmada=True
    pp.aplicar(db,cid,ext,ruta,ejercicio=2025)
    fila=db.listar('catastro',cid)[0]
    assert fila['anio_revision']==2019 and not revision_pendiente(fila)


def test_anexo_anio_valor_no_acredita_revision(expediente):
    db,cid,inm,ruta,_=expediente
    ext=pp.Extraccion(tipo=pp.TipoDocumento.ESCRITURA_COMPRA,confianza=.95,
        campos=dict(ref_catastral='REF-FICTICIA', fecha_adquisicion='2024-01-01'),
        registros=[pp.RegistroDocumento(tabla='catastro',evidencia='Valor catastral: [2024]',
          campos=dict(ref_catastral='REF-FICTICIA',ejercicio=2024,valor_catastral=80000,anio_revision=2024))],
        texto='Valor catastral: [2024]',nombre_archivo=ruta.name)
    pp.aplicar(db,cid,ext,ruta,ejercicio=2025)
    fila=db.listar('catastro',cid)[0]
    assert fila['ejercicio']==2024 and fila['anio_revision'] is None
    assert revision_pendiente(fila)


def test_revision_futura_no_rebaja_porcentaje_ni_anula_manual():
    assert tax.pct_imputacion(2026,2025)==.02
    assert tax.pct_imputacion(2026,2025,.011)==.011


def test_provisional_y_presentados_preservados(expediente):
    db,cid,inm,ruta,_=expediente
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    tax.recalcular_cliente(db,cid,2026)
    fila=db.listar('m210_imputacion',cid,ejercicio=2026)[0]
    assert fila['estado']=='Falta documentación' and 'provisional' in fila['observaciones']
    assert any(PENDIENTE_REVISION in p.detalle for p in tax.revisar_pendientes(db,cid,2026))
    db.upsert('m210_imputacion',cid,dict(codigo=fila['codigo'],estado='Presentado',cuota=123.45))
    tax.recalcular_cliente(db,cid,2026)
    assert db.get('m210_imputacion',cid,fila['codigo'])['cuota']==123.45


def test_porcentaje_manual_resuelve_provisional_sin_perderlo(expediente):
    db,cid,inm,ruta,_=expediente
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    cat=db.listar('catastro',cid)[0]
    db.upsert('catastro',cid,dict(codigo=cat['codigo'],pct_imputacion=.011))
    tax.recalcular_cliente(db,cid,2026)
    fila=db.listar('m210_imputacion',cid,ejercicio=2026)[0]
    assert fila['pct_imputacion']==.011 and 'provisional' not in (fila['observaciones'] or '')
    assert not revision_pendiente(db.get('catastro',cid,cat['codigo']))


def test_marcado_presentado_bloqueado_sin_modificar(expediente,monkeypatch):
    db,cid,inm,ruta,_=expediente
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    tax.recalcular_cliente(db,cid,2026)
    fila=db.listar('m210_imputacion',cid,ejercicio=2026)[0]
    import threading
    ctrl=object.__new__(Controlador)
    ctrl.db=db
    ctrl._lock=threading.RLock()
    monkeypatch.setattr(ctrl, '_importar', lambda *a: None)
    with pytest.raises(ValueError,match='provisional'):
        ctrl.marcar_presentado(cid,'m210_imputacion',fila['codigo'],2026)
    with pytest.raises(ValueError,match='provisional'):
        ctrl.editar(cid,'m210_imputacion',fila['codigo'],{'estado':'Presentado'},2026)
    obligacion=next(o for o in db.listar('obligaciones',cid) if o.get('id_modelo')==fila['codigo'])
    with pytest.raises(ValueError,match='provisional'):
        ctrl.marcar_presentado(cid,'obligaciones',obligacion['codigo'],2026)
    assert db.get('m210_imputacion',cid,fila['codigo'])['estado']!='Presentado'


def test_alta_presentada_y_cambio_destino_no_eluden_guard(expediente,monkeypatch):
    import threading
    db,cid,inm,ruta,_=expediente
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    ctrl=object.__new__(Controlador)
    ctrl.db=db
    ctrl._lock=threading.RLock()
    monkeypatch.setattr(ctrl,'_importar',lambda *a:None)
    with pytest.raises(ValueError,match='provisional'):
        ctrl.editar(cid,'m210_imputacion',None,dict(inmueble=inm,ejercicio=2026,estado='Presentado'),2026)
    assert not db.listar('m210_imputacion',cid)
    codigo=db.upsert('m210_imputacion',cid,dict(inmueble=inm,ejercicio=2025,estado='Pendiente'))
    with pytest.raises(ValueError,match='provisional'):
        ctrl.editar(cid,'m210_imputacion',codigo,dict(ejercicio=2026,estado='Presentado'),2026)
    assert db.get('m210_imputacion',cid,codigo)['ejercicio']==2025


def test_auditoria_historica_solo_previsualiza(expediente):
    import json
    from validacion_catastro import auditar_importaciones_anteriores
    db,cid,inm,ruta,_=expediente
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    gestion=db.con.execute('SELECT codigo,extraccion FROM documentos_gestion WHERE cliente_id=?',(cid,)).fetchone()
    historica=json.loads(gestion['extraccion'])
    historica['campos'].pop('ejercicio')
    historica['campos']['anio_revision']=2026
    db.con.execute('UPDATE documentos_gestion SET extraccion=? WHERE cliente_id=? AND codigo=?',
        (json.dumps(historica),cid,gestion['codigo']))
    db.con.commit()
    antes=list(db.con.iterdump())
    resultados=auditar_importaciones_anteriores(db,cid)
    assert len(resultados)==2
    assert list(db.con.iterdump())==antes

def test_importar_no_borra_nota_manual_y_porcentaje(expediente):
    db,cid,inm,ruta,_=expediente
    codigo=db.upsert('catastro',cid,dict(inmueble=inm,ejercicio=2026,observaciones='Nota manual importante',pct_imputacion=.011),clave=f'CAT|{inm}|2026')
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    fila=db.get('catastro',cid,codigo)
    assert 'Nota manual importante' in fila['observaciones']
    assert fila['pct_imputacion']==.011
    assert not revision_pendiente(fila)

def test_borrar_observaciones_no_elimina_control_pendiente(expediente):
    db,cid,inm,ruta,_=expediente
    pp.aplicar(db,cid,propuesta(ruta,ejercicio=2026),ruta,ejercicio=2026)
    fila=db.listar('catastro',cid)[0]
    db.vaciar_campo('catastro',cid,fila['codigo'],'observaciones')
    assert revision_pendiente(db.get('catastro',cid,fila['codigo']))


def test_migracion_v2_respalda_y_conserva_datos(tmp_path):
    import sqlite3
    from database import VERSION_ESQUEMA
    ruta=tmp_path/'anterior.db'
    with Database(ruta) as db:
        db.set_config('manual','conservar')
        db.con.execute('ALTER TABLE catastro DROP COLUMN revision_pendiente')
        db.con.execute('PRAGMA user_version=2')
        db.con.commit()
    with Database(ruta) as db:
        assert db.get_config('manual')=='conservar'
        assert db.con.execute('PRAGMA user_version').fetchone()[0]==VERSION_ESQUEMA
        assert 'revision_pendiente' in {r['name'] for r in db.con.execute('PRAGMA table_info(catastro)')}
    copias=list((tmp_path/'Backups_Seguridad').glob('*.db'))
    assert len(copias)==1
    with sqlite3.connect(copias[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==2
