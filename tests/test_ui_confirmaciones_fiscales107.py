"""Auditoría de la interfaz de Claude: decisiones explícitas, teclado y errores sin perder campos."""
from types import SimpleNamespace
import threading

import pytest
import flet as ft

import main
import ui_estilo as ui
import fiscal_residentes as fr
from test_saldo303_confirmacion import caso303


def recorrer(control):
    yield control
    contenido=getattr(control,'content',None)
    if isinstance(contenido,ft.Control):yield from recorrer(contenido)
    for campo in ('controls','actions'):
        for hijo in getattr(control,campo,[]) or []:
            if isinstance(hijo,ft.Control):yield from recorrer(hijo)


@pytest.fixture
def interfaz303(caso303):
    main._cargar_modulos()
    c,cid,codigo=caso303
    app=main.App.__new__(main.App)
    app.p,app.cid,app.ejercicio=ui.CLARO,cid,2025
    app.ctrl=c;app.cerrando=False
    app.page=SimpleNamespace(update=lambda:None)
    app.ocupada=lambda:False
    app._dialogos=[]
    datos={'cierres':0,'tareas':[],'dialogos':[]}
    def mostrar(titulo,contenido,acciones,**kw):
        d=SimpleNamespace(titulo=titulo,content=contenido,actions=acciones,opciones=kw)
        app._dialogos.append(d);datos['dialogos'].append(d);return d
    def cerrar(_e=None):
        datos['cierres']+=1
        if app._dialogos:app._dialogos.pop()
    def trabajador(texto,trabajo,*args,**kwargs):
        datos['tareas'].append((trabajo,kwargs));return True
    app._dialogo=mostrar;app.cerrar_dialogo=cerrar;app.en_segundo_plano=trabajador
    fila=c.db.get('m303',cid,codigo)
    app.dialogo_saldo_303(fila)
    d=datos['dialogos'][-1]
    controles=list(recorrer(d.content))
    datos.update(destino=next(x for x in controles if isinstance(x,ft.Dropdown)),
        saldo=next(x for x in controles if isinstance(x,ft.TextField) and x.label.startswith('Saldo total')),
        nota=next(x for x in controles if isinstance(x,ft.TextField) and x.label.startswith('Justificante')),
        confirmar=next(x for x in controles if isinstance(x,ft.Checkbox)),
        guardar=next(x for x in d.actions if x.content=='Guardar decisión'),
        cancelar=next(x for x in d.actions if x.content=='Cancelar'),
        cerrar=next(x for x in d.actions if x.content=='Cerrar'))
    return app,c,fila,datos


def completar(datos,destino='compensar',saldo='500'):
    datos['destino'].value=destino;datos['destino'].on_select(None)
    datos['saldo'].value=saldo;datos['saldo'].on_change(None)
    datos['nota'].value='Justificante contrastado';datos['nota'].on_change(None)
    datos['confirmar'].value=True;datos['confirmar'].on_change(None)


def test_dialogo_sin_decision_preseleccionada_y_con_acciones_nativas(interfaz303):
    app,c,fila,d=interfaz303
    assert d['destino'].value is None and d['guardar'].disabled
    assert not d['saldo'].visible
    assert all(isinstance(b,ft.TextButton) and b.height==44 for b in d['dialogos'][-1].actions)
    assert d['dialogos'][-1].opciones['ancho']==560
    assert not d['confirmar'].value


def test_cancelar_no_modifica_ni_envia_a_backend(interfaz303):
    app,c,fila,d=interfaz303
    completar(d)
    d['cancelar'].on_click(None)
    assert not d['tareas'] and d['cierres']==1
    assert c.db.get_config(f"saldo303|{app.cid}|{fila['codigo']}") is None


@pytest.mark.parametrize('destino',['compensar','devolver','pendiente'])
def test_confirmar_bloquea_doble_envio_y_cierra_solo_al_terminar(interfaz303,destino):
    app,c,fila,d=interfaz303
    completar(d,destino)
    assert not d['guardar'].disabled
    d['guardar'].on_click(None);d['guardar'].on_click(None)
    assert len(d['tareas'])==1 and d['cierres']==0
    assert d['guardar'].disabled and d['cancelar'].disabled and d['destino'].disabled
    tarea,callbacks=d['tareas'][0]
    d['saldo'].value='9999';d['nota'].value='Cambio posterior'
    tarea()
    decision=fr.saldo_303(c.db,app.cid,c.db.get('m303',app.cid,fila['codigo']))
    assert decision['destino']==destino
    assert decision['saldo']==(500 if destino=='compensar' else 0)
    callbacks['despues']()
    assert d['cierres']==1


def test_cambiar_decision_desmarca_confirmacion(interfaz303):
    app,c,fila,d=interfaz303
    completar(d)
    d['destino'].value='devolver';d['destino'].on_select(None)
    assert not d['confirmar'].value and d['guardar'].disabled and not d['saldo'].visible


@pytest.mark.parametrize('valor',['','50%','nan','inf','-1','1e13','499.99'])
def test_importe_invalido_conserva_formulario(interfaz303,valor):
    app,c,fila,d=interfaz303
    completar(d,saldo=valor)
    assert d['guardar'].disabled
    d['guardar'].on_click(None)
    assert not d['tareas'] and not d['cierres'] and d['saldo'].value==valor


