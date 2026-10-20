"""Gestión documental real con SQLite y PDFs ficticios; ninguna llamada a APIs."""
from pathlib import Path

import pytest

from database import Database
import documentos as dg
import fixtures_pdf as f
import pdf_processor as pp


@pytest.fixture
def entorno(tmp_path):
    db = Database(tmp_path / "ficticia.sqlite")
    db.carpeta_clientes = tmp_path / "Clientes"
    cliente = db.crear_cliente("Cliente", "Ficticio", "FICTICIO-DOC")
    yield db, cliente["id"], tmp_path
    db.cerrar()


def certificado(entorno, pais="Francia", ejercicio=2025, nombre="cert.pdf"):
    db, cid, tmp = entorno
    ruta = f.certificado_residencia(tmp / nombre, pais=pais, ejercicio=ejercicio)
    return pp.aplicar(db, cid, pp.extraer_local(ruta), ruta)


def test_retirar_y_restaurar_certificado_datos_y_archivo(entorno):
    db, cid, _ = entorno
    resultado = certificado(entorno)
    codigo = resultado.documento
    doc = db.get("documentos", cid, codigo)
    ruta = Path(db.get_cliente(cid)["carpeta"]) / doc["ruta"]
    huella = ruta.read_bytes()
    assert db.residencia(cid, 2025)["certificado"] == "Sí"
    assert dg.previsualizar(db, cid, codigo, "retirar")["confirmable"]
    assert 2025 in dg.retirar(db, cid, codigo)
    assert not ruta.exists()
    assert not db.residencia(cid, 2025)
    assert dg.listar(db, cid) == []
    assert len(dg.listar(db, cid, papelera=True)) == 1
    assert doc["ruta"] in dg.rutas_retiradas(db, cid)
    assert dg.revision(db, cid) == 1
    dg.restaurar(db, cid, codigo)
    assert ruta.read_bytes() == huella
    assert db.residencia(cid, 2025)["pais"] == "Francia"
    assert db.residencia(cid, 2025)["certificado"] == "Sí"
    assert dg.revision(db, cid) == 2


