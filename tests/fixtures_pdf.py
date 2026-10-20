"""PDFs sintéticos, sin datos reales, reutilizables por las pruebas E2E.

Las funciones devuelven la ruta creada. Fechas: ISO, date o texto español.
Los importes se imprimen con separadores españoles y tildes en el texto.
"""
from datetime import date
from pathlib import Path

import fitz

REF = "1234567AB1234C0001DE"
DIRECCION = "Calle Mayor 1, 2º A"


def _dinero(valor):
    return f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " euros"


def _fecha(valor):
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    try:
        return date.fromisoformat(str(valor)).strftime("%d/%m/%Y")
    except ValueError:
        return str(valor)


def _crear(ruta, texto):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as doc:
        page = doc.new_page()
        resultado = page.insert_textbox(fitz.Rect(40, 40, 555, 800), texto, fontname="helv", fontsize=10)
        if resultado < 0:
            raise ValueError("El texto del fixture no cabe en una página")
        doc.save(str(ruta))
    return ruta


def escritura_compra(ruta, ref=REF, precio=180000, fecha="2019-05-10", titulares=None,
                      gananciales=False, direccion=DIRECCION, pct_titularidad=1.0):
    titulares = ["Pepito Pérez"] if titulares is None else titulares
    if isinstance(titulares, str):
        titulares = [titulares]
    return _crear(ruta, f"""ESCRITURA PÚBLICA DE COMPRAVENTA
Ante el notario comparecen la parte vendedora y la parte compradora.
Cliente comprador. La parte vendedora transmite la finca descrita.
Titulares: {'; '.join(titulares)}
Régimen económico: {'sociedad de gananciales' if gananciales else 'separación de bienes'}
Dirección: {direccion}
Municipio: Alicante
Provincia: Alicante
CP: 03001
Referencia catastral: {ref}
Fecha escritura: {_fecha(fecha)}
Precio de compra: {_dinero(precio)}
Porcentaje de titularidad: {pct_titularidad * 100:g}%
Notario: María García López
Número de protocolo: 1234/2019
Tipo de adquisición: Compraventa
""")


def escritura_venta(ruta, ref=REF, precio=250000, fecha="2026-08-15", gastos=3000):
    return _crear(ruta, f"""ESCRITURA DE VENTA
Cliente vendedor: Pepito Pérez. La parte vendedora vende y transmite la finca.
Notario: María García López
Referencia catastral: {ref}
Fecha de venta: {_fecha(fecha)}
Precio de venta: {_dinero(precio)}
Gastos de venta: {_dinero(gastos)}
""")


def recibo_ibi(ruta, ref=REF, ejercicio=2026, valor_catastral=90000, valor_suelo=36000,
                valor_construccion=54000, ibi=450, anio_revision=2019, direccion=DIRECCION, tasa_basura=None):
    return _crear(ruta, f"""AYUNTAMIENTO DE ALICANTE - RECIBO IBI
Impuesto sobre Bienes Inmuebles. Carta de pago del tributo municipal.
Ejercicio: {ejercicio}
Dirección: {direccion}
Referencia catastral: {ref}
Valor catastral total: {_dinero(valor_catastral)}
Valor del suelo: {_dinero(valor_suelo)}
Valor de construcción: {_dinero(valor_construccion)}
Año de revisión: {anio_revision}
Cuota IBI: {_dinero(ibi)}
{f"Tasa de basuras: {_dinero(tasa_basura)}" if tasa_basura is not None else ""}
""")


def valor_referencia(ruta, ref=REF, ejercicio=2026, valor=210000):
    return _crear(ruta, f"""DIRECCIÓN GENERAL DEL CATASTRO
CERTIFICACIÓN DEL VALOR DE REFERENCIA
Ejercicio: {ejercicio}
Referencia catastral: {ref}
Valor de referencia: {_dinero(valor)}
Este documento certifica el valor de referencia para el ejercicio indicado.
""")


def contrato_alquiler(ruta, ref=REF, fecha_inicio="2026-07-01", fecha_fin_prevista="2026-12-31",
                      renta_inicial=1000, inquilinos=None, direccion=DIRECCION, duracion=None):
    inquilinos = [{"nombre": "Ana López", "documento": "12345678Z", "pais": "España"}] if inquilinos is None else inquilinos
    fin = f"Fecha fin: {_fecha(fecha_fin_prevista)}" if fecha_fin_prevista else ""
    plazo = f"Duración: {duracion}" if duracion else ""
    lineas_inq = "\n".join(
        f"Arrendatario: {i['nombre']}; {i.get('documento', '')}; {i.get('pais', '')}" for i in inquilinos
    )
    return _crear(ruta, f"""CONTRATO DE ARRENDAMIENTO DE VIVIENDA
Arrendador: Pepito Pérez. El arrendador cede el uso al arrendatario.
Dirección: {direccion}
Referencia catastral: {ref}
Fecha inicio: {_fecha(fecha_inicio)}
{fin}
{plazo}
Renta mensual: {_dinero(renta_inicial)}
Periodicidad: Mensual
Fianza: {_dinero(renta_inicial)}
Tipo arrendamiento: Vivienda
{lineas_inq}
Ambas partes firman las condiciones de alquiler aquí indicadas.
""")


