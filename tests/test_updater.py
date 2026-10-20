import asyncio
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import threading

import pytest

import updater
from updater import ErrorActualizacion, Publicacion
from version import RELEASE_REPOSITORY


def asset(payload=b'instalador ficticio', version='1.0.1'):
    name=f'ContabilidadAMedida-{version}-windows-x64-Setup.exe'
    return {'draft':False,'prerelease':False,'tag_name':'v'+version,'assets':[{
        'name':name,'state':'uploaded','size':len(payload),'digest':'sha256:'+hashlib.sha256(payload).hexdigest(),
        'browser_download_url':f'https://github.com/{RELEASE_REPOSITORY}/releases/download/v{version}/{name}'}]}


def consultar(data):
    return updater.comprobar('1.0.0',abrir=lambda _:io.BytesIO(json.dumps(data).encode()))


def test_version_compara_numericamente_y_no_downgrade():
    assert consultar(asset(version='1.0.10')).version=='1.0.10'
    assert consultar(asset(version='1.0.0')) is None
    assert consultar(asset(version='0.9.9')) is None


@pytest.mark.parametrize('version',['v1.2','1.2.3-beta','01.2.3','1.2.3.4','../x',None,True])
def test_rechaza_versiones_ambiguas(version):
    with pytest.raises(ErrorActualizacion): updater.version_numerica(version)


@pytest.mark.parametrize('cambio',[{'draft':True},{'prerelease':True},{'assets':None},{'tag_name':None}])
def test_rechaza_publicaciones_incompletas(cambio):
    data=asset(); data.update(cambio)
    with pytest.raises(ErrorActualizacion): consultar(data)


@pytest.mark.parametrize('campo,valor',[('digest',None),('digest','sha256:abc'),('size',0),('size',True),
    ('size',updater.MAX_INSTALLER+1),('browser_download_url','https://evil.example/Setup.exe'),
    ('browser_download_url','http://github.com/x.exe'),('browser_download_url','https://github.com/otro/repo/releases/download/v1.0.1/x.exe')])
def test_valida_asset(campo,valor):
    data=asset(); data['assets'][0][campo]=valor
    with pytest.raises(ErrorActualizacion): consultar(data)


def test_no_elige_asset_duplicado():
    data=asset(); data['assets'].append(data['assets'][0])
    with pytest.raises(ErrorActualizacion): consultar(data)


@pytest.mark.parametrize('url',['http://github.com/a','https://github.com.evil.example/a',
    'https://usuario:clave@github.com/a','https://github.com:444/a','https://github.com/a#x','file:///C:/x.exe'])
def test_origen_y_redirects(url):
    with pytest.raises(ErrorActualizacion): updater._url_segura(url,descarga=True)


def test_json_malformado_no_crash():
    for raw in [b'null', b'[]', b'{', b'x'*(updater.MAX_JSON+1)]:
        with pytest.raises(ErrorActualizacion): updater.comprobar(abrir=lambda _:io.BytesIO(raw))


def test_timeout_error_local():
    def fallo(_): raise TimeoutError('secreto interno')
    with pytest.raises(ErrorActualizacion,match='conexión') as e: updater.comprobar(abrir=fallo)
    assert 'secreto' not in str(e.value)


def test_descarga_verificada_y_progreso(tmp_path):
    contenido=b'x'*600000
    pub=consultar(asset(contenido)); estados=[]
    ruta=updater.descargar(pub,estados.append,threading.Event(),carpeta=tmp_path,abrir=lambda _:io.BytesIO(contenido))
    assert ruta.read_bytes()==contenido and ruta.suffix=='.exe'
    assert estados[-1]==1 and estados==sorted(estados)
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('contenido',[b'',b'alterado',b'instalador ficticio y bytes extra'])
def test_descarga_corrupta_no_deja_ejecutable(tmp_path,contenido):
    with pytest.raises(ErrorActualizacion):
        updater.descargar(consultar(asset()),lambda _:None,threading.Event(),carpeta=tmp_path,abrir=lambda _:io.BytesIO(contenido))
    assert not list(tmp_path.iterdir())


def test_cancelar_conserva_datos_y_no_deja_descarga(tmp_path):
    prueba=tmp_path/'contabilidad.db'; prueba.write_bytes(b'base ficticia inmutable')
    cancelar=threading.Event(); cancelar.set()
    with pytest.raises(updater.DescargaCancelada):
        updater.descargar(consultar(asset()),lambda _:None,cancelar,carpeta=tmp_path,abrir=lambda _:io.BytesIO(b'abc'))
    assert [p.name for p in tmp_path.iterdir()]==['contabilidad.db']
    assert prueba.read_bytes()==b'base ficticia inmutable'


