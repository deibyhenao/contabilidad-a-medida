"""Cotitular de un inmueble (p. ej. el cónyuge al 50 %): alta copiando la información, sin datos ni API reales."""
import pytest

from controlador import Controlador


@pytest.fixture
def ctrl(tmp_path):
    c = Controlador(tmp_path / "t.db")
    c.cambiar_carpeta_clientes(tmp_path / "C")
    yield c
    c.cerrar()


def _marido(ctrl, pct=0.5):
    db = ctrl.db
    cid = ctrl.crear_cliente("Iván", "Prueba", "X0000001T", pais="Rusia", ejercicio=2025)["id"]
    inm = db.upsert("inmuebles", cid, {"direccion": "Calle Ficticia 1", "ref_catastral": "0000000AA0000A0001AA",
                                       "fecha_adquisicion": "2020-01-01", "pct_titularidad": pct,
                                       "precio_escritura": 300000})
    db.upsert("catastro", cid, {"ejercicio": 2025, "inmueble": inm, "valor_catastral": 100000,
                                "valor_construccion": 40000, "anio_revision": 2019})
    ctr = db.upsert("contratos", cid, {"inmueble": inm, "fecha_inicio": "2025-01-01", "estado": "Vigente",
                                       "tipo": "Temporada", "renta_inicial": 1000, "periodicidad": "Mensual",
                                       "fecha_fin_prevista": "2025-06-30"})
    db.upsert("inquilinos", cid, {"contrato": ctr, "inmueble": inm, "nombre": "Inquilino Ficticio"})
    db.upsert("rentas", cid, {"contrato": ctr, "inmueble": inm, "desde": "2025-04-01", "renta": 1100,
                              "periodicidad": "Mensual"})
    db.upsert("gastos", cid, {"ejercicio": 2025, "inmueble": inm, "fecha": "2025-03-01", "categoria": "IBI",
                              "total": 400, "importe_deducible": 200})
    return cid, inm


def test_crea_la_esposa_con_el_inmueble_y_sus_datos(ctrl):
    cid, inm = _marido(ctrl)
    r = ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "apellidos": "Prueba", "nif": "x0000002t", "pais": "Rusia"}, "50", 2025)
    esposa = r["cliente"]
    assert r["nuevo"] and esposa["nif"] == "X0000002T" and esposa["tipo_cliente"] == "No residente"
    db, did = ctrl.db, esposa["id"]
    inm2 = db.get("inmuebles", did, r["inmueble"])
    assert inm2["ref_catastral"] == "0000000AA0000A0001AA" and inm2["pct_titularidad"] == 0.5
    assert inm2["precio_escritura"] == 300000 and "Cotitular de Iván" in inm2["observaciones"]
    assert db.residencia(did, 2025)["pais"] == "Rusia"                     # la indicada en el alta
    ctr2 = db.listar("contratos", did)[0]
    assert ctr2["inmueble"] == r["inmueble"] and ctr2["renta_inicial"] == 1000 and not ctr2.get("ruta")
    assert db.listar("rentas", did)[0]["contrato"] == ctr2["codigo"]
    assert db.listar("inquilinos", did)[0]["contrato"] == ctr2["codigo"]
    gasto = db.listar("gastos", did)[0]
    assert gasto["importe_deducible"] is None and "no copiado" in gasto["observaciones"]
    assert db.listar("m210_alquiler", did, ejercicio=2025)                 # recalculado, no copiado
    assert [c["nif"] for c in ctrl.cotitulares(cid, inm)] == ["X0000002T"]
    assert [c["nif"] for c in ctrl.cotitulares(did, r["inmueble"])] == ["X0000001T"]


def test_corrige_el_100_por_cien_del_primer_titular(ctrl):
    cid, inm = _marido(ctrl, pct=1)
    with pytest.raises(ValueError, match="supera el 100"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T", "pais": "Rusia"}, 0.5, 2025)
    assert ctrl.db.get_cliente_por_nif("X0000002T") is None                # nada a medias
    ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T", "pais": "Rusia"}, 0.5, 2025, pct_cliente=0.5)
    assert ctrl.db.get("inmuebles", cid, inm)["pct_titularidad"] == 0.5


def test_cotitular_existente_y_errores(ctrl):
    cid, inm = _marido(ctrl)
    otra = ctrl.crear_cliente("Olga", "Prueba", "X0000002T", pais="Rusia", ejercicio=2025)["id"]
    r = ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T"}, 0.5, 2025)
    assert not r["nuevo"] and r["cliente"]["id"] == otra
    with pytest.raises(ValueError, match="ya tiene este inmueble|supera el 100"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T"}, 0.5, 2025)
    with pytest.raises(ValueError, match="mismo cliente"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Iván", "nif": "X0000001T"}, 0.5, 2025)
    with pytest.raises(ValueError, match="nombre y el NIF"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "", "nif": ""}, 0.5, 2025)
    with pytest.raises(ValueError, match="mayor que 0"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Ana", "nif": "X0000003T", "pais": "Rusia"}, 0, 2025)
    with pytest.raises(ValueError, match="supera el 100"):                 # tercer titular sin hueco
        ctrl.crear_cotitular(cid, inm, {"nombre": "Ana", "nif": "X0000003T", "pais": "Rusia"}, 0.1, 2025)


def test_exige_referencia_pais_y_respeta_gananciales(ctrl):
    cid, inm = _marido(ctrl)
    with pytest.raises(ValueError, match="país de residencia"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T"}, 0.5, 2025)
    ctrl.db.upsert("inmuebles", cid, {"codigo": inm, "gananciales_forzado": 1})
    with pytest.raises(ValueError, match="ganancial"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T", "pais": "Rusia"}, 0.4, 2025)
    ctrl.db.vaciar_campo("inmuebles", cid, inm, "ref_catastral")
    with pytest.raises(ValueError, match="referencia catastral"):
        ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T", "pais": "Rusia"}, 0.5, 2025)
    assert ctrl.db.get_cliente_por_nif("X0000002T") is None


def test_copia_los_pdf_de_escritura_y_contrato(ctrl):
    import hashlib
    from pathlib import Path
    cid, inm = _marido(ctrl)
    db = ctrl.db
    carpeta = Path(db.get_cliente(cid)["carpeta"]) / "Soportes_PDF"
    carpeta.mkdir(parents=True, exist_ok=True)
    pdf = carpeta / "escritura.pdf"
    pdf.write_bytes(b"%PDF-1.4 escritura ficticia")
    db.registrar_documento(cid, "Soportes_PDF/escritura.pdf", "Escritura de compra",
                           hashlib.sha256(pdf.read_bytes()).hexdigest(), "escritura.pdf", inmueble=inm)
    r = ctrl.crear_cotitular(cid, inm, {"nombre": "Olga", "nif": "X0000002T", "pais": "Rusia"}, 0.5, 2025)
    did = r["cliente"]["id"]
    assert r["copiados"]["documentos"] == 1
    assert (Path(db.get_cliente(did)["carpeta"]) / "Soportes_PDF" / "escritura.pdf").is_file()
    docs = [d for d in db.listar("documentos", did) if d.get("tipo") == "Escritura de compra"]
    assert docs and all(d.get("estado") != "Falta" for d in docs)          # sin falta grave de escritura
