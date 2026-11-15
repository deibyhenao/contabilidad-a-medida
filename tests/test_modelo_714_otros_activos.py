"""Casos ficticios: patrimonio, aislamiento y migración bidireccional sin API real."""
import hashlib
from pathlib import Path

import openpyxl
import pytest

import database
import excel_generator as eg
import otros_activos as oa
import tax_calculator as tc


def activo(**cambios):
    base = dict(ejercicio=2025, tipo="CUENTA", pct_titularidad=1, ubicacion="ESPANA",
                saldo_31dic=100000, saldo_medio_4t=100000, revision_confirmada=1,
                confirmacion_legal="Revisado por asesor con certificado y convenio", estado="Activo")
    return {**base, **cambios}


@pytest.fixture
def expediente(tmp_path):
    db = database.Database(tmp_path / 'ficticia.sqlite')
    db.carpeta_base = tmp_path / 'Expedientes'
    cliente = db.crear_cliente('Prueba', 'Activos', 'X0000000Z')
    yield db, cliente
    db.cerrar()


def test_cartera_rusa_y_ue_misma_tarifa_estatal_sin_nacionalidad():
    pisos = [dict(codigo='INM-001', precio_escritura=400000, pct_titularidad=1),
             dict(codigo='INM-002', precio_escritura=300000, pct_titularidad=.5)]
    filas = [activo(), activo(saldo_31dic=150000, saldo_medio_4t=200000, pct_titularidad=.5)]
    filas += [activo(tipo='CRIPTO', saldo_31dic=None, saldo_medio_4t=None, valor_informe=n)
              for n in (10000, 20000, 30000, 40000)]
    filas += [activo(tipo='FONDO', valor_informe=200000, pct_titularidad=.5)]
    pat = tc.calcular_patrimonio(pisos, {}, 2025, otros_activos=filas)
    assert (pat.valor_bienes, pat.base, pat.minimo_exento, pat.base_liquidable) == (950000, 950000, 700000, 250000)
    assert pat.resultado == 582.87
    assert pat.obligado and not pat.provisional


@pytest.mark.parametrize('bruto,obligado', [(2000000, False), (2000000.01, True)])
def test_umbral_bruto_incluye_exentos_antes_de_deudas(bruto, obligado):
    pat = tc.calcular_patrimonio([], {}, 2025, otros_activos=[
        activo(tipo='FONDO', valor_informe=bruto, exento=1, motivo_exencion='Exención acreditada art4')])
    assert pat.base == 0 and pat.resultado == 0
    assert pat.valor_bienes == bruto and pat.obligado is obligado


def test_cuenta_media_4t_y_nulo_no_se_confunde_con_cero():
    assert oa.valorar(activo(saldo_31dic=10, saldo_medio_4t=20))['valor_cliente'] == 20
    assert oa.valorar(activo(saldo_31dic=0, saldo_medio_4t=0))['provisional'] is False
    assert oa.valorar(activo(saldo_medio_4t=None))['provisional'] is True


def test_cripto_precision_y_no_multiplicar_total_dos_veces():
    a = activo(tipo='CRIPTO', valor_informe=120, unidades='0.00000012', valor_unitario='1000000000')
    assert oa.valorar(a)['valor_cliente'] == 120
    a.pop('valor_informe')
    assert oa.valorar(a)['valor_cliente'] == 120


def test_art16_no_cotizados_no_solo_balance_sin_auditoria():
    a = activo(tipo='NO_COTIZADO', valor_nominal=10000, valor_balance=20000,
               beneficio_1=10000, beneficio_2=12000, beneficio_3=14000)
    assert oa.valorar(a)['valor_cliente'] == 60000
    a['auditoria_favorable'] = 1
    assert oa.valorar(a)['valor_cliente'] == 20000


@pytest.mark.parametrize('auditoria', [0, 1])
def test_art16_informe_no_oculta_datos_ausentes(auditoria):
    a = activo(tipo='NO_COTIZADO', valor_informe=20000, auditoria_favorable=auditoria)
    resultado = oa.valorar(a)
    assert resultado['valor_cliente'] == 20000
    assert resultado['provisional'] and any('art. 16' in aviso for aviso in resultado['avisos'])


