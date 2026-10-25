"""Regresiones de importación: datos, archivos, anexos y cobros sin devengo inventado."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import ia_extractor as ia
import pdf_processor as pp
import tax_calculator as tax
from controlador import Controlador
from database import Database


@pytest.fixture
def expediente(tmp_path):
    ruta = tmp_path / 'prueba.db'
    db = Database(ruta)
    db.carpeta_clientes = tmp_path / 'Clientes'
    cliente = db.crear_cliente('Prueba', 'Importacion', 'X1234567A')
    db.cerrar()
    ctrl = Controlador(ruta)
    try:
        yield ctrl, cliente['id'], Path(cliente['carpeta']), tmp_path
    finally:
        ctrl.cerrar()


def respuesta(tipo, campos=None, registros=None):
    return dict(tipo=tipo, confianza=.95, campos=campos_ia(campos or {}), registros=registros or [],
                titulares=[], gananciales=False, inquilinos=[], avisos=[])


def campos_ia(campos):
    return [dict(campo=k, valor=str(v), evidencia='Página 2') for k, v in campos.items()]


def archivo(tmp, nombre='soporte.csv', contenido='dato;importe\nx;100'):
    ruta = tmp / nombre
    ruta.write_text(contenido, encoding='utf-8')
    return ruta


def inmueble(db, cid):
    return db.upsert('inmuebles', cid, dict(ref_catastral='REF-PRUEBA', direccion='Calle Prueba',
                     fecha_adquisicion='2024-06-04', precio_escritura=535000, pct_titularidad=1))


def compra():
    return respuesta('ESCRITURA_COMPRA', dict(ref_catastral='REF-PRUEBA', fecha_adquisicion='2024-06-04',
                     precio_escritura=535000, pct_titularidad=1))


@pytest.mark.parametrize('reutilizado', [False, True])
def test_fallo_recalculo_revierte_importacion_y_solo_copias_nuevas(expediente, monkeypatch, reutilizado):
    ctrl, cid, carpeta, tmp = expediente
    ruta = archivo(tmp)
    if reutilizado:
        relativo, _ = pp.archivar_pdf(ruta, carpeta, pp.TipoDocumento.ESCRITURA_COMPRA, None)
    antes = {p.name: p.read_bytes() for p in (carpeta/'Soportes_PDF').glob('*') if p.is_file()}
    ext = ia.a_extraccion(compra(), ruta.name)
    monkeypatch.setattr(pp, 'extraer', lambda *a, **kw: ext)
    def fallar(*a, **kw):
        raise ValueError('Recálculo simulado fallido')
    monkeypatch.setattr(ctrl, '_recalcular_bd', fallar)
    with pytest.raises(ValueError, match='simulado'):
        ctrl.procesar_pdf(cid, ruta, 2025)
    assert not ctrl.db.listar('inmuebles', cid)
    assert not [d for d in ctrl.db.listar('documentos', cid) if d.get('sha256')]
    assert not ctrl.db.con.execute('SELECT 1 FROM documentos_gestion WHERE cliente_id=?', (cid,)).fetchone()
    assert {p.name: p.read_bytes() for p in (carpeta/'Soportes_PDF').glob('*') if p.is_file()} == antes
    assert ruta.exists() and not ctrl.db.con.in_transaction


def test_rechazo_fila_tardia_no_deja_copia_ni_altas(expediente):
    ctrl, cid, carpeta, tmp = expediente
    inm = inmueble(ctrl.db, cid)
    filas = [dict(tabla='cobros', campos=campos_ia(dict(fecha_cobro='2025-06-10', importe_cobrado=100)), evidencia='fila1'),
             dict(tabla='cobros', campos=campos_ia(dict(periodo_desde='2025-07-01', importe_cobrado=200)), evidencia='fila2')]
    ruta = archivo(tmp)
    ext = ia.a_extraccion(respuesta('RESUMEN_ALQUILER', registros=filas), ruta.name)
    with pytest.raises(ValueError, match='periodo'):
        pp.aplicar(ctrl.db, cid, ext, ruta, inmueble_destino=inm, ejercicio=2025)
    assert not ctrl.db.listar('cobros', cid)
    assert not list((carpeta/'Soportes_PDF').glob('*'))
    assert not [d for d in ctrl.db.listar('documentos', cid) if d.get('sha256')]


def test_modelo_anual_no_cambia_preferencia_global_y_se_exporta(expediente, monkeypatch):
    ctrl, cid, carpeta, tmp = expediente
    inm = inmueble(ctrl.db, cid)
    ctrl.db.upsert('contratos', cid, dict(inmueble=inm, fecha_inicio='2025-06-01', renta_inicial=2400,
                                         periodicidad='Mensual', estado='Vigente'))
    ctrl.db.set_config('periodicidad_210_alquiler', 'Trimestral')
    ruta = archivo(tmp, 'modelo.csv')
    ext = ia.a_extraccion(respuesta('MODELO_210', dict(ref_catastral='REF-PRUEBA', tipo_renta='35',
                          ejercicio=2025, periodo='0A', base=14620, cuota=3508.8)), ruta.name)
    monkeypatch.setattr(pp, 'extraer', lambda *a, **kw: ext)
    out = ctrl.procesar_pdf(cid, ruta, 2025)
    assert out['excel'].is_file() and not out['error_excel']
    assert ctrl.db.get_config('periodicidad_210_alquiler') == 'Trimestral'
    presentado = next(r for r in ctrl.db.listar('m210_alquiler', cid) if r['estado']=='Presentado')
    assert (presentado['periodo'], presentado['base'], presentado['cuota']) == ('Anual',14620,3508.8)
    assert all(r['periodo']=='Anual' for r in ctrl.db.listar('m210_alquiler', cid, ejercicio=2025))
    from openpyxl import load_workbook
    wb = load_workbook(out['excel'])
    try:
        assert wb['09_210_ALQUILER']['K2'].value == 14620
        assert wb['09_210_ALQUILER']['M2'].value == 3508.8
    finally:
        wb.close()


def test_periodicidades_presentadas_incompatibles_revierten_soporte(expediente, monkeypatch):
    ctrl, cid, carpeta, tmp = expediente
    inm = inmueble(ctrl.db, cid)
    ctrl.db.upsert('m210_alquiler', cid, dict(inmueble=inm, ejercicio=2025, periodo='1T',
                   estado='Presentado', base=100, cuota=24), clave=f'210A|2025|{inm}|1T')
    ruta = archivo(tmp, 'anual.csv')
    ext = ia.a_extraccion(respuesta('MODELO_210', dict(ref_catastral='REF-PRUEBA', tipo_renta='35',
                          ejercicio=2025, periodo='0A', base=14620, cuota=3508.8)), ruta.name)
    monkeypatch.setattr(pp, 'extraer', lambda *a, **kw: ext)
    with pytest.raises(ValueError, match='incompatibles'):
        ctrl.procesar_pdf(cid, ruta, 2025)
    assert len(ctrl.db.listar('m210_alquiler', cid)) == 1
    assert not list((carpeta/'Soportes_PDF').glob('*'))


def test_cobros_sin_devengo_se_guardan_separados_no_cambian_renta(expediente, monkeypatch):
    ctrl, cid, carpeta, tmp = expediente
    inm = inmueble(ctrl.db, cid)
    ctrl.db.guardar_residencia(cid, 2025, pais='Rusia')
    ctrl.db.upsert('contratos', cid, dict(inmueble=inm, fecha_inicio='2025-06-01', renta_inicial=2400,
                                         periodicidad='Mensual', estado='Vigente'))
    filas = [dict(tabla='cobros', campos=campos_ia(dict(fecha_cobro='2025-07-20', importe_cobrado=importe)),
                  evidencia=f'Fila {i}') for i, importe in enumerate([2400,2110,2400,760,2115,35,2400,2400],1)]
    ruta = archivo(tmp, 'banco.csv')
    ext = ia.a_extraccion(respuesta('RESUMEN_ALQUILER', registros=filas), ruta.name)
    monkeypatch.setattr(pp, 'extraer', lambda *a, **kw: ext)
    out = ctrl.procesar_pdf(cid, ruta, 2025, inmueble_destino=inm)
    cobros = [r for r in ctrl.db.listar('cobros', cid) if r['importe_cobrado'] is not None]
    assert len(cobros)==8 and sum(r['importe_cobrado'] for r in cobros)==14620
    assert all(r['contrato'] is None and r['periodo_desde'] is None and r['periodo_hasta'] is None
               and r['importe_contractual'] is None and r['estado']=='Pendiente' for r in cobros)
    calc = tax.calcular_210_alquiler(ctrl.db.get('inmuebles',cid,inm),ctrl.db.listar('contratos',cid),{},[],None,
                                    tax.resolver_residencia(dict(pais='Rusia')),2025,'Anual')[0]
    assert calc.ingresos==16800 and calc.gastos==0 and calc.cuota==4032
    assert out['resultado'].mensajes and not out['error_excel']
    assert ctrl.procesar_pdf(cid,ruta,2025,inmueble_destino=inm)['resultado'].duplicado
    assert len([r for r in ctrl.db.listar('cobros',cid) if r['importe_cobrado'] is not None])==8


def anexo(**cambios):
    campos=dict(ref_catastral='REF-PRUEBA', ejercicio=2024, valor_catastral=123824.29,
                valor_suelo=79746.63, valor_construccion=44077.66, fuente='Catastro')
    campos.update(cambios)
    return dict(tabla='catastro', campos=campos_ia(campos), evidencia='Anexo página42, certificado año2024')


def test_anexo_catastral_conserva_ejercicio_y_procedencia(expediente):
    ctrl, cid, carpeta, tmp = expediente
    datos=compra();datos['registros']=[anexo()]
    ruta=archivo(tmp,'escritura.csv')
    ext=ia.a_extraccion(datos,ruta.name)
    out=pp.aplicar(ctrl.db,cid,ext,ruta,ejercicio=2026)
    cat=ctrl.db.listar('catastro',cid)
    assert len(cat)==1 and cat[0]['ejercicio']==2024 and cat[0]['valor_catastral']==123824.29
    assert cat[0]['ruta']==out.ruta_relativa and 'página42' in cat[0]['observaciones']
    assert 2024 in out.ejercicios_afectados
    assert not ctrl.db.listar('catastro',cid,ejercicio=2025)
    assert ctrl.db.catastro(cid,out.codigos['inmueble'],2025)['_heredado']==2024


def test_anexo_admite_referencia_guardada_con_espacios_y_cantidades_locales(expediente):
    ctrl,cid,carpeta,tmp=expediente
    inm=inmueble(ctrl.db,cid)
    ctrl.db.upsert('inmuebles',cid,dict(codigo=inm,ref_catastral=' ref-prueba '))
    ctrl.db.proteger_campos_inmueble(cid,inm,('ref_catastral',))
    datos=compra();datos['registros']=[anexo()]
    ruta=archivo(tmp)
    ext=ia.a_extraccion(datos,ruta.name)
    ext.registros[0].campos.update(valor_catastral='123.824,29',valor_suelo='79.746,63',valor_construccion='44.077,66')
    pp.aplicar(ctrl.db,cid,ext,ruta,ejercicio=2025)
    assert ctrl.db.catastro(cid,inm,2024)['valor_catastral']==123824.29


@pytest.mark.parametrize('cambios',[dict(ref_catastral='OTRO'),dict(valor_suelo=1),dict(valor_catastral=-1)])
def test_anexo_invalido_revierte_todo(expediente,cambios):
    ctrl,cid,carpeta,tmp=expediente
    datos=compra();datos['registros']=[anexo(**cambios)]
    ruta=archivo(tmp)
    with pytest.raises(ValueError):pp.aplicar(ctrl.db,cid,ia.a_extraccion(datos,ruta.name),ruta,ejercicio=2026)
    assert not ctrl.db.listar('catastro',cid) and not ctrl.db.listar('inmuebles',cid)
    assert not list((carpeta/'Soportes_PDF').glob('*'))


def test_anexo_no_pisa_catastro_existente(expediente):
    ctrl,cid,carpeta,tmp=expediente
    inm=inmueble(ctrl.db,cid)
    ctrl.db.upsert('catastro',cid,dict(inmueble=inm,ejercicio=2024,valor_catastral=100000),clave=f'CAT|{inm}|2024')
    datos=compra();datos['registros']=[anexo()];ruta=archivo(tmp)
    result=pp.aplicar(ctrl.db,cid,ia.a_extraccion(datos,ruta.name),ruta,ejercicio=2026)
    assert ctrl.db.catastro(cid,inm,2024)['valor_catastral']==100000
    assert ctrl.db.catastro(cid,inm,2024)['valor_suelo'] is None
    assert ctrl.db.catastro(cid,inm,2024)['valor_construccion'] is None
    assert any('se conservan' in m for m in result.mensajes)


@pytest.mark.parametrize('previo,nuevo',[(dict(valor_catastral=100),dict(valor_suelo=120)),
                                       (dict(valor_suelo=60,valor_construccion=40),dict(valor_catastral=200))])
def test_anexos_parciales_no_mezclan_valoraciones(expediente,previo,nuevo):
    ctrl,cid,carpeta,tmp=expediente
    inm=inmueble(ctrl.db,cid)
    ctrl.db.upsert('catastro',cid,dict(inmueble=inm,ejercicio=2024,**previo),clave=f'CAT|{inm}|2024')
    datos=compra()
    datos['registros']=[dict(tabla='catastro',campos=campos_ia(dict(ref_catastral='REF-PRUEBA',ejercicio=2024,**nuevo)),evidencia='Página42')]
    ruta=archivo(tmp)
    result=pp.aplicar(ctrl.db,cid,ia.a_extraccion(datos,ruta.name),ruta,ejercicio=2026)
    row=ctrl.db.catastro(cid,inm,2024)
    assert all(row[k]==previo.get(k) for k in ('valor_catastral','valor_suelo','valor_construccion'))
    assert any('se conservan' in m for m in result.mensajes)


def test_anexo_nuevo_parcial_rechaza_componente_superior_total(expediente):
    ctrl,cid,carpeta,tmp=expediente
    datos=compra()
    datos['registros']=[dict(tabla='catastro',campos=campos_ia(dict(ref_catastral='REF-PRUEBA',
        ejercicio=2024,valor_catastral=100,valor_suelo=120)),evidencia='Página42')]
    ruta=archivo(tmp)
    with pytest.raises(ValueError,match='supera'):
        pp.aplicar(ctrl.db,cid,ia.a_extraccion(datos,ruta.name),ruta,ejercicio=2025)
    assert not ctrl.db.listar('catastro',cid)
    assert not list((carpeta/'Soportes_PDF').glob('*'))


def test_revision_no_bloquea_otra_conexion_sqlite(expediente, monkeypatch):
    ctrl,cid,carpeta,tmp=expediente
    ruta=archivo(tmp)
    monkeypatch.setattr(pp,'extraer',lambda *a,**kw:ia.a_extraccion(compra(),ruta.name))
    def confirmar(ext):
        assert not ctrl.db.con.in_transaction
        otra=Database(ctrl.db.db_path)
        try:
            otra.set_config('revision_concurrente','OK')
        finally:
            otra.cerrar()
        return ext
    assert ctrl.procesar_pdf(cid,ruta,2025,confirmar=confirmar)['excel'].is_file()
    assert ctrl.db.get_config('revision_concurrente')=='OK'


@pytest.mark.parametrize('tipo,tabla',[('ESCRITURA_COMPRA','cobros'),('RESUMEN_ALQUILER','catastro'),('MODELO_210','catastro')])
def test_anexos_no_amplian_tablas_arbitrarias(tipo,tabla):
    fila=anexo();fila['tabla']=tabla
    with pytest.raises(ia.ErrorIA):ia.a_extraccion(respuesta(tipo,registros=[fila]),'x.pdf')


def test_revision_muestra_anexo_y_cobro_pendiente(monkeypatch):
    import main
    import ui_estilo as ui
    app=main.App.__new__(main.App)
    app.p=ui.CLARO;app.cid=1;app.ejercicio=2025;app.cerrando=False
    app.ctrl=NS(lector=NS(get_cliente=lambda _:dict(nombre='Prueba',nif='X1234567A'),listar=lambda *a:[]))
    app.page=NS(pop_dialog=lambda:None,update=lambda:None)
    observados=[]
    def recorrer(c):
        yield c
        for h in getattr(c,'controls',[]) or []:yield from recorrer(h)
    def dialogo(titulo,contenido,acciones,**kwargs):
        for c in recorrer(contenido):
            if isinstance(c,main.ft.Checkbox) and c.label=='He revisado los datos propuestos':c.value=True
            if isinstance(c,main.ft.Text):observados.append(c.value)
        acciones[-1].on_click(None)
        return NS(on_dismiss=None)
    app._dialogo=dialogo
    datos=compra();datos['registros']=[anexo()]
    app.revisar_extraccion(ia.a_extraccion(datos,'escritura.pdf'))
    fila=dict(tabla='cobros',campos=campos_ia(dict(fecha_cobro='2025-07-20',importe_cobrado=35)),evidencia='Fila1')
    app.revisar_extraccion(ia.a_extraccion(respuesta('RESUMEN_ALQUILER',registros=[fila]),'banco.csv'))
    assert any('Catastro · ejercicio 2024' in str(v) for v in observados)
    assert any('Periodo pendiente de asignar' in str(v) for v in observados)
