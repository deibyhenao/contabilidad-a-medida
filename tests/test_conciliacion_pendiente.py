"""Contraejemplos de A8: copias antiguas, bajas y representación de negativos."""
import shutil
import json
from pathlib import Path

import openpyxl
import pytest

import database
import excel_generator as eg


@pytest.fixture
def db(tmp_path):
    with database.Database(tmp_path/'qa.sqlite') as d:
        d.carpeta_base = tmp_path/'Expedientes'
        yield d


def activo():
    return dict(ejercicio=2025, tipo='CUENTA', ubicacion='ESPANA',
                saldo_31dic=100, saldo_medio_4t=100, pct_titularidad=1)


def test_copia_pendiente_anterior_no_revierte_una_correccion_aba(db, tmp_path, monkeypatch):
    c = db.crear_cliente('QA', 'ABA', 'X7777771A')
    cid = c['id']
    cod = db.upsert('otros_activos', cid, activo())
    ruta = eg.exportar_cliente(db, cid, 2025)
    guardar = eg._guardar_atomico
    copias = []
    def bloquear(wb, destino, carpeta, **opciones):
        copia = tmp_path/f'pendiente-{len(copias)}.xlsx'
        wb.save(copia)
        copias.append(copia)
        raise eg.ExcelBloqueadoError(copia)
    monkeypatch.setattr(eg, '_guardar_atomico', bloquear)
    for saldo in (200, 100):
        db.upsert('otros_activos', cid, {'codigo': cod, 'saldo_31dic': saldo}, respetar_manuales=False)
        with pytest.raises(eg.ExcelBloqueadoError):
            eg.exportar_cliente(db, cid, 2025)
    monkeypatch.setattr(eg, '_guardar_atomico', guardar)
    shutil.copyfile(copias[0], ruta)
    original = ruta.read_bytes()
    with pytest.raises(eg.ExcelConflictoImportacion):
        eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod)['saldo_31dic'] == 100
    assert ruta.read_bytes() == original
    recuperada = Path(db.get_config(f'excel_pendiente|{cid}'))
    assert recuperada.is_file() and recuperada != ruta
    wb = openpyxl.load_workbook(recuperada)
    assert wb['16_OTROS_ACTIVOS']['I2'].value == 100
    wb['16_OTROS_ACTIVOS']['Z2'] = 'Nota recuperada por el asesor'
    wb.save(recuperada)
    wb.close()
    shutil.copyfile(recuperada, ruta)
    eg.import_excel_to_db(db, cid)
    fila = db.get('otros_activos', cid, cod)
    assert fila['saldo_31dic'] == 100
    assert fila['observaciones'] == 'Nota recuperada por el asesor'


@pytest.mark.parametrize('editar', [False, True])
def test_baja_en_app_no_se_recrea_desde_excel_pendiente(db, editar):
    c = db.crear_cliente('QA', 'Baja', 'X7777772A')
    cid = c['id']
    cod = db.upsert('otros_activos', cid, activo())
    ruta = eg.exportar_cliente(db, cid, 2025)
    db.borrar('otros_activos', cid, cod)
    db.set_config(f'excel_pendiente|{cid}', '1')
    wb = openpyxl.load_workbook(ruta)
    wb['00_RESUMEN']['E4'] = 2025
    if editar:
        wb['16_OTROS_ACTIVOS']['Z2'] = 'Cambio mientras se borraba'
    wb.save(ruta)
    wb.close()
    if editar:
        with pytest.raises(eg.ExcelConflictoImportacion):
            eg.import_excel_to_db(db, cid)
    else:
        eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod) is None


def test_303_negativo_borrador_no_lo_pisa_su_cero_visual(db):
    c = db.crear_cliente('QA', '303', 'X7777773A', tipo_cliente='Autónomo')
    cid = c['id']
    cod = db.upsert('m303', cid, dict(ejercicio=2025, periodo='1T', a_ingresar=-100,
                                    resultado=-100, estado='Pendiente'))
    ruta = eg.exportar_cliente(db, cid, 2025)
    wb = openpyxl.load_workbook(ruta)
    celda = database.CELDAS_RESUMEN['RES']['email']
    wb['00_RESUMEN'][celda] = 'qa@example.invalid'
    wb.save(ruta)
    wb.close()
    eg.import_excel_to_db(db, cid)
    assert db.get('m303', cid, cod)['a_ingresar'] == -100