def test_resumen_sin_posiciones_y_discrepancia_no_son_definitivos():
    resumen = activo(tipo='RESUMEN_CARTERA', valor_informe=900000, cartera_id='C1')
    pat = tc.calcular_patrimonio([], {}, 2025, otros_activos=[resumen])
    assert pat.provisional and pat.valor_bienes == 0
    assert any('sin posiciones' in a for a in pat.avisos_activos)
    pat = tc.calcular_patrimonio([], {}, 2025, otros_activos=[resumen,
        activo(tipo='FONDO', valor_informe=100000, cartera_id='C1')])
    assert pat.provisional and pat.valor_bienes == 100000
    assert any('no coincide' in a for a in pat.avisos_activos)


@pytest.mark.parametrize('cambios', [dict(pct_titularidad=1.01), dict(pct_titularidad=0),
    dict(saldo_31dic=float('nan')), dict(unidades='Infinity'), dict(valor_manual=50),
    dict(exento=1), dict(ubicacion='../'), dict(saldo_31dic=-1)])
def test_validacion_datos_invalidos(cambios):
    with pytest.raises(ValueError):
        oa.valorar(activo(**cambios))


def test_desconocido_no_da_falso_no_obligado_definitivo():
    pat = tc.calcular_patrimonio([], {}, 2025, otros_activos=[activo(ubicacion='PENDIENTE')])
    assert pat.provisional and not pat.obligado
    assert 'PROVISIONAL' in pat.mensaje


def test_resumen_cartera_no_duplica_posiciones():
    pat = tc.calcular_patrimonio([], {}, 2025, otros_activos=[
        activo(tipo='RESUMEN_CARTERA', valor_informe=1000000), activo(tipo='FONDO', valor_informe=100000)])
    assert pat.valor_bienes == 100000


def test_override_persiste_ante_nueva_ia_y_aislamiento(expediente):
    db, cliente = expediente
    cid = cliente['id']
    cod = db.upsert('otros_activos', cid, activo(), clave='cuenta')
    db.upsert('otros_activos', cid, {'codigo': cod, 'valor_manual': 250000,
                                    'motivo_manual': 'Corrección asesor'}, respetar_manuales=False)
    db.upsert('otros_activos', cid, activo(saldo_31dic=0), clave='cuenta')
    assert oa.valorar(db.get('otros_activos', cid, cod))['valor_cliente'] == 250000
    otro = db.crear_cliente('Otra', 'Persona', 'X0000001Z')
    assert db.get('otros_activos', otro['id'], cod) is None


def test_excel_antiguo_nombre_personalizado_y_roundtrip(expediente):
    db, cliente = expediente
    cid = cliente['id']
    huella = hashlib.sha256(database.PLANTILLA_EXCEL.read_bytes()).hexdigest()
    antiguo = eg.exportar_cliente(db, cid, 2025)
    personalizado = antiguo.with_name('Libro del asesor.xlsx')
    antiguo.rename(personalizado)
    assert db.ruta_excel(cliente) == personalizado
    cod = db.upsert('otros_activos', cid, activo(), respetar_manuales=False)
    nuevo = eg.exportar_cliente(db, cid, 2025)
    assert nuevo == personalizado
    wb = openpyxl.load_workbook(nuevo)
    assert len(wb.sheetnames) == 18
    assert wb[oa.HOJA]['AF2'].data_type == 'f'
    wb[oa.HOJA]['I2'] = 250000
    wb.save(nuevo)
    wb.close()
    eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod)['saldo_31dic'] == 250000
    assert oa.valorar(db.get('otros_activos', cid, cod))['valor_cliente'] == 250000
    assert eg.verificar_estructura(nuevo) == []
    assert hashlib.sha256(database.PLANTILLA_EXCEL.read_bytes()).hexdigest() == huella


def test_migracion_v3_reabrir_no_duplica_y_preserva_cliente(tmp_path):
    ruta = tmp_path/'vieja.sqlite'
    with database.Database(ruta) as db:
        db.carpeta_base = tmp_path/'Clientes'
        c = db.crear_cliente('Histórico', 'Ficticio', 'X0000002Z')
        db.con.execute('DROP TABLE otros_activos')
        db.con.execute('PRAGMA user_version=3')
        db.con.commit()
    for _ in range(2):
        with database.Database(ruta) as db:
            assert db.get_cliente(c['id'])['carpeta'] == c['carpeta']
            assert db.listar('otros_activos', c['id']) == []
            assert db.con.execute('SELECT count(*) FROM clientes').fetchone()[0] == 1


