"""Vencimientos generales de presentación telemática ante la AEAT.

Por defecto solo se ajustan fines de semana y festivos nacionales. Los festivos
autonómicos/locales dependen del ámbito aplicable: no se puede extender a todos
los contribuyentes una fiesta de Madrid. El calendario Madrid 2024-2027 se
ofrece explícitamente para los supuestos en los que corresponda (p. ej., el
criterio de la AEAT para domiciliación). No calcula plazos de domiciliación,
plazos autonómicos de compra ni la excepción del modelo 369.
"""
from datetime import date, timedelta

# Nacionales fijos (incluida Epifanía, 6 de enero).
_FIJOS = ((1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25))
_ADICIONALES_MADRID = {
    2024: ((1, 6), (3, 28), (3, 29), (5, 2), (5, 15), (7, 25), (11, 9)),
    2025: ((1, 6), (4, 17), (4, 18), (5, 2), (5, 15), (7, 25), (11, 10)),
    2026: ((1, 6), (4, 2), (4, 3), (5, 2), (5, 15), (11, 2), (11, 9), (12, 7)),
    2027: ((1, 6), (3, 19), (3, 25), (3, 26), (5, 15), (8, 16), (11, 9)),
}


def domingo_de_pascua(anio: int) -> date:
    """Algoritmo gregoriano anónimo (Meeus/Jones/Butcher)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(anio, mes, dia + 1)


def viernes_santo(anio: int) -> date:
    """Festivo nacional en todo el territorio, calculado para cualquier año."""
    return domingo_de_pascua(anio) - timedelta(days=2)


def calendario_contrastado(ejercicio: int) -> bool:
    return ejercicio in _ADICIONALES_MADRID


def siguiente_habil(fecha: date, *, municipio: str | None = None) -> date:
    """Prorroga el último día inhábil; los festivos locales requieren ámbito explícito."""
    while (fecha.weekday() >= 5 or (fecha.month, fecha.day) in _FIJOS
           or fecha == viernes_santo(fecha.year)
           or (municipio == 'Madrid' and (fecha.month, fecha.day) in _ADICIONALES_MADRID.get(fecha.year, ()))):
        fecha += timedelta(days=1)
    return fecha
