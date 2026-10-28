"""Regresiones independientes de la auditoría A7: datos ficticios y ninguna llamada API."""
from datetime import date

import pytest

from database import Database
import tax_calculator as tc


@pytest.fixture
def expediente(tmp_path):
    with Database(tmp_path / 'auditoria.sqlite') as db:
        db.carpeta_clientes = tmp_path / 'Clientes'
        cid = db.crear_cliente('Prueba', 'Auditoria', 'X0000077T')['id']
        db.guardar_residencia(cid, 2025, pais='Alemania')
        inm = db.upsert('inmuebles', cid, {'fecha_adquisicion': '2020-01-01', 'pct_titularidad': 1,
                                         'precio_escritura': 100000, 'estado': 'Activo'})
        db.upsert('contratos', cid, {'inmueble': inm, 'fecha_inicio': '2025-01-01',
            'fecha_fin_real': '2025-12-31', 'renta_inicial': 1000, 'tipo': 'Temporada',
            'estado': 'Finalizado', 'periodicidad': 'Mensual'})
        yield db, cid, inm


@pytest.mark.parametrize('marca,base', [('No', 200000), (None, 200000), ('Sí', 200000), ('No', 0)])
def test_referencia_no_suplanta_datos_explicitos_del_impuesto_de_compra(marca, base):
    inm = {'fecha_adquisicion': '2023-06-01', 'precio_escritura': 200000, 'valor_referencia': 300000,
           'uso_valor_referencia': marca, 'base_impuesto_adquisicion': base}
    assert tc.valor_patrimonial(inm, {'valor_catastral': 90000}).valor == 200000


def test_referencia_confirmada_como_base_se_conserva():
    inm = {'fecha_adquisicion': '2023-06-01', 'precio_escritura': 200000, 'valor_referencia': 300000,
           'uso_valor_referencia': 'Sí', 'base_impuesto_adquisicion': 300000}
    assert tc.valor_patrimonial(inm, {'valor_catastral': 90000}).valor == 300000


def presentado_oa(db, cid, inm):
    clave = f'210A|2025|{inm}|OA'
    cod = db.upsert('m210_alquiler', cid, {'ejercicio': 2025, 'inmueble': inm, 'periodo': 'OA',
        'base': 12000, 'cuota': 2280, 'pagado': 2280, 'estado': 'Presentado'}, clave=clave)
    db.upsert('obligaciones', cid, {'ejercicio': 2025, 'tipo': '210 Alquiler', 'inmueble': inm,
        'fecha_limite': date(2026, 1, 20), 'id_modelo': cod, 'estado': 'Presentado', 'pagado': 2280},
        clave=f'OBL|{clave}')
    return cod


def test_anual_heredado_oa_no_duplica_obligacion_ni_pago_al_recalcular(expediente):
    db, cid, inm = expediente
    cod = presentado_oa(db, cid, inm)
    for _ in range(2):
        tc.recalcular_cliente(db, cid, 2025)
        obligaciones = db.listar('obligaciones', cid, id_modelo=cod, tipo='210 Alquiler')
        assert len(obligaciones) == 1
        assert sum(o.get('pagado') or 0 for o in obligaciones) == 2280
    assert db.get('m210_alquiler', cid, cod)['periodo'] == 'OA'


def test_duplicados_presentados_equivalentes_no_se_colapsan_silenciosamente(expediente):
    db, cid, inm = expediente
    cod = presentado_oa(db, cid, inm)
    otro = db.upsert('m210_alquiler', cid, {'ejercicio': 2025, 'inmueble': inm, 'periodo': 'Anual',
        'base': 10000, 'cuota': 1900, 'estado': 'Presentado'}, clave=f'210A|2025|{inm}|Anual')
    informe = tc.recalcular_cliente(db, cid, 2025)
    assert inm in informe.alquileres_bloqueados
    assert any(a.nivel == 'grave' and 'presentados duplicados' in a.mensaje for a in informe.alertas)
    assert db.get('m210_alquiler', cid, cod)['cuota'] == 2280
    assert db.get('m210_alquiler', cid, otro)['cuota'] == 1900


@pytest.mark.parametrize('periodo_clave', ['1T', 'Anual'])
def test_periodo_invalido_con_clave_automatica_no_pierde_importes_y_avisa(expediente, periodo_clave):
    db, cid, inm = expediente
    datos = {'ejercicio': 2025, 'inmueble': inm, 'periodo': 'Enero', 'estado': 'En preparación',
             'base': 999, 'cuota': 123.45}
    cod = db.upsert('m210_alquiler', cid, datos, clave=f'210A|2025|{inm}|{periodo_clave}')
    informe = tc.recalcular_cliente(db, cid, 2025)
    actual = db.get('m210_alquiler', cid, cod)
    assert all(actual[k] == v for k, v in datos.items())
    assert any('periodo no reconocido' in a.mensaje.lower() for a in informe.alertas)


def test_obligaciones_ya_duplicadas_se_senalan_sin_borrar_historicos(expediente):
    db, cid, inm = expediente
    cod = presentado_oa(db, cid, inm)
    db.upsert('obligaciones', cid, {'ejercicio': 2025, 'tipo': '210 Alquiler', 'inmueble': inm,
        'id_modelo': cod, 'estado': 'Presentado', 'pagado': 2280},
        clave=f'OBL|210A|2025|{inm}|Anual')
    informe = tc.recalcular_cliente(db, cid, 2025)
    assert len(db.listar('obligaciones', cid, id_modelo=cod, tipo='210 Alquiler')) == 2
    assert any('obligaciones duplicadas' in a.mensaje.lower() for a in informe.alertas)


def test_referencia_presumida_sin_base_documentada_avisa(expediente):
    db, cid, inm = expediente
    db.upsert('inmuebles', cid, {'codigo': inm, 'fecha_adquisicion': '2023-01-01',
                               'valor_referencia': 300000, 'precio_escritura': 200000})
    informe = tc.recalcular_cliente(db, cid, 2025)
    assert any(a.tipo == 'referencia adquisición' and 'VERIFICAR' in a.mensaje for a in informe.alertas)


def test_borrador_oa_conserva_identidad_estado_y_observaciones(expediente):
    db, cid, inm = expediente
    cod = db.upsert('m210_alquiler', cid, {'ejercicio': 2025, 'inmueble': inm, 'periodo': 'OA',
        'base': 12000, 'cuota': 2280, 'estado': 'En preparación', 'observaciones': 'Revisar con asesor'},
        clave=f'210A|2025|{inm}|OA')
    tc.recalcular_cliente(db, cid, 2025)
    modelos = db.listar('m210_alquiler', cid, ejercicio=2025, inmueble=inm)
    assert len(modelos) == 1
    assert modelos[0]['codigo'] == cod
    assert modelos[0]['estado'] == 'En preparación'
    assert modelos[0]['observaciones'] == 'Revisar con asesor'