def test_informe_haiku_simulado_revision_manual_y_recalculo(expediente, tmp_path):
    import ia_extractor as ia
    import pdf_processor as pp
    import fitz
    db, cliente = expediente
    campos = {'tipo': 'CRIPTO', 'entidad': 'Custodio ficticio', 'identificador': 'BTC',
              'ejercicio': '2025', 'unidades': '0.00000012', 'valor_unitario': '1000000000'}
    respuesta = dict(tipo='INFORME_ACTIVOS', confianza=.95, campos=[], titulares=[],
                     gananciales=False, inquilinos=[], avisos=[], registros=[dict(
                         tabla='otros_activos', evidencia='Hoja tenencias, fila2', campos=[
                             dict(campo=c, valor=v, evidencia='Informe ficticio') for c, v in campos.items()])])
    ext = ia.a_extraccion(respuesta, 'cripto.pdf')
    ext.comentario_guia = 'Tenencias de cada especie a 31/12, no ventas'
    ruta = tmp_path/'cripto.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), 'Informe ficticio BTC 2025')
        pdf.save(ruta)
    aplicado = pp.aplicar(db, cliente['id'], ext, ruta, ejercicio=2025)
    fila = db.listar('otros_activos', cliente['id'])[0]
    assert fila['revision_confirmada'] == 0 and fila['ubicacion'] == 'PENDIENTE'
    assert oa.valorar(fila)['valor_cliente'] == 120
    assert aplicado.ejercicios_afectados == {2025}
    db.upsert('otros_activos', cliente['id'], dict(codigo=fila['codigo'], ubicacion='ESPANA',
        revision_confirmada=1, confirmacion_legal='Custodia y convenio acreditados', valor_manual=150,
        motivo_manual='Corrección de informe cliente'), respetar_manuales=False)
    informe = tc.recalcular_cliente(db, cliente['id'], 2025)
    assert informe.patrimonio.valor_bienes == 150
    # Un nuevo documento con la misma posición no pisa lo corregido a mano.
    otra = ruta.with_name('corregido.pdf')
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), 'Otro informe ficticio BTC')
        pdf.save(otra)
    segundo = pp.aplicar(db, cliente['id'], ext, otra, ejercicio=2025)
    filas = db.listar('otros_activos', cliente['id'])
    assert len(filas) == 1 and filas[0]['valor_manual'] == 150
    assert any('se conservan las correcciones manuales' in m for m in segundo.mensajes)


def test_presentado_714_permanece_congelado_al_anadir_activos(expediente):
    db, cliente = expediente
    cid = cliente['id']
    db.guardar_residencia(cid, 2025, pais='Rusia')
    tc.recalcular_cliente(db, cid, 2025)
    codigo = db.get_por_clave('patrimonio_714', cid, '714|2025')['codigo']
    db.upsert('patrimonio_714', cid, dict(codigo=codigo, estado='Presentado', valor_bienes=99999,
                                       resultado=123, pagado=123, csv='FICTICIO'))
    db.upsert('otros_activos', cid, activo(saldo_31dic=1000000, saldo_medio_4t=1000000))
    tc.recalcular_cliente(db, cid, 2025)
    congelada = db.get('patrimonio_714', cid, codigo)
    assert (congelada['valor_bienes'], congelada['resultado'], congelada['pagado']) == (99999, 123, 123)


def test_no_ocultar_activos_al_cambiar_de_cartera(tmp_path):
    from controlador import Controlador
    ctrl = Controlador(tmp_path/'carteras.sqlite')
    try:
        ctrl.db.carpeta_base = tmp_path/'Clientes'
        cliente = ctrl.crear_cliente('Cambio', 'Ficticio', 'X0000888Z', pais='Rusia')
        cid = cliente['id']
        ctrl.editar(cid, 'otros_activos', None, activo(), 2025)
        original = ctrl.db.ruta_excel(ctrl.db.get_cliente(cid))
        with pytest.raises(ValueError, match='Otros Activos'):
            ctrl.cambiar_tipo_cliente(cid, 'Autónomo', 2025)
        assert ctrl.db.get_cliente(cid)['carpeta'] == cliente['carpeta']
        assert ctrl.db.listar('otros_activos', cid)
        assert ctrl.db.ruta_excel(ctrl.db.get_cliente(cid)) == original
    finally:
        ctrl.cerrar()


