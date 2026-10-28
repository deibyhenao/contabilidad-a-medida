"""Reglas fiscales España para no residentes (IRNR Modelo 210, Patrimonio 714, ITS 718) y avisos de autónomos.

Autor: Claude (arquitecto). Auditor: Codex.

Dos capas:
1. Funciones puras (sin BD) con cada regla: fáciles de testear y de auditar.
2. ``recalcular_cliente(db, cliente_id, ejercicio)``: lee la BD, aplica las reglas y escribe los resultados en las
   tablas espejo de las pestañas 07, 09, 10, 11, 12, 14 y 15. Es idempotente: se puede llamar tras cada PDF.

Reglas de prudencia:
- Una fila de modelo con estado "Presentado" es un hecho histórico: nunca se recalcula ni se sobrescribe.
- Si falta la residencia fiscal se asume Extra-UE (24 %, sin gastos) y se avisa.
- Toda regla cuyo criterio puede cambiar por normativa lleva la marca ``VERIFICAR`` en su docstring.
"""
from __future__ import annotations

from validacion_catastro import PENDIENTE_REVISION, revision_pendiente

import calendar
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

from database import Database, a_fecha
from gastos import decimal_finito, ejercicio_registrado, fraccion, redondear_eur

# ============================================================================================ constantes
TIPO_UE_EEE = 0.19
TIPO_EXTRA_UE = 0.24
TIPO_GANANCIA_PATRIMONIAL = 0.19
RETENCION_VENTA = 0.03            # retención del comprador (modelo 211)
PCT_IMPUTACION_GENERAL = 0.02
PCT_IMPUTACION_REVISADO = 0.011   # valor catastral revisado en el ejercicio o en los 10 anteriores
AMORTIZACION_PCT = 0.03
DIAS_ANIO_PLANTILLA = 365         # la fórmula H de 10_210_IMPUTACION divide entre 365

IP_MINIMO_EXENTO = 700_000.00
IP_UMBRAL_DECLARAR_BIENES = 2_000_000.00
ITS_MINIMO_TRAMO = 3_000_000.00

# Tarifa estatal del Impuesto sobre el Patrimonio (art. 30 Ley 19/1991): (base desde, cuota en ese punto, tipo resto)
TARIFA_IP_ESTATAL = (
    (0.00, 0.00, 0.002),
    (167_129.45, 334.26, 0.003),
    (334_252.88, 835.63, 0.005),
    (668_499.75, 2_506.86, 0.009),
    (1_336_999.51, 8_523.36, 0.013),
    (2_673_999.01, 25_904.35, 0.017),
    (5_347_998.03, 71_362.33, 0.021),
    (10_695_996.06, 183_670.29, 0.035),
)
# Impuesto Temporal de Solidaridad de las Grandes Fortunas (Ley 38/2022, prorrogado). VERIFICAR vigencia anual.
TARIFA_ITS = (
    (0.00, 0.00, 0.000),
    (3_000_000.00, 0.00, 0.017),
    (5_347_998.03, 39_915.97, 0.021),
    (10_695_996.06, 152_223.93, 0.035),
)

GRUPO_UE = "UE/EEE"
GRUPO_EXTRA = "Extra-UE"

_PAISES_UE_EEE = {
    "espana", "es", "spain", "alemania", "austria", "belgica", "bulgaria", "chipre", "croacia", "dinamarca", "eslovaquia", "eslovenia",
    "estonia", "finlandia", "francia", "grecia", "hungria", "irlanda", "italia", "letonia", "lituania",
    "luxemburgo", "malta", "paises bajos", "holanda", "polonia", "portugal", "republica checa", "chequia",
    "rumania", "rumanía", "suecia",
    # EEE con intercambio efectivo de información tributaria
    "islandia", "noruega", "liechtenstein",
    # Códigos ISO
    "de", "at", "be", "bg", "cy", "hr", "dk", "sk", "si", "ee", "fi", "fr", "gr", "hu", "ie", "it", "lv",
    "lt", "lu", "mt", "nl", "pl", "pt", "cz", "ro", "se", "is", "no", "li",
    # Nombres en inglés frecuentes en documentos
    "germany", "belgium", "croatia", "cyprus", "denmark", "slovakia", "slovenia", "finland", "france", "greece",
    "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg", "netherlands", "poland", "romania",
    "sweden", "czech republic", "czechia", "iceland", "norway",
}


def _sin_tildes(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().strip().lower()


def r2(x: float) -> float:
    return redondear_eur(x)


# ============================================================================================ residencia
def grupo_territorio(pais: Optional[str]) -> str:
    """'Francia' -> 'UE/EEE'; 'Reino Unido', 'Rusia', '' -> 'Extra-UE' (Reino Unido es Extra-UE desde 2021)."""
    return GRUPO_UE if _sin_tildes(pais or "") in {_sin_tildes(p) for p in _PAISES_UE_EEE} else GRUPO_EXTRA


def tipo_gravamen_210(grupo: str) -> float:
    return TIPO_UE_EEE if grupo == GRUPO_UE else TIPO_EXTRA_UE


def gastos_deducibles(grupo: str) -> bool:
    """Solo residentes UE/EEE deducen gastos en el 210 de rendimientos (art. 24.6 TRLIRNR)."""
    return grupo == GRUPO_UE


@dataclass
class Residencia:
    pais: str
    grupo: str
    tipo: float
    deduce_gastos: bool
    asumida: bool = False  # True si no había residencia registrada


def resolver_residencia(fila: Optional[dict]) -> Residencia:
    if not fila or not (fila.get("pais") or fila.get("grupo")):
        return Residencia("", GRUPO_EXTRA, TIPO_EXTRA_UE, False, asumida=True)
    # El país manda (auditoría A1-P4). El grupo guardado solo se usa si no consta país.
    if fila.get("pais"):
        grupo = grupo_territorio(fila.get("pais"))
    else:
        grupo = fila.get("grupo") if fila.get("grupo") in (GRUPO_UE, GRUPO_EXTRA) else GRUPO_EXTRA
    deduce = gastos_deducibles(grupo)
    if (fila.get("gastos_deducibles") or "").startswith("No"):
        deduce = False
    return Residencia(fila.get("pais") or "", grupo, tipo_gravamen_210(grupo), deduce)


# ============================================================================================ fechas
def dias(desde: date, hasta: date) -> int:
    """Días naturales ambos inclusive. 0 si el rango está vacío."""
    return max(0, (hasta - desde).days + 1)


def interseccion(a: tuple[date, date], b: tuple[date, date]) -> Optional[tuple[date, date]]:
    ini, fin = max(a[0], b[0]), min(a[1], b[1])
    return (ini, fin) if ini <= fin else None


def unir(intervalos: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for ini, fin in sorted(intervalos):
        if out and ini <= out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], fin))
        else:
            out.append((ini, fin))
    return out


def dias_totales(intervalos: Iterable[tuple[date, date]]) -> int:
    return sum(dias(a, b) for a, b in unir(intervalos))


def anio(ejercicio: int) -> tuple[date, date]:
    return date(ejercicio, 1, 1), date(ejercicio, 12, 31)


def dias_anio(ejercicio: int) -> int:
    return 366 if calendar.isleap(ejercicio) else 365


def normalizar_periodo_210(periodo: str) -> str:
    """Identidad fiscal única: 0A y Anual son el mismo periodo, no dos declaraciones."""
    texto = re.sub(r"\s+", "", str(periodo or "")).upper()
    if texto in ("0A", "OA", "ANUAL"):
        return "Anual"
    if texto in ("1T", "2T", "3T", "4T"):
        return texto
    raise ValueError("Periodo del modelo 210 no válido: usa Anual (0A) o 1T–4T.")


def periodo_210_o_none(periodo) -> Optional[str]:
    """Como normalizar_periodo_210, pero None si no se reconoce: una fila mal escrita a mano en el Excel no debe
    bloquear el recálculo de todo el cliente (la fila se conserva tal cual)."""
    try:
        return normalizar_periodo_210(periodo)
    except ValueError:
        return None


def periodicidad_210_alquiler(db: Database, cid: int, inmueble: str, ejercicio: int) -> str:
    """Preferencia por inmueble/año; el calendario presentado prevalece sobre una preferencia nueva."""
    presentados = {periodo_210_o_none(f.get("periodo"))
                  for f in db.listar("m210_alquiler", cid, inmueble=inmueble, ejercicio=int(ejercicio))
                  if _presentado(f)} - {None}
    if presentados == {"Anual"}:
        return "Anual"
    if presentados and presentados <= {"1T", "2T", "3T", "4T"}:
        return "Trimestral"
    elegido = db.get_config(f"periodicidad_210|{cid}|{inmueble}|{int(ejercicio)}")
    if elegido in ("Anual", "Trimestral"):
        return elegido
    # El calendario ya existente se conserva, también si las filas eran Pendiente.
    # Solo un cálculo sin histórico ni preferencia recibe el nuevo valor por defecto.
    historicos = [f for f in db.listar('m210_alquiler', cid, inmueble=inmueble, ejercicio=int(ejercicio))
                  if not _presentado(f)]
    activos = [f for f in historicos if f.get('estado') != 'No procede']
    borradores = {periodo_210_o_none(f.get("periodo"))
                  for f in (activos or historicos)} - {None}
    if borradores and borradores <= {"1T", "2T", "3T", "4T"}:
        return "Trimestral"
    if borradores == {"Anual"}:
        return "Anual"
    # Una preferencia general solo sirve de valor inicial para cálculos nuevos.
    elegido = db.get_config("periodicidad_210_alquiler")
    if elegido in ("Anual", "Trimestral"):
        return elegido
    return "Anual" if int(ejercicio) >= 2024 else "Trimestral"


def resumen_arrendamiento(inm: dict, contratos: list[dict], rentas_por_contrato: dict,
                         ejercicio: int) -> dict:
    """Ocupación sin duplicar días solapados e ingresos contractuales del inmueble al 100 %."""
    tenencia = periodo_tenencia(inm, int(ejercicio))
    propiedad = dias(*tenencia) if tenencia else 0
    alquilados = dias_totales(intervalos_alquilados(contratos, tenencia)) if tenencia else 0
    devengados = (r2(sum(renta_devengada(c, rentas_por_contrato.get(c["codigo"], []), *tenencia)
                         for c in contratos)) if tenencia else 0.0)
    return {"dias_propiedad": propiedad, "dias_alquilado": alquilados,
            "dias_vacio": max(0, propiedad - alquilados), "ingresos_devengados": devengados}


def trimestres(ejercicio: int) -> list[tuple[str, date, date]]:
    return [("1T", date(ejercicio, 1, 1), date(ejercicio, 3, 31)),
            ("2T", date(ejercicio, 4, 1), date(ejercicio, 6, 30)),
            ("3T", date(ejercicio, 7, 1), date(ejercicio, 9, 30)),
            ("4T", date(ejercicio, 10, 1), date(ejercicio, 12, 31))]


