"""Integridad PE y copias preventivas sin acceso a datos de producción."""
import hashlib
import io
import sqlite3
import threading

import pytest

import updater


def publicacion(payload):
    return updater.Publicacion('1.0.2', 'https://github.com/x', hashlib.sha256(payload).hexdigest(), len(payload), 'prueba')


def pe():
    return b'MZ' + b'\0'*58 + (64).to_bytes(4, 'little') + b'PE\0\0' + b'prueba'


@pytest.mark.parametrize('payload', [b'HTML con hash correcto', b'MZ', b'MZ'+b'\0'*100,
    b'MZ'+b'\0'*58+(2**31).to_bytes(4,'little')+b'PE\0\0'])
def test_hash_correcto_no_admite_archivo_ajeno_a_pe(tmp_path, payload):
    with pytest.raises(updater.ErrorActualizacion):
        updater.descargar(publicacion(payload), lambda _:None, threading.Event(),
                          carpeta=tmp_path, abrir=lambda _:io.BytesIO(payload))
    assert not list(tmp_path.iterdir())


def test_snapshot_antes_de_abrir_red_conserva_datos(tmp_path):
    bd = tmp_path/'datos.db'
    with sqlite3.connect(bd) as con:
        con.execute('CREATE TABLE clientes (nombre TEXT)')
        con.execute('INSERT INTO clientes VALUES (?)', ('Михаил',))
    def abrir(_):
        copias = list((tmp_path/'Backups_Seguridad').glob('*.db'))
        assert len(copias) == 1
        with sqlite3.connect(copias[0]) as con:
            assert con.execute('SELECT nombre FROM clientes').fetchall() == [('Михаил',)]
        return io.BytesIO(pe())
    ruta = updater.descargar(publicacion(pe()), lambda _:None, threading.Event(),
                            carpeta=tmp_path/'cache', abrir=abrir, db_path=bd)
    assert ruta.read_bytes() == pe()
    with sqlite3.connect(bd) as con:
        assert con.execute('SELECT nombre FROM clientes').fetchall() == [('Михаил',)]


def test_sin_backup_no_abre_red_ni_crea_base(tmp_path):
    def abrir(_):
        pytest.fail('No debe abrir red si falla la copia')
    with pytest.raises(updater.ErrorActualizacion, match='copia de seguridad'):
        updater.descargar(publicacion(pe()), lambda _:None, threading.Event(),
                          carpeta=tmp_path/'cache', abrir=abrir, db_path=tmp_path/'ausente.db')
    assert not list(tmp_path.iterdir())
