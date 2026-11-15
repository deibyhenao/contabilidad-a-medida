"""Divisas y precisión: contraejemplos reproducidos tras la auditoría externa."""
import pytest

import otros_activos as oa
import pdf_processor as pp


@pytest.mark.parametrize('tipo', ['CUENTA', 'CRIPTO', 'FONDO', 'NO_COTIZADO'])
def test_titularidad_se_aplica_antes_de_redondear_el_importe_atribuido(tipo):
    fila = dict(ejercicio=2025, tipo=tipo, pct_titularidad='0.5', ubicacion='ESPANA',
        revision_confirmada=1, confirmacion_legal='QA ficticia', valor_informe='95000.005',
        saldo_31dic='95000.005', saldo_medio_4t='90000')
    r = oa.valorar(fila)
    assert r['valor_calculado'] == 95000.01
    assert r['valor_cliente'] == r['valor_sujeto'] == 47500.00


@pytest.mark.parametrize('manual', ['95000.005', 95000.005])
def test_valor_manual_tambien_redondea_una_sola_vez_por_titular(manual):
    r = oa.valorar(dict(ejercicio=2025, tipo='FONDO', pct_titularidad='0.5',
        ubicacion='ESPANA', valor_informe=100000, valor_manual=manual,
        motivo_manual='Precisión del certificado', revision_confirmada=1, confirmacion_legal='QA ficticia'))
    assert r['valor_cliente'] == 47500.00


@pytest.mark.parametrize('moneda', ['USD', 'JPY', 'MXN', '$', 'US$', 'dólar', 'dólares', 'yenes', 'pesos',
                                    'Moneda: ABC', 'Currency: CAD', 'Divisa: desconocida'])
def test_lector_local_no_etiqueta_saldos_extranjeros_como_euros(tmp_path, monkeypatch, moneda):
    texto = ('Certificado bancario\nEjercicio: 2025\nBanco: Banco Ficticio\nIBAN: FICTICIO\n'
             f'{moneda}\nSaldo a 31/12: 1.500,00\nSaldo medio 4T: 1.600,00')
    monkeypatch.setattr(pp, 'extraer_texto', lambda ruta: (texto, False))
    ext = pp.extraer_local(tmp_path/'certificado.pdf')
    assert ext.tipo == pp.TipoDocumento.INFORME_ACTIVOS
    assert 'saldo_31dic' not in ext.campos and 'saldo_medio_4t' not in ext.campos
    assert any('divisas' in a for a in ext.avisos)


@pytest.mark.parametrize('moneda', ['Moneda: EUR', 'Divisa: euro', 'Currency: EUR (euro)', '€'])
def test_lector_local_conserva_saldos_en_euros(tmp_path, monkeypatch, moneda):
    texto = ('Certificado bancario\nEjercicio: 2025\nBanco: Banco Ficticio\nIBAN: FICTICIO\n'
             f'{moneda}\nSaldo a 31/12: 1.500,00\nSaldo medio 4T: 1.600,00')
    monkeypatch.setattr(pp, 'extraer_texto', lambda ruta: (texto, False))
    ext = pp.extraer_local(tmp_path/'certificado.pdf')
    assert ext.campos['saldo_31dic'] == 1500
    assert ext.campos['saldo_medio_4t'] == 1600


def test_libro_anterior_renueva_formula_una_vez_y_conserva_notas(tmp_path):
    import database
    import excel_generator as eg
    import openpyxl
    from controlador import Controlador
    db = database.Database(tmp_path/'qa.sqlite')
    db.carpeta_base = tmp_path/'Expedientes'
    db.cerrar()
    c = Controlador(tmp_path/'qa.sqlite')
    try:
        cid = c.crear_cliente('QA', 'Fórmula anterior', 'X7777789A', ejercicio=2025)['id']
        c.editar(cid, 'otros_activos', None, dict(ejercicio=2025, tipo='CRIPTO',
            unidades='0.95000005', valor_unitario='100000', pct_titularidad=.5,
            ubicacion='ESPANA', revision_confirmada=1, confirmacion_legal='QA ficticia'), 2025)
        ruta = c.db.ruta_excel(c.db.get_cliente(cid))
        wb = openpyxl.load_workbook(ruta)
        wb['16_OTROS_ACTIVOS']['AG2'] = '=ROUND(AF2*G2,2)'
        wb['16_OTROS_ACTIVOS']['Z2'] = 'Nota manual del libro anterior'
        wb.save(ruta)
        wb.close()
        c.db.set_config(f'excel_firma|{cid}', eg._firma_excel(ruta))
        c.db.set_config(f'excel_revision|{cid}', 'otros-activos-714-local-20261104')
        assert eg.necesita_actualizar_formato(c.db, cid)
        c.abrir_cliente(cid, 2025)
        assert not eg.necesita_actualizar_formato(c.db, cid)
        wb = openpyxl.load_workbook(ruta)
        assert 'AF2' not in wb['16_OTROS_ACTIVOS']['AG2'].value
        assert wb['16_OTROS_ACTIVOS']['Z2'].value == 'Nota manual del libro anterior'
        wb.close()
        firma = eg._firma_excel(ruta)
        assert c.abrir_cliente(cid, 2025)['excel'] is None
        assert eg._firma_excel(ruta) == firma
        assert len(c.db.listar('otros_activos', cid)) == 1
    finally:
        c.cerrar()