def plazo_210_alquiler(ejercicio: int, periodo: str) -> date:
    """Plazo del 210 de arrendamientos (Orden EHA/3316/2010, art. 5, según la AEAT a 26/10/2026):
    - anual (agrupación desde 2024): 1-20 de enero del año siguiente para 2024 y 2025; 1-20 de abril desde 2026;
    - declaración separada por trimestre: 20 primeros días del mes siguiente al trimestre hasta los devengos de
      septiembre de 2026; desde octubre de 2026, 1-20 de abril del año siguiente."""
    periodo = periodo_210_o_none(periodo) or periodo
    trimestral = {"1T": date(ejercicio, 4, 20), "2T": date(ejercicio, 7, 20), "3T": date(ejercicio, 10, 20)}
    if periodo in trimestral and ejercicio <= 2026:
        return trimestral[periodo]
    if ejercicio >= 2026:  # anual de 2026 en adelante y separadas desde el 4T de 2026
        return date(ejercicio + 1, 4, 20)
    return date(ejercicio + 1, 1, 20)


def plazo_210_imputacion(ejercicio: int) -> date:
    return date(ejercicio + 1, 12, 31)


def plazo_714(ejercicio: int) -> date:
    return date(ejercicio + 1, 6, 30)


def plazo_718(ejercicio: int) -> date:
    """VERIFICAR: en ejercicios anteriores el plazo del 718 fue julio del año siguiente."""
    return date(ejercicio + 1, 7, 31)


def plazo_210_venta(fecha_venta: date) -> date:
    """Un mes de plazo para la retención del 211 y tres meses más para el 210 de ganancia: ~4 meses."""
    m = fecha_venta.month + 4
    y = fecha_venta.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, min(fecha_venta.day, calendar.monthrange(y, m)[1]))


# ============================================================================================ inmueble
def pct_titularidad(inm: dict) -> float:
    """% efectivo. El selector 'gananciales_forzado' fuerza 50 % aunque la escritura diga 100 % (un solo nombre)."""
    p = inm.get("pct_titularidad")
    efectivo = 1.0 if p in (None, "") else fraccion(p)
    if inm.get("gananciales_forzado"):
        return 0.5
    return efectivo


def periodo_tenencia(inm: dict, ejercicio: int) -> Optional[tuple[date, date]]:
    """Días del ejercicio en que el cliente es titular: [adquisición, venta - 1 día] ∩ año.
    El día de la venta ya no es del vendedor."""
    if inm.get("estado") == "Baja":
        return None
    ini_anio, fin_anio = anio(ejercicio)
    adq = a_fecha(inm.get("fecha_adquisicion")) or ini_anio
    venta = a_fecha(inm.get("fecha_venta"))
    fin = (venta - timedelta(days=1)) if venta else fin_anio
    return interseccion((adq, fin), (ini_anio, fin_anio))


def titular_a_31_diciembre(inm: dict, ejercicio: int) -> bool:
    t = periodo_tenencia(inm, ejercicio)
    return bool(t and t[1] == date(ejercicio, 12, 31))


def valor_adquisicion_total(inm: dict) -> float:
    """Espejo de la fórmula Q de 02_INMUEBLES: SUM(J:P)."""
    campos = ("precio_escritura", "itp_iva_ajd", "notaria", "registro", "agencia", "abogado_gestoria", "otros_gastos")
    return r2(sum(float(inm.get(c) or 0) for c in campos))


@dataclass
class ValorPatrimonial:
    valor: float           # mayor de los valores (100 %)
    fuente: str            # 'Valor catastral' | 'Valor escritura' | 'Valor referencia' | 'Valor comprobado'
    catastral: float
    escritura: float
    referencia: float
    comprobado: float


def valor_patrimonial(inm: dict, cat: Optional[dict]) -> ValorPatrimonial:
    """Regla de los 3 valores (art. 10 Ley 19/1991): el MAYOR de
    1) valor catastral, 2) valor de adquisición con gastos inherentes, 3) valor de referencia / comprobado por la Administración."""
    catastral = float((cat or {}).get("valor_catastral") or 0)
    escritura = float(inm.get("precio_escritura") or 0)
    referencia = float((cat or {}).get("valor_referencia") or inm.get("valor_referencia") or 0)
    # AEAT (Patrimonio 2025, bienes inmuebles): la referencia solo cuenta en adquisiciones desde el 01/01/2022 y si
    # fue la base del tributo de la adquisición (ITP/ISD). Se usa la de la escritura, no la del año consultado, y se
    # presume base cuando supera el precio declarado (la base del ITP es el mayor de ambos) · VERIFICAR si se recurrió.
    adquisicion = a_fecha(inm.get("fecha_adquisicion"))
    ref_compra = float(inm.get("valor_referencia") or 0)
    if not (adquisicion and adquisicion >= date(2022, 1, 1) and ref_compra > escritura):
        referencia = 0.0
    else:
        referencia = ref_compra
    # Los datos documentados por el asesor prevalecen sobre la presunción anterior.
    base_compra = inm.get("base_impuesto_adquisicion")
    if (_sin_tildes(str(inm.get("uso_valor_referencia") or "")) == "no"
            or (base_compra not in (None, "") and float(base_compra) >= 0
                and abs(float(base_compra) - ref_compra) > 0.01)):
        referencia = 0.0
    comprobado = float(inm.get("valor_comprobado") or 0)
    candidatos = [("Valor catastral", r2(catastral)), ("Valor escritura", r2(escritura)),
                  ("Valor referencia", r2(referencia)), ("Valor comprobado", r2(comprobado))]
    # Gastos/tributos inherentes documentados; la escritura conserva su valor bruto.
    # No crear una adquisición compuesta solo de gastos ni estimar partidas ausentes.
    if escritura > 0:
        candidatos.append(("Valor adquisición", valor_adquisicion_total(inm)))
    fuente, valor = max(candidatos, key=lambda kv: kv[1])
    return ValorPatrimonial(r2(valor), fuente if valor > 0 else "Sin valores", catastral, escritura, referencia,
                            comprobado)


def pct_imputacion(anio_revision: Optional[int], ejercicio: int, manual: Optional[float] = None) -> float:
    """1,1 % si la revisión catastral entró en vigor en el ejercicio o en los 10 anteriores (y desde 1994);
    2 % en otro caso o si no consta el año. Un % introducido a mano en 03_CATASTRO_IBI (col. J) manda."""
    if manual not in (None, ""):
        return fraccion(manual)
    if anio_revision and int(anio_revision) >= 1994 and ejercicio - 10 <= int(anio_revision) <= ejercicio:
        return PCT_IMPUTACION_REVISADO
    return PCT_IMPUTACION_GENERAL


# ============================================================================================ contratos y rentas
_DIVISOR_PERIODICIDAD = {"Mensual": 1, "Trimestral": 3, "Semestral": 6, "Anual": 12}


def a_mensual(importe: Optional[float], periodicidad: Optional[str]) -> float:
    return float(importe or 0) / _DIVISOR_PERIODICIDAD.get(periodicidad or "Mensual", 1)


def fin_con_prorroga(ctr: dict) -> Optional[date]:
    """Fin legal de un arrendamiento de VIVIENDA sin fecha de fin real: la LAU (art. 9) lo prorroga
    obligatoriamente para el arrendador hasta 5 años (contratos desde el 06/03/2019) o 3 años (06/06/2013-05/03/2019),
    salvo que el inquilino comunique que no renueva. Devuelve None si no aplica (temporada, local, terminado...).
    Arrendador persona física (los clientes no residentes lo son); con persona jurídica serían 7 años · VERIFICAR."""
    # Solo un contrato vigente se prorroga por ley: «Prorrogado» ya es indefinido y el resto terminó o no empezó.
    if ctr.get("fecha_fin_real") or (ctr.get("estado") or "Vigente") != "Vigente":
        return None
    tipo = _sin_tildes(ctr.get("tipo") or "")
    ini, fin = a_fecha(ctr.get("fecha_inicio")), a_fecha(ctr.get("fecha_fin_prevista"))
    if "vivienda" not in tipo or "temporada" in tipo or not ini or not fin:
        return None
    anios = 5 if ini >= date(2019, 3, 6) else 3 if ini >= date(2013, 6, 6) else 0
    if not anios:
        return None
    try:
        legal = ini.replace(year=ini.year + anios) - timedelta(days=1)
    except ValueError:  # 29 de febrero
        legal = date(ini.year + anios, 2, 28)
    return legal if legal > fin else None


def intervalo_contrato(ctr: dict) -> Optional[tuple[date, date]]:
    """[inicio, fin real o fin prevista]. Sin fin = indefinido. Contratos Cancelado / Pendiente firma no cuentan.
    Un contrato de vivienda sin fin real sigue hasta su fin legal con prórroga (``fin_con_prorroga``)."""
    if ctr.get("estado") in ("Cancelado", "Pendiente firma"):
        return None
    ini = a_fecha(ctr.get("fecha_inicio"))
    if not ini:
        return None
    fin = a_fecha(ctr.get("fecha_fin_real")) or a_fecha(ctr.get("fecha_fin_prevista")) or date(9999, 12, 31)
    if ctr.get("estado") == "Prorrogado" and not ctr.get("fecha_fin_real"):
        fin = date(9999, 12, 31)
    fin = fin_con_prorroga(ctr) or fin
    return (ini, fin) if ini <= fin else None


def renta_mensual_en(ctr: dict, rentas: list[dict], dia: date) -> float:
    """Renta mensual vigente el día ``dia``: el último cambio de 06_RENTAS_CONTRATO que lo cubra, si no la inicial."""
    vigentes = [r for r in rentas
                if a_fecha(r.get("desde")) and a_fecha(r["desde"]) <= dia
                and (not r.get("hasta") or dia <= a_fecha(r["hasta"]))]
    if vigentes:
        r = max(vigentes, key=lambda r: a_fecha(r["desde"]))
        return a_mensual(r.get("renta"), r.get("periodicidad") or ctr.get("periodicidad"))
    return a_mensual(ctr.get("renta_inicial"), ctr.get("periodicidad"))


def renta_devengada(ctr: dict, rentas: list[dict], desde: date, hasta: date) -> float:
    """Renta devengada (100 % del contrato) en [desde, hasta], prorrateada por días de cada mes."""
    iv = intervalo_contrato(ctr)
    tramo = iv and interseccion(iv, (desde, hasta))
    if not tramo:
        return 0.0
    total, d = 0.0, tramo[0]
    while d <= tramo[1]:
        total += renta_mensual_en(ctr, rentas, d) / calendar.monthrange(d.year, d.month)[1]
        d += timedelta(days=1)
    return r2(total)


def intervalos_alquilados(contratos: list[dict], ventana: tuple[date, date]) -> list[tuple[date, date]]:
    out = []
    for c in contratos:
        iv = intervalo_contrato(c)
        tramo = iv and interseccion(iv, ventana)
        if tramo:
            out.append(tramo)
    return unir(out)


# ============================================================================================ 210 rendimientos
def periodo_gasto(gasto: dict) -> Optional[tuple[date, date]]:
    """Anclaje económico único; un gasto sin ejercicio ni fechas queda pendiente de clasificar."""
    ej = ejercicio_registrado(gasto)
    if ej is None:
        return None
    ini = a_fecha(gasto.get("periodo_desde")) or a_fecha(gasto.get("fecha")) or date(ej, 1, 1)
    fin = a_fecha(gasto.get("periodo_hasta")) or (a_fecha(gasto.get("fecha"))
          if not gasto.get("periodo_desde") else None) or date(ej, 12, 31)
    return min(ini, fin), max(ini, fin)


def ejercicios_gasto(gasto: dict) -> set[int]:
    periodo = periodo_gasto(gasto)
    registrados = {ejercicio_registrado(gasto)} - {None}
    return registrados | (set(range(periodo[0].year, periodo[1].year + 1)) if periodo else set())