def modelo_210(ruta, ref=REF, ejercicio=2026, periodo="3T", tipo_renta="01", base=1500,
               cuota=285, pagado=None, fecha_presentacion="2026-10-15", nrc="NRC-SINTETICO-001"):
    return _crear(ruta, f"""AGENCIA TRIBUTARIA - MODELO 210
Impuesto sobre la Renta de no Residentes. Justificante de presentación.
Tipo de renta: {tipo_renta}
Ejercicio: {ejercicio}
Periodo: {periodo}
Referencia catastral: {ref}
Base imponible: {_dinero(base)}
Cuota: {_dinero(cuota)}
Importe ingresado: {_dinero(cuota if pagado is None else pagado)}
Fecha de presentación: {_fecha(fecha_presentacion)}
NRC: {nrc}
""")


def certificado_residencia(ruta, pais="Francia", ejercicio=2026, fecha="2026-01-15",
                          valido_hasta="2026-12-31", nif_extranjero="FR-SINTETICO-123"):
    return _crear(ruta, f"""CERTIFICADO DE RESIDENCIA FISCAL
Certificate of residence. Certificación emitida por la administración fiscal.
País de residencia fiscal: {pais}
Ejercicio fiscal: {ejercicio}
Fecha certificado: {_fecha(fecha)}
Válido hasta: {_fecha(valido_hasta)}
NIF extranjero: {nif_extranjero}
Se certifica la residencia fiscal de Pepito Pérez en el país indicado.
""")


def modelo_036(ruta, fecha_alta="2026-02-01", epigrafe="763", modelos="303,130"):
    return _crear(ruta, f"""AGENCIA TRIBUTARIA - MODELO 036
DECLARACIÓN CENSAL DE ALTA DE EMPRESARIOS Y PROFESIONALES
Fecha de alta: {_fecha(fecha_alta)}
Epígrafe IAE: {epigrafe}
Descripción actividad: Servicios de programación informática
Modelos: {modelos}
Declaración de inicio de actividad presentada por el obligado tributario.
""")


def modelo_presentado(ruta, modelo="303", ejercicio=2026, periodo="1T", importe=200,
                      fecha_presentacion="2026-04-15", csv="CSV-SINTETICO-001"):
    return _crear(ruta, f"""AGENCIA TRIBUTARIA - MODELO {modelo}
AUTOLIQUIDACIÓN PRESENTADA - Justificante de presentación telemática
Ejercicio: {ejercicio}
Periodo: {periodo}
Fecha de presentación: {_fecha(fecha_presentacion)}
Importe ingresado: {_dinero(importe)}
CSV: {csv}
""")


def factura_gasto(ruta, categoria="Suministros", concepto="Luz", proveedor="Servicios del Hogar SA",
                  nif_proveedor="A12345678", n_factura="F-2026-0001", fecha="2026-03-10",
                  periodo_desde="2026-02-01", periodo_hasta="2026-02-28", base=100, iva=21,
                  total=121, ref=REF):
    """Factura o recibo sintético; en hipotecas base son intereses, total es la cuota."""
    if categoria == "Comunidad":
        encabezado = "RECIBO DE CUOTA DE COMUNIDAD DE PROPIETARIOS"
        detalle = "Cuota ordinaria para servicios comunes del edificio."
    elif categoria == "Seguro":
        encabezado = "RECIBO DE PRIMA DE SEGURO"
        detalle = "Póliza: HOGAR-2026-001. Cobertura de hogar, RC e impago."
    elif categoria == "Intereses/financiación":
        encabezado = "RECIBO DE PRÉSTAMO HIPOTECARIO"
        detalle = (f"Intereses: {_dinero(base)}\n" if base is not None else "")
        detalle += f"Amortización de capital: {_dinero(total - (base or 0))}"
    elif categoria == "Reparación/conservación":
        encabezado = "FACTURA DE REPARACIÓN Y MANTENIMIENTO"
        detalle = "Reparación de instalación y conservación de la vivienda."
    else:
        encabezado = "FACTURA DE SUMINISTRO"
        detalle = {
            "luz": "Suministro de electricidad. Consumo: 180 kWh.",
            "agua": "Suministro de agua. Consumo: 12 m3.",
            "gas": "Suministro de gas natural. Consumo: 250 kWh.",
        }.get(concepto.lower(), "Servicios y suministros de la vivienda.")
    periodo = (f"Periodo de facturación: del {_fecha(periodo_desde)} al {_fecha(periodo_hasta)}"
               if periodo_desde is not None and periodo_hasta is not None else "")
    # La categoría decide el texto del documento, pero no se imprime como pista del parser.
    importes = []
    if categoria != "Intereses/financiación":
        if base is not None:
            importes.append(f"Base imponible: {_dinero(base)}")
        if iva is not None:
            importes.append(f"IVA (21%): {_dinero(iva)}")
    importes.append(f"Total factura: {_dinero(total)}")
    return _crear(ruta, f"""{encabezado}
Proveedor: {proveedor}
CIF proveedor: {nif_proveedor}
Nº de factura: {n_factura}
Fecha de emisión: {_fecha(fecha)}
Dirección del inmueble: {DIRECCION}
Referencia catastral: {ref}
Concepto: {concepto}
{detalle}
{periodo}
{chr(10).join(importes)}
Documento sintético para pruebas; pago por domiciliación bancaria.
""")
