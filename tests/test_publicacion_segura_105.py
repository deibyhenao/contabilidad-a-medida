"""La exportación no arrastra archivos privados sin seguimiento y conserva binarios/BOM."""
from pathlib import Path
import importlib.util
import subprocess
import sys
import pytest


spec = importlib.util.spec_from_file_location("preparar_publicacion_105", Path(__file__).parents[1] / "scripts/preparar_publicacion.py")
publicacion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publicacion)


def test_exportacion_no_incluye_python_privado_sin_seguir_ni_secretos_packaging(tmp_path, monkeypatch):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    (tmp_path/'app.py').write_text('print("Programa")',encoding='utf-8')
    (tmp_path/'analisis_privado.py').write_text('DATOS_FICTICIOS_PRIVADOS = True',encoding='utf-8')
    (tmp_path/'packaging').mkdir()
    (tmp_path/'packaging/firma.pfx').write_bytes(b'certificado ficticio')
    (tmp_path/'packaging/instalador.iss').write_text('[Setup]',encoding='utf-8')
    subprocess.run(['git','add','--','app.py'],cwd=tmp_path,check=True)
    monkeypatch.setattr(publicacion,'ROOT',tmp_path)
    archivos=publicacion.archivos_publicables()
    assert tmp_path/'app.py' in archivos and tmp_path/'packaging/instalador.iss' in archivos
    assert tmp_path/'analisis_privado.py' not in archivos and tmp_path/'packaging/firma.pfx' not in archivos


def test_normaliza_finales_sin_quitar_bom_de_powershell(tmp_path):
    src=tmp_path/'fuente.ps1';dst=tmp_path/'exportado/fuente.ps1'
    src.write_bytes(b'\xef\xbb\xbf# UTF-8\r\nWrite-Output "Prueba"\n')
    publicacion.copiar_publicable(src,dst)
    assert dst.read_bytes()==b'\xef\xbb\xbf# UTF-8\nWrite-Output "Prueba"\n'


def test_no_normaliza_plantillas_binarias(tmp_path):
    src=tmp_path/'plantilla.xlsx';dst=tmp_path/'exportado/plantilla.xlsx'
    datos=b'PK\x03\x04\r\n\x00\xffdatos binarios'
    src.write_bytes(datos)
    publicacion.copiar_publicable(src,dst)
    assert dst.read_bytes()==datos


def test_raiz_redirigida_se_rechaza_antes_de_operar_con_git(tmp_path, monkeypatch):
    real=tmp_path/'real';real.mkdir()
    enlace=tmp_path/'enlace'
    try:
        enlace.symlink_to(real,target_is_directory=True)
    except OSError:
        pytest.skip('El sistema no permite crear enlaces sin privilegios')
    monkeypatch.setattr(sys,'argv',['preparar_publicacion','--destino',str(enlace)])
    monkeypatch.setattr(publicacion,'ejecutar',lambda *args:pytest.fail('No debe clonar ni modificar Git'))
    with pytest.raises(SystemExit) as salida:
        publicacion.main()
    assert salida.value.code==2