@pytest.mark.parametrize('tipo', ['ACCION_COTIZADA', 'RENTA_FIJA_COTIZADA', 'FONDO'])
def test_valores_unitarios_fiscales_se_multiplican_una_vez(tipo):
    a = activo(tipo=tipo, unidades='100', valor_unitario='25.50')
    assert oa.valorar(a)['valor_cliente'] == 2550
    a['valor_informe'] = 2550
    assert oa.valorar(a)['valor_cliente'] == 2550
    assert not oa.valorar(a)['provisional']
    a['valor_informe'] = 2600
    assert oa.valorar(a)['provisional']


def test_conciliacion_comun_por_entidad_y_valor_efectivo():
    filas = [activo(codigo='R1', tipo='RESUMEN_CARTERA', entidad='Banco', valor_informe=150),
             activo(codigo='P1', tipo='FONDO', entidad=' banco ', valor_informe=100,
                    valor_manual=150, motivo_manual='Corrección acreditada', pct_titularidad=.5)]
    assert oa.conciliar_carteras(filas) == {}
    filas[0]['valor_informe'] = 100
    assert 'no coincide' in oa.conciliar_carteras(filas)['R1'][0]


def test_revision_automatica_de_714_se_resuelve_sin_cerrar_revision_manual(expediente):
    db, cliente = expediente
    cid = cliente['id']
    db.guardar_residencia(cid, 2025, pais='Rusia')
    cod = db.upsert('otros_activos', cid, activo(ubicacion='PENDIENTE'))
    tc.recalcular_cliente(db, cid, 2025)
    assert db.get_por_clave('patrimonio_714', cid, '714|2025')['estado'] == 'Revisar'
    assert db.get_por_clave('obligaciones', cid, 'OBL|714|2025')['estado'] == 'Revisar'
    db.upsert('otros_activos', cid, {'codigo': cod, 'ubicacion': 'ESPANA'}, respetar_manuales=False)
    import json
    db.set_config(f'normativa714|{cid}|2025', json.dumps(dict(normativa='ESTATAL', confirmado=True,
        comunidad='', justificacion='Normativa estatal elegida por el asesor QA')))
    tc.recalcular_cliente(db, cid, 2025)
    fila = db.get_por_clave('patrimonio_714', cid, '714|2025')
    assert fila['estado'] == 'No procede'
    db.upsert('patrimonio_714', cid, {'codigo': fila['codigo'], 'estado': 'Revisar', 'revision_automatica': 0})
    tc.recalcular_cliente(db, cid, 2025)
    assert db.get_por_clave('patrimonio_714', cid, '714|2025')['estado'] == 'Revisar'


def test_autonomica_conserva_minimo_y_718_tiene_minimo_estatal_independiente(expediente):
    db, cliente = expediente
    cid = cliente['id']
    db.guardar_residencia(cid, 2025, pais='Rusia')
    db.upsert('otros_activos', cid, activo(tipo='FONDO', valor_informe=4000000))
    tc.recalcular_cliente(db, cid, 2025)
    previa = db.get_por_clave('patrimonio_714', cid, '714|2025')
    db.upsert('patrimonio_714', cid, dict(codigo=previa['codigo'], normativa='Autonómica por validar',
        minimo_exento=1200000, bonificacion=999999, pagado=0))
    tc.recalcular_cliente(db, cid, 2025)
    p = db.get_por_clave('patrimonio_714', cid, '714|2025')
    its = db.get_por_clave('its_718', cid, '718|2025')
    assert p['minimo_exento'] == 1200000 and p['base_liquidable'] == 2800000
    assert p['estado'] == 'Revisar' and 'PROVISIONAL' in p['observaciones']
    # Ley 38/2022 art.3.9 vigente y AEAT: 700000 también en obligación real.
    assert its['base'] == 3300000 and its['cuota'] == 5100
    assert its['resultado'] == 5100