def test_revision_perdida_no_se_asume_actual_y_libro_legado_sigue_admitido(db):
    c = db.crear_cliente('QA', 'Revisión', 'X7777774A')
    cid = c['id']
    db.upsert('otros_activos', cid, activo())
    ruta = eg.exportar_cliente(db, cid, 2025)
    wb = openpyxl.load_workbook(ruta)
    del wb.custom_doc_props['ContabilidadRevision']
    wb.save(ruta)
    wb.close()
    original = ruta.read_bytes()
    with pytest.raises(eg.ExcelConflictoImportacion, match='copia anterior'):
        eg.import_excel_to_db(db, cid)
    assert ruta.read_bytes() == original
    # Una instalación antigua todavía no ha confirmado ninguna revisión nueva.
    db.set_config(f'excel_revision_id|{cid}', '')
    db.set_config(f'excel_revision_propuesta|{cid}', '')
    eg.import_excel_to_db(db, cid)
    assert len(db.listar('otros_activos', cid)) == 1


def propuesta_bloqueada(db, cid, monkeypatch):
    guardar = eg._guardar_atomico
    def bloquear(wb, destino, carpeta, **opciones):
        copia = destino.with_name('PENDIENTE_QA.xlsx')
        wb.save(copia)
        raise eg.ExcelBloqueadoError(copia)
    monkeypatch.setattr(eg, '_guardar_atomico', bloquear)
    try:
        with pytest.raises(eg.ExcelBloqueadoError) as exc:
            eg.exportar_cliente(db, cid, 2025)
        return exc.value.ruta_pendiente
    finally:
        monkeypatch.setattr(eg, '_guardar_atomico', guardar)


@pytest.mark.parametrize('nueva', [True, False])
def test_edicion_en_propuesta_usa_su_base_sin_pisar_la_canonica(db, monkeypatch, nueva):
    cid = db.crear_cliente('QA', 'Propuesta', 'X7777780A')['id']
    cod = db.upsert('otros_activos', cid, activo())
    ruta = eg.exportar_cliente(db, cid, 2025)
    base = db.get_config(f'excel_base|{cid}')
    if nueva:
        cod = db.upsert('otros_activos', cid, {**activo(), 'entidad': 'Otra entidad'})
    else:
        db.upsert('otros_activos', cid, {'codigo': cod, 'observaciones': 'Corregida en app'}, respetar_manuales=False)
    copia = propuesta_bloqueada(db, cid, monkeypatch)
    assert db.get_config(f'excel_base|{cid}') == base
    wb = openpyxl.load_workbook(copia)
    ws = wb['16_OTROS_ACTIVOS']
    fila = next(r for r in range(2, ws.max_row + 1) if ws[f'A{r}'].value == cod)
    ws[f'Z{fila}'] = 'Nota posterior en la copia propuesta'
    wb.save(copia)
    wb.close()
    shutil.copyfile(copia, ruta)
    eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod)['observaciones'] == 'Nota posterior en la copia propuesta'
    eg.exportar_cliente(db, cid, 2025)
    assert not db.get_config(f'excel_base_propuesta|{cid}')


def test_original_con_propuesta_pendiente_no_toma_la_base_de_la_propuesta(db, monkeypatch):
    cid = db.crear_cliente('QA', 'Original', 'X7777781A')['id']
    cod = db.upsert('otros_activos', cid, activo())
    ruta = eg.exportar_cliente(db, cid, 2025)
    db.upsert('otros_activos', cid, {'codigo': cod, 'saldo_31dic': 200}, respetar_manuales=False)
    propuesta_bloqueada(db, cid, monkeypatch)
    wb = openpyxl.load_workbook(ruta)
    wb['16_OTROS_ACTIVOS']['Z2'] = 'Nota en el original antiguo'
    wb.save(ruta)
    wb.close()
    eg.import_excel_to_db(db, cid)
    fila = db.get('otros_activos', cid, cod)
    assert fila['saldo_31dic'] == 200
    assert fila['observaciones'] == 'Nota en el original antiguo'


def test_propuesta_tambien_rechaza_ediciones_concurrentes_del_mismo_campo(db, monkeypatch):
    cid = db.crear_cliente('QA', 'Conflicto', 'X7777782A')['id']
    cod = db.upsert('otros_activos', cid, activo())
    ruta = eg.exportar_cliente(db, cid, 2025)
    copia = propuesta_bloqueada(db, cid, monkeypatch)
    db.upsert('otros_activos', cid, {'codigo': cod, 'observaciones': 'Cambio en app'}, respetar_manuales=False)
    wb = openpyxl.load_workbook(copia)
    wb['16_OTROS_ACTIVOS']['Z2'] = 'Cambio en Excel'
    wb.save(copia)
    wb.close()
    shutil.copyfile(copia, ruta)
    with pytest.raises(eg.ExcelConflictoImportacion):
        eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod)['observaciones'] == 'Cambio en app'


