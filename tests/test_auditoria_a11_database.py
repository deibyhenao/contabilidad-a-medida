"""A11: invariantes de fechas, aislamiento y justificantes. Solo datos ficticios."""
from pathlib import Path
import os
import subprocess

import pytest
import database
from database import Database
import fiscal_residentes as fr
import fixtures_pdf as fx
import pdf_processor as pp


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path/'ficticios.sqlite') as d:
        d.carpeta_base=tmp_path/'base'
        yield d


@pytest.mark.parametrize('tabla',['facturas_emitidas','facturas_recibidas'])
def test_sin_fecha_no_admite_trimestre_y_borrarla_desclasifica(db,tabla):
    cid=db.crear_cliente('Cliente','Fechas','A11-FECHAS',tipo_cliente='Autónomo')['id']
    codigo=db.upsert(tabla,cid,{'fecha':'2025-01-15','base':1000,'cuota_iva':210})
    db.vaciar_campo(tabla,cid,codigo,'fecha')
    fila=db.get(tabla,cid,codigo)
    assert fila['fecha'] is None and fila['trimestre'] is None
    assert fila['ejercicio']==2025  # Sigue visible para clasificarla en su ejercicio.
    db.upsert(tabla,cid,{'codigo':codigo,'trimestre':'4T'})
    assert db.get(tabla,cid,codigo)['trimestre'] is None
    nuevo=db.upsert(tabla,cid,{'ejercicio':2025,'trimestre':'1T','base':2000})
    assert db.get(tabla,cid,nuevo)['trimestre'] is None
    informe=fr.recalcular_residente(db,cid,2025)
    assert all(t.ingresos==0 and t.gastos==0 for t in informe.trimestres)


@pytest.mark.parametrize('modelo,tabla,clave,campo',[
    ('303','m303','303|2025|1T','a_ingresar'),('130','m130','130|2025|1T','a_ingresar'),
    ('111','m111_115','RET|2025|1T|111','retenciones'),('115','m111_115','RET|2025|1T|115','retenciones')])
@pytest.mark.parametrize('importe',[0,190])
def test_justificante_conserva_importe_real_y_soporte(db,modelo,tabla,clave,campo,importe):
    cid=db.crear_cliente('Cliente','Presentación','A11-PRES',tipo_cliente='Autónomo')['id']
    db.upsert('facturas_emitidas',cid,{'fecha':'2025-02-15','base':1000,'cuota_iva':210})
    db.upsert('modelos_presentados',cid,{'modelo':modelo,'ejercicio':2025,'periodo':'1T','importe':importe,
        'csv':'FICTICIO-CSV','ruta':'Soportes_PDF/justificante.pdf','fecha_presentacion':'2025-04-15','estado':'Presentado'})
    fr.recalcular_residente(db,cid,2025)
    fila=db.get_por_clave(tabla,cid,clave)
    assert fila and fila['estado']=='Presentado'
    assert fila[campo]==importe
    assert fila['ruta']=='Soportes_PDF/justificante.pdf' and fila['nrc']=='FICTICIO-CSV'
    fr.recalcular_residente(db,cid,2025)
    assert db.get_por_clave(tabla,cid,clave)[campo]==importe


@pytest.mark.parametrize('base,retencion,esperada',[(1000,-150,150),(-1000,-150,-150),(-1000,150,-150)])
def test_retencion_rectificativa_conserva_signo_de_base(tmp_path,base,retencion,esperada):
    ruta=fx._crear(tmp_path/'rectificativa.pdf',f'''FACTURA RECTIFICATIVA
Proveedor: Cliente Ficticio
NIF: A11FICTICIO
Cliente: Destinatario ficticio
NIF cliente: A11DESTINO
Número de factura: A11-RECT
Fecha de emisión: 12/03/2025
Base imponible: {base},00 euros
Cuota IVA: {int(base*.21)},00 euros
Retención IRPF: {retencion},00 euros
Total factura: {int(base*1.21-esperada)},00 euros''')
    ext=pp.extraer_local(ruta)
    assert ext.campos['retencion']==esperada
    assert fr.total_factura({'base':ext.campos['base'],'cuota_iva':ext.campos['iva'],
                            'retencion':ext.campos['retencion']})==round(base*1.21-esperada,2)


def test_alta_no_reutiliza_junction_de_otro_expediente(db,tmp_path):
    otro=db.crear_cliente('Cliente','NR','A11-OTRO')
    nif='A11ENLACE'
    enlace=db.raiz('RES')/database.componente_ruta(f'Cliente Falso_{nif}')
    p=subprocess.run(['cmd','/c','mklink','/J',str(enlace),otro['carpeta']],capture_output=True)
    assert p.returncode==0
    try:
        with pytest.raises(ValueError):
            db.crear_cliente('Cliente','Falso',nif,tipo_cliente='Autónomo')
        assert db.get_cliente_por_nif(nif) is None
    finally:os.rmdir(enlace)


