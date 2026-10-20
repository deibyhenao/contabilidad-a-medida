# Ejecución: python -m PyInstaller packaging/ContabilidadAMedida.spec --noconfirm
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata
import flet_desktop

ROOT = Path(SPECPATH).parent
datas = [(str(ROOT/'assets'), 'assets'), (str(ROOT/'plantillas'), 'plantillas'),
         (str(ROOT/'packaging/aplicar_actualizacion.ps1'), 'packaging')]
# Incluye el cliente oficial completo; el usuario final no tiene que descargarlo.
desktop = Path(flet_desktop.ensure_client_cached())
datas.append((str(desktop/'flet'), 'desktop/flet'))
binaries, hidden = [], []
for package in ['flet', 'flet_desktop', 'keyring']:
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h
for package in ['flet', 'flet-desktop', 'anthropic', 'keyring', 'openpyxl']:
    datas += copy_metadata(package)

a = Analysis([str(ROOT/'launcher.py')], pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=hidden + ['main', 'keyring.backends.Windows'],
             excludes=['pytest', '_pytest', 'tkinter', 'IPython', 'notebook'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ContabilidadAMedida',
          debug=False, strip=False, upx=False, console=False, icon=str(ROOT/'assets/icono.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ContabilidadAMedida')
