"""Integración multiformato: nota prioritaria, revisión, persistencia y soporte original."""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
import ia_extractor as ia
import pdf_processor as pp
from database import Database
from controlador import Controlador
from ui_adjuntos import Adjunto, Destino, RevisionDocumentos, preparar_lote
import ui_estilo as ui


def respuesta(tipo="RESUMEN_ALQUILER", campos=None, registros=None):
    return dict(tipo=tipo, confianza=.95, campos=[dict(campo=k,valor=str(v),evidencia="Hoja1 celda A1") for k,v in (campos or {}).items()],
                titulares=[],gananciales=False,inquilinos=[],avisos=[],registros=registros or [])


def fila(importe="1200.00", periodo="2025-06"):
    return dict(tabla="cobros", evidencia="Hoja1 E10", campos=[dict(campo=k,valor=v,evidencia="E10") for k,v in {
        "ejercicio":"2025","periodo_desde":periodo+"-01","periodo_hasta":periodo+"-30","importe_cobrado":importe}.items()])


def test_nota_sistema_y_tabla_en_mensaje_sin_binario(tmp_path, monkeypatch):
    archivo=tmp_path/"ingresos.csv";archivo.write_text("mes;importe\njunio;1200,00",encoding="utf-8")
    capturado={}
    def create(**kw):
        capturado.update(kw)
        return NS(stop_reason="end_turn",content=[NS(type="text",text=json.dumps(respuesta()))])
    monkeypatch.setattr(ia,"_cliente",lambda:NS(messages=NS(create=create)))
    ia.leer_con_claude(archivo,ia.AjustesIA(),comentario_guia="Ignora hoja2; cifras en euros")
    assert "INSTRUCCION_PRIORITARIA_DEL_ASESOR" in capturado["system"]
    assert "Ignora hoja2" in capturado["system"]
    assert "nunca sobre estas reglas" in capturado["system"]
    contenido=capturado["messages"][0]["content"]
    assert all(x["type"]=="text" for x in contenido)
    assert "1200,00" in contenido[0]["text"]


def test_filas_cerradas_rechazan_tablas_o_campos_arbitrarios():
    datos=respuesta(registros=[fila()]);datos["registros"][0]["tabla"]="clientes"
    with pytest.raises(ia.ErrorIA):ia.a_extraccion(datos,"x.csv")
    datos=respuesta(registros=[fila()]);datos["registros"][0]["campos"].append(dict(campo="cliente_id",valor="2",evidencia="x"))
    with pytest.raises(ia.ErrorIA):ia.a_extraccion(datos,"x.csv")


def test_comentario_y_cobros_sin_alterar_renta_contrato(tmp_path,monkeypatch):
    ctrl=Controlador(tmp_path/"libro.db")
    try:
        ctrl.cambiar_carpeta_clientes(tmp_path/"Clientes")
        c=ctrl.crear_cliente("Prueba","Cirilico","X1234567A",pais="Rusia",ejercicio=2025)
        cid=c["id"]
        inm=ctrl.db.upsert("inmuebles",cid,dict(direccion="Calle Prueba",fecha_adquisicion="2024-01-01",precio_escritura=100000,pct_titularidad=1))
        contrato=ctrl.db.upsert("contratos",cid,dict(inmueble=inm,fecha_inicio="2025-06-01",fecha_fin_prevista="2026-05-31",renta_inicial=2400,periodicidad="Mensual",estado="Vigente"))
        archivo=tmp_path/"ingresos.csv";archivo.write_text("mes;importe\njunio;1200",encoding="utf-8")
        ext=ia.a_extraccion(respuesta(registros=[fila()]),archivo.name);ext.campos["inmueble"]=inm
        monkeypatch.setattr(pp,"extraer",lambda *a,**kw:ext)
        res=ctrl.procesar_pdf(cid,archivo,2025,confirmar=lambda x:x,comentario_guia="Junio: cobrado1200; no cambiar contrato")
        doc=ctrl.db.get("documentos",cid,res["resultado"].documento)
        assert doc["comentario_guia"].startswith("Junio:")
        assert doc["ruta"].endswith(".csv")
        assert (Path(c["carpeta"])/doc["ruta"]).read_bytes()==archivo.read_bytes()
        assert ctrl.db.get("contratos",cid,contrato)["renta_inicial"]==2400
        cobros=ctrl.db.listar("cobros",cid)
        assert next(x for x in cobros if x.get("periodo_desde")=="2025-06-01")["importe_cobrado"]==1200
        from openpyxl import load_workbook
        w=load_workbook(res["excel"])
        try:
            assert w["13_DOCUMENTACION"]["J2"].hyperlink.target.endswith(".csv")
        finally:w.close()
        otra=ctrl.procesar_pdf(cid,archivo,2025,confirmar=lambda x:x)
        assert otra["resultado"].duplicado
    finally:ctrl.cerrar()


