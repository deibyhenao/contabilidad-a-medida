"""Contraejemplos de la auditoría de noviembre: datos ficticios y API bloqueada por conftest."""
from datetime import date
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

import calendario_fiscal as cf
from controlador import Controlador
import database
import tax_calculator as tc


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / 'qa.sqlite')
    c.cambiar_carpeta_clientes(tmp_path / 'Expedientes')
    yield c
    c.cerrar()


def origen(c, pct=.5):
    cid = c.crear_cliente('QA', 'Origen', 'X8877701A', pais='Rusia', ejercicio=2025)['id']
    inm = c.db.upsert('inmuebles', cid, dict(direccion='Calle QA', ref_catastral='0000000AA0000A0001AA',
        fecha_adquisicion='2020-01-01', pct_titularidad=pct, precio_escritura=200000))
    c.db.upsert('catastro', cid, dict(inmueble=inm, ejercicio=2025, valor_catastral=100000,
        valor_construccion=60000, anio_revision=2018))
    c.db.upsert('contratos', cid, dict(inmueble=inm, fecha_inicio='2025-01-01', fecha_fin_prevista='2025-12-31',
        fecha_fin_real='2025-12-31', estado='Finalizado', tipo='Temporada', renta_inicial=1000, periodicidad='Mensual'))
    c.commit_and_sync_excel(cid, 2025)
    return cid, inm


def datos():
    return dict(nombre='QA', apellidos='Cotitular', nif='X8877702B', pais='Rusia')


@pytest.mark.parametrize('ej,periodo', [(2025,'Anual'),(2026,'Anual'),(2026,'1T'),(2026,'4T'),(2027,'Anual'),(2027,'1T')])
def test_cuota_cero_conserva_enero_segun_articulo_5c2(ej, periodo):
    assert tc.plazo_210_alquiler(ej, periodo, 0) == cf.siguiente_habil(date(ej+1, 1, 20))


def test_recalculo_fallido_no_deja_otro_cliente_ni_cambia_el_original(ctrl, monkeypatch):
    cid, inm = origen(ctrl, 1)
    ruta = ctrl.db.ruta_excel(ctrl.db.get_cliente(cid))
    huella = hashlib.sha256(ruta.read_bytes()).hexdigest()
    calcular = ctrl._recalcular_bd
    def fallar(did, *args):
        if did != cid:
            raise ValueError('Recálculo ficticio rechazado')
        return calcular(did, *args)
    monkeypatch.setattr(ctrl, '_recalcular_bd', fallar)
    with pytest.raises(ValueError, match='ficticio'):
        ctrl.crear_cotitular(cid, inm, datos(), .5, 2025, pct_cliente=.5)
    assert ctrl.db.get_cliente_por_nif(datos()['nif']) is None
    assert ctrl.db.get('inmuebles', cid, inm)['pct_titularidad'] == 1
    assert hashlib.sha256(ruta.read_bytes()).hexdigest() == huella


def test_fallo_en_pdf_no_aborta_lo_ya_confirmado_y_ambos_se_recalculan(ctrl, monkeypatch):
    cid, inm = origen(ctrl, 1)
    def fallar(*args):
        raise PermissionError('Soporte ficticio bloqueado')
    monkeypatch.setattr(ctrl, '_copiar_soportes_cotitular', fallar)
    r = ctrl.crear_cotitular(cid, inm, datos(), .5, 2025, pct_cliente=.5)
    assert r['cliente']['id'] != cid
    assert any('soporte' in a.lower() or 'pdf' in a.lower() for a in r['avisos_documentales'])
    for did in (cid, r['cliente']['id']):
        filas = ctrl.db.listar('m210_alquiler', did, ejercicio=2025)
        assert len(filas) == 1 and filas[0]['ingresos'] == 6000 and filas[0]['cuota'] == 1440