def test_no_instala_desde_fuentes(tmp_path):
    with pytest.raises(ErrorActualizacion,match='Instala la aplicación'):
        updater.preparar_instalacion(tmp_path/'x.exe',consultar(asset()))


def test_banner_claro_oscuro_y_estados():
    import ui_estilo as ui
    from ui_actualizaciones import AvisoActualizacion,ESTADOS
    aviso=AvisoActualizacion(ui.CLARO)
    for p in [ui.CLARO,ui.OSCURO]:
        aviso.cambiar_paleta(p)
        for estado in ESTADOS:
            aviso.mostrar(estado,version='1.0.1',progreso=.42)
            assert aviso.estado==estado and aviso._titulo.value
            assert aviso.bgcolor==p.tarjeta
    aviso.mostrar('descargando',progreso=.42)
    assert '42' in aviso._detalle.value and aviso._barra.value==.42


def test_fallo_no_muestra_datos_internos():
    import ui_estilo as ui
    from ui_actualizaciones import AvisoActualizacion
    aviso=AvisoActualizacion(ui.CLARO)
    aviso.mostrar('error',mensaje='clave-privada/ruta/cliente')
    assert 'clave-privada' not in aviso._detalle.value


def test_busqueda_no_bloquea_tarea_contable(monkeypatch):
    from main import App
    import ui_estilo as ui
    from ui_actualizaciones import AvisoActualizacion
    app=App.__new__(App)
    app.cerrando=False; app._update_lock=threading.Lock(); app._update_cancel=threading.Event()
    app._update_ruta=None
    app._turno=threading.Lock(); app._turno.acquire()
    app.aviso_actualizacion=AvisoActualizacion(ui.CLARO)
    trabajos=[]
    class Pagina:
        def run_thread(self,f): trabajos.append(f)
        def update(self): pass
    app.page=Pagina()
    publicacion = consultar(asset())
    monkeypatch.setattr(updater,'comprobar',lambda:publicacion)
    app.comprobar_actualizacion()
    assert len(trabajos)==1 and app._turno.locked()
    trabajos[0]()
    assert app.aviso_actualizacion.estado=='disponible' and app._turno.locked()
    assert not app._update_lock.locked()


def test_descarta_solo_archivo_propio_de_cache(tmp_path,monkeypatch):
    monkeypatch.setattr(updater.app_paths,'carpeta_actualizaciones',lambda:tmp_path)
    descarga=tmp_path/'CAM-prueba.exe'; descarga.write_bytes(b'ficticio')
    updater.descartar_descarga(descarga)
    assert not descarga.exists()
    db=tmp_path/'contabilidad.db'; db.write_bytes(b'conservar')
    with pytest.raises(ErrorActualizacion): updater.descartar_descarga(db)
    assert db.read_bytes()==b'conservar'


def test_cierre_de_ventana_fallido_reabre_controlador(monkeypatch,tmp_path):
    import main
    from main import App
    from types import SimpleNamespace
    import ui_estilo as ui
    from ui_actualizaciones import AvisoActualizacion
    app=App.__new__(App)
    app.cerrando=False; app._turno=threading.Lock(); app._update_lock=threading.Lock()
    app._update_cancel=threading.Event(); app._evento_dialogo=None
    app._update_ruta=tmp_path/'CAM-ficticio.exe'; app._update_publicacion=consultar(asset())
    app.aviso_actualizacion=AvisoActualizacion(ui.CLARO)
    eventos=[]
    class Ctrl:
        lector=SimpleNamespace(db_path=tmp_path/'ficticia.db')
        def cerrar(self): eventos.append('cerrado')
    class Auxiliar:
        def poll(self): return None
        def terminate(self): eventos.append('auxiliar_cancelado')
    class Ventana:
        async def destroy(self): raise OSError('fallo simulado de cierre')
    class Pagina:
        window=Ventana()
        def update(self): pass
    anterior=Ctrl(); nuevo=Ctrl(); app.ctrl=anterior; app.page=Pagina()
    monkeypatch.setattr(updater,'preparar_instalacion',lambda *a:Auxiliar())
    monkeypatch.setattr(main,'Controlador',lambda ruta:nuevo)
    asyncio.run(app.actualizar_aplicacion())
    assert eventos==['cerrado','auxiliar_cancelado']
    assert app.ctrl is nuevo and not app.cerrando
    assert not app._turno.locked() and not app._update_lock.locked()