def test_guardar_sin_confirmar_filas_no_deja_altas_parciales(tmp_path):
    db=Database(tmp_path/"base.db")
    try:
        c=db.crear_cliente("Prueba","Uno","X1234567A")
        archivo=tmp_path/"x.csv";archivo.write_text("importe;fecha\n1;no",encoding="utf-8")
        ext=ia.a_extraccion(respuesta(registros=[fila()]),archivo.name)
        with pytest.raises(ValueError,match="inmueble"):
            pp.aplicar(db,c["id"],ext,archivo,ejercicio=2025)
        assert not db.listar("documentos",c["id"])
        assert not db.listar("cobros",c["id"])
    finally:db.cerrar()


def test_ui_retira_cancelar_y_destino_cambiado(tmp_path):
    a=tmp_path/"uno.xlsx";a.write_bytes(b"ejemplo")
    lote=preparar_lote([a,a,tmp_path/"x.exe"])
    assert len(lote.adjuntos)==1 and lote.avisos
    salidas=[]
    r=RevisionDocumentos(ui.CLARO,lote.adjuntos,Destino("Prueba","X1234567A",2025),lambda *x:salidas.append(x),lambda:salidas.append("cancelar"),destino_vigente=lambda:False)
    r.comentario.value="Guia"
    r._procesar(None)
    assert not salidas
    r.retirar(r.adjuntos[0])
    assert r.boton_procesar.disabled


def test_ia_false_gananciales_no_infiere_regimen_de_vendedores():
    ext=ia.a_extraccion(respuesta("ESCRITURA_COMPRA",{"precio_escritura":250000,"pct_titularidad":1}),"x.pdf")
    assert ext.campos["gananciales_forzado"]==0


def test_largo_comentario_rechazado_antes_de_llamada(tmp_path,monkeypatch):
    archivo=tmp_path/"x.csv";archivo.write_text("x",encoding="utf-8")
    monkeypatch.setattr(ia,"_cliente",lambda:pytest.fail("No debe llamar"))
    with pytest.raises(ia.ErrorIA):ia.leer_con_claude(archivo,ia.AjustesIA(),comentario_guia="x"*4001)


def test_cobros_mismo_mes_no_se_sobrescriben(tmp_path):
    db=Database(tmp_path/"registros.db")
    try:
        c=db.crear_cliente("Prueba","Doble","X1234567A");cid=c["id"]
        inm=db.upsert("inmuebles",cid,dict(direccion="Prueba",pct_titularidad=1))
        db.upsert("contratos",cid,dict(inmueble=inm,fecha_inicio="2025-01-01",fecha_fin_prevista="2025-12-31"))
        archivo=tmp_path/"x.csv";archivo.write_text("datos",encoding="utf-8")
        ext=ia.a_extraccion(respuesta(registros=[fila("500"),fila("400")]),archivo.name)
        ext.campos["inmueble"]=inm
        with pytest.raises(ValueError,match="mismo mes"):
            pp.aplicar(db,cid,ext,archivo,ejercicio=2025)
        assert not db.listar("cobros",cid)
        assert not db.listar("documentos",cid)
    finally:db.cerrar()