def importe_gasto_periodo(gasto: dict, ventana: tuple[date, date], alquilado: list[tuple[date, date]],
                          titularidad: float) -> float:
    """Parte del gasto imputable a ``ventana``.

    Convenio (ver arquitectura §Gastos):
    - R 'Importe deducible final' escrito a mano = importe final del cliente (ya con titularidad y prorrateo):
      se reparte por días del periodo del gasto, sin más ajustes.
    - Si R está vacío: N total × O %inmueble (def. 100 %) × P %alquiler × titularidad.
      Si P está vacío se usa la proporción de días alquilados dentro del periodo del gasto.
    - Periodo del gasto: [F, G]; si faltan, el día E; si tampoco, el ejercicio completo.
    """
    periodo = periodo_gasto(gasto)
    if periodo is None:
        return 0.0
    ini, fin = periodo
    total_dias = dias(ini, fin)
    tramo = interseccion((ini, fin), ventana)
    if not tramo or total_dias == 0:
        return 0.0
    if gasto.get("importe_deducible") not in (None, ""):
        return r2(decimal_finito(gasto["importe_deducible"]) * dias(*tramo) / total_dias)
    total = decimal_finito(gasto["total"] if gasto.get("total") not in (None, "") else gasto.get("base") or 0)
    pct_inm = decimal_finito(fraccion(gasto["pct_inmueble"])) if gasto.get("pct_inmueble") not in (None, "") else 1
    if gasto.get("pct_alquiler") not in (None, ""):
        importe = total * pct_inm * decimal_finito(fraccion(gasto["pct_alquiler"])) * dias(*tramo) / total_dias
    else:
        alquilados_tramo = dias_totales(t for a in alquilado if (t := interseccion(a, tramo)))
        importe = total * pct_inm * alquilados_tramo / total_dias
    return r2(importe * decimal_finito(fraccion(titularidad)))


def amortizacion_periodo(inm: dict, cat: Optional[dict], dias_alquilados: int, ejercicio: int,
                         titularidad: float) -> float:
    """3 % anual sobre el mayor entre coste de adquisición y valor catastral, excluido el suelo,
    prorrateado por días alquilados del periodo (art. 14 RIRPF por remisión del TRLIRNR)."""
    if dias_alquilados <= 0:
        return 0.0
    coste = valor_adquisicion_total(inm)
    vc = float((cat or {}).get("valor_catastral") or 0)
    vc_constr = float((cat or {}).get("valor_construccion") or 0)
    if vc <= 0 or vc_constr <= 0:
        return 0.0  # sin desglose suelo/construcción no se puede calcular con seguridad
    base = max(coste, vc) * (vc_constr / vc)
    return r2(base * AMORTIZACION_PCT * dias_alquilados / dias_anio(ejercicio) * titularidad)


@dataclass
class Resultado210Alquiler:
    periodo: str
    desde: date
    hasta: date
    contratos: list[str]
    ingresos: float
    gastos: float
    base: float
    tipo: float
    cuota: float
    detalle: list[str] = field(default_factory=list)
    detalle_limite: dict = field(default_factory=dict)
    ingresos_contrato: Optional[float] = None   # periodo presentado: lo que devengan los contratos (comparación)


CATEGORIAS_LIMITADAS = frozenset(("Intereses/financiación", "Reparación/conservación"))
_CATEGORIAS_LIMITADAS_NORMALIZADAS = frozenset(_sin_tildes(c) for c in CATEGORIAS_LIMITADAS)


@dataclass
class LiquidacionAlquiler:
    resultados: list[Resultado210Alquiler] = field(default_factory=list)
    saldos: dict[int, float] = field(default_factory=dict)
    aplicaciones: dict[int, float] = field(default_factory=dict)
    caducados: dict[int, float] = field(default_factory=dict)
    generado: float = 0.0
    revisar: bool = False


def liquidar_210_alquiler(inm: dict, contratos: list[dict], rentas_por_contrato: dict[str, list[dict]],
                          gastos: list[dict], cat: Optional[dict], res: Residencia, ejercicio: int,
                          periodicidad: str = "Trimestral", arrastres: Optional[dict[int, float]] = None,
                          presentados: Optional[dict[str, dict]] = None) -> LiquidacionAlquiler:
    """Límite conjunto por declaración (anexo del 210): primero saldos previos, luego gastos actuales.

    Solo intereses/financiación y reparación/conservación generan exceso. Cada origen caduca al
    terminar los cuatro ejercicios siguientes. Los saldos ya incluyen la titularidad del cliente.
    El exceso de otra declaración del mismo año no se vuelve a aplicar en este año.
    """
    liquidacion = LiquidacionAlquiler()
    for origen, saldo in (arrastres or {}).items():
        origen, saldo = int(origen), max(0.0, r2(saldo))
        if origen < ejercicio - 4:
            liquidacion.caducados[origen] = saldo
        elif origen < ejercicio and saldo:
            liquidacion.saldos[origen] = saldo
    presentados = presentados or {}
    tenencia = periodo_tenencia(inm, ejercicio)
    if not tenencia and not presentados:
        return liquidacion
    tit = pct_titularidad(inm)
    periodos = trimestres(ejercicio) if periodicidad == "Trimestral" else [("Anual", *anio(ejercicio))]
    # Un cambio de periodicidad no debe liberar saldos de declaraciones ya presentadas.
    if set(presentados) - {p[0] for p in periodos}:
        raise ValueError("La periodicidad del 210 difiere de una declaración presentada. "
                         "Restaura su periodicidad antes de recalcular el arrastre.")
    hay_amortizacion_manual = any(_sin_tildes(g.get("categoria")) == "amortizacion" for g in gastos)
    for etiqueta, p_ini, p_fin in periodos:
        congelada = presentados.get(etiqueta)
        ventana = ((a_fecha(congelada.get("desde")) or p_ini, a_fecha(congelada.get("hasta")) or p_fin)
                   if congelada else (interseccion((p_ini, p_fin), tenencia) if tenencia else None))
        if not ventana:
            continue
        if congelada:
            try:
                meta = json.loads(congelada.get("detalle_limite") or "null")
                if (not isinstance(meta, dict) or meta.get("version") != 1
                        or meta["ingresos"] != congelada.get("ingresos")
                        or meta["gastos"] != congelada.get("gastos")):
                    raise ValueError("Histórico sin desglose verificable")
                aplicaciones = {int(a): r2(v) for a, v in meta["aplicaciones"].items()}
                if any(v < 0 or v > liquidacion.saldos.get(a, 0) for a, v in aplicaciones.items()):
                    raise ValueError("Saldo histórico insuficiente")
                for origen, aplicado in aplicaciones.items():
                    liquidacion.saldos[origen] = r2(liquidacion.saldos[origen] - aplicado)
                    liquidacion.aplicaciones[origen] = r2(liquidacion.aplicaciones.get(origen, 0) + aplicado)
                liquidacion.generado = r2(liquidacion.generado + max(0.0, r2(meta["generado"])))
            except (ValueError, TypeError, KeyError):
                liquidacion.revisar = True
                liquidacion.saldos.clear()
            devengado = sum(renta_devengada(c, rentas_por_contrato.get(c["codigo"], []), *ventana) for c in contratos)
            liquidacion.resultados.append(Resultado210Alquiler(
                etiqueta, ventana[0], ventana[1], [], float(congelada.get("ingresos") or 0),
                float(congelada.get("gastos") or 0), float(congelada.get("base") or 0),
                float(congelada.get("tipo") or res.tipo), float(congelada.get("cuota") or 0),
                ingresos_contrato=r2(devengado * tit)))
            continue
        usados, ingresos_100 = [], 0.0
        for c in contratos:
            importe = renta_devengada(c, rentas_por_contrato.get(c["codigo"], []), *ventana)
            if importe > 0:
                ingresos_100 += importe
                usados.append(c["codigo"])
        ingresos = r2(ingresos_100 * tit)
        alquilado = intervalos_alquilados(contratos, ventana)
        detalle = [f"Titularidad {tit:.0%}"]
        gastos_p, limitado, aplicado_actual, generado = 0.0, 0.0, 0.0, 0.0
        aplicaciones = {}
        if res.deduce_gastos:
            otros = 0.0
            for g in gastos:
                importe = importe_gasto_periodo(g, ventana, alquilado, tit)
                if _sin_tildes(g.get("categoria")) in _CATEGORIAS_LIMITADAS_NORMALIZADAS:
                    limitado += importe
                else:
                    otros += importe
            limitado = max(0.0, r2(limitado))
            disponible = max(0.0, ingresos)
            for origen in sorted(liquidacion.saldos):
                aplicado = min(liquidacion.saldos[origen], disponible)
                if aplicado:
                    aplicaciones[origen] = aplicado
                    liquidacion.aplicaciones[origen] = r2(liquidacion.aplicaciones.get(origen, 0) + aplicado)
                    liquidacion.saldos[origen] = r2(liquidacion.saldos[origen] - aplicado)
                    disponible = r2(disponible - aplicado)
            aplicado_actual = min(limitado, disponible)
            generado = r2(limitado - aplicado_actual)
            liquidacion.generado = r2(liquidacion.generado + generado)
            if not hay_amortizacion_manual:
                amort = amortizacion_periodo(inm, cat, dias_totales(alquilado), ejercicio, tit)
                if amort:
                    detalle.append(f"Amortización 3 % automática: {amort:.2f} €")
                otros += amort
            gastos_p = r2(otros + aplicado_actual + sum(aplicaciones.values()))
            detalle.append(f"Intereses y reparaciones: {aplicado_actual:.2f} € actuales; "
                           f"{sum(aplicaciones.values()):.2f} € anteriores; exceso {generado:.2f} €")
        else:
            detalle.append("Extra-UE o deducción desactivada: sin gastos deducibles")
        if ingresos <= 0:
            continue
        base = r2(max(0.0, ingresos - gastos_p))
        meta = {"version": 1, "ingresos": ingresos, "gastos": gastos_p, "limitado": limitado,
                "aplicado_actual": aplicado_actual, "generado": generado, "aplicaciones": aplicaciones}
        liquidacion.resultados.append(Resultado210Alquiler(
            etiqueta, ventana[0], ventana[1], usados, ingresos, gastos_p, base, res.tipo,
            r2(base * res.tipo), detalle, meta))
    liquidacion.saldos = {a: v for a, v in liquidacion.saldos.items() if v > 0}
    if liquidacion.generado:
        liquidacion.saldos[ejercicio] = liquidacion.generado
    return liquidacion


def calcular_210_alquiler(inm: dict, contratos: list[dict], rentas_por_contrato: dict[str, list[dict]],
                          gastos: list[dict], cat: Optional[dict], res: Residencia, ejercicio: int,
                          periodicidad: str = "Trimestral") -> list[Resultado210Alquiler]:
    """Resultados del ejercicio; el registro interanual se resuelve al recalcular el expediente."""
    return liquidar_210_alquiler(inm, contratos, rentas_por_contrato, gastos, cat, res,
                                ejercicio, periodicidad).resultados


# ============================================================================================ 210 imputación
@dataclass
class Resultado210Imputacion:
    dias: int
    valor_catastral: float     # valor usado (columna E)
    pct_imputacion: float
    pct_titularidad: float
    tipo: float
    renta_imputada: float
    cuota: float
    detalle: list[str] = field(default_factory=list)
    falta_valor: bool = False
    provisional: bool = False