def test_migracion_respeta_base_ya_configurada(db):
    base=db.carpeta_base
    db.crear_cliente('Cliente','Base','A11-BASE')
    db.migrar_carteras()
    assert db.carpeta_base==base


def test_identidades_distintas_no_comparten_carpeta_por_nombre(db):
    a=db.crear_cliente('Ana','_B','C')
    b=db.crear_cliente('Ana','','B_C')
    assert a['id']!=b['id'] and Path(a['carpeta'])!=Path(b['carpeta'])
    assert db.verificar_cartera(a)!=db.verificar_cartera(b)
    assert db.crear_cliente('Ana','_B','C')['id']==a['id']


def test_alta_no_reutiliza_carpeta_huerfana(db):
    carpeta=db.raiz('NR')/database.componente_ruta('Cliente Huerfano_NIFUNICO')
    carpeta.mkdir()
    (carpeta/'original.txt').write_text('conservar',encoding='utf-8')
    c=db.crear_cliente('Cliente','Huerfano','NIFUNICO')
    assert Path(c['carpeta'])!=carpeta
    assert (carpeta/'original.txt').read_text()=='conservar'


def test_rollback_migracion_incompleto_se_registra_y_no_reintenta(db,tmp_path,monkeypatch):
    import json
    antigua=tmp_path/'antigua'
    db.set_config('carpeta_clientes',str(antigua))
    for n in (1,2):
        origen=antigua/f'Cliente{n}'
        origen.mkdir(parents=True)
        (origen/'conservar.txt').write_text(str(n),encoding='utf-8')
        db.con.execute('INSERT INTO clientes(nombre,apellidos,nif,tipo_cliente,carpeta,creado,cartera) VALUES(?,?,?,?,?,?,?)',
                       ('Cliente',str(n),f'A11-MIGRA-{n}','No residente',str(origen),'2025-01-01','NR'))
    db.con.commit()
    original=Path.rename

    def fallar(self,destino):
        destino=Path(destino)
        if self.name=='Cliente2' or (self.name=='Cliente1' and destino==antigua/'Cliente1'):
            raise PermissionError('Fallo simulado de traslado y recuperación')
        return original(self,destino)

    with monkeypatch.context() as m:
        m.setattr(Path,'rename',fallar)
        with pytest.raises(RuntimeError,match='incompleta') as error:
            db.migrar_carteras()
    assert getattr(error.value,'requiere_recuperacion',False)
    registros=list((db.db_path.parent/'backups').glob('*.recuperacion.json'))
    assert len(registros)==1
    datos=json.loads(registros[0].read_text(encoding='utf-8'))
    assert datos['movimientos_pendientes'][0]['origen']==str(antigua/'Cliente1')
    assert (antigua/'Clientes_No_Residentes'/'Cliente1'/'conservar.txt').read_text()=='1'
    assert db.get_config('version_carteras') is None
    with pytest.raises(RuntimeError,match='recuperación manual'):
        db.migrar_carteras()


def test_migracion_rechaza_raiz_junction_antes_de_mover(db,tmp_path):
    antigua=tmp_path/'antigua'
    origen=antigua/'Cliente'
    origen.mkdir(parents=True)
    (origen/'conservar.txt').write_text('original',encoding='utf-8')
    db.set_config('carpeta_clientes',str(antigua))
    db.con.execute('INSERT INTO clientes(nombre,apellidos,nif,tipo_cliente,carpeta,creado,cartera) VALUES(?,?,?,?,?,?,?)',
                   ('Cliente','','A11-J-MIGRA','No residente',str(origen),'2025-01-01','NR'))
    db.con.commit()
    destino=tmp_path/'AJENO'; destino.mkdir()
    enlace=antigua/'Clientes_No_Residentes'
    p=subprocess.run(['cmd','/c','mklink','/J',str(enlace),str(destino)],capture_output=True)
    assert p.returncode==0
    try:
        with pytest.raises(ValueError,match='junction'):
            db.migrar_carteras()
        assert (origen/'conservar.txt').read_text()=='original'
        assert list(destino.iterdir())==[]
    finally:os.rmdir(enlace)


def test_migrar_carteras_sincroniza_columna_si_carpeta_ya_esta_en_raiz(db, tmp_path):
    db.con.execute("DELETE FROM config WHERE clave='version_carteras'")
    carpeta = db.raiz("RES") / "Autonomo_Ya_En_Raiz_A11SYNC"
    carpeta.mkdir(parents=True)
    db.con.execute(
        "INSERT INTO clientes(nombre,apellidos,nif,tipo_cliente,carpeta,creado,cartera) VALUES(?,?,?,?,?,?,?)",
        ("Autonomo", "YaEnRaiz", "A11SYNC", "Autónomo", str(carpeta), "2025-01-01", "NR"),
    )
    db.con.commit()
    db.migrar_carteras()
    assert db.contar_clientes("RES") == 1
    assert db.contar_clientes("NR") == 0