def test_exportacion_del_destino_fallida_no_deja_sin_recalcular_al_origen(ctrl, monkeypatch):
    cid, inm = origen(ctrl, 1)
    exportar = ctrl._exportar_informe
    def fallar(did, *args):
        if did != cid:
            raise OSError('Excel ficticio no disponible')
        return exportar(did, *args)
    monkeypatch.setattr(ctrl, '_exportar_informe', fallar)
    r = ctrl.crear_cotitular(cid, inm, datos(), .5, 2025, pct_cliente=.5)
    assert r['error_excel_cotitular']
    assert ctrl.db.listar('m210_alquiler', cid, ejercicio=2025)[0]['ingresos'] == 6000
    assert ctrl.db.get_config(f"excel_pendiente|{r['cliente']['id']}")


def test_revalida_duplicados_despues_de_importar_el_excel_del_destino(ctrl, monkeypatch):
    cid, inm = origen(ctrl)
    did = ctrl.crear_cliente(**datos(), ejercicio=2025)['id']
    importar = ctrl._importar
    def importacion_que_trae_un_inmueble(cliente):
        importar(cliente)
        if cliente == did:
            ctrl.db.upsert('inmuebles', did, dict(codigo='INM-099', ref_catastral='0000000AA0000A0001AA'))
    monkeypatch.setattr(ctrl, '_importar', importacion_que_trae_un_inmueble)
    with pytest.raises(ValueError, match='ya tiene este inmueble|supera el 100'):
        ctrl.crear_cotitular(cid, inm, datos(), .5, 2025)
    assert len(ctrl.db.listar('inmuebles', did)) == 1


def test_no_registra_pdf_cambiado_con_la_huella_antigua(ctrl):
    cid, inm = origen(ctrl)
    carpeta = Path(ctrl.db.get_cliente(cid)['carpeta']) / 'Soportes_PDF'
    pdf = carpeta / 'comun.pdf'
    pdf.write_bytes(b'%PDF-1.4 version ficticia A')
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    ctrl.db.registrar_documento(cid, 'Soportes_PDF/comun.pdf', 'Escritura de compra', sha, pdf.name, inmueble=inm)
    pdf.write_bytes(b'%PDF-1.4 version ficticia B')
    r = ctrl.crear_cotitular(cid, inm, datos(), .5, 2025)
    assert r['copiados']['documentos'] == 0
    assert not any(d.get('sha256') == sha for d in ctrl.db.listar('documentos', r['cliente']['id']))
    assert r['avisos_documentales']


def test_referencia_con_tab_no_permite_superar_cien_por_cien(ctrl):
    cid, inm = origen(ctrl, .3)
    tercero = ctrl.crear_cliente('QA', 'Tercero', 'X8877703C', pais='Rusia', ejercicio=2025)['id']
    ctrl.db.upsert('inmuebles', tercero, dict(ref_catastral='0000000AA\t0000A0001AA', pct_titularidad=.5))
    with pytest.raises(ValueError, match='supera el 100'):
        ctrl.crear_cotitular(cid, inm, datos(), .3, 2025)


def test_ganancial_forzado_no_acepta_porcentaje_interno_distinto(ctrl):
    cid, inm = origen(ctrl)
    ctrl.db.upsert('inmuebles', cid, dict(codigo=inm, gananciales_forzado=1))
    with pytest.raises(ValueError, match='ganancial'):
        ctrl.crear_cotitular(cid, inm, datos(), .5, 2025, pct_cliente=.4)


def test_importa_titularidad_editada_del_tercero_antes_de_sumar(ctrl, monkeypatch):
    cid, inm = origen(ctrl, .3)
    tercero = ctrl.crear_cliente('QA', 'Tercero', 'X8877703C', pais='Rusia', ejercicio=2025)['id']
    cod = ctrl.db.upsert('inmuebles', tercero, dict(ref_catastral='0000000AA0000A0001AA', pct_titularidad=.3))
    importar = ctrl._importar
    def importar_edicion(cliente):
        importar(cliente)
        if cliente == tercero:
            ctrl.db.upsert('inmuebles', tercero, dict(codigo=cod, pct_titularidad=.5))
    monkeypatch.setattr(ctrl, '_importar', importar_edicion)
    with pytest.raises(ValueError, match='supera el 100'):
        ctrl.crear_cotitular(cid, inm, datos(), .3, 2025)
    assert ctrl.db.get_cliente_por_nif(datos()['nif']) is None
    assert ctrl.db.get('inmuebles', tercero, cod)['pct_titularidad'] == .5