_OBS_AUTO_IMPUTACION = ("Sin valor catastral", "Valor catastral heredado", "Año bisiesto", PENDIENTE_REVISION)


def calcular_210_imputacion(inm: dict, contratos: list[dict], cat: Optional[dict], res: Residencia,
                            ejercicio: int) -> Optional[Resultado210Imputacion]:
    """Imputación de rentas inmobiliarias por los días no alquilados del ejercicio.
    renta = VC × % × titularidad × días / 365 (misma fórmula que la plantilla). Cuota = renta × tipo.
    Sin valor catastral: 1,1 % sobre el 50 % del valor patrimonial (art. 85 LIRPF)."""
    tenencia = periodo_tenencia(inm, ejercicio)
    if not tenencia:
        return None
    dias_vacio = dias(*tenencia) - dias_totales(intervalos_alquilados(contratos, tenencia))
    if dias_vacio <= 0:
        return None
    tit = pct_titularidad(inm)
    detalle = []
    vc = float((cat or {}).get("valor_catastral") or 0)
    if vc > 0:
        pct = pct_imputacion((cat or {}).get("anio_revision"), ejercicio, (cat or {}).get("pct_imputacion"))
        if cat and cat.get("_heredado"):
            detalle.append(f"Valor catastral heredado de {cat['_heredado']}: falta Catastro/IBI {ejercicio}")
    else:
        vp = valor_patrimonial(inm, cat)
        vc = r2(vp.valor * 0.5)
        pct = PCT_IMPUTACION_REVISADO
        detalle.append("Sin valor catastral: 1,1 % sobre 50 % del mayor valor")
    provisional = revision_pendiente(cat)
    if provisional:
        detalle.append(PENDIENTE_REVISION + ": cálculo provisional; confirma el año o el porcentaje manual.")
    if calendar.isleap(ejercicio):
        detalle.append("Año bisiesto: la plantilla divide entre 365")
    renta = r2(vc * pct * tit * dias_vacio / DIAS_ANIO_PLANTILLA)
    return Resultado210Imputacion(dias_vacio, r2(vc), pct, tit, res.tipo, renta, r2(renta * res.tipo), detalle,
                                  falta_valor=vc <= 0, provisional=provisional)


# ============================================================================================ Patrimonio / ITS
def aplicar_tarifa(base: float, tarifa=TARIFA_IP_ESTATAL) -> float:
    if base <= 0:
        return 0.0
    desde, cuota, tipo = [t for t in tarifa if base > t[0]][-1]
    return r2(cuota + (base - desde) * tipo)


@dataclass
class ResultadoPatrimonio:
    num_inmuebles: int
    valor_bienes: float
    deudas: float
    base: float
    minimo_exento: float
    base_liquidable: float
    cuota_integra: float
    bonificacion: float
    resultado: float
    obligado: bool
    detalle_inmuebles: list[tuple[str, ValorPatrimonial, float, float]]  # (codigo, valores, tit, computado)

    @property
    def mensaje(self) -> str:
        pisos = "piso" if self.num_inmuebles == 1 else "pisos"
        return (f"Tiene {self.num_inmuebles} {pisos}, suma {fmt_eur(self.valor_bienes)} de base y "
                f"{'SÍ' if self.obligado else 'NO'} está obligado a presentar Impuesto sobre el Patrimonio")


def calcular_patrimonio(inmuebles: list[dict], catastros: dict[str, Optional[dict]], ejercicio: int,
                        deudas: float = 0.0, bonificacion: float = 0.0,
                        minimo_exento: float = IP_MINIMO_EXENTO) -> ResultadoPatrimonio:
    """Obligación real: bienes en España a 31/12. Valor = mayor de los 3 valores × titularidad.
    Obligado a declarar si resultado > 0 o si el valor de los bienes supera 2.000.000 €.
    ``bonificacion``: importe de la bonificación autonómica (col. L, manual). VERIFICAR normativa CCAA aplicable."""
    detalle = []
    for inm in inmuebles:
        if not titular_a_31_diciembre(inm, ejercicio):
            continue
        vp = valor_patrimonial(inm, catastros.get(inm["codigo"]))
        tit = pct_titularidad(inm)
        detalle.append((inm["codigo"], vp, tit, r2(vp.valor * tit)))
    valor = r2(sum(d[3] for d in detalle))
    base = r2(max(0.0, valor - (deudas or 0)))
    base_liq = r2(max(0.0, base - minimo_exento))
    cuota = aplicar_tarifa(base_liq)
    resultado = r2(max(0.0, cuota - (bonificacion or 0)))
    obligado = resultado > 0 or valor > IP_UMBRAL_DECLARAR_BIENES
    return ResultadoPatrimonio(len(detalle), valor, r2(deudas or 0), base, minimo_exento, base_liq, cuota,
                               r2(bonificacion or 0), resultado, obligado, detalle)


def calcular_its(base_liquidable: float, cuota_ip_pagada: float) -> tuple[float, float, float]:
    """(cuota ITS, deducción por cuota IP, resultado). Tributa la base liquidable que excede de 3 M€."""
    cuota = aplicar_tarifa(base_liquidable, TARIFA_ITS)
    deduccion = r2(min(cuota, cuota_ip_pagada or 0))
    return cuota, deduccion, r2(max(0.0, cuota - deduccion))


# ============================================================================================ venta
@dataclass
class ResultadoVenta:
    precio_venta: float
    valor_adquisicion: float
    amortizaciones: float
    gastos_venta: float
    ganancia: float          # ya con titularidad
    cuota: float
    retencion: float         # 3 % retenido por el comprador
    resultado: float         # cuota − retención (negativo = a devolver)
    fecha_limite: date


def calcular_venta(inm: dict, amortizaciones: float = 0.0) -> Optional[ResultadoVenta]:
    """210 de ganancia patrimonial por venta. ganancia = (venta − gastos venta) − (adquisición − amortizaciones)."""
    fv = a_fecha(inm.get("fecha_venta"))
    if not fv or not inm.get("precio_venta"):
        return None
    tit = pct_titularidad(inm)
    pv = float(inm["precio_venta"])
    adq = valor_adquisicion_total(inm)
    gv = float(inm.get("gastos_venta") or 0)
    # ``amortizaciones`` = suma de R (importe final del cliente, ya con titularidad): no se vuelve a multiplicar.
    ganancia = r2((pv - gv) * tit - (adq * tit - amortizaciones))
    cuota = r2(max(0.0, ganancia) * TIPO_GANANCIA_PATRIMONIAL)
    retencion = r2(pv * tit * RETENCION_VENTA)
    return ResultadoVenta(pv, adq, r2(amortizaciones), gv, ganancia, cuota, retencion, r2(cuota - retencion),
                          plazo_210_venta(fv))


# ============================================================================================ asesor interanual
@dataclass
class Alerta:
    nivel: str          # 'info' | 'aviso' | 'grave'
    inmueble: Optional[str]
    tipo: str           # clave corta: 'contrato', 'renta', 'catastro', 'residencia', ...
    mensaje: str


def fmt_eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _contrato_principal(contratos: list[dict], ventana: tuple[date, date]) -> Optional[dict]:
    """El contrato con más días dentro de la ventana."""
    mejor, mejor_dias = None, 0
    for c in contratos:
        iv = intervalo_contrato(c)
        t = iv and interseccion(iv, ventana)
        if t and dias(*t) > mejor_dias:
            mejor, mejor_dias = c, dias(*t)
    return mejor


def _inquilinos(db: Database, cid: int, contrato: str) -> str:
    nombres = [i["nombre"] for i in db.listar("inquilinos", cid, contrato=contrato) if i.get("nombre")]
    return ", ".join(nombres) or "inquilino sin registrar"


def comparar_interanual(db: Database, cid: int, ejercicio: int) -> list[Alerta]:
    """Compara el ejercicio con el anterior y devuelve alertas para el asesor."""
    alertas: list[Alerta] = []
    prev = ejercicio - 1
    r_act, r_prev = db.residencia(cid, ejercicio), db.residencia(cid, prev)
    if r_act and r_prev and r_act.get("ejercicio") == ejercicio and r_prev.get("ejercicio") == prev \
            and _sin_tildes(r_act.get("pais") or "") != _sin_tildes(r_prev.get("pais") or ""):
        alertas.append(Alerta("aviso", None, "residencia",
                              f"Cambio de residencia fiscal: {r_prev.get('pais')} ({prev}) → {r_act.get('pais')} "
                              f"({ejercicio}). Verificar certificado y tipo de gravamen."))
    if not r_act or r_act.get("ejercicio") != ejercicio:
        alertas.append(Alerta("aviso", None, "residencia",
                              f"Falta la residencia fiscal de {ejercicio}"
                              + (f": se usa la de {r_act['ejercicio']}." if r_act else ": se asume Extra-UE (24 %).")))

    for inm in db.listar("inmuebles", cid):
        if not periodo_tenencia(inm, ejercicio):
            continue
        cod = inm["codigo"]
        contratos = db.listar("contratos", cid, inmueble=cod)
        c_prev = _contrato_principal(contratos, anio(prev))
        c_act = _contrato_principal(contratos, anio(ejercicio))
        if c_prev:
            rentas_prev = db.listar("rentas", cid, contrato=c_prev["codigo"])
            iv = intervalo_contrato(c_prev)
            dia_ref = min(iv[1], date(prev, 12, 31))
            renta_prev = renta_mensual_en(c_prev, rentas_prev, dia_ref)
            inq_prev = _inquilinos(db, cid, c_prev["codigo"])
            base = f"El año pasado este inmueble tenía contrato de {fmt_eur(renta_prev)}/mes con {inq_prev}."
            if not c_act:
                alertas.append(Alerta("grave", cod, "contrato",
                                      f"{base} En {ejercicio} no hay contrato: verificar si estuvo vacío "
                                      f"(imputación de rentas) o falta aportar el contrato."))
            else:
                rentas_act = db.listar("rentas", cid, contrato=c_act["codigo"])
                iv_a = intervalo_contrato(c_act)
                dia_act = max(iv_a[0], date(ejercicio, 1, 1))
                renta_act = renta_mensual_en(c_act, rentas_act, dia_act)
                inq_act = _inquilinos(db, cid, c_act["codigo"])
                if abs(renta_act - renta_prev) > max(1.0, 0.005 * renta_prev):
                    alertas.append(Alerta("aviso", cod, "renta",
                                          f"{base} En {ejercicio} figura {fmt_eur(renta_act)}/mes con {inq_act}. "
                                          f"Verificar discrepancia (¿actualización IPC, anexo o error?)."))
                elif _sin_tildes(inq_act) != _sin_tildes(inq_prev):
                    alertas.append(Alerta("aviso", cod, "inquilino",
                                          f"{base} En {ejercicio} el inquilino es {inq_act}. Verificar discrepancia."))
        cat_act = db.catastro(cid, cod, ejercicio)
        cat_prev = db.catastro(cid, cod, prev)
        if cat_act and cat_act.get("_heredado"):
            alertas.append(Alerta("aviso", cod, "catastro",
                                  f"Falta Catastro/IBI {ejercicio}: se usa el valor catastral de {cat_act['_heredado']}."))
        elif cat_act and cat_prev and not cat_prev.get("_heredado"):
            v1, v0 = float(cat_act.get("valor_catastral") or 0), float(cat_prev.get("valor_catastral") or 0)
            if v0 and abs(v1 - v0) / v0 > 0.10:
                alertas.append(Alerta("aviso", cod, "catastro",
                                      f"El valor catastral cambia más de un 10 %: {fmt_eur(v0)} ({prev}) → "
                                      f"{fmt_eur(v1)} ({ejercicio}). ¿Revisión catastral? Revisar % de imputación."))
        elif not cat_act:
            alertas.append(Alerta("grave", cod, "catastro", f"No hay ningún valor catastral para {cod}."))
    return alertas