def test_certificado_cambia_residencia_real_no_solo_ficha(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    assert dg.previsualizar(db, cid, codigo, "cambiar_ejercicio", 2026)["confirmable"]
    assert {2025, 2026} <= dg.cambiar_ejercicio(db, cid, codigo, 2026)
    assert not db.residencia(cid, 2025)
    assert db.residencia(cid, 2026)["pais"] == "Francia"
    assert db.get("documentos", cid, codigo)["ejercicio"] == 2026
    dg.retirar(db, cid, codigo)
    dg.restaurar(db, cid, codigo)
    assert not db.residencia(cid, 2025)
    assert db.residencia(cid, 2026)["certificado"] == "Sí"


def test_no_pisa_fuente_nueva_y_resuelve_cadena_retirada(entorno):
    db, cid, _ = entorno
    a = certificado(entorno).documento
    b = certificado(entorno, pais="Rusia", nombre="otro.pdf").documento
    assert db.residencia(cid, 2025)["pais"] == "Rusia"
    dg.retirar(db, cid, a)
    assert db.residencia(cid, 2025)["pais"] == "Rusia"
    dg.retirar(db, cid, b)
    assert not db.residencia(cid, 2025)
    dg.restaurar(db, cid, a)
    assert db.residencia(cid, 2025)["pais"] == "Francia"
    dg.restaurar(db, cid, b)
    assert db.residencia(cid, 2025)["pais"] == "Rusia"


def test_correccion_manual_posterior_se_conserva(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    db.guardar_residencia(cid, 2025, pais="Alemania", grupo="UE/EEE")
    dg.retirar(db, cid, codigo)
    assert db.residencia(cid, 2025)["pais"] == "Alemania"
    assert db.residencia(cid, 2025)["certificado"] is None
    dg.restaurar(db, cid, codigo)
    assert db.residencia(cid, 2025)["pais"] == "Alemania"


def test_legado_sin_historial_invalidacion_y_advertencia(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    db.con.execute("DELETE FROM documentos_cambios")
    db.con.execute("DELETE FROM documentos_gestion")
    db.con.commit()
    vista = dg.previsualizar(db, cid, codigo, "retirar")
    assert vista["confirmable"] and any("sin historial" in a for a in vista["advertencias"])
    dg.retirar(db, cid, codigo)
    r = db.residencia(cid, 2025)
    assert r and r["certificado"] is None and r["pais"] is None and r["ruta"] is None
    dg.restaurar(db, cid, codigo)
    assert db.residencia(cid, 2025)["certificado"] == "Sí"


def test_legado_certificado_cambia_datos_reales(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    db.con.execute("DELETE FROM documentos_cambios")
    db.con.execute("DELETE FROM documentos_gestion")
    db.con.commit()
    dg.cambiar_ejercicio(db, cid, codigo, 2026)
    assert db.residencia(cid, 2025)["pais"] is None
    assert db.residencia(cid, 2026)["pais"] == "Francia"


def test_presentado_impide_cambio_retirada_y_restauracion(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    db.upsert("patrimonio_714", cid, {"ejercicio": 2025, "estado": "Presentado"})
    for accion in ("retirar", "cambiar_ejercicio"):
        assert not dg.previsualizar(db, cid, codigo, accion, 2026)["confirmable"]
    with pytest.raises(ValueError, match="presentadas"):
        dg.retirar(db, cid, codigo)
    assert db.residencia(cid, 2025)["certificado"] == "Sí"


def test_escritura_antigua_registrada_despues_protege_historico_presentado(entorno):
    db, cid, tmp = entorno
    ruta = f.escritura_compra(tmp/'compra_antigua.pdf', fecha='2010-05-10')
    res = pp.aplicar(db, cid, pp.extraer_local(ruta), ruta, ejercicio=2026)
    assert db.get('documentos', cid, res.documento)['ejercicio'] == 2026
    db.upsert('patrimonio_714', cid, {'ejercicio':2024, 'estado':'Presentado'})
    assert not dg.previsualizar(db, cid, res.documento, 'retirar')['confirmable']
    with pytest.raises(ValueError, match='presentadas'):
        dg.retirar(db, cid, res.documento)


def test_no_sobrescribe_destino_al_restaurar(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    ruta = Path(db.get_cliente(cid)["carpeta"]) / db.get("documentos", cid, codigo)["ruta"]
    dg.retirar(db, cid, codigo)
    ruta.write_bytes(b"otro documento")
    with pytest.raises(ValueError, match="diferente"):
        dg.restaurar(db, cid, codigo)
    assert ruta.read_bytes() == b"otro documento"
    assert dg.listar(db, cid, papelera=True)


def test_hash_retirado_no_se_reimporta_entorno_ni_copia(entorno):
    db, cid, tmp = entorno
    codigo = certificado(entorno).documento
    dg.retirar(db, cid, codigo)
    entrada = tmp / "cert.pdf"
    with pytest.raises(ValueError, match="papelera"):
        pp.aplicar(db, cid, pp.extraer_local(entrada), entrada)
    assert list((Path(db.get_cliente(cid)["carpeta"]) / "Soportes_PDF").glob("*.pdf")) == []


def test_escritura_ejercicio_no_altera_fecha_real_y_manual(entorno):
    db, cid, tmp = entorno
    ruta = f.escritura_compra(tmp / "escritura.pdf", precio=180000, fecha="2025-05-10")
    r = pp.aplicar(db, cid, pp.extraer_local(ruta), ruta)
    inmueble = r.codigos["inmueble"]
    db.upsert("inmuebles", cid, {"codigo": inmueble, "precio_escritura": 195000}, respetar_manuales=False)
    db.proteger_campos_inmueble(cid, inmueble, ["precio_escritura"])
    dg.cambiar_ejercicio(db, cid, r.documento, 2026)
    assert db.get("inmuebles", cid, inmueble)["fecha_adquisicion"] == "2025-05-10"
    dg.retirar(db, cid, r.documento)
    assert db.get("inmuebles", cid, inmueble)["precio_escritura"] == 195000
    dg.restaurar(db, cid, r.documento)
    assert db.get("inmuebles", cid, inmueble)["precio_escritura"] == 195000


def test_fallo_db_despues_copia_preserva_original_y_datos(entorno, monkeypatch):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    ruta = Path(db.get_cliente(cid)["carpeta"]) / db.get("documentos", cid, codigo)["ruta"]
    def fallar(*args):
        raise RuntimeError("fallo simulado")
    monkeypatch.setattr(dg, "_activar_datos", fallar)
    with pytest.raises(RuntimeError, match="simulado"):
        dg.retirar(db, cid, codigo)
    assert ruta.is_file()
    assert db.residencia(cid, 2025)["certificado"] == "Sí"
    assert dg.listar(db, cid)
    assert not dg.listar(db, cid, papelera=True)


def test_movimiento_diferido_hasta_commit_y_recuperacion(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    ruta = Path(db.get_cliente(cid)["carpeta"]) / db.get("documentos", cid, codigo)["ruta"]
    with db.transaccion():
        dg.retirar(db, cid, codigo)
        assert ruta.is_file()  # No retirar un original antes de confirmar la transacción externa.
        assert db.con.execute("SELECT 1 FROM documentos_archivos").fetchone()
    dg.recuperar_archivos(db, cid)
    assert not ruta.exists()
    assert not db.con.execute("SELECT 1 FROM documentos_archivos").fetchone()


def test_aislamiento_y_escape_ruta(entorno):
    db, cid, _ = entorno
    codigo = certificado(entorno).documento
    otro = db.crear_cliente("Otro", "Ficticio", "OTRO-DOC")["id"]
    with pytest.raises(ValueError, match="inexistente"):
        dg.retirar(db, otro, codigo)
    db.upsert("documentos", cid, {"codigo": codigo, "ruta": "../externo.pdf"})
    assert not dg.previsualizar(db, cid, codigo, "retirar")["confirmable"]


def test_trasladar_fuente_antigua_no_resucita_pais_al_retirar_nueva(entorno):
    db, cid, _ = entorno
    a = certificado(entorno).documento
    b = certificado(entorno, pais="Rusia", nombre="rusia.pdf").documento
    dg.cambiar_ejercicio(db, cid, a, 2026)
    assert db.residencia(cid, 2025)["pais"] == "Rusia"
    dg.retirar(db, cid, b)
    assert not db.residencia(cid, 2025)
    assert db.residencia(cid, 2026)["pais"] == "Francia"


def test_dos_soportes_mismo_pais_mantienen_justificacion_hasta_retirar_ambos(entorno):
    db, cid, _ = entorno
    a = certificado(entorno).documento
    b = certificado(entorno, nombre="igual.pdf").documento
    assert a != b
    dg.retirar(db, cid, a)
    assert db.residencia(cid, 2025)["pais"] == "Francia"
    assert db.residencia(cid, 2025)["certificado"] == "Sí"
    dg.retirar(db, cid, b)
    assert not db.residencia(cid, 2025)


def test_previsualizacion_no_anuncia_borrado_de_manual_ni_fuente_nueva(entorno):
    db, cid, _ = entorno
    a = certificado(entorno).documento
    certificado(entorno, pais="Rusia", nombre="posterior.pdf")
    vista = dg.previsualizar(db, cid, a, "retirar")
    assert not any(c["campo"] == "pais" for c in vista["cambios"])
    assert any("conservan" in a for a in vista["advertencias"])
    assert db.residencia(cid, 2025)["pais"] == "Rusia"


def test_legado_no_pierde_montos_manuales_distintos_del_pdf(entorno):
    db, cid, tmp = entorno
    ruta = f._crear(tmp / "gasto.pdf", "FACTURA DE GASTO\nFecha factura: 2025-02-10\nBase imponible: 100,00 euros\nIVA: 21,00 euros\nTotal: 121,00 euros\n")
    relativo, huella = pp.archivar_pdf(ruta, Path(db.get_cliente(cid)["carpeta"]), pp.TipoDocumento.FACTURA_GASTO, None)
    codigo, _ = db.registrar_documento(cid, relativo, "Factura de gasto", huella, ruta.name, ejercicio=2025)
    factura = db.upsert("facturas_recibidas", cid, {"fecha": "2025-02-10", "base": 100, "cuota_iva": 21,
        "origen": "pdf", "ruta": relativo})
    db.upsert("facturas_recibidas", cid, {"codigo": factura, "base": 77})
    dg.retirar(db, cid, codigo)
    assert db.get("facturas_recibidas", cid, factura)["base"] == 77


def test_factura_legada_no_residente_traslada_misma_fila_y_su_inmueble(entorno):
    db, cid, tmp = entorno
    db.upsert('inmuebles', cid, {'codigo':'INM-001', 'direccion':'Primero'})
    db.upsert('inmuebles', cid, {'codigo':'INM-002', 'direccion':'Segundo'})
    ruta = f._crear(tmp/'gasto_legado.pdf', 'FACTURA DE GASTO\nNúmero de factura: LEG-1\nFecha factura: 10/02/2025\nBase imponible: 100,00 euros\nIVA: 21,00 euros\nTotal: 121,00 euros')
    ext = pp.extraer_local(ruta)
    res = pp.aplicar(db, cid, ext, ruta, inmueble_destino='INM-002')
    codigo = res.codigos['gasto']
    db.upsert('gastos', cid, {'codigo':codigo, 'total':77, 'fecha':'2025-04-10'})
    db.vaciar_campo('documentos', cid, res.documento, 'inmueble')
    db.con.execute('DELETE FROM documentos_cambios')
    db.con.execute('DELETE FROM documentos_gestion')
    db.con.commit()
    dg.cambiar_ejercicio(db, cid, res.documento, 2026)
    filas = db.listar('gastos', cid)
    assert len(filas) == 1 and filas[0]['codigo'] == codigo
    assert filas[0]['inmueble'] == 'INM-002' and filas[0]['total'] == 77
    assert filas[0]['ejercicio'] == 2026 and filas[0]['fecha'] == '2026-04-10'
