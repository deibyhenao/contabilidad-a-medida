"""Criterios compartidos de registro y redondeo; sin interfaz ni acceso a la BD."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def decimal_finito(valor) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Introduce un número válido.") from None
    if not numero.is_finite():
        raise ValueError("El importe o porcentaje debe ser finito.")
    return numero


def redondear_eur(valor) -> float:
    return float(decimal_finito(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fraccion(valor) -> float:
    numero = decimal_finito(valor)
    if not 0 <= numero <= 1:
        raise ValueError("El porcentaje debe estar entre 0 % y 100 %.")
    return float(numero)


def ejercicio_registrado(gasto: dict):
    """Ejercicio explícito; en su ausencia, fecha y después inicio del periodo. Sin dato: sin clasificar."""
    if gasto.get("ejercicio") not in (None, ""):
        return int(gasto["ejercicio"])
    for campo in ("fecha", "periodo_desde", "periodo_hasta"):
        valor = gasto.get(campo)
        if valor not in (None, ""):
            if isinstance(valor, (date, datetime)):
                return valor.year
            return date.fromisoformat(str(valor)[:10]).year
    return None


def importe_registrado(gasto: dict) -> float:
    """R final o N/base × O × P; redondeo por fila, igual que ROUND(...,2) en el resumen Excel."""
    if gasto.get("importe_deducible") not in (None, ""):
        return redondear_eur(gasto["importe_deducible"])
    total = gasto.get("total")
    if total in (None, ""):
        total = gasto.get("base") or 0
    inmueble = fraccion(gasto["pct_inmueble"]) if gasto.get("pct_inmueble") not in (None, "") else 1
    alquiler = fraccion(gasto["pct_alquiler"]) if gasto.get("pct_alquiler") not in (None, "") else 1
    return redondear_eur(decimal_finito(total) * decimal_finito(inmueble) * decimal_finito(alquiler))