# ============================================================================================ orquestador
@dataclass
class InformeFiscal:
    ejercicio: int
    residencia: Residencia
    patrimonio: ResultadoPatrimonio
    its: tuple[float, float, float]
    filas_210_alquiler: list[str] = field(default_factory=list)
    filas_210_imputacion: list[str] = field(default_factory=list)
    alertas: list[Alerta] = field(default_factory=list)
    estados: list[str] = field(default_factory=list)  # control de estado legible para la UI
    pendientes: list["Pendiente"] = field(default_factory=list)  # documentos y declaraciones por hacer
    alquileres_bloqueados: dict[str, str] = field(default_factory=dict)

    @property
    def mensaje_patrimonio(self) -> str:
        return self.patrimonio.mensaje


def _presentado(fila: Optional[dict], campo: str = "estado") -> bool:
    return bool(fila) and fila.get(campo) == "Presentado"


ESTADOS_AUTOMATICOS = ("Pendiente", "No procede", "Falta documentación")


def _estado_auto(previa: Optional[dict], nuevo: str) -> str:
    """Estado que pone la calculadora. Si el usuario puso uno manual (En preparación, Revisar, Presentado), se respeta."""
    actual = (previa or {}).get("estado")
    return nuevo if actual in (None, "", *ESTADOS_AUTOMATICOS) else actual


def _gasto_toca_ejercicio(g: dict, ejercicio: int) -> bool:
    """El gasto entra en el ejercicio si su periodo económico [F,G] (o su fecha, o su ejercicio) lo cruza."""
    periodo = periodo_gasto(g)
    return bool(periodo and interseccion(periodo, anio(ejercicio)))


NOTA_PAGO_NRC = "Pagado tomado de la cuota por constar NRC"


def pago_inferido_por_nrc(fila: dict) -> Optional[float]:
    """Completa un pago ausente, sin validar el NRC con el banco ni recalcular lo presentado."""
    if fila.get("estado") != "Presentado" or fila.get("pagado") not in (None, ""):
        return None
    nrc = fila.get("nrc")
    if (not isinstance(nrc, str) or not nrc.strip().isascii()
            or not re.fullmatch(r"[A-Z0-9]{22}", nrc.strip().upper(), flags=re.ASCII)):
        return None
    cuota = fila.get("cuota")
    if type(cuota) not in (int, float) or not math.isfinite(cuota) or cuota <= 0:
        return None
    return r2(cuota)


def _anadir_nota_nrc(texto: Optional[str]) -> str:
    texto = texto or ""
    return texto if NOTA_PAGO_NRC in texto else (texto + "; " if texto else "") + NOTA_PAGO_NRC


def completar_pago_210_nrc(db: Database, cid: int, tabla: str, codigo: str) -> dict:
    if tabla not in ("m210_alquiler", "m210_imputacion"):
        raise ValueError("El pago por NRC solo se completa en modelos 210.")
    fila = db.get(tabla, cid, codigo) or {}
    importe = pago_inferido_por_nrc(fila)
    if importe is not None:
        db.upsert(tabla, cid, {"codigo": codigo, "pagado": importe,
                              "observaciones": _anadir_nota_nrc(fila.get("observaciones"))},
                  solo_si_vacio=("pagado",))
        fila = db.get(tabla, cid, codigo) or fila
    return fila


def _obligacion(db: Database, cid: int, ejercicio: int, tipo: str, inmueble: Optional[str], clave: str,
                fecha_limite: date, id_modelo: Optional[str], estado: str, notas: str = "",
                pagado: Optional[float] = None, fecha_presentacion=None) -> str:
    previa = db.get_por_clave("obligaciones", cid, clave)
    if tipo == "210 Alquiler" and id_modelo:
        # La identidad es el modelo, aunque la clave histórica diga OA/0A.
        vinculadas = db.listar("obligaciones", cid, ejercicio=ejercicio, tipo=tipo,
                              inmueble=inmueble, id_modelo=id_modelo)
        if vinculadas:
            previa = next((f for f in vinculadas if _presentado(f)), vinculadas[0])
            clave = previa.get("clave") or clave
    es_210 = tipo in ("210 Alquiler", "210 Imputación")
    nota_nrc = False
    if es_210 and id_modelo:
        tabla = "m210_alquiler" if tipo == "210 Alquiler" else "m210_imputacion"
        modelo = completar_pago_210_nrc(db, cid, tabla, id_modelo)
        if pagado in (None, ""):
            pagado = modelo.get("pagado")
        nota_nrc = NOTA_PAGO_NRC in (modelo.get("observaciones") or "")
    if _presentado(previa):
        if es_210 and previa.get("pagado") in (None, "") and pagado not in (None, ""):
            cambios = {"codigo": previa["codigo"], "pagado": pagado}
            if nota_nrc:
                cambios["notas"] = _anadir_nota_nrc(previa.get("notas"))
            db.upsert("obligaciones", cid, cambios, solo_si_vacio=("pagado",))
        return previa["codigo"]
    if nota_nrc:
        notas = _anadir_nota_nrc(notas)
    datos = {"ejercicio": ejercicio, "tipo": tipo, "inmueble": inmueble, "fecha_limite": fecha_limite,
             "id_modelo": id_modelo, "notas": notas or None}
    if previa:
        datos["codigo"] = previa["codigo"]
    if not _presentado(previa):
        datos.update(estado=estado, pagado=pagado, fecha_presentacion=fecha_presentacion)
    return db.upsert("obligaciones", cid, datos, clave=clave, solo_si_vacio=("pagado",) if es_210 else ())


def _generar_cobros(db: Database, cid: int, ejercicio: int, inm: dict, contratos: list[dict],
                    rentas_por_contrato: dict[str, list[dict]]) -> None:
    """Una fila de 07_COBROS por contrato y mes con el importe contractual. Cobrado (I) y estado (K) son manuales."""
    tenencia = periodo_tenencia(inm, ejercicio)
    for c in contratos:
        for mes in range(1, 13):
            ini = date(ejercicio, mes, 1)
            fin = date(ejercicio, mes, calendar.monthrange(ejercicio, mes)[1])
            tramo = tenencia and interseccion((ini, fin), tenencia)
            importe = renta_devengada(c, rentas_por_contrato.get(c["codigo"], []), *tramo) if tramo else 0.0
            clave = f"COB|{c['codigo']}|{ejercicio}-{mes:02d}"
            if importe <= 0:
                previa = db.get_por_clave("cobros", cid, clave)
                # Fila automática que ya no corresponde (contrato rescindido o recortado) y sin nada cobrado: fuera.
                if previa and not previa.get("importe_cobrado") and not previa.get("fecha_cobro") \
                        and (previa.get("estado") or "Pendiente") == "Pendiente":
                    db.borrar("cobros", cid, previa["codigo"])
                if not previa or not db.get("cobros", cid, previa["codigo"]):
                    continue
            db.upsert("cobros", cid, {"ejercicio": ejercicio, "inmueble": inm["codigo"], "contrato": c["codigo"],
                                      "periodo_desde": ini, "periodo_hasta": fin, "importe_contractual": importe,
                                      "estado": "Pendiente"}, clave=clave, solo_si_vacio=("estado",))


class ConflictoHistorial210(ValueError):
    """Histórico ambiguo: se suspende solo el alquiler del inmueble afectado."""


class ConflictoDuplicados210(ConflictoHistorial210):
    """Dos registros con la misma identidad fiscal requieren conciliación."""


def _conflictos_modelos_210(modelos: list[dict], ejercicio: int) -> dict[str, str]:
    grupos = {}
    presentados, conflictos = {}, {}
    for fila in sorted(modelos, key=lambda f: (int(f.get("ejercicio") or 0), f["codigo"])):
        ej = int(fila.get("ejercicio") or 0)
        periodo = periodo_210_o_none(fila.get("periodo"))
        if not 1900 <= ej <= ejercicio or fila.get('estado') == 'No procede':
            continue
        inmueble = fila.get('inmueble')
        if _presentado(fila):
            presentados.setdefault((inmueble, ej), set()).add(periodo)
            if not periodo:
                conflictos.setdefault(inmueble, f"{inmueble}, ejercicio {ej}: periodo no reconocido en presentado "
                                      f"{fila['codigo']}. Revisa el histórico antes de reutilizar arrastres.")
        if not periodo:
            continue
        tipo = "presentados" if _presentado(fila) else "borradores"
        grupos.setdefault((fila.get("inmueble"), ej, periodo, tipo), []).append(fila["codigo"])
    for (inmueble, ej, periodo, tipo), codigos in grupos.items():
        if len(codigos) > 1:
            conflictos.setdefault(inmueble, f"{inmueble}, ejercicio {ej}, periodo {periodo}: modelos {tipo} "
                                  f"duplicados ({', '.join(codigos)}). Revisa el histórico; no se recalculan "
                                  "el alquiler ni los arrastres dependientes de este inmueble.")
    for (inmueble, ej), periodos in presentados.items():
        if 'Anual' in periodos and len(periodos) > 1:
            conflictos.setdefault(inmueble, f"{inmueble}, ejercicio {ej}: declaraciones presentadas con "
                                  "periodicidades incompatibles. Revisa el histórico y sus arrastres.")
    return conflictos


def conflictos_210_alquiler(db: Database, cid: int, ejercicio: int) -> dict[str, str]:
    """Conflictos vivos, también al reabrir o exportar sin una nueva sesión de cálculo."""
    return _conflictos_modelos_210(db.listar("m210_alquiler", cid), int(ejercicio))