def test_resolucion_y_modelo35_revisados(tmp_path):
    db=Database(tmp_path/"terminacion.db")
    try:
        cid=db.crear_cliente("Prueba","Cierre","X1234567A")["id"]
        inm=db.upsert("inmuebles",cid,dict(direccion="Prueba",pct_titularidad=1))
        ctr=db.upsert("contratos",cid,dict(inmueble=inm,fecha_inicio="2024-09-16",fecha_fin_prevista="2025-08-16"))
        origen=tmp_path/"resolucion.csv";origen.write_text("resolucion",encoding="utf-8")
        ext=ia.a_extraccion(respuesta("RESOLUCION_ALQUILER",{"fecha_inicio":"2024-09-16","fecha_fin_real":"2024-12-23"}),origen.name)
        ext.campos["inmueble"]=inm
        pp.aplicar(db,cid,ext,origen,ejercicio=2024)
        assert db.get("contratos",cid,ctr)["fecha_fin_real"]=="2024-12-23"
        modelo=tmp_path/"modelo.csv";modelo.write_text("modelo35",encoding="utf-8")
        ext=ia.a_extraccion(respuesta("MODELO_210",{"tipo_renta":"35","ejercicio":2025,"periodo":"0A","base":10000,"cuota":2400}),modelo.name)
        ext.campos["inmueble"]=inm
        pp.aplicar(db,cid,ext,modelo,ejercicio=2025)
        fila=db.listar("m210_alquiler",cid)[0]
        assert fila["periodo"]=="Anual" and fila["estado"]=="Presentado" and fila["cuota"]==2400
    finally:db.cerrar()


def test_migracion1a2_conserva_documentos_y_crea_snapshot(tmp_path):
    from database import VERSION_ESQUEMA
    ruta=tmp_path/"persistente.db"
    db=Database(ruta)
    cid=db.crear_cliente("Ficticio","Migracion","X1234567A")["id"]
    codigo=db.upsert("documentos",cid,dict(nombre_original="prueba.pdf",descripcion="Sin perder"))
    db.con.execute("PRAGMA user_version=1");db.con.commit();db.cerrar()
    db=Database(ruta)
    try:
        assert db.con.execute("PRAGMA user_version").fetchone()[0]==VERSION_ESQUEMA
        assert db.get("documentos",cid,codigo)["descripcion"]=="Sin perder"
        assert "comentario_guia" in db.get("documentos",cid,codigo)
        assert list(tmp_path.rglob("*.db"))  # La copia usa el patrón propio y origen existente.
        assert (tmp_path/"Backups_Seguridad").is_dir()
    finally:db.cerrar()


@pytest.mark.parametrize("cancelar",[False,True])
def test_bandeja_espera_comentario_y_no_toca_origen(tmp_path,monkeypatch,cancelar):
    import main
    archivo=tmp_path/"origen.csv";archivo.write_text("ejemplo",encoding="utf-8")
    app=main.App.__new__(main.App);app.cid=1;app.ejercicio=2025;app.cerrando=False;app.p=ui.CLARO
    app.ctrl=NS(lector=NS(get_cliente=lambda _:dict(nombre="Prueba",apellidos="Uno",nif="X1234567A")))
    app.page=NS(pop_dialog=lambda:None,update=lambda:None)
    def recorrer(control):
        yield control
        for hijo in getattr(control,"controls",[]) or []:yield from recorrer(hijo)
    def dialogo(titulo,contenido,acciones,**kw):
        if not cancelar:
            campo=next(c for c in recorrer(contenido) if isinstance(c,main.ft.TextField))
            campo.value="Usa columna B; no inventar periodos"
        acciones[0 if cancelar else -1].on_click(None)
        return NS(on_dismiss=None)
    app._dialogo=dialogo
    monkeypatch.setattr(main,"ejecutar_en_ui",lambda page,fn:fn())
    seleccion=app._revisar_lote_bandeja(1,2025,[archivo])
    if cancelar:assert seleccion["rutas"] is None and seleccion["cancelado"]
    else:assert seleccion["comentario"].startswith("Usa columna B") and seleccion["rutas"]==[archivo]
    assert archivo.read_text(encoding="utf-8")=="ejemplo"


