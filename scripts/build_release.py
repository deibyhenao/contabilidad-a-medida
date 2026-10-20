"""Construye solo código/recursos permitidos; nunca empaqueta datos del proyecto."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from version import VERSION


def ejecutar(args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iscc', default=shutil.which('ISCC.exe'))
    parser.add_argument('--solo-ejecutable', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('El paquete Windows se construye en Windows x64.')
    if not args.solo_ejecutable and not args.iscc:
        parser.error('Instala Inno Setup 6 e indica --iscc RUTA/ISCC.exe.')
    # El spec enumera únicamente los dos árboles de recursos y el auxiliar.
    ejecutar([sys.executable, '-m', 'PyInstaller', ROOT/'packaging/ContabilidadAMedida.spec', '--noconfirm'])
    dist = ROOT/'dist/ContabilidadAMedida'
    prohibidos = []
    for p in dist.rglob('*'):
        if p.name.startswith('Clientes') or p.suffix.lower() in {'.db','.sqlite','.pdf','.pfx','.p12'} or p.name.startswith('.env'):
            prohibidos.append(str(p.relative_to(dist)))
    if prohibidos:
        raise RuntimeError('Contenido ajeno al programa en el paquete: '+repr(prohibidos))
    salida = ROOT/'build/diagnostico-paquete.json'
    salida.unlink(missing_ok=True)
    try:
        diagnostico = subprocess.run(
            [str(dist/'ContabilidadAMedida.exe'), '--diagnostico-paquete', str(salida)],
            cwd=ROOT, timeout=180, check=False)
    except subprocess.TimeoutExpired as exc:
        detalle = salida.read_text(encoding='utf-8') if salida.exists() else 'No se generó el informe JSON.'
        raise RuntimeError('El diagnóstico no terminó en 180 segundos: '+detalle) from exc
    detalle = salida.read_text(encoding='utf-8') if salida.exists() else 'No se generó el informe JSON.'
    print('Diagnóstico del ejecutable: '+detalle, flush=True)
    if diagnostico.returncode != 0 or not salida.exists() or not json.loads(detalle).get('ok'):
        raise RuntimeError('El diagnóstico del ejecutable falló: '+detalle)
    if args.solo_ejecutable:
        return
    ejecutar([args.iscc, '/DAppVersion='+VERSION, '/DProjectRoot='+str(ROOT), ROOT/'packaging/instalador.iss'])
    instalador = ROOT/f'release/ContabilidadAMedida-{VERSION}-windows-x64-Setup.exe'
    h=hashlib.sha256()
    with instalador.open('rb') as f:
        for bloque in iter(lambda:f.read(1024*1024),b''): h.update(bloque)
    (ROOT/'release/SHA256SUMS.txt').write_text(h.hexdigest()+'  '+instalador.name+'\n',encoding='utf-8')
    print(json.dumps({'instalador':str(instalador),'sha256':h.hexdigest(),'bytes':instalador.stat().st_size}))


if __name__ == '__main__':
    main()
