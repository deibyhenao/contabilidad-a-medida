"""Seguridad e idempotencia de la publicación: sin red ni instaladores reales."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from scripts import verificar_release as vr


def release():
    return {'tag_name':'v1.0.7','draft':False,'prerelease':False,'assets':[
        {'name':'ContabilidadAMedida-1.0.7-windows-x64-Setup.exe','state':'uploaded','size':100,'digest':'sha256:'+'a'*64},
        {'name':'SHA256SUMS.txt','state':'uploaded','size':128,'digest':'sha256:'+'b'*64}]}


def respuesta(monkeypatch,datos):
    monkeypatch.setattr(vr,'urlopen',lambda *a,**k:BytesIO(json.dumps(datos).encode()))


def test_publicada_se_conserva_sin_escrituras(monkeypatch):
    respuesta(monkeypatch,release())
    assert vr.publicada('deibyhenao/contabilidad-a-medida','v1.0.7') is True


@pytest.mark.parametrize('estado',[403,429,500,404])
def test_solo404_autoriza_intentar_crear(monkeypatch,estado):
    def fallar(req,**k):
        if estado==404 and '/releases?' in req.full_url:return BytesIO(b'[]')
        raise HTTPError('https://api.github.com/',estado,'ficticio',None,None)
    monkeypatch.setattr(vr,'urlopen',fallar)
    if estado==404:assert not vr.publicada('owner/repo','v1.0.7')
    else:
        with pytest.raises(HTTPError):vr.publicada('owner/repo','v1.0.7')


def test_error_red_no_es_ausencia(monkeypatch):
    def fallar(*a,**k):raise URLError('sin conexión')
    monkeypatch.setattr(vr,'urlopen',fallar)
    with pytest.raises(URLError):vr.publicada('owner/repo','v1.0.7')


@pytest.mark.parametrize('campo,valor',[('draft',True),('prerelease',True),('tag_name','v1.0.8')])
def test_no_se_oculta_publicacion_incompatible(monkeypatch,campo,valor):
    dato=release();dato[campo]=valor;respuesta(monkeypatch,dato)
    with pytest.raises(ValueError):vr.publicada('owner/repo','v1.0.7')


@pytest.mark.parametrize('fallo',['sin_exe','sin_sumas','duplicado','size_cero','size_bool','sin_digest','digest_invalido','no_subido'])
def test_release_incompleta_no_se_da_por_buena(monkeypatch,fallo):
    dato=release()
    if fallo=='sin_exe':dato['assets'].pop(0)
    elif fallo=='sin_sumas':dato['assets'].pop()
    elif fallo=='duplicado':dato['assets'].append(deepcopy(dato['assets'][0]))
    elif fallo=='size_cero':dato['assets'][0]['size']=0
    elif fallo=='size_bool':dato['assets'][0]['size']=True
    elif fallo=='sin_digest':dato['assets'][0].pop('digest')
    elif fallo=='digest_invalido':dato['assets'][0]['digest']='sha256:abc'
    else:dato['assets'][0]['state']='starter'
    respuesta(monkeypatch,dato)
    with pytest.raises(ValueError):vr.publicada('owner/repo','v1.0.7')


def test_salida_ci_y_token_no_se_imprimen(monkeypatch,tmp_path,capsys):
    respuesta(monkeypatch,release())
    monkeypatch.setenv('GH_REPO','owner/repo');monkeypatch.setenv('TAG_PUBLICACION','v1.0.7')
    monkeypatch.setenv('GH_TOKEN','CLAVE_FICTICIA_NO_REAL')
    salida=tmp_path/'outputs';monkeypatch.setenv('GITHUB_OUTPUT',str(salida))
    vr.main()
    assert capsys.readouterr().out=='true\n'
    assert salida.read_text()=='existe=true\n'



def test_borrador_oculto_en_endpoint_por_tag_no_se_duplica(monkeypatch):
    dato=release();dato['draft']=True
    def abrir(req,**k):
        if '/tags/' in req.full_url:raise HTTPError(req.full_url,404,'no publicado',None,None)
        return BytesIO(json.dumps([dato]).encode())
    monkeypatch.setattr(vr,'urlopen',abrir)
    with pytest.raises(ValueError,match='estable publicada'):vr.publicada('owner/repo','v1.0.7')


def test_borrador_tras_primera_pagina_tambien_se_detecta(monkeypatch):
    dato=release();dato['draft']=True
    def abrir(req,**k):
        if '/tags/' in req.full_url:raise HTTPError(req.full_url,404,'no publicado',None,None)
        filas=[{'tag_name':f'v9.0.{i}'} for i in range(100)] if req.full_url.endswith('&page=1') else [dato]
        return BytesIO(json.dumps(filas).encode())
    monkeypatch.setattr(vr,'urlopen',abrir)
    with pytest.raises(ValueError,match='estable publicada'):vr.publicada('owner/repo','v1.0.7')


def test_listado_inaccesible_no_se_confunde_con_ausencia(monkeypatch):
    def abrir(req,**k):
        raise HTTPError(req.full_url,404 if '/tags/' in req.full_url else 403,'ficticio',None,None)
    monkeypatch.setattr(vr,'urlopen',abrir)
    with pytest.raises(HTTPError):vr.publicada('owner/repo','v1.0.7')