def test_copia_diaria_no_elimina_la_que_acaba_de_crear_por_fecha_del_reloj(tmp_path):
    from datetime import datetime
    import persistencia
    with database.Database(tmp_path/'qa.sqlite'):
        pass
    for anno in range(2030,2044):
        persistencia.crear_snapshot_diario(tmp_path/'qa.sqlite', datetime(anno,1,1))
    nueva = persistencia.crear_snapshot_diario(tmp_path/'qa.sqlite', datetime(2026,11,15))
    assert nueva and nueva.is_file()


@pytest.mark.parametrize('contenido', [b'archivo ficticio incompleto', b''])
def test_copia_diaria_invalida_no_se_pisa_y_no_impide_un_respaldo_valido(tmp_path, contenido):
    from datetime import datetime
    import sqlite3
    import persistencia
    with database.Database(tmp_path/'qa.sqlite'):
        pass
    carpeta = tmp_path/'Backups_Seguridad'
    carpeta.mkdir(exist_ok=True)
    invalida = carpeta/'backup_diario_20261115.db'
    invalida.write_bytes(contenido)
    nueva = persistencia.crear_snapshot_diario(tmp_path/'qa.sqlite', datetime(2026,11,15))
    assert nueva and nueva != invalida and invalida.read_bytes() == contenido
    with sqlite3.connect(nueva) as con:
        assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_no_se_permite_exceder_cien_por_cien_por_una_millonesima(ctrl):
    cid, inm = origen(ctrl, .500001)
    with pytest.raises(ValueError, match='supera el 100'):
        ctrl.crear_cotitular(cid, inm, datos(), .5, 2025)


def test_confirmar_residencia_revierte_si_falla_el_recalculo(ctrl, monkeypatch):
    cid, inm = origen(ctrl)
    def fallar(*args):
        raise ValueError('Fallo ficticio de cálculo')
    monkeypatch.setattr(ctrl, '_recalcular_bd', fallar)
    with pytest.raises(ValueError, match='ficticio'):
        ctrl.confirmar_residencia(cid, 2026)
    assert not ctrl.db.listar('residencias', cid, ejercicio=2026)


def test_residencia_heredada_no_se_puede_dar_por_confirmada_desde_el_estado(ctrl):
    cid, inm = origen(ctrl)
    modelo = ctrl.db.upsert('m210_alquiler', cid, dict(ejercicio=2026, inmueble=inm, estado='Falta documentación'))
    with pytest.raises(ValueError, match='residencia fiscal'):
        ctrl._validar_imputacion_presentable(cid, 'm210_alquiler', modelo)


def test_denominacion_oficial_francesa_no_se_convierte_en_extra_ue():
    assert tc.resolver_residencia({'pais':'République Française'}).tipo == .19


def test_arranque_no_falla_si_no_se_puede_crear_el_hilo_de_backup(monkeypatch, tmp_path):
    import asyncio
    import flet as ft
    import main
    tareas, vistas = [], []
    page = NS(platform_brightness=ft.Brightness.LIGHT, window=None, add=vistas.append,
              run_task=tareas.append, update=lambda:None)
    class AppFalsa:
        cerrando = False
        def __init__(self, *args):
            pass
        def iniciar(self, *args):
            pass
    class HiloFalso:
        def __init__(self, *args, **kw):
            pass
        def start(self):
            raise RuntimeError('Hilo ficticio no disponible')
    monkeypatch.setattr(main, 'App', AppFalsa)
    monkeypatch.setattr(main, 'Controlador', lambda:NS(db=NS(db_path=tmp_path/'qa.sqlite')))
    monkeypatch.setattr(main, '_cargar_modulos', lambda:None)
    monkeypatch.setattr(main, 'preparar_pagina', lambda *args:None)
    monkeypatch.setattr(main, 'threading', NS(Thread=HiloFalso))
    monkeypatch.setenv('SPLASH_MIN','0')
    main.main(page)
    asyncio.run(tareas.pop()())
    assert vistas[0].controls == []