def _liquidacion_con_historia(db: Database, cid: int, inm: dict, contratos: list[dict],
                             rentas: dict[str, list[dict]], ejercicio: int,
                             periodicidad: str) -> LiquidacionAlquiler:
    """Reproduce los saldos desde su origen: nunca descuenta de un saldo ya descontado.

    Los ejercicios anteriores se simulan sin crear declaraciones ni obligaciones históricas.
    La residencia se consulta en cada ejercicio. Sin desglose de un presentado no se presupone
    saldo reutilizable; la revisión permanece visible durante su horizonte de cuatro años.
    """
    cod = inm["codigo"]
    gastos = db.listar("gastos", cid, inmueble=cod)
    modelos = db.listar("m210_alquiler", cid, inmueble=cod)
    conflicto = _conflictos_modelos_210(modelos, ejercicio).get(cod)
    if conflicto:
        raise ConflictoDuplicados210(conflicto)
    saldos_guardados = db.listar("arrastres_gastos", cid, inmueble=cod)
    ejercicios = [ejercicio]
    for g in gastos:
        ejercicios.extend(a for a in ejercicios_gasto(g) if 1900 <= a <= ejercicio)
    ejercicios.extend(int(f["ejercicio"]) for f in modelos + saldos_guardados
                      if f.get("ejercicio") and 1900 <= int(f["ejercicio"]) <= ejercicio)
    saldos = {}
    revisar_hasta = 0
    for ej in range(min(ejercicios), ejercicio + 1):
        # 0A, OA y Anual son el mismo periodo (importaciones antiguas guardaban el código del modelo)
        presentados = {}
        for f in modelos:
            if f.get("ejercicio") != ej or not _presentado(f):
                continue
            periodo = periodo_210_o_none(f.get("periodo")) or f.get("periodo")
            if periodo in presentados:
                raise ConflictoDuplicados210(f"{cod}, ejercicio {ej}: modelos presentados duplicados para {periodo}. "
                                 "Revisa el histórico antes de continuar.")
            presentados[periodo] = f
        # La preferencia global no cambia el calendario de un histórico ya presentado.
        # Resolver por inmueble y año permite carteras con declaraciones de ambos tipos.
        periodos = set(presentados)
        if periodos and not (periodos <= {"1T", "2T", "3T", "4T"} or periodos == {"Anual"}):
            raise ValueError(f"{cod}, ejercicio {ej}: hay declaraciones presentadas con periodicidades incompatibles. "
                             "Revisa el histórico antes de continuar.")
        periodicidad_ej = ("Anual" if periodos == {"Anual"} else
                           "Trimestral" if periodos else periodicidad_210_alquiler(db, cid, cod, ej))
        liquidacion = liquidar_210_alquiler(
            inm, contratos, rentas, [g for g in gastos if _gasto_toca_ejercicio(g, ej)],
            db.catastro(cid, cod, ej), resolver_residencia(db.residencia(cid, ej)), ej,
            periodicidad_ej, saldos, presentados)
        if liquidacion.revisar:
            revisar_hasta = ej + 4
        saldos = liquidacion.saldos
        liquidacion.revisar = revisar_hasta >= ej
        observaciones = ("Histórico presentado sin desglose verificable: revisar saldos antes de reutilizarlos."
                         if liquidacion.revisar else
                         "Saldos recalculados por origen; los ejercicios pendientes son provisionales.")
        db.upsert("arrastres_gastos", cid, {
            "inmueble": cod, "ejercicio": ej, "generado": liquidacion.generado,
            "aplicado": r2(sum(liquidacion.aplicaciones.values())), "pendiente": r2(sum(saldos.values())),
            "saldos": json.dumps(saldos, sort_keys=True),
            "aplicaciones": json.dumps(liquidacion.aplicaciones, sort_keys=True),
            "caducados": json.dumps(liquidacion.caducados, sort_keys=True),
            "estado": "Revisar" if liquidacion.revisar else "Calculado", "observaciones": observaciones},
            clave=f"ARR|{cod}|{ej}")
    return liquidacion


