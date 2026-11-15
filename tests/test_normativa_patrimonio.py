"""Cuota general y selección anual NR. Sin API ni datos reales."""
import json

import pytest
import openpyxl

import database
import normativa_patrimonio as np
import tax_calculator as tc
from controlador import Controlador


@pytest.mark.parametrize('region,ejercicio,minimo', [
    ('CATALUNA', 2024, 500000), ('CATALUNA', 2025, 500000),
    ('VALENCIANA', 2024, 500000), ('VALENCIANA', 2025, 1000000),
    ('VALENCIANA', 2026, 2000000), ('ESTATAL', 2025, 700000)])
def test_minimos_por_devengo(region, ejercicio, minimo):
    assert np.regla(region, ejercicio)[0] == minimo


@pytest.mark.parametrize('tarifa', [np.CATALUNA, np.VALENCIANA])
def test_todos_los_puntos_de_la_escala_publicada(tarifa):
    for base, fija, _ in tarifa:
        assert np.cuota(base, tarifa) == float(fija)


def test_reglas_desconocidas_no_se_inventan():
    assert np.regla('OTRA', 2025) is None
    assert np.regla('CATALUNA', 2027) is None


@pytest.fixture
def ctrl(tmp_path):
    db = database.Database(tmp_path/'qa.sqlite')
    db.carpeta_base = tmp_path/'Expedientes'
    db.cerrar()
    c = Controlador(tmp_path/'qa.sqlite')
    c.db.carpeta_base = c.lector.carpeta_base = tmp_path/'Expedientes'
    yield c
    c.cerrar()


def test_eleccion_anual_recalcula_excel_sin_modificar_presentado(ctrl):
    cid = ctrl.crear_cliente('QA', 'Normativa', 'X5555555E', pais='Rusia', ejercicio=2025)['id']
    ctrl.editar(cid, 'otros_activos', None, dict(ejercicio=2025, tipo='FONDO',
        valor_informe=1500000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='Certificado QA'), 2025)
    version = ctrl.configuracion_patrimonio(cid, 2025)['version']
    r = ctrl.configurar_patrimonio(cid, 2025, 'CATALUNA', '', 'Mayor valor sujeto radica en Cataluña', True, version)
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')
    assert (p['minimo_exento'], p['cuota_integra']) == (500000, 5764.89)
    wb = openpyxl.load_workbook(r['excel'])
    assert wb['11_PATRIMONIO_714']['I2'].value == 500000
    assert wb['11_PATRIMONIO_714']['K2'].value == 5764.89
    wb.close()
    with pytest.raises(ValueError, match='cambió'):
        ctrl.configurar_patrimonio(cid, 2025, 'ESTATAL', '', 'Cambio', True, version)
    assert not ctrl.configuracion_patrimonio(cid, 2024)['confirmado']
    ctrl.db.upsert('patrimonio_714', cid, {'codigo': p['codigo'], 'estado': 'Presentado'})
    version = ctrl.configuracion_patrimonio(cid, 2025)['version']
    ctrl.configurar_patrimonio(cid, 2025, 'VALENCIANA', '', 'Corrección QA', True, version)
    assert ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')['cuota_integra'] == 5764.89


def test_cancelar_o_no_confirmar_no_escribe(ctrl):
    cid = ctrl.crear_cliente('QA', 'Cancelar', 'X5555556E', ejercicio=2025)['id']
    antes = ctrl.configuracion_patrimonio(cid, 2025)
    with pytest.raises(ValueError, match='Confirma'):
        ctrl.configurar_patrimonio(cid, 2025, 'CATALUNA', '', 'Justificación', False, antes['version'])
    assert ctrl.configuracion_patrimonio(cid, 2025) == antes


