"""Ningún test llama a la API real de Claude: la clave se oculta salvo que un test la simule."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def raices_por_defecto_aisladas(tmp_path, monkeypatch):
    """La migración de un controlador de prueba no crea raíces en el proyecto real."""
    import database

    monkeypatch.setattr(database, "PROYECTO_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH_DEFECTO", tmp_path / "predeterminada_ficticia.sqlite")


@pytest.fixture(autouse=True)
def sin_api_real(monkeypatch):
    import ia_extractor

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ia_extractor, "obtener_clave", lambda: None)

    def bloquear_cliente():
        pytest.fail("La API real está bloqueada: simula ia_extractor._cliente en este test.")

    monkeypatch.setattr(ia_extractor, "_cliente", bloquear_cliente)