@pytest.mark.parametrize('editar', [False, True])
def test_baja_posterior_a_propuesta_no_se_recrea(db, monkeypatch, editar):
    cid = db.crear_cliente('QA', 'Baja propuesta', 'X7777783A')['id']
    ruta = eg.exportar_cliente(db, cid, 2025)
    cod = db.upsert('otros_activos', cid, activo())
    copia = propuesta_bloqueada(db, cid, monkeypatch)
    db.borrar('otros_activos', cid, cod)
    wb = openpyxl.load_workbook(copia)
    if editar:
        wb['16_OTROS_ACTIVOS']['Z2'] = 'Editada después de borrar'
    wb.save(copia)
    wb.close()
    shutil.copyfile(copia, ruta)
    if editar:
        with pytest.raises(eg.ExcelConflictoImportacion):
            eg.import_excel_to_db(db, cid)
    else:
        eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod) is None


def test_propuesta_con_resumen_editado_y_fallo_de_guardado_posterior(db, monkeypatch):
    cid = db.crear_cliente('QA', 'Resumen', 'X7777784A')['id']
    ruta = eg.exportar_cliente(db, cid, 2025)
    db.actualizar_cliente(cid, email='app@example.invalid')
    copia = propuesta_bloqueada(db, cid, monkeypatch)
    revision = db.get_config(f'excel_revision_propuesta|{cid}')
    base = db.get_config(f'excel_base_propuesta|{cid}')
    def error(*a, **k):
        raise OSError('No se pudo guardar ninguna copia')
    with monkeypatch.context() as m:
        m.setattr(eg, '_guardar_atomico', error)
        with pytest.raises(OSError):
            eg.exportar_cliente(db, cid, 2025)
    assert db.get_config(f'excel_revision_propuesta|{cid}') == revision
    assert db.get_config(f'excel_base_propuesta|{cid}') == base
    wb = openpyxl.load_workbook(copia)
    wb['00_RESUMEN'][database.CELDAS_RESUMEN['NR']['email']] = 'excel@example.invalid'
    wb.save(copia)
    wb.close()
    shutil.copyfile(copia, ruta)
    eg.import_excel_to_db(db, cid)
    assert db.get_cliente(cid)['email'] == 'excel@example.invalid'


@pytest.mark.parametrize('base', ['', '{dañada', json.dumps({'revision': 'otra'}),
    json.dumps({'revision': 'ajustar', 'tablas': {}, 'filas': {}, 'resumen': {}})])
def test_propuesta_sin_base_verificable_no_resucita_una_fila_borrada(db, monkeypatch, base):
    cid = db.crear_cliente('QA', 'Propuesta antigua', 'X7777785A')['id']
    ruta = eg.exportar_cliente(db, cid, 2025)
    cod = db.upsert('otros_activos', cid, activo())
    copia = propuesta_bloqueada(db, cid, monkeypatch)
    if 'ajustar' in base:
        base = base.replace('ajustar', db.get_config(f'excel_revision_propuesta|{cid}'))
    db.set_config(f'excel_base_propuesta|{cid}', base)
    db.borrar('otros_activos', cid, cod)
    shutil.copyfile(copia, ruta)
    original = ruta.read_bytes()
    with pytest.raises(eg.ExcelConflictoImportacion, match='base de conciliación'):
        eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod) is None
    assert ruta.read_bytes() == original


def test_renovacion_lee_libro_sin_revertir_un_cambio_en_bd_aun_no_exportado(db):
    cid = db.crear_cliente('QA', 'Renovación segura', 'X7777786A')['id']
    cod = db.upsert('otros_activos', cid, activo())
    eg.exportar_cliente(db, cid, 2025)
    # Simula un cierre entre confirmar el cambio de ficha y exportarlo.
    db.upsert('otros_activos', cid, {'codigo': cod, 'saldo_31dic': 200}, respetar_manuales=False)
    assert not db.get_config(f'excel_pendiente|{cid}')
    db.set_config(f'excel_revision|{cid}', 'formato anterior')
    eg.import_excel_to_db(db, cid)
    assert db.get('otros_activos', cid, cod)['saldo_31dic'] == 200