def test_hereda_borradores_hacia_futuro_y_respeta_elecciones_anuales(ctrl):
    cid = ctrl.crear_cliente('QA', 'Historia', 'X5555557E', ejercicio=2025)['id']
    inicial = ctrl.configuracion_patrimonio(cid, 2025)
    ctrl.configurar_patrimonio(cid, 2025, 'CATALUNA', '', 'Mayor valor en Cataluña', True, inicial['version'])
    siguiente = ctrl.configuracion_patrimonio(cid, 2026)
    assert siguiente['confirmado'] and siguiente['heredada_de'] == 2025
    tc.recalcular_cliente(ctrl.db, cid, 2026)
    nueva = ctrl.configuracion_patrimonio(cid, 2025)
    ctrl.configurar_patrimonio(cid, 2025, 'VALENCIANA', '', 'Rectificación 2025', True, nueva['version'])
    assert ctrl.configuracion_patrimonio(cid, 2026)['normativa'] == 'VALENCIANA'
    assert not ctrl.configuracion_patrimonio(cid, 2024)['confirmado']
    heredada = ctrl.configuracion_patrimonio(cid, 2026)
    ctrl.configurar_patrimonio(cid, 2026, 'CATALUNA', '', 'Elección propia 2026', True, heredada['version'])
    assert ctrl.configuracion_patrimonio(cid, 2027)['normativa'] == 'CATALUNA'


def test_cambiar_a_normativa_sin_motor_no_hereda_bonificacion_otra_region(ctrl):
    cid = ctrl.crear_cliente('QA', 'Sin motor', 'X5555558E', ejercicio=2025)['id']
    ctrl.editar(cid, 'otros_activos', None, dict(ejercicio=2025, tipo='FONDO',
        valor_informe=1500000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='Certificado QA'), 2025)
    for codigo in ('MADRID', 'NAVARRA'):
        v = ctrl.configuracion_patrimonio(cid, 2025)['version']
        ctrl.configurar_patrimonio(cid, 2025, codigo, '', 'Prueba del asesor QA', True, v)
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')
    assert p['bonificacion'] == 0 and p['minimo_exento'] == 700000
    assert p['estado'] == 'Revisar' and 'sin motor validado' in p['observaciones']


@pytest.mark.parametrize('region,ip,its,esperado', [
    ('MADRID', 36546.37, 5100, 31446.37), ('MADRID', 2000, 0, 2000),
    ('GALICIA', 20000, 5000, 10000), ('EXTREMADURA', 20000, 5000, 20000),
    ('CATALUNA', 20000, 5000, 0)])
def test_bonificacion_general_sin_limites_ni_circularidad(region, ip, its, esperado):
    assert np.bonificacion_general(region, 2025, ip, its) == esperado


def test_regimen_comun_todas_comunidades_con_minimo_y_tarifa_2025():
    comunes = set(np.NOMBRES) - {'PAIS_VASCO', 'NAVARRA', 'CEUTA', 'MELILLA', 'OTRA'}
    assert len(comunes) == 16  # Quince comunidades de régimen común y estatal.
    for codigo in comunes:
        minimo, tarifa = np.regla(codigo, 2025)
        assert minimo >= 0
        if tarifa:
            for base, cuota, _ in tarifa:
                assert np.cuota(base, tarifa) == float(cuota)


def test_bonificacion_manual_no_se_pisa_y_puede_volver_a_automatica(ctrl):
    cid = ctrl.crear_cliente('QA', 'Bonificación', 'X5555559E', ejercicio=2025)['id']
    ctrl.editar(cid, 'otros_activos', None, dict(ejercicio=2025, tipo='FONDO',
        valor_informe=1500000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='Certificado QA'), 2025)
    cfg = ctrl.configuracion_patrimonio(cid, 2025)
    ctrl.configurar_patrimonio(cid, 2025, 'MADRID', '', 'Mayor valor en Madrid', True, cfg['version'])
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')
    ctrl.editar(cid, 'patrimonio_714', p['codigo'], {'bonificacion': 123}, 2025)
    assert ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')['bonificacion'] == 123
    cfg = ctrl.configuracion_patrimonio(cid, 2025)
    assert cfg['bonificacion_manual']
    ctrl.usar_bonificacion_automatica(cid, 2025, cfg['version'])
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')
    assert p['bonificacion'] == p['cuota_integra'] and p['resultado'] == 0
    assert not ctrl.configuracion_patrimonio(cid, 2025)['bonificacion_manual']