def recalcular_cliente(db: Database, cid: int, ejercicio: int) -> InformeFiscal:
    """Recalcula todas las obligaciones de no residente del ejercicio y las guarda en BD."""
    res = resolver_residencia(db.residencia(cid, ejercicio))
    fila_res = db.residencia(cid, ejercicio)
    if fila_res and fila_res.get("ejercicio") == ejercicio:
        # Extra-UE nunca deduce (A2-P02): se fuerza "No". En UE se respeta un "No" manual.
        db.upsert("residencias", cid, {"codigo": fila_res["codigo"], "grupo": res.grupo,
                                       "gastos_deducibles": "Sí" if res.deduce_gastos else "No"},
                  solo_si_vacio=() if res.grupo == GRUPO_EXTRA else ("gastos_deducibles",))
    informe_estados: list[str] = []
    filas_a, filas_i = [], []
    inmuebles = db.listar("inmuebles", cid)
    catastros: dict[str, Optional[dict]] = {}
    discrepancias: list[Alerta] = []   # declaraciones presentadas que no cuadran con el cálculo de la app
    alquileres_bloqueados = {}

    for inm in inmuebles:
        cod = inm["codigo"]
        periodicidad = periodicidad_210_alquiler(db, cid, cod, ejercicio)
        cat = db.catastro(cid, cod, ejercicio)
        catastros[cod] = cat
        adquisicion = a_fecha(inm.get("fecha_adquisicion"))
        referencia = float(inm.get("valor_referencia") or 0)
        base_compra = float(inm.get("base_impuesto_adquisicion") or 0)
        uso_referencia = _sin_tildes(str(inm.get("uso_valor_referencia") or ""))
        if (adquisicion and adquisicion >= date(2022, 1, 1)
                and referencia > float(inm.get("precio_escritura") or 0)
                and uso_referencia != "no"
                and ((base_compra <= 0 and uso_referencia != "si")
                     or (base_compra > 0 and abs(base_compra - referencia) > 0.01))):
            discrepancias.append(Alerta(
                "aviso", cod, "referencia adquisición",
                "Valor de referencia: falta confirmar o hay contradicción con la base del impuesto de adquisición. "
                "Se respetan los datos explícitos del asesor; revisa la valoración de Patrimonio · VERIFICAR"))
        contratos = db.listar("contratos", cid, inmueble=cod)
        rentas = {c["codigo"]: db.listar("rentas", cid, contrato=c["codigo"]) for c in contratos}
        gastos = [g for g in db.listar("gastos", cid, inmueble=cod) if _gasto_toca_ejercicio(g, ejercicio)]
        filas_modelos = db.listar("m210_alquiler", cid, inmueble=cod, ejercicio=ejercicio)
        invalidos = [f for f in filas_modelos if not periodo_210_o_none(f.get("periodo"))]
        for f in invalidos:
            discrepancias.append(Alerta(
                "grave", cod, f"periodo 210 {f['codigo']}",
                f"{f['codigo']}: periodo no reconocido ({f.get('periodo') or 'vacío'}). "
                "Se conservan sus importes; corrige el periodo antes de dar por cerrado el cálculo."))
        _generar_cobros(db, cid, ejercicio, inm, contratos, rentas)

        # ---- 210 rendimientos
        inicio_filas, inicio_estados, inicio_alertas = len(filas_a), len(informe_estados), len(discrepancias)
        try:
            with db.transaccion():
                liquidacion = _liquidacion_con_historia(db, cid, inm, contratos, rentas, ejercicio, periodicidad)
                if liquidacion.revisar:
                    informe_estados.append(f"{cod} · REVISAR arrastre: declaración presentada sin desglose verificable")
                for r in liquidacion.resultados:
                    clave = f"210A|{ejercicio}|{cod}|{r.periodo}"
                    previa = db.get_por_clave("m210_alquiler", cid, clave)
                    if previa and not periodo_210_o_none(previa.get("periodo")):
                        informe_estados.append(f"{cod} · REVISAR 210 {r.periodo}: periodo no reconocido en {previa['codigo']}")
                        continue
                    if not _presentado(previa):
                        # Un presentado importado con otra clave («0A», «OA») es el mismo periodo: no se duplica
                        equivalentes = [f for f in filas_modelos
                                        if periodo_210_o_none(f.get("periodo")) == r.periodo]
                        previa = next((f for f in equivalentes if _presentado(f)), previa)
                        if previa is None and equivalentes:
                            if len(equivalentes) > 1:
                                raise ConflictoDuplicados210(f"{cod}, ejercicio {ejercicio}: borradores duplicados para {r.periodo}. "
                                                 "Revisa sus datos antes de continuar.")
                            previa = equivalentes[0]
                    if _presentado(previa):
                        codigo = previa["codigo"]
                        base_pres = float(previa.get("base") or 0)
                        # Sin ingresos guardados solo vale la base como ingresos si no hay gastos (Extra-UE)
                        ing_pres = (float(previa["ingresos"]) if previa.get("ingresos") not in (None, "") else
                                    base_pres if not res.deduce_gastos else None)
                        ing_calc = r.ingresos_contrato
                        gastos_reg = previa.get("gastos")
                        esperada = (max(0.0, r2(float(previa["ingresos"]) - float(gastos_reg)))
                                    if previa.get("ingresos") not in (None, "") and gastos_reg not in (None, "") else None)
                        if esperada is not None and abs(base_pres - esperada) > 1:
                            discrepancias.append(Alerta(
                                "aviso", cod, f"base 210 {r.periodo}",
                                f"210 alquiler {r.periodo} {ejercicio} presentado con base {fmt_eur(base_pres)}; los ingresos "
                                f"({fmt_eur(float(previa['ingresos']))}) menos los gastos ({fmt_eur(float(gastos_reg))}) del periodo "
                                f"dan {fmt_eur(esperada)} (diferencia {fmt_eur(esperada - base_pres)}). Revisa la declaración · VERIFICAR"))
                        if ing_calc is not None and ing_pres is not None and abs(ing_pres - ing_calc) > 1:
                            discrepancias.append(Alerta(
                                "aviso", cod, f"presentado 210 {r.periodo}",
                                f"210 alquiler {r.periodo} {ejercicio} presentado con ingresos de {fmt_eur(ing_pres)}; los "
                                f"contratos devengan {fmt_eur(ing_calc)} (diferencia {fmt_eur(ing_calc - ing_pres)}). Revisa si faltan "
                                "rentas o se restaron gastos o descuentos del inquilino"
                                + (" (Extra-UE no deduce gastos)" if res.grupo == GRUPO_EXTRA else "") + " · VERIFICAR"))
                    else:
                        codigo = db.upsert("m210_alquiler", cid, {
                            "codigo": previa["codigo"] if previa else None,
                            "ejercicio": ejercicio, "inmueble": cod, "contrato": ", ".join(r.contratos), "periodo": r.periodo,
                            "desde": r.desde, "hasta": r.hasta, "pais": res.pais or res.grupo, "ingresos": r.ingresos,
                            "gastos": r.gastos, "base": r.base, "tipo": r.tipo, "cuota": r.cuota,
                            "estado": _estado_auto(previa, "Pendiente"),
                            "observaciones": "; ".join(r.detalle),
                            "detalle_limite": json.dumps(r.detalle_limite, sort_keys=True)}, clave=clave,
                            solo_si_vacio=("observaciones",) if previa and previa.get("estado")
                            not in (None, "", *ESTADOS_AUTOMATICOS) else ())
                    fila = db.get("m210_alquiler", cid, codigo)
                    filas_a.append(codigo)
                    _obligacion(db, cid, ejercicio, "210 Alquiler", cod, f"OBL|{clave}", plazo_210_alquiler(ejercicio, r.periodo),
                                codigo, fila.get("estado") or "Pendiente", f"210 rendimientos {r.periodo} {ejercicio}",
                                fila.get("pagado"), fila.get("fecha_presentacion"))
                    informe_estados.append(f"{cod} · 210 Arrendamientos {r.periodo}/{ejercicio}: {fila.get('estado')}"
                                           f" · cuota {fmt_eur(fila.get('cuota') or 0)}")

                # ---- 210 rendimientos obsoletos (contrato cancelado, venta...): se anulan si no están presentados
                vigentes = set(filas_a)
                for viejo in db.listar("m210_alquiler", cid, ejercicio=ejercicio, inmueble=cod):
                    if (viejo["codigo"] in vigentes or _presentado(viejo)
                            or not periodo_210_o_none(viejo.get("periodo"))
                            or not (viejo.get("clave") or "").startswith("210A|")):
                        continue
                    db.upsert("m210_alquiler", cid, {"codigo": viejo["codigo"], "ingresos": 0, "gastos": 0, "base": 0,
                                                     "cuota": 0, "estado": _estado_auto(viejo, "No procede")})
                    fila_v = db.get("m210_alquiler", cid, viejo["codigo"])
                    _obligacion(db, cid, ejercicio, "210 Alquiler", cod, f"OBL|{viejo['clave']}",
                                plazo_210_alquiler(ejercicio, viejo.get("periodo") or ""), viejo["codigo"],
                                fila_v.get("estado"), "Sin ingresos en el periodo tras recalcular")
        except ConflictoHistorial210 as exc:
            # El savepoint revierte exclusivamente las escrituras de alquiler de este inmueble.
            del filas_a[inicio_filas:]
            del informe_estados[inicio_estados:]
            del discrepancias[inicio_alertas:]
            alquileres_bloqueados[cod] = str(exc)
            discrepancias.append(Alerta("grave", cod, "conflicto 210 alquiler", str(exc)))
            informe_estados.append(f"{cod} · PROVISIONAL: 210 alquiler {ejercicio} pendiente de revisión; "
                                   "histórico ambiguo y arrastres sin recalcular")

        # ---- 210 imputación
        clave_i = f"210I|{ejercicio}|{cod}"
        previa_i = db.get_por_clave("m210_imputacion", cid, clave_i)
        imp = calcular_210_imputacion(inm, contratos, cat, res, ejercicio)
        if imp and _presentado(previa_i) and abs(float(previa_i.get("renta_imputada") or 0) - imp.renta_imputada) > 1:
            discrepancias.append(Alerta(
                "aviso", cod, "presentado 210 imputación",
                f"210 imputación {ejercicio} presentado con renta imputada "
                f"{fmt_eur(float(previa_i.get('renta_imputada') or 0))}; la app calcula {fmt_eur(imp.renta_imputada)} "
                f"({imp.dias} días vacíos al {imp.pct_imputacion * 100:g} %). Revisa los días y el porcentaje · VERIFICAR"))
        if imp and not _presentado(previa_i):
            codigo_i = db.upsert("m210_imputacion", cid, {
                "ejercicio": ejercicio, "inmueble": cod, "dias": imp.dias, "valor_catastral": imp.valor_catastral,
                "pct_imputacion": imp.pct_imputacion, "pct_titularidad": imp.pct_titularidad, "tipo": imp.tipo,
                "renta_imputada": imp.renta_imputada, "cuota": imp.cuota,
                "estado": _estado_auto(previa_i, "Falta documentación" if imp.falta_valor or imp.provisional else "Pendiente"),
                "observaciones": "; ".join(imp.detalle) or None}, clave=clave_i)
            # Una nota automática de un cálculo anterior (p. ej. "Sin valor catastral") ya no aplica: se quita.
            # Las notas escritas a mano se conservan.
            if not imp.detalle and str((previa_i or {}).get("observaciones") or "").startswith(_OBS_AUTO_IMPUTACION):
                db.vaciar_campo("m210_imputacion", cid, codigo_i, "observaciones")
        elif not imp and previa_i and not _presentado(previa_i):
            codigo_i = db.upsert("m210_imputacion", cid, {"codigo": previa_i["codigo"], "dias": 0,
                                                            "renta_imputada": 0, "cuota": 0,
                                                            "estado": _estado_auto(previa_i, "No procede")})
        else:
            codigo_i = previa_i["codigo"] if previa_i else None
        if codigo_i:
            fila_i = db.get("m210_imputacion", cid, codigo_i)
            filas_i.append(codigo_i)
            _obligacion(db, cid, ejercicio, "210 Imputación", cod, f"OBL|{clave_i}", plazo_210_imputacion(ejercicio),
                        codigo_i, fila_i.get("estado") or "Pendiente", f"Imputación {fila_i.get('dias')} días",
                        fila_i.get("pagado"), fila_i.get("fecha_presentacion"))
            if fila_i.get("estado") not in ("Presentado", "No procede"):
                motivo = ("sin valor catastral" if imp and imp.falta_valor else
                          "revisión catastral sin acreditar" if imp and imp.provisional else None)
                # La cuota va delante: el resumen recorta las líneas largas.
                informe_estados.append(f"{cod} · FALTA por presentar 210 Imputación {ejercicio} · cuota "
                                       f"{fmt_eur(fila_i.get('cuota') or 0)}"
                                       + (f" · PROVISIONAL: {motivo}" if motivo else "")
                                       + f" · {fila_i.get('dias')} días vacíos")
            else:
                informe_estados.append(f"{cod} · 210 Imputación {ejercicio}: {fila_i.get('estado')}")

        # ---- compra: impuesto de adquisición pendiente
        adq = a_fecha(inm.get("fecha_adquisicion"))
        if adq and adq.year == ejercicio:
            liquidado = inm.get("itp_iva_ajd") not in (None, "", 0, 0.0)
            _obligacion(db, cid, ejercicio, "Otra", cod, f"COMPRA|{cod}", adq + timedelta(days=30), None,
                        "Revisar" if liquidado else "Pendiente",
                        "Impuesto de compra (ITP o IVA+AJD): " + ("importe registrado, comprobar liquidación"
                                                                   if liquidado else "liquidación pendiente de acreditar "
                                                                   "(sube el modelo 600 o su justificante). Plazo orientativo "
                                                                   "30 días hábiles, según CCAA"))
            if not liquidado:
                informe_estados.append(f"{cod} · Escritura nueva: pendiente liquidar ITP/IVA-AJD de la compra")

        # ---- venta
        fv = a_fecha(inm.get("fecha_venta"))
        if fv and fv.year == ejercicio:
            amort = sum(float(g.get("importe_deducible") or 0) for g in db.listar("gastos", cid, inmueble=cod)
                        if g.get("categoria") == "Amortización")
            v = calcular_venta(inm, amort)
            if v:
                texto = (f"210 ganancia patrimonial venta {fv:%d/%m/%Y}: ganancia {fmt_eur(v.ganancia)}, "
                         f"cuota 19 % {fmt_eur(v.cuota)}, retención 3 % comprador {fmt_eur(v.retencion)}, "
                         f"resultado {fmt_eur(v.resultado)}")
                _obligacion(db, cid, ejercicio, "Otra", cod, f"VENTA|{cod}", v.fecha_limite, None, "Pendiente", texto)
                informe_estados.append(f"{cod} · VENDIDO: {texto}")
            if inm.get("estado") != "Vendido":
                db.upsert("inmuebles", cid, {"codigo": cod, "estado": "Vendido"})

    # ---- Patrimonio 714 + ITS 718 (una fila por ejercicio)
    clave_p = f"714|{ejercicio}"
    previa_p = db.get_por_clave("patrimonio_714", cid, clave_p)
    deudas = float((previa_p or {}).get("deudas") or 0)
    bonif = float((previa_p or {}).get("bonificacion") or 0)
    pat = calcular_patrimonio(inmuebles, catastros, ejercicio, deudas, bonif)
    obs_p = pat.mensaje + ". " + "; ".join(f"{c}: {vp.fuente} {fmt_eur(vp.valor)} × {t:.0%} = {fmt_eur(v)}"
                                            for c, vp, t, v in pat.detalle_inmuebles)
    fila_r = db.residencia(cid, ejercicio) or {}
    if not _presentado(previa_p):
        codigo_p = db.upsert("patrimonio_714", cid, {
            "ejercicio": ejercicio, "pais": res.pais or res.grupo, "ccaa": fila_r.get("ccaa_ip"),
            "normativa": fila_r.get("normativa_ip") or "Estatal (no residente)", "valor_bienes": pat.valor_bienes,
            "deudas": pat.deudas, "base": pat.base, "minimo_exento": pat.minimo_exento,
            "base_liquidable": pat.base_liquidable, "cuota_integra": pat.cuota_integra,
            "bonificacion": pat.bonificacion, "resultado": pat.resultado,
            "estado": _estado_auto(previa_p, "Pendiente" if pat.obligado else "No procede"),
            "observaciones": obs_p[:1000]},
            clave=clave_p)
    else:
        codigo_p = previa_p["codigo"]
    fila_p = db.get("patrimonio_714", cid, codigo_p)
    _obligacion(db, cid, ejercicio, "Patrimonio 714", None, f"OBL|{clave_p}", plazo_714(ejercicio), codigo_p,
                fila_p.get("estado"), pat.mensaje, fila_p.get("pagado"), fila_p.get("fecha_presentacion"))

    clave_t = f"718|{ejercicio}"
    previa_t = db.get_por_clave("its_718", cid, clave_t)
    its = calcular_its(float(fila_p.get("base_liquidable") or 0), float(fila_p.get("resultado") or 0))
    if not _presentado(previa_t):
        codigo_t = db.upsert("its_718", cid, {
            "ejercicio": ejercicio, "pais": res.pais or res.grupo, "criterio": "Ley 38/2022 (no residente)",
            "base": float(fila_p.get("base_liquidable") or 0), "cuota": its[0], "deducciones": its[1], "resultado": its[2],
            "estado": _estado_auto(previa_t, "Pendiente" if its[2] > 0 else "No procede")}, clave=clave_t)
    else:
        codigo_t = previa_t["codigo"]
    fila_t = db.get("its_718", cid, codigo_t)
    _obligacion(db, cid, ejercicio, "ITS 718", None, f"OBL|{clave_t}", plazo_718(ejercicio), codigo_t,
                fila_t.get("estado"), "", fila_t.get("pagado"), fila_t.get("fecha_presentacion"))

    # ---- Asesor interanual + avisos -> 15_NOTAS
    alertas = comparar_interanual(db, cid, ejercicio)
    for inm in inmuebles:
        for c in db.listar("contratos", cid, inmueble=inm["codigo"]):
            legal, prevista = fin_con_prorroga(c), a_fecha(c.get("fecha_fin_prevista"))
            if legal and prevista and prevista < date(ejercicio, 12, 31) and legal >= date(ejercicio, 1, 1):
                alertas.append(Alerta(
                    "aviso", inm["codigo"], f"prórroga {c['codigo']}",
                    f"{c['codigo']}: fin previsto el {prevista:%d/%m/%Y}, pero un alquiler de vivienda se prorroga por "
                    f"ley hasta el {legal:%d/%m/%Y} (LAU art. 9). Se calcula como alquilado; si el inquilino se fue, "
                    "registra la fecha de fin real · VERIFICAR"))
    alertas.extend(discrepancias)
    vinculadas = {}
    for obligacion in db.listar("obligaciones", cid, ejercicio=ejercicio, tipo="210 Alquiler"):
        modelo = obligacion.get("id_modelo")
        if modelo:
            vinculadas.setdefault(modelo, []).append(obligacion)
    for modelo, filas in vinculadas.items():
        if len(filas) > 1:
            alertas.append(Alerta(
                "grave", filas[0].get("inmueble"), f"obligaciones 210 {modelo}",
                f"{modelo}: obligaciones duplicadas. Se conservan los históricos y sus pagos; "
                "revisa la conciliación antes de sumar importes."))
    if res.asumida:
        alertas.insert(0, Alerta("grave", None, "residencia",
                                 "Sin residencia fiscal registrada: se calcula como Extra-UE (24 %, sin gastos)."))
    for inm in inmuebles:
        if inm['codigo'] not in alquileres_bloqueados:
            clave = f"ALERTA|{ejercicio}|{inm['codigo']}|conflicto 210 alquiler"
            nota = db.get_por_clave('notas', cid, clave)
            if nota and not str(nota.get('nota') or '').startswith('RESUELTO'):
                db.nota(cid, 'Asesor interanual', nota.get('asunto') or 'Conflicto 210',
                        'RESUELTO al recalcular: ya no hay duplicados que bloqueen este alquiler.\n'
                        + (nota.get('nota') or ''), clave=clave, proxima_accion='Sin acción pendiente')
    for a in alertas:
        db.nota(cid, "Asesor interanual", f"{ejercicio} · {a.inmueble or 'Cliente'} · {a.tipo}", a.mensaje,
                clave=f"ALERTA|{ejercicio}|{a.inmueble or '-'}|{a.tipo}", proxima_accion="Verificar con el cliente")
    db.nota(cid, "Patrimonio", f"{ejercicio} · Impuesto sobre el Patrimonio", pat.mensaje, clave=f"IP|{ejercicio}")

    informe_estados.insert(0, pat.mensaje)
    pendientes = revisar_pendientes(db, cid, ejercicio)
    pendientes.extend(Pendiente("grave", "declaracion", f"{cod} · 210 alquiler pendiente de revisión", mensaje)
                       for cod, mensaje in alquileres_bloqueados.items())
    return InformeFiscal(ejercicio, res, pat, its, filas_a, filas_i, alertas, informe_estados, pendientes,
                         alquileres_bloqueados)


