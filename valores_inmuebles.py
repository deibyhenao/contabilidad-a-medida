"""Presentación de valores existentes; no decide bases ni reglas fiscales."""
from tax_calculator import valor_adquisicion_total


CAMPOS_COMPRA = (
    ("precio_escritura", "Precio de escritura"),
    ("itp_iva_ajd", "ITP / IVA / AJD de la compra"),
    ("notaria", "Gastos de notaría"),
    ("registro", "Registro de la Propiedad"),
    ("agencia", "Agencia / inmobiliaria"),
    ("abogado_gestoria", "Abogado / gestoría"),
    ("otros_gastos", "Otros gastos inherentes a la compra"),
)


def resumen(inmueble, catastro=None):
    catastro = catastro or {}
    registrado = any(inmueble.get(c) not in (None, "") for c, _ in CAMPOS_COMPRA)
    gastos_registrados = any(inmueble.get(c) not in (None, "") for c, _ in CAMPOS_COMPRA if c != "precio_escritura")
    referencia_catastro = catastro.get("valor_referencia")
    referencia_compra = inmueble.get("valor_referencia")
    return {
        "catastral": catastro.get("valor_catastral"),
        "adquisicion": valor_adquisicion_total(inmueble) if registrado else None,
        "parcial": registrado and inmueble.get("precio_escritura") in (None, ""),
        "escritura": inmueble.get("precio_escritura"),
        "referencia": referencia_catastro if referencia_catastro not in (None, "") else referencia_compra,
        "fuente_referencia": "Catastro" if referencia_catastro not in (None, "") else "En adquisición",
        "referencia_compra": referencia_compra,
        "referencia_catastro": referencia_catastro,
        "gastos": valor_adquisicion_total({c: inmueble.get(c) for c, _ in CAMPOS_COMPRA if c != "precio_escritura"}) if gastos_registrados else None,
    }