def test_cambio_de_normativa_en_excel_no_se_ignora_y_exige_confirmacion(ctrl):
    cid = ctrl.crear_cliente('QA', 'Cambio Excel', 'X5555560E', ejercicio=2025)['id']
    v = ctrl.configuracion_patrimonio(cid, 2025)['version']
    r = ctrl.configurar_patrimonio(cid, 2025, 'CATALUNA', '', 'Mayor valor en Cataluña', True, v)
    cfg = ctrl.configuracion_patrimonio(cid, 2025)
    wb = openpyxl.load_workbook(r['excel'])
    wb['11_PATRIMONIO_714']['E2'] = 'Comunidad de Madrid'
    wb.save(r['excel'])
    wb.close()
    ctrl.sincronizar(cid, 2025)
    nueva = ctrl.configuracion_patrimonio(cid, 2025)
    assert nueva['normativa'] == 'MADRID' and not nueva['confirmado']
    assert nueva['version'] != cfg['version']


def test_minimo_manual_en_excel_se_conserva_tras_recalcular(ctrl):
    cid = ctrl.crear_cliente('QA', 'Mínimo manual', 'X5555561E', ejercicio=2025)['id']
    ctrl.editar(cid, 'otros_activos', None, dict(ejercicio=2025, tipo='FONDO',
        valor_informe=1500000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='Certificado QA'), 2025)
    cfg = ctrl.configuracion_patrimonio(cid, 2025)
    r = ctrl.configurar_patrimonio(cid, 2025, 'CATALUNA', '', 'Mayor valor en Cataluña', True, cfg['version'])
    wb = openpyxl.load_workbook(r['excel'])
    wb['11_PATRIMONIO_714']['I2'] = 1250000
    wb.save(r['excel'])
    wb.close()
    ctrl.sincronizar(cid, 2025)
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2025')
    assert (p['minimo_exento'], p['base_liquidable'], p['cuota_integra']) == (1250000, 250000, 612.01)
    assert 'mínimo exento manual' in p['observaciones'] and p['estado'] == 'Revisar'
    assert ctrl.configuracion_patrimonio(cid, 2025)['minimo_manual']


def test_restaurar_automatica_sin_regla_no_conserva_la_bonificacion_manual(ctrl):
    cid = ctrl.crear_cliente('QA', 'Restaurar automática', 'X5555562E', ejercicio=2024)['id']
    ctrl.editar(cid, 'otros_activos', None, dict(ejercicio=2024, tipo='FONDO',
        valor_informe=5000000, ubicacion='ESPANA', pct_titularidad=1,
        revision_confirmada=1, confirmacion_legal='QA ficticia'), 2024)
    cfg = ctrl.configuracion_patrimonio(cid, 2024)
    ctrl.configurar_patrimonio(cid, 2024, 'ESTATAL', '', 'QA ficticia', True, cfg['version'])
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2024')
    ctrl.editar(cid, 'patrimonio_714', p['codigo'], {'bonificacion': 1000}, 2024)
    cfg = ctrl.configuracion_patrimonio(cid, 2024)
    assert cfg['bonificacion_manual']
    ctrl.usar_bonificacion_automatica(cid, 2024, cfg['version'])
    p = ctrl.db.get_por_clave('patrimonio_714', cid, '714|2024')
    assert (p['bonificacion'], p['resultado'], p['estado']) == (0, 53546.37, 'Revisar')
    assert not ctrl.configuracion_patrimonio(cid, 2024)['bonificacion_manual']