def test_718_solo_deduce_pago_ip_acreditado(expediente):
    db, cliente = expediente
    cid = cliente['id']
    db.guardar_residencia(cid, 2025, pais='Francia')
    db.upsert('otros_activos', cid, activo(tipo='FONDO', valor_informe=4000000))
    tc.recalcular_cliente(db, cid, 2025)
    p = db.get_por_clave('patrimonio_714', cid, '714|2025')
    its = db.get_por_clave('its_718', cid, '718|2025')
    assert its['deducciones'] == 0 and its['estado'] == 'Revisar'
    db.upsert('patrimonio_714', cid, dict(codigo=p['codigo'], pagado=2000))
    tc.recalcular_cliente(db, cid, 2025)
    its = db.get_por_clave('its_718', cid, '718|2025')
    assert its['deducciones'] == 2000 and its['resultado'] == 3100


def test_lector_local_bancario_basico_sin_api(tmp_path):
    import pdf_processor as pp
    import pymupdf
    ruta = tmp_path/'certificado_ficticio.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), 'Certificado bancario\nEjercicio: 2025\nBanco: Banco Ficticio\nIBAN: ES00 FICTICIO\nSaldo a 31/12: 150.000,00 EUR\nSaldo medio 4T: 160.000,00 EUR')
        pdf.save(ruta)
    ext = pp.extraer_local(ruta)
    assert ext.tipo == pp.TipoDocumento.INFORME_ACTIVOS
    assert ext.campos['saldo_31dic'] == 150000
    assert ext.campos['saldo_medio_4t'] == 160000
    assert ext.campos['tipo'] == 'CUENTA'


def test_contexto_chat_incluye_activos_del_cliente(expediente):
    import asistente
    db, cliente = expediente
    db.upsert('otros_activos', cliente['id'], activo(identificador='CUENTA_FICTICIA_UNICA'))
    contexto = asistente.construir_contexto(db, cliente['id'], 2025)
    assert 'CUENTA_FICTICIA_UNICA' in str(contexto)


def test_restaurar_excel_pdf_sin_edicion_no_bloquea_relectura(expediente, tmp_path):
    db, cliente = expediente
    codigo = db.upsert('otros_activos', cliente['id'], activo(identificador='ES00 1234', origen='pdf'))
    ruta = eg.exportar_cliente(db, cliente['id'], 2025)
    with database.Database(tmp_path/'restauracion.sqlite') as restaurada:
        restaurada.carpeta_base = tmp_path/'restaurados'
        copia = restaurada.crear_cliente('Prueba', 'Activos', cliente['nif'])
        eg.import_excel_to_db(restaurada, copia['id'], ruta)
        fila = restaurada.get('otros_activos', copia['id'], codigo)
        assert not fila.get('manual_override')
        clave = oa.clave_natural(fila)
        restaurada.upsert('otros_activos', copia['id'], activo(identificador='ES001234', origen='pdf', saldo_31dic=130000), clave=clave)
        assert len(restaurada.listar('otros_activos', copia['id'])) == 1
        assert restaurada.get('otros_activos', copia['id'], codigo)['saldo_31dic'] == 130000


@pytest.mark.parametrize('carteras,correcto', [(('A', 'B'), True), (('A', 'A'), False)])
def test_informe_no_sobrescribe_posiciones_de_dos_carteras(expediente, tmp_path, carteras, correcto):
    import pdf_processor as pp
    import pymupdf
    db, cliente = expediente
    registros = [pp.RegistroDocumento(tabla='otros_activos', evidencia='Informe ficticio', campos=dict(
        ejercicio=2025, tipo='FONDO', entidad='Banco', identificador='ISIN-FICTICIO',
        valor_informe=valor, cartera_id=cartera)) for cartera, valor in zip(carteras, (1000, 2000))]
    ext = pp.Extraccion(tipo=pp.TipoDocumento.INFORME_ACTIVOS, confianza=.9,
                        registros=registros, nombre_archivo='informe.pdf')
    ruta = tmp_path/'informe.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), 'Informe ficticio de dos carteras')
        pdf.save(ruta)
    if correcto:
        pp.aplicar(db, cliente['id'], ext, ruta, ejercicio=2025)
        filas = db.listar('otros_activos', cliente['id'])
        assert len(filas) == 2 and sum(f['valor_informe'] for f in filas) == 3000
    else:
        with pytest.raises(ValueError, match='misma identidad'):
            pp.aplicar(db, cliente['id'], ext, ruta, ejercicio=2025)
        assert db.listar('otros_activos', cliente['id']) == []
        assert db.listar('documentos', cliente['id']) == []


