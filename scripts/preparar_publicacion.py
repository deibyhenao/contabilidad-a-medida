"""Exporta código permitido a un clon limpio; jamás publica el historial privado."""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/deibyhenao/contabilidad-a-medida.git'


def ejecutar(args, cwd):
    return subprocess.run(args,cwd=cwd,check=True,text=True,capture_output=True).stdout


def archivos_publicables():
    rutas=[p for p in ROOT.glob('*.py') if p.is_file()]
    rutas += [ROOT/n for n in ['requirements.txt','requirements-build.txt','GUIA_ACTUALIZACIONES.md','README.md','.gitignore']]
    for patron in ['assets/icono.ico','assets/icono.png','plantillas/*.xlsx','packaging/*','scripts/build_release.py','scripts/preparar_publicacion.py','.github/workflows/*.yml']:
        rutas += [p for p in ROOT.glob(patron) if p.is_file()]
    # Solo pruebas seguidas por Git, además de las nuevas incluidas en esta entrega.
    tracked=ejecutar(['git','ls-files','--','tests/*.py'],ROOT).splitlines()
    rutas += [ROOT/p for p in tracked]
    return sorted(set(rutas))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destino',type=Path,required=True,help='Clon limpio separado del proyecto')
    args=parser.parse_args()
    destino=args.destino.resolve()
    if destino==ROOT or ROOT in destino.parents:
        parser.error('El clon público debe estar fuera del proyecto original.')
    if not destino.exists():
        ejecutar(['git','clone',REPO,str(destino)],ROOT)
    if ejecutar(['git','remote','get-url','origin'],destino).strip()!=REPO:
        parser.error('El destino no corresponde al repositorio oficial.')
    if ejecutar(['git','status','--porcelain'],destino).strip():
        parser.error('El clon contiene cambios pendientes; revísalos antes de exportar.')
    ejecutar(['git','pull','--ff-only'],destino)
    rutas=archivos_publicables()
    for src in rutas:
        relative=src.relative_to(ROOT)
        if src.is_symlink() or src.resolve()!=src.absolute():
            parser.error('No se publican archivos redirigidos: '+str(relative))
        dst=destino/relative
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dst)
    print('Código exportado sin bases, expedientes ni historial:',destino)
    print('Revisa git diff y git status en ese clon. Commit/push/tag se hacen desde el clon público.')
    print('Si se eliminó un módulo, revisa también su eliminación en el clon: no se borra automáticamente.')


if __name__=='__main__': main()