@pytest.mark.parametrize('paginas', [41,81,160])
def test_pdf_escaneado_preparado_no_excede_cuarenta_y_conserva_orden(paginas):
    import fitz
    import re
    import ia_extractor as ia
    from test_pdf_escaneado_107 import _pdf
    reducido, aviso = ia._pdf_para_ia(_pdf(paginas, set(range(paginas))))
    with fitz.open(stream=reducido,filetype='pdf') as doc:
        assert doc.page_count == 40
    indices = [int(n) for n in re.search(r'orden enviado: ([\d, ]+)', aviso).group(1).split(',')]
    assert indices == sorted(set(indices)) and len(indices) == 40
    assert indices[0] == 1 and indices[-1] == paginas


@pytest.mark.parametrize('porcentaje', ['1', '0,5', '', '70', '70%'])
def test_dialogo_de_cotitular_interpreta_porcentajes_y_conserva_datos_si_faltan(porcentaje):
    import main
    import ui_estilo as ui
    a = main.App.__new__(main.App)
    a.p, a.cid, a.ejercicio = ui.CLARO, 1, 2025
    a.ctrl = NS(lector=NS(get=lambda *args: {'pct_titularidad':.5}, get_cliente_por_nif=lambda nif:None),
                crear_cotitular=Mock(return_value={}))
    a._es_residente = lambda: False
    a.cerrar_dialogo = Mock()
    a.page = NS(update=Mock())
    a._refrescar_clientes = Mock()
    a.en_segundo_plano = lambda texto, trabajo, *args, **kw: trabajo()
    captura = {}
    a._dialogo = lambda titulo, contenido, acciones, **kw: captura.update(contenido=contenido, acciones=acciones)
    a.dialogo_cotitular('INM-001')
    campos = {c.label: c for c in captura['contenido'].controls if getattr(c,'label',None)}
    campos['Nombre'].value = 'QA'
    campos['NIF / NIE'].value = 'X8877799Z'
    campos['País de residencia fiscal'].value = 'Rusia'
    campos['Titularidad del cotitular (%)'].value = porcentaje
    captura['acciones'][-1].on_click(None)
    if porcentaje in ('', '70', '70%'):
        a.cerrar_dialogo.assert_not_called()
        a.ctrl.crear_cotitular.assert_not_called()
    else:
        valor = a.ctrl.crear_cotitular.call_args.args[3]
        col = next(c for c in database.TABLAS['inmuebles'].columnas_bd if c.campo == 'pct_titularidad')
        assert database._normalizar(col, valor) == (.01 if porcentaje == '1' else .005)


def test_copia_diaria_reutiliza_respaldo_valido_si_nombre_base_esta_danado(tmp_path):
    from datetime import datetime
    import persistencia
    db_path = tmp_path/'qa.sqlite'
    with database.Database(db_path):
        pass
    carpeta = tmp_path/'Backups_Seguridad'
    carpeta.mkdir()
    (carpeta/'backup_diario_20261115.db').write_bytes(b'copia ficticia truncada')
    assert persistencia.crear_snapshot_diario(db_path, datetime(2026,11,15)) is not None
    nombres = sorted(p.name for p in carpeta.iterdir())
    assert persistencia.crear_snapshot_diario(db_path, datetime(2026,11,15)) is None
    assert sorted(p.name for p in carpeta.iterdir()) == nombres
