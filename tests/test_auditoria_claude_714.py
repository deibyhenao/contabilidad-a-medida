"""Regresiones de la auditoría de da10d93; BD aislada, sin API."""
import json

import pytest

import database
import normativa_patrimonio as np
import tax_calculator as tc


@pytest.fixture
def db(tmp_path):
    with database.Database(tmp_path/'qa.sqlite') as d:
        d.carpeta_base = tmp_path/'Clientes'
        yield d


def eleccion(db, cid, year, region):
    db.set_config(np.clave(cid, year), json.dumps(dict(normativa=region,
        confirmado=True, justificacion='Caso ficticio de prueba', comunidad='')))
    tc.recalcular_cliente(db, cid, year)
    return db.get_por_clave('patrimonio_714', cid, f'714|{year}')


@pytest.mark.parametrize('year,region,cuota', [(2024, 'ESTATAL', 53546.37),
                                             (2026, 'CATALUNA', 59793.70)])
def test_cambio_normativa_no_arrastra_bonificacion_automatica(db, year, region, cuota):
    cid = db.crear_cliente('QA', 'Normativa', 'X1234567Q')['id']
    db.upsert('otros_activos', cid, dict(ejercicio=year, tipo='FONDO',
        valor_informe=5000000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='QA ficticia'))
    antes = eleccion(db, cid, year, 'MADRID')
    assert antes['bonificacion'] == 31446.37
    despues = eleccion(db, cid, year, region)
    assert despues['bonificacion'] == 0
    assert despues['resultado'] == cuota
    assert despues['estado'] == 'Revisar'
    # La ausencia de regla no se convierte en override manual en la siguiente lectura.
    otra = eleccion(db, cid, year, region)
    assert otra['bonificacion'] == 0
    assert not db.get_config(f'bonificacion714_manual|{cid}|{year}')


@pytest.mark.parametrize('year', [2024, 2025])
def test_aviso_limite_estatal_tambien_solo_con_inmuebles(db, year):
    cid = db.crear_cliente('QA', 'Inmueble', 'X1234567R')['id']
    db.upsert('inmuebles', cid, dict(direccion='Calle QA 1', precio_escritura=2500000,
        pct_titularidad=1, fecha_compra='2020-01-01'))
    p = eleccion(db, cid, year, 'ESTATAL')
    assert p['cuota_integra'] == 14542.37
    assert 'art. 31' in p['observaciones']
    assert p['estado'] == 'Revisar'


@pytest.mark.parametrize('ip,its,bonificacion', [
    (20000, 5000, 10000), (154354.37, 122908.01, 31446.36),
    (20000, 30000, 0), (100.01, 0, 50.01)])
def test_galicia_secuencia_oficial_casillas_50_55_205_210(ip, its, bonificacion):
    # AEAT Patrimonio 2025, ayuda de cumplimentación 4.3.9.8.
    assert np.bonificacion_general('GALICIA', 2025, ip, its) == bonificacion


def test_galicia_no_simula_un_pago_real_del_714_para_el_718(db):
    cid = db.crear_cliente('QA', 'Galicia', 'X1234567S')['id']
    db.upsert('otros_activos', cid, dict(ejercicio=2025, tipo='FONDO',
        valor_informe=10000000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='QA ficticia'))
    p = eleccion(db, cid, 2025, 'GALICIA')
    assert (p['cuota_integra'], p['bonificacion'], p['resultado']) == (154354.37, 31446.36, 122908.01)
    its = db.get_por_clave('its_718', cid, '718|2025')
    assert its['deducciones'] == 0
    assert its['resultado'] == 122908.01
    assert its['estado'] == 'Revisar'