# ============================================================================================ pendientes
@dataclass
class Pendiente:
    nivel: str            # 'grave' (vencido o falta algo imprescindible) | 'aviso' | 'info'
    clase: str            # 'documento' | 'declaracion'
    titulo: str
    detalle: str
    inmueble: Optional[str] = None
    fecha_limite: Optional[date] = None
    codigo: Optional[str] = None      # obligación (14_OBLIGACIONES) o fila "Falta" de 13_DOCUMENTACION


def _hay_documento(docs: list[dict], tipo: str, inmueble: Optional[str] = None,
                   ejercicio: Optional[int] = None) -> bool:
    return any(d.get("tipo") == tipo and d.get("estado") != "Falta"
               and (inmueble is None or d.get("inmueble") == inmueble)
               and (ejercicio is None or d.get("ejercicio") == ejercicio) for d in docs)


def _es_falta_automatica(doc: dict) -> bool:
    return ((doc.get("clave") or "").startswith("FALTA|") and doc.get("estado") == "Falta"
            and not any(doc.get(c) for c in ("ruta", "sha256", "nombre_original")))


def revisar_pendientes(db: Database, cid: int, ejercicio: int, hoy: Optional[date] = None) -> list[Pendiente]:
    """Documentación que falta y declaraciones sin presentar (vencidas, próximas o pendientes).

    La documentación que falta se refleja en 13_DOCUMENTACION con estado "Falta" (la plantilla la cuenta en
    00_RESUMEN!H10). Cuando el documento llega, la fila "Falta" se elimina sola.
    """
    hoy = hoy or date.today()
    docs = db.listar("documentos", cid)
    faltan: dict[str, tuple[str, str, Optional[str], str]] = {}  # clave -> (tipo, descripción, inmueble, nivel)

    fila_r = db.residencia(cid, ejercicio)
    cert_ok = (fila_r and fila_r.get("ejercicio") == ejercicio and (fila_r.get("certificado") or "") == "Sí")         or _hay_documento(docs, "Certificado de residencia fiscal", ejercicio=ejercicio)
    if not cert_ok:
        faltan[f"FALTA|{ejercicio}|-|residencia"] = (
            "Certificado de residencia fiscal", f"Certificado de residencia fiscal {ejercicio}", None, "aviso")

    for inm in db.listar("inmuebles", cid):
        cod = inm["codigo"]
        # Escritura y contratos son documentos de toda la vida del inmueble: se exigen sea cual sea el año
        # consultado (A4-P05), así su falta no aparece y desaparece al cambiar de ejercicio.
        if inm.get("estado") != "Baja" and not inm.get("carpeta")                 and not _hay_documento(docs, "Escritura de compra", inmueble=cod):
            faltan[f"FALTA|-|{cod}|escritura"] = ("Escritura de compra", f"Escritura de compra de {cod}", cod, "grave")
        for c in db.listar("contratos", cid, inmueble=cod):
            if intervalo_contrato(c) and not c.get("ruta"):
                faltan[f"FALTA|-|{c['codigo']}|contrato"] = (
                    "Contrato de alquiler", f"PDF del contrato {c['codigo']} de {cod}", cod, "aviso")
        if not periodo_tenencia(inm, ejercicio):
            continue
        cat = db.catastro(cid, cod, ejercicio)
        if revision_pendiente(cat):
            faltan[f"FALTA|{ejercicio}|{cod}|revision-catastro"] = (
                "Catastro / IBI", f"{PENDIENTE_REVISION} de {cod}. Cálculo provisional: revisa Catastro.", cod, "grave")
        if not cat or cat.get("_heredado"):
            faltan[f"FALTA|{ejercicio}|{cod}|ibi"] = (
                "Catastro / IBI", f"Recibo IBI o certificado catastral {ejercicio} de {cod}", cod, "grave")

    # Sincroniza 13_DOCUMENTACION: alta de lo que falta, baja de lo que ya llegó
    # Clave: FALTA|<ejercicio o ->|<inmueble/contrato o ->|<qué>
    # Una fila completada manualmente deja de pertenecer al generador de faltas.
    protegidas = {d.get("clave") for d in docs if not _es_falta_automatica(d)}
    faltan = {clave: valor for clave, valor in faltan.items() if clave not in protegidas}
    for clave, (tipo, descripcion, inm, _nivel) in faltan.items():
        anio_clave = clave.split("|")[1]
        db.upsert("documentos", cid, {"tipo": tipo, "descripcion": descripcion, "estado": "Falta",
                                      "inmueble": inm, "ejercicio": int(anio_clave) if anio_clave.isdigit() else None,
                                      "ultima_revision": hoy}, clave=clave)
    for d in docs:
        clave = d.get("clave") or ""
        if (_es_falta_automatica(d) and clave not in faltan
                and clave.split("|")[1] in (str(ejercicio), "-")):
            db.borrar("documentos", cid, d["codigo"])

    codigos_falta = {d.get("clave"): d["codigo"] for d in db.listar("documentos", cid) if d.get("clave")}
    pendientes = [Pendiente(nivel, "documento", f"Falta: {tipo}", descripcion, inm, codigo=codigos_falta.get(clave))
                  for clave, (tipo, descripcion, inm, nivel) in faltan.items()]

    for o in db.listar("obligaciones", cid, orden="fecha_limite"):
        if o.get("estado") in ("Presentado", "No procede") or (o.get("ejercicio") or 0) > ejercicio:
            continue
        limite = a_fecha(o.get("fecha_limite"))
        nombre = f"{o.get('tipo')} {o.get('ejercicio')}" + (f" · {o['notas']}" if o.get("notas") else "")
        if limite and limite < hoy:
            pendientes.append(Pendiente("grave", "declaracion", f"VENCIDA: {o.get('tipo')}",
                                        f"{nombre}. Plazo terminó el {limite:%d/%m/%Y}.", o.get("inmueble"), limite,
                                        o["codigo"]))
        elif limite and (limite - hoy).days <= 30:
            pendientes.append(Pendiente("aviso", "declaracion", f"Vence pronto: {o.get('tipo')}",
                                        f"{nombre}. Plazo hasta el {limite:%d/%m/%Y}.", o.get("inmueble"), limite,
                                        o["codigo"]))
        elif o.get("ejercicio") == ejercicio:
            pendientes.append(Pendiente("info", "declaracion", f"Por presentar: {o.get('tipo')}",
                                        f"{nombre}" + (f". Plazo hasta el {limite:%d/%m/%Y}." if limite else "."),
                                        o.get("inmueble"), limite, o["codigo"]))
    orden = {"grave": 0, "aviso": 1, "info": 2}
    return sorted(pendientes, key=lambda p: (orden[p.nivel], p.fecha_limite or date.max))


def recalcular_ejercicios(db: Database, cid: int, ejercicios: Iterable[int]) -> dict[int, InformeFiscal]:
    afectados = set(ejercicios)
    if not afectados:
        return {}
    desde = min(afectados)
    for tabla in ("arrastres_gastos", "m210_alquiler"):
        afectados.update(int(f["ejercicio"]) for f in db.listar(tabla, cid)
                         if f.get("ejercicio") and int(f["ejercicio"]) >= desde)
    return {e: recalcular_cliente(db, cid, e) for e in sorted(afectados)}


# ============================================================================================ autónomos (módulo 2)
MODELOS_PERIODICOS = {
    "303": "Trimestral", "130": "Trimestral", "131": "Trimestral", "111": "Trimestral", "115": "Trimestral",
    "349": "Trimestral", "390": "Anual", "190": "Anual", "180": "Anual", "347": "Anual", "100": "Anual",
}


def plazos_autonomo(modelo: str, ejercicio: int) -> list[tuple[str, date]]:
    """Plazos generales: trimestrales 1-20 abr/jul/oct y 1-30 ene; anuales 30 ene (390/190/180), feb (347), jun (100)."""
    if MODELOS_PERIODICOS.get(modelo) == "Trimestral":
        return [("1T", date(ejercicio, 4, 20)), ("2T", date(ejercicio, 7, 20)), ("3T", date(ejercicio, 10, 20)),
                ("4T", date(ejercicio + 1, 1, 30))]
    anuales = {"390": date(ejercicio + 1, 1, 30), "190": date(ejercicio + 1, 1, 31), "180": date(ejercicio + 1, 1, 31),
               "347": date(ejercicio + 1, 2, 28), "100": date(ejercicio + 1, 6, 30)}
    return [("Anual", anuales[modelo])] if modelo in anuales else []


def recalcular_autonomo(db: Database, cid: int, ejercicio: int) -> list[str]:
    """Genera el calendario fiscal del autónomo en 14_OBLIGACIONES (tipo 'Otra') y marca lo presentado."""
    estados = []
    presentados = {(m["modelo"], m.get("periodo")): m for m in db.listar("modelos_presentados", cid, ejercicio=ejercicio)}
    modelos = set()
    for act in db.listar("actividades", cid):
        alta = a_fecha(act.get("fecha_alta"))
        if alta and alta.year > ejercicio:
            continue
        modelos |= {m.strip() for m in (act.get("modelos") or "").split(",") if m.strip()}
    for modelo in sorted(modelos):
        for periodo, limite in plazos_autonomo(modelo, ejercicio):
            p = presentados.get((modelo, periodo))
            estado = "Presentado" if p else "Pendiente"
            _obligacion(db, cid, ejercicio, "Otra", None, f"AUT|{modelo}|{ejercicio}|{periodo}", limite,
                        p["codigo"] if p else None, estado, f"Modelo {modelo} {periodo}",
                        p.get("importe") if p else None, p.get("fecha_presentacion") if p else None)
            estados.append(f"Modelo {modelo} {periodo}/{ejercicio}: "
                           + ("PRESENTADO [OK]" if p else f"pendiente (límite {limite:%d/%m/%Y})"))
    return estados