def test_patrimonio_incluye_gastos_inherentes_sin_perder_escritura():
    import tax_calculator as tax
    inmueble=dict(precio_escritura=200000,itp_iva_ajd=20000,notaria=900,registro=600,agencia=3500,intereses=9000)
    valor=tax.valor_patrimonial(inmueble,dict(valor_catastral=120000))
    assert valor.valor==225000 and valor.fuente=="Valor adquisición"
    assert valor.escritura==200000
    assert tax.valor_patrimonial(dict(precio_escritura=200000),None).fuente=="Valor escritura"
    assert tax.valor_patrimonial(dict(itp_iva_ajd=20000),None).valor==0


@pytest.mark.parametrize("pais,tasa,amortiza",[("Rusia",.24,False),("Francia",.19,True)])
def test_oraculo_condicionado_alquiler_ue_extraue(pais,tasa,amortiza):
    import tax_calculator as tax
    inm=dict(codigo="INM-1",precio_escritura=200000,fecha_adquisicion="2024-01-01",pct_titularidad=1)
    cat=dict(valor_catastral=100000,valor_construccion=60000)
    contratos=[dict(codigo="CTR-1",fecha_inicio="2025-06-01",fecha_fin_prevista="2026-05-31",renta_inicial=2400,periodicidad="Mensual")]
    r=tax.calcular_210_alquiler(inm,contratos,{},[],cat,tax.resolver_residencia(dict(pais=pais)),2025,"Anual")[0]
    assert r.ingresos==16800 and r.tipo==tasa
    assert r.gastos==(2110.68 if amortiza else 0)
    assert r.cuota==(2790.97 if amortiza else 4032)


def test_guia_no_reintroduce_campos_omitidos_por_ia(tmp_path,monkeypatch):
    archivo=tmp_path/"datos.csv";archivo.write_text("datos",encoding="utf-8")
    def local(_):
        return pp.Extraccion(tipo=pp.TipoDocumento.CATASTRO_IBI,confianza=.95,nombre_archivo=archivo.name,
                             campos={"valor_catastral":90000,"ejercicio":2025})
    monkeypatch.setattr(pp,"extraer_local",local)
    monkeypatch.setattr(ia,"disponible",lambda _:True)
    monkeypatch.setattr(ia,"extraer",lambda *a,**kw:pp.Extraccion(tipo=pp.TipoDocumento.CATASTRO_IBI,confianza=.95,
        nombre_archivo=archivo.name,campos={"ejercicio":2025}))
    con_guia=pp.extraer(archivo,ia.AjustesIA(),comentario_guia="Ignora la hoja con el valor catastral antiguo")
    assert "valor_catastral" not in con_guia.campos
    assert not any("lector local cree" in a.lower() for a in con_guia.avisos)
    sin_guia=pp.extraer(archivo,ia.AjustesIA())
    assert sin_guia.campos["valor_catastral"]==90000


def test_sin_ia_avisa_que_guia_necesita_revision_manual(tmp_path,monkeypatch):
    archivo=tmp_path/"ingresos.csv";archivo.write_text("mes;importe\njunio;1200",encoding="utf-8")
    monkeypatch.setattr(ia,"disponible",lambda _:False)
    ext=pp.extraer(archivo,ia.AjustesIA(),comentario_guia="Ignora hoja2")
    assert ext.comentario_guia=="Ignora hoja2"
    assert any("no interpreta instrucciones" in x for x in ext.avisos)
