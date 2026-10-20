"""El diagnóstico automático falla sin dejar un diálogo esperando en CI."""
import json
from pathlib import Path
import subprocess
import sys


def test_diagnostico_sin_empaquetar_devuelve_error_json_y_termina(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / 'launcher.py'
    salida = tmp_path / 'diagnostico.json'
    proceso = subprocess.run(
        [sys.executable, str(launcher), '--diagnostico-paquete', str(salida)],
        cwd=tmp_path, timeout=45, capture_output=True)
    assert proceso.returncode == 1
    resultado = json.loads(salida.read_text(encoding='utf-8'))
    assert resultado['ok'] is False
    assert 'AssertionError' in resultado['error']
    assert 'Traceback' in resultado['traceback']
