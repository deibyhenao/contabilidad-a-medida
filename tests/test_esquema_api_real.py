"""Compatibilidad del esquema enviado a Anthropic; sin red ni clave real."""
import pytest
import ia_extractor as ia


def test_esquema_api_no_envia_maxitems_no_soportado():
    def recorrer(valor):
        if isinstance(valor,dict):
            assert "maxItems" not in valor
            for contenido in valor.values():recorrer(contenido)
        elif isinstance(valor,list):
            for contenido in valor:recorrer(contenido)
    recorrer(ia.ESQUEMA)


def test_limite_500_registros_sigue_validandose_localmente():
    datos=dict(tipo="ESCRITURA_COMPRA",confianza=.9,campos=[],titulares=[],
               gananciales=False,inquilinos=[],avisos=[],
               registros=[dict(tabla="catastro",campos=[],evidencia="Anexo ficticio")]*501)
    with pytest.raises(ia.ErrorIA,match="fuera del esquema"):
        ia.a_extraccion(datos,"ficticio.pdf")
    datos["registros"]=datos["registros"][:500]
    assert len(ia.a_extraccion(datos,"ficticio.pdf").registros)==500
