"""Benchmark reproducible, aislado y sin API: python auditar_rendimiento.py --clientes 600.

No recibe rutas de producción. Crea SQLite y carpetas ficticias en TemporaryDirectory.
Mide consultas; no atribuye estos tiempos al renderizado de Flet ni a la red.
"""
import argparse
import json
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from database import Database


def ejecutar(clientes=600):
    if type(clientes) is not int or not 100 <= clientes <= 6000 or clientes % 5:
        raise ValueError('Usa entre 100 y 6000 clientes, múltiplo de 5.')
    with tempfile.TemporaryDirectory(prefix='contabilidad-audit-') as temporal:
        raiz = Path(temporal)
        db = Database(raiz / 'ficticia.sqlite')
        db.carpeta_clientes = raiz / 'Clientes'
        tracemalloc.start()
        inicio = time.perf_counter()
        try:
            with db.transaccion():
                for n in range(clientes):
                    ruso = n < clientes // 5
                    c = db.crear_cliente('Михаил' if ruso else 'Cliente ficticio', f'Auditoria {n:04}', f'AUDIT-{n:06}')
                    cid = c['id']
                    db.upsert('residencias', cid, {'codigo': '2026', 'ejercicio': 2026, 'pais': 'Rusia' if ruso else 'Francia'})
                    for inm in range(4):
                        db.upsert('inmuebles', cid, {'codigo': f'INM-{inm+1:03}', 'ref_catastral': f'FICTICIA-{n:06}-{inm}', 'direccion': 'Dirección ficticia'})
                    for factura in range(200):
                        db.upsert('gastos', cid, {'codigo': f'GAS-{factura+1:03}', 'ejercicio': 2026, 'inmueble': 'INM-001',
                                                 'fecha': '2026-03-10', 'categoria': 'Luz', 'importe': 61.20})
            carga = time.perf_counter() - inicio
            mediciones = []
            for n in range(120):
                inicio = time.perf_counter()
                pagina = db.listar_clientes(limite=50, desplazamiento=(n % (clientes // 50)) * 50)
                assert len(pagina) == 50
                gastos = db.listar_pagina('gastos', n % clientes + 1, limite=50, ejercicio=2026)
                assert len(gastos) == 50
                assert db.get_cliente_por_nif(f'AUDIT-{n % clientes:06}')
                mediciones.append((time.perf_counter() - inicio) * 1000)
            planes = {}
            for nombre, sql, valores in (
                ('nif', 'SELECT * FROM clientes WHERE nif=?', ('AUDIT-000001',)),
                ('cartera', 'SELECT * FROM clientes WHERE cartera=? ORDER BY apellidos,nombre,id LIMIT 50', ('NR',)),
                ('tipo', 'SELECT * FROM clientes WHERE tipo_cliente=?', ('No residente',)),
                ('gastos', 'SELECT * FROM gastos WHERE cliente_id=? AND ejercicio=? ORDER BY codigo LIMIT 50', (1,2026)),
                ('referencia', 'SELECT * FROM inmuebles WHERE cliente_id=? AND ref_catastral=?', (1,'FICTICIA-000000-0'))):
                planes[nombre] = [str(r[3]) for r in db.con.execute('EXPLAIN QUERY PLAN '+sql, valores)]
                assert any('INDEX' in p.upper() for p in planes[nombre]), planes[nombre]
            _, pico = tracemalloc.get_traced_memory()
            return {'clientes': clientes, 'rusos': clientes // 5, 'inmuebles': clientes * 4, 'gastos': clientes * 200,
                    'carga_segundos': round(carga, 3), 'consulta_grupo_mediana_ms': round(statistics.median(mediciones), 3),
                    'consulta_grupo_p95_ms': round(sorted(mediciones)[int(len(mediciones)*.95)-1], 3),
                    'pico_python_mib': round(pico/1024**2, 3), 'planes': planes,
                    'limites': 'Datos sintéticos; memoria tracemalloc de Python, no RSS; consultas BD, sin UI, red ni Excel.'}
        finally:
            tracemalloc.stop()
            db.cerrar()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clientes', type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(ejecutar(args.clientes), ensure_ascii=False, indent=2))
