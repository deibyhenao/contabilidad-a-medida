"""Valoración local de activos financieros para obligación real (Ley 19/1991).

Los informes aportan datos, no determinan por sí solos su sujeción o exención.
Una valoración incompleta se conserva como provisional para revisión del asesor.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import hashlib
import json

TIPOS = ("CUENTA", "CRIPTO", "ACCION_COTIZADA", "FONDO", "NO_COTIZADO",
         "RENTA_FIJA_COTIZADA", "RENTA_FIJA_NO_COTIZADA", "RESUMEN_CARTERA")
UBICACIONES = ("ESPANA", "EXTRANJERO", "PENDIENTE")
HOJA = "16_OTROS_ACTIVOS"

# Campo, letra, tipo SQLite/Excel. Los cálculos nunca se importan del libro.
CAMPOS = (
    ("codigo", "A", "txt"), ("ejercicio", "B", "int"), ("tipo", "C", "txt"),
    ("entidad", "D", "txt"), ("identificador", "E", "txt"), ("descripcion", "F", "txt"),
    ("pct_titularidad", "G", "pct"), ("ubicacion", "H", "txt"),
    ("saldo_31dic", "I", "eur"), ("saldo_medio_4t", "J", "eur"),
    ("valor_informe", "K", "eur"), ("unidades", "L", "txt"), ("valor_unitario", "M", "txt"),
    ("valor_nominal", "N", "eur"), ("valor_balance", "O", "eur"),
    ("beneficio_1", "P", "eur"), ("beneficio_2", "Q", "eur"), ("beneficio_3", "R", "eur"),
    ("auditoria_favorable", "S", "int"), ("exento", "T", "int"),
    ("motivo_exencion", "U", "txt"), ("valor_manual", "V", "eur"),
    ("motivo_manual", "W", "txt"), ("origen", "X", "txt"), ("ruta", "Y", "link"),
    ("observaciones", "Z", "txt"), ("revision_confirmada", "AA", "int"),
    ("manual_override", "AB", "int"), ("estado", "AC", "txt"),
    ("cartera_id", "AD", "txt"), ("confirmacion_legal", "AE", "txt"),
    ("valor_calculado", "AF", "formula"), ("valor_cliente", "AG", "formula"),
    ("valor_sujeto", "AH", "formula"),
)


def numero(valor):
    if valor in (None, ""):
        return None
    if isinstance(valor, bool):
        raise ValueError("Introduce un número, no un valor lógico.")
    try:
        n = Decimal(str(valor).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Número de activo no válido") from exc
    if not n.is_finite():
        raise ValueError("El importe debe ser finito.")
    return n


def euros(n):
    return float(n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def ejercicio_normalizado(valor):
    try:
        n = numero(valor)
        return int(n) if n is not None and n == int(n) and 1900 <= n <= 2200 else None
    except (ValueError, TypeError, OverflowError):
        return None


def clave_natural(fila):
    identificador = str(fila.get("identificador") or "").strip()
    if not identificador:
        return None
    if fila.get("tipo") != "CRIPTO":
        identificador = re.sub(r"[\s-]+", "", identificador).casefold()
    elif len(identificador) <= 12:
        identificador = identificador.upper()  # Ticker; las direcciones largas conservan mayúsculas.
    elif re.fullmatch(r"0x[0-9a-fA-F]{40}", identificador) or identificador.lower().startswith(("bc1", "tb1", "bcrt1")):
        identificador = identificador.lower()  # EVM/bech32, a diferencia de Base58.
    entidad = " ".join(str(fila.get("entidad") or "").casefold().split())
    cartera = str(fila.get("cartera_id") or "").strip()
    ejercicio = numero(fila.get('ejercicio'))
    if ejercicio is None or ejercicio != int(ejercicio):
        raise ValueError("Ejercicio de activo no válido")
    identidad = [int(ejercicio), fila.get('tipo'), entidad, cartera, identificador]
    codificada = json.dumps(identidad, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "ACTIVO|" + hashlib.sha256(codificada).hexdigest()


def validar(fila):
    if fila.get("tipo") not in TIPOS:
        raise ValueError("Tipo de activo no válido")
    if fila.get("ubicacion", "PENDIENTE") not in UBICACIONES:
        raise ValueError("Ubicación fiscal no válida")
    ejercicio = numero(fila.get("ejercicio"))
    if ejercicio is None or ejercicio != int(ejercicio) or not 1900 <= ejercicio <= 2200:
        raise ValueError("Indica el ejercicio del activo (1900–2200).")
    pct = numero(fila.get("pct_titularidad", 1))
    if pct is None or not 0 < pct <= 1:
        raise ValueError("La titularidad debe ser mayor de 0 y no superar el 100 %.")
    for campo, _, tipo in CAMPOS:
        if tipo == "eur" or campo in ("unidades", "valor_unitario"):
            n = numero(fila.get(campo))
            if n is not None and n < 0 and not campo.startswith("beneficio_"):
                raise ValueError(f"{campo}: no se admite un valor negativo; registra deudas por separado.")
    for campo in ("auditoria_favorable", "exento", "revision_confirmada", "manual_override"):
        if fila.get(campo) not in (None, "", 0, 1):
            raise ValueError(f"{campo}: debe ser 0 o 1.")
    if fila.get("estado", "Activo") not in ("Activo", "Baja"):
        raise ValueError("Estado de activo no válido")
    if fila.get("valor_manual") not in (None, "") and not str(fila.get("motivo_manual") or "").strip():
        raise ValueError("Explica el motivo del valor manual.")
    if fila.get("exento") == 1 and not str(fila.get("motivo_exencion") or "").strip():
        raise ValueError("Justifica la exención fiscal.")


def valorar(fila):
    validar(fila)
    avisos = []
    tipo = fila["tipo"]
    get = lambda campo: numero(fila.get(campo))
    valor = get("valor_informe")
    regla = "Valor fiscal total del informe"
    if tipo == "RESUMEN_CARTERA" or fila.get("estado") == "Baja":
        return dict(valor_calculado=0.0, valor_cliente=0.0, valor_sujeto=0.0,
                    avisos=[], provisional=False, regla="Informativo / baja: no suma")
    if tipo == "CUENTA":
        saldos = [get(c) for c in ("saldo_31dic", "saldo_medio_4t") if get(c) is not None]
        if saldos:
            valor = max(saldos)
        if any(get(c) is None for c in ("saldo_31dic", "saldo_medio_4t")):
            avisos.append("Falta saldo a 31/12 o media 4T ajustada: valoración provisional (art. 12).")
        regla = "Art. 12: mayor saldo 31/12 / media 4T ajustada"
    elif tipo == "CRIPTO":
        unidades, unitario = get("unidades"), get("valor_unitario")
        if valor is None and unidades is not None and unitario is not None:
            valor = unidades * unitario
        elif valor is not None and unidades is not None and unitario is not None:
            if abs(valor - unidades * unitario) >= Decimal("0.01"):
                avisos.append("El total del informe difiere de unidades × valor unitario; revisar.")
        regla = "Art. 24: valor EUR por especie a 31/12"
    elif tipo == "NO_COTIZADO":
        balance = get("valor_balance")
        if fila.get("auditoria_favorable") == 1 and balance is not None:
            valor = balance
        else:
            nominal = get("valor_nominal")
            beneficios = [get("beneficio_" + str(i)) for i in (1, 2, 3)]
            if balance is not None and nominal is not None and all(b is not None for b in beneficios):
                valor = max(balance, nominal, max(Decimal(0), sum(beneficios) / 3 / Decimal("0.20")))
            else:
                avisos.append("Faltan nominal, último balance aprobado o beneficios de tres ejercicios para contrastar el mayor valor del art. 16; el informe se conserva provisionalmente.")
            if fila.get("auditoria_favorable") == 1 and balance is None:
                avisos.append("Auditoría favorable sin valor del último balance aprobado: falta acreditar el dato.")
        regla = "Art. 16: balance auditado favorable; si no, mayor nominal/balance/capitalización 20 %"
    elif tipo == "RENTA_FIJA_NO_COTIZADA":
        valor = get("valor_nominal") if get("valor_nominal") is not None else valor
        regla = "Art. 14: nominal incluidas primas de amortización/reembolso"
    elif tipo in ("ACCION_COTIZADA", "RENTA_FIJA_COTIZADA"):
        regla = "Arts. 13/15: valor total según negociación media del 4T"
    elif tipo == "FONDO":
        regla = "Art. 16.2: valor liquidativo total a 31/12"
    if tipo in ("ACCION_COTIZADA", "RENTA_FIJA_COTIZADA", "FONDO"):
        unidades, unitario = get("unidades"), get("valor_unitario")
        if unidades is not None and unitario is not None:
            por_unidades = unidades * unitario
            if valor is None:
                valor = por_unidades
            elif abs(valor - por_unidades) >= Decimal("0.01"):
                avisos.append("El total fiscal del informe difiere de unidades × cotización/liquidativo documentado; revisar.")
    if "POSIBLE DUPLICADO:" in str(fila.get("observaciones") or ""):
        avisos.append("Posible posición duplicada: concilia las filas y archiva la que no corresponda antes de cerrar el patrimonio.")
    if valor is None:
        avisos.append("Falta valoración: el cero mostrado no acredita ausencia de patrimonio.")
        valor = Decimal(0)
    calculado = euros(valor)
    if get("valor_manual") is not None:
        valor = get("valor_manual")
        regla += " · Valor manual: " + fila["motivo_manual"]
    # La cuota del titular se redondea una vez, después de aplicar su fracción.
    # Redondear antes el total puede añadir un céntimo en medias o cotizaciones.
    cliente = euros(valor * get("pct_titularidad") if get("pct_titularidad") is not None else valor)
    if fila.get("ubicacion", "PENDIENTE") == "PENDIENTE":
        avisos.append("Pendiente confirmar sujeción en España y posible convenio; no se incluye todavía.")
    if fila.get("revision_confirmada") != 1 or not str(fila.get("confirmacion_legal") or "").strip():
        avisos.append("El asesor debe confirmar ubicación, exenciones y regla de valoración con su justificante.")
    sujeto = cliente if fila.get("ubicacion") == "ESPANA" and not fila.get("exento") else 0.0
    return dict(valor_calculado=calculado, valor_efectivo=euros(valor), valor_cliente=cliente, valor_sujeto=sujeto,
                avisos=avisos, provisional=bool(avisos), regla=regla)


def valorar_seguro(fila):
    """Conservar registros dañados para su corrección sin cerrar el resto del expediente.

    La escritura sigue validando y rechazando entradas inválidas; este adaptador
    de lectura nunca convierte un dato desconocido en una valoración definitiva.
    """
    try:
        return valorar(fila)
    except (ValueError, TypeError, InvalidOperation) as exc:
        mensaje = f"Activo con datos inválidos: {exc}. Corrige su ficha antes de cerrar el patrimonio."
        return dict(valor_calculado=0.0, valor_efectivo=0.0, valor_cliente=0.0, valor_sujeto=0.0,
                    provisional=True, avisos=[mensaje], regla="Datos pendientes de corregir", error=mensaje)


def conciliar_carteras(filas, valoraciones=None):
    """Avisos por resumen: identidad común y valores efectivos antes de titularidad.

    Un total de cartera es un control, nunca otro activo. No mezclar cuentas de
    distinta entidad cuando solo coincida el identificador interno de cartera.
    """
    valoraciones = valoraciones or {f.get("codigo"): valorar_seguro(f) for f in filas}
    avisos = {}
    for resumen in filas:
        if resumen.get("tipo") != "RESUMEN_CARTERA" or resumen.get("estado") == "Baja":
            continue
        codigo = resumen.get("codigo")
        entidad = str(resumen.get("entidad") or "").strip().casefold()
        grupo = str(resumen.get("cartera_id") or "").strip()
        hijas = [f for f in filas if f.get("tipo") != "RESUMEN_CARTERA" and f.get("estado") != "Baja"
                 and ejercicio_normalizado(f.get("ejercicio")) is not None
                 and ejercicio_normalizado(f.get("ejercicio")) == ejercicio_normalizado(resumen.get("ejercicio"))
                 and ((grupo and str(f.get("cartera_id") or "").strip() == grupo
                       and (not entidad or str(f.get("entidad") or "").strip().casefold() == entidad))
                      or (not grupo and entidad and str(f.get("entidad") or "").strip().casefold() == entidad))]
        problemas = []
        if not hijas:
            problemas.append("Resumen de cartera sin posiciones desglosadas: valoración incompleta.")
        else:
            esperado = numero(resumen.get("valor_manual"))
            if esperado is None:
                esperado = numero(resumen.get("valor_informe"))
            if esperado is not None:
                total = sum((Decimal(str(valoraciones[f.get("codigo")].get("valor_efectivo",
                             valoraciones[f.get("codigo")]["valor_calculado"]))) for f in hijas), Decimal(0))
                if abs(Decimal(str(euros(total))) - esperado) >= Decimal("0.01"):
                    problemas.append("El resumen de cartera no coincide con la valoración de sus posiciones: conciliar sin duplicar.")
        if problemas:
            avisos[codigo] = problemas
    return avisos
