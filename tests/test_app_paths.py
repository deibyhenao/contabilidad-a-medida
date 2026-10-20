import sys
from pathlib import Path
import pytest
import app_paths


def test_fuentes_conservan_raiz(monkeypatch):
    monkeypatch.setattr(sys,'frozen',False,raising=False)
    assert app_paths.carpeta_datos()==app_paths.RECURSOS


def test_instalada_lee_directorio_elegido_cirilico(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    datos=tmp_path/'Михаил_Иванов datos'; datos.mkdir()
    config=app_paths.configuracion_usuario(); config.parent.mkdir(parents=True)
    config.write_text('[Datos]\nCarpeta='+str(datos)+'\n',encoding='utf-16')
    assert app_paths.carpeta_datos()==datos
    (datos/'contabilidad.db').write_bytes(b'ficticia')
    assert app_paths.carpeta_datos()==datos
    assert (datos/'contabilidad.db').read_bytes()==b'ficticia'


def test_no_fallback_si_carpeta_desconectada(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    config=app_paths.configuracion_usuario(); config.parent.mkdir(parents=True)
    config.write_text('[Datos]\nCarpeta='+str(tmp_path/'no_existe')+'\n',encoding='utf-8')
    with pytest.raises(RuntimeError,match='no se ha creado otra base'):
        app_paths.carpeta_datos()
    assert not (tmp_path/'no_existe').exists()


def test_sin_config_no_inventa_almacen(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    with pytest.raises(RuntimeError): app_paths.carpeta_datos()
    assert not list(tmp_path.iterdir())