def test_cambio_cliente_impide_nueva_confirmacion(interfaz303):
    app,c,fila,d=interfaz303
    completar(d);app.cid+=1
    d['guardar'].on_click(None)
    assert not d['tareas'] and not d['cierres']


def test_error_backend_recupera_edicion_y_conserva_campos(interfaz303):
    app,c,fila,d=interfaz303
    completar(d);d['guardar'].on_click(None)
    _,callbacks=d['tareas'][0]
    callbacks['al_fallar'](ValueError('El justificante cambió'))
    assert not d['destino'].disabled and not d['cancelar'].disabled and not d['guardar'].disabled
    assert d['saldo'].value=='500' and d['nota'].value=='Justificante contrastado' and not d['cierres']
    assert any('El justificante cambió' in str(getattr(x,'value','')) for x in recorrer(d['dialogos'][-1].content))


def test_turno_rechazado_conserva_edicion_sin_cierre(interfaz303):
    app,c,fila,d=interfaz303
    app.en_segundo_plano=lambda *a,**kw:False
    completar(d);d['guardar'].on_click(None)
    assert not d['guardar'].disabled and not d['destino'].disabled and not d['cierres']


def test_error_excel_tras_commit_no_invita_a_repetir(interfaz303):
    app,c,fila,d=interfaz303
    completar(d);d['guardar'].on_click(None)
    from controlador import CalculoConfirmadoSinExcel
    exc=CalculoConfirmadoSinExcel(app.cid,2025,{},RuntimeError('Excel bloqueado'))
    d['tareas'][0][1]['al_fallar'](exc)
    assert not d['guardar'].visible and not d['cancelar'].visible and d['cerrar'].visible
    assert d['destino'].disabled
    assert any('se ha guardado' in str(getattr(x,'value','')) for x in recorrer(d['dialogos'][-1].content))


def test_callback_exito_no_cierra_un_dialogo_ajeno(interfaz303):
    app,c,fila,d=interfaz303
    completar(d);d['guardar'].on_click(None)
    otro=SimpleNamespace();app._dialogos.append(otro)
    d['tareas'][0][1]['despues']()
    assert not d['cierres'] and app._dialogos[-1] is otro


def test_error_worker_notifica_en_ui_y_libera_turno(interfaz303,monkeypatch):
    app,c,fila,d=interfaz303
    pendientes=[];hilos=[];avisos=[];fallos=[]
    app._turno=threading.Lock()
    app.set_ocupado=lambda *a:None
    app._pintar_principal=lambda:None
    app._reintentar_excel=lambda:None
    app.avisar=lambda *a,**kw:avisos.append(a)
    app._aplicar_resultado=lambda *a:None
    def run_thread(fn):
        t=threading.Thread(target=fn);hilos.append(t);t.start()
    app.page.run_thread=run_thread
    monkeypatch.setattr(main,'ejecutar_en_ui',lambda page,fn:pendientes.append(fn))
    def trabajo():raise ValueError('Error de prueba sin cambios')
    principal=threading.get_ident()
    assert main.App.en_segundo_plano(app,'Guardando',trabajo,al_fallar=lambda e:fallos.append((str(e),threading.get_ident())))
    hilos[0].join(timeout=3);assert not hilos[0].is_alive()
    assert app._turno.locked() and not fallos
    for fn in pendientes:fn()
    assert not app._turno.locked()
    assert fallos==[('Error de prueba sin cambios',principal)]
    assert avisos


def test_cancelar_despues_de_decision_confirmada_conserva_la_eleccion(interfaz303):
    app,c,fila,d=interfaz303
    c.confirmar_saldo_303(app.cid,fila['codigo'],2025,'compensar',500,'Justificante contrastado',
                        fr.firma_saldo_303(fila),confirmado=True)
    antes=c.db.get_config(f"saldo303|{app.cid}|{fila['codigo']}")
    app.dialogo_saldo_303(fila)
    dialogo=d['dialogos'][-1]
    cancelar=next(x for x in dialogo.actions if x.content=='Cancelar')
    cancelar.on_click(None)
    assert c.db.get_config(f"saldo303|{app.cid}|{fila['codigo']}")==antes


def test_ui_resultado_negativo_y_ingreso_cero_consume_minimo_comun(interfaz303):
    app,c,fila,d=interfaz303
    nueva={**fila,'a_ingresar':0,'resultado':-500}
    app.dialogo_saldo_303(nueva)
    dialogo=d['dialogos'][-1]
    controles=list(recorrer(dialogo.content))
    saldo=next(x for x in controles if isinstance(x,ft.TextField) and x.label.startswith('Saldo total'))
    assert '500,00' in saldo.helper
    assert any('-500,00' in str(getattr(x,'value','')) or '−500,00' in str(getattr(x,'value','')) for x in controles)


@pytest.mark.parametrize('destino',['compensar','devolver','pendiente'])
def test_etiquetas_confirmacion_son_breves_para_pantalla_estrecha(interfaz303,destino):
    app,c,fila,d=interfaz303
    d['destino'].value=destino;d['destino'].on_select(None)
    assert len(d['confirmar'].label)<=55
    assert 'asesor' in d['confirmar'].label