@pytest.mark.parametrize('provisional,obligado,color', [(False, False, 'V'), (True, False, 'N'), (False, True, 'R')])
def test_resumen_no_da_verde_a_patrimonio_provisional(provisional, obligado, color):
    from types import SimpleNamespace
    from main import _color_resumen
    assert _color_resumen(SimpleNamespace(provisional=provisional, obligado=obligado),
                          SimpleNamespace(rojo='R', naranja='N', verde='V')) == color


def test_claves_no_colisionan_con_separadores_y_normalizan_ejercicio():
    a = activo(tipo='FONDO', identificador='ISIN', entidad='A|B', cartera_id='C')
    b = {**a, 'entidad': 'A', 'cartera_id': 'B|C'}
    assert oa.clave_natural(a) != oa.clave_natural(b)
    assert oa.clave_natural(a) == oa.clave_natural({**a, 'ejercicio': '2025.0'})


def test_lectura_de_registro_danado_no_da_modelo_cerrado():
    fila = activo(codigo='MAL-001', ubicacion=None)
    r = tc.calcular_patrimonio([], {}, 2025, otros_activos=[fila])
    assert r.provisional and any('datos inválidos' in a for a in r.avisos_activos)
    assert tc.calcular_its(3500000, -100) == (8500, 0, 8500)


@pytest.mark.parametrize('conflicto', [False, True])
def test_pendiente_concilia_edicion_app_y_excel_sin_perderlas(expediente, conflicto):
    db, cliente = expediente
    cid = cliente['id']
    codigo = db.upsert('otros_activos', cid, activo(identificador='ESQA', saldo_31dic=100))
    ruta = eg.exportar_cliente(db, cid, 2025)
    # La edición de app no llegó al libro principal por un bloqueo.
    db.upsert('otros_activos', cid, {'codigo': codigo, 'saldo_31dic': 777}, respetar_manuales=False)
    db.set_config(f'excel_pendiente|{cid}', '1')
    wb = openpyxl.load_workbook(ruta)
    ws = wb['16_OTROS_ACTIVOS']
    ws['Z2'] = 'Nota manual en Excel'
    if conflicto:
        ws['I2'] = 888
    wb.save(ruta)
    wb.close()
    if conflicto:
        antes = ruta.read_bytes()
        with pytest.raises(eg.ExcelConflictoImportacion):
            eg.import_excel_to_db(db, cid)
        assert ruta.read_bytes() == antes
        assert db.get('otros_activos', cid, codigo)['observaciones'] is None
    else:
        eg.import_excel_to_db(db, cid)
        assert db.get('otros_activos', cid, codigo)['observaciones'] == 'Nota manual en Excel'
        eg.exportar_cliente(db, cid, 2025)
        wb = openpyxl.load_workbook(ruta)
        assert wb['16_OTROS_ACTIVOS']['I2'].value == 777
        assert wb['16_OTROS_ACTIVOS']['Z2'].value == 'Nota manual en Excel'
        wb.close()
    assert db.get('otros_activos', cid, codigo)['saldo_31dic'] == 777


def test_hoja_personal_del_asesor_se_detecta_antes_de_regenerar(expediente):
    db, cliente = expediente
    cid = cliente['id']
    ruta = eg.exportar_cliente(db, cid, 2025)
    wb = openpyxl.load_workbook(ruta)
    wb.create_sheet('Notas personales')['A1'] = 'Información que no se puede perder'
    wb.save(ruta)
    wb.close()
    antes = ruta.read_bytes()
    with pytest.raises(eg.ExcelConflictoImportacion, match='hojas adicionales'):
        eg.sincronizar(db, cid, 2025)
    assert ruta.read_bytes() == antes
