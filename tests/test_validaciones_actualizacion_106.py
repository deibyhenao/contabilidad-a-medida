from pathlib import Path
import sys,sqlite3,os
import pytest,openpyxl,database,ia_extractor,excel_generator as eg
from controlador import Controlador
from migracion_actualizacion import validar_almacen_existente
from app_paths import AlmacenNoDisponible

def test_bd_valida_sin_tablas_no_oculta_clientes_anteriores(tmp_path):
 d=tmp_path/'Clientes_No_Residentes/EXISTENTE';d.mkdir(parents=True);(d/'soporte.txt').write_text('FICTICIO')
 db=tmp_path/'contabilidad.db'
 with sqlite3.connect(db) as c:c.execute('PRAGMA user_version=0')
 assert db.stat().st_size>0
 with pytest.raises(AlmacenNoDisponible):validar_almacen_existente(db)


def test_libro_renombrado_se_detecta_sin_recrear_el_anterior(tmp_path,monkeypatch):
 monkeypatch.setattr(database,'PROYECTO_DIR',tmp_path);monkeypatch.setattr(ia_extractor,'obtener_clave',lambda:None)
 c=Controlador(tmp_path/'ficticio.db');c.cambiar_carpeta_clientes(tmp_path/'Datos')
 try:
  cli=c.crear_cliente('FICTICIO','RENOMBRADO','QA-RENAME',pais='Rusia',ejercicio=2025);cid=cli['id'];original=c.sincronizar(cid,2025)['excel']
  nuevo=original.with_name('MI_LIBRO_EDITADO.xlsx');original.rename(nuevo)
  wb=openpyxl.load_workbook(nuevo);wb['00_RESUMEN']['B8']='nuevo@example.invalid';wb.save(nuevo);wb.close()
  out=c.abrir_cliente(cid,2025)
  assert not original.exists() and c.db.get_cliente(cid)['email']=='nuevo@example.invalid'
  assert c.db.ruta_excel(c.db.get_cliente(cid))==nuevo
 finally:c.cerrar()


def test_copia_reciente_de_archivo_con_fecha_antigua_se_conserva(tmp_path):
 f=tmp_path/'principal.xlsx';w=openpyxl.Workbook();w.active['A1']='INICIO';w.save(f)
 for n in range(12):w.active['A1']=n;eg._guardar_atomico(w,f,tmp_path)
 w.active['A1']='RESTAURADO';w.save(f);os.utime(f,(946684800,946684800));w.active['A1']='NUEVO';eg._guardar_atomico(w,f,tmp_path);w.close()
 textos=[]
 for b in (tmp_path/'_backups').glob('*.xlsx'):
  q=openpyxl.load_workbook(b,read_only=True);textos.append(q.active['A1'].value);q.close()
 assert 'RESTAURADO' in textos


def test_intentos_fallidos_no_purgan_respaldos_previos(tmp_path,monkeypatch):
 f=tmp_path/'principal.xlsx';w=openpyxl.Workbook();w.active['A1']='INICIO';w.save(f)
 for n in range(12):w.active['A1']=n;eg._guardar_atomico(w,f,tmp_path)
 prev={b.name for b in (tmp_path/'_backups').glob('*.xlsx')};reemplazar=eg.os.replace
 def bloqueado(a,b):
  if Path(b)==f:raise PermissionError('Bloqueo simulado')
  return reemplazar(a,b)
 monkeypatch.setattr(eg.os,'replace',bloqueado)
 for _ in range(3):
  with pytest.raises(eg.ExcelBloqueadoError):eg._guardar_atomico(w,f,tmp_path)
 assert prev <= {b.name for b in (tmp_path/'_backups').glob('*.xlsx')}
 w.close()


def test_wal_sin_base_no_se_descarta(tmp_path):
    ruta = tmp_path / 'contabilidad.db'
    diario = tmp_path / 'contabilidad.db-wal'
    diario.write_bytes(b'DIARIO FICTICIO ANTERIOR')
    with pytest.raises(AlmacenNoDisponible):
        validar_almacen_existente(ruta)
    assert not ruta.exists() and diario.read_bytes() == b'DIARIO FICTICIO ANTERIOR'


def test_copias_ambiguas_no_se_adoptan_ni_sobrescriben(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(database, 'PROYECTO_DIR', tmp_path)
    monkeypatch.setattr(ia_extractor, 'obtener_clave', lambda: None)
    c = Controlador(tmp_path / 'ficticio.db')
    c.cambiar_carpeta_clientes(tmp_path / 'Datos')
    try:
        cli = c.crear_cliente('FICTICIO', 'AMBIGUO', 'QA-AMBIGUO', pais='Rusia', ejercicio=2025)
        original = c.sincronizar(cli['id'], 2025)['excel']
        uno, dos = original.with_name('UNO.xlsx'), original.with_name('DOS.xlsx')
        original.rename(uno)
        shutil.copy2(uno, dos)
        contenido = uno.read_bytes()
        with pytest.raises(ValueError, match='único Excel'):
            c.abrir_cliente(cli['id'], 2025)
        assert not original.exists() and uno.read_bytes() == dos.read_bytes() == contenido
    finally:
        c.cerrar()
