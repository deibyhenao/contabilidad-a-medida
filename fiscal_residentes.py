"""Fiscalidad de residentes: autónomos y pymes (modelos 303, 130, 111 y 115).

Reglas puras + ``recalcular_residente`` (guarda en BD). Mismos principios que el motor de no residentes:
- una fila «Presentado» queda congelada y no se recalcula;
- los estados puestos a mano (En preparación, Revisar…) se respetan;
- los criterios no automatizados se marcan VERIFICAR en observaciones.

Autor: Claude (arquitecto). Auditor: Codex.

Reglas (resumen; detalle en .tripartito/2026-10-18-carteras-residentes/01-arquitectura.md):
  303 (IVA régimen General), por trimestre:
      devengado = Σ cuota IVA emitidas · deducible = Σ cuota IVA × % deducible recibidas
      resultado = devengado − deducible − a compensar del trimestre anterior (4T del año previo para el 1T)
  130 (autónomo en estimación directa), acumulado desde el 1 de enero:
      gastos = Σ (base + IVA no deducible) × % deducible IRPF; simplificada: + min(5 % rendimiento, 2.000)
      resultado = 20 % × rendimiento − retenciones soportadas − pagos de los 130 anteriores del año
  111 / 115: retenciones practicadas en facturas recibidas (115 = alquiler de local; 111 = resto).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from carteras import TIPO_AUTONOMO, TIPO_PYME
from database import Database, a_fecha
import tax_calculator as tc
from tax_calculator import Alerta, Pendiente, fmt_eur, r2

TRIMESTRES = ("1T", "2T", "3T", "4T")
LIMITE_DIFICIL_JUSTIFICACION = 2000.0
PCT_DIFICIL_JUSTIFICACION = 0.05
PCT_PAGO_130 = 0.20
CATEGORIA_ALQUILER = "Alquiler local"
MODELOS_CALCULADOS = ("303", "130", "111", "115")
PLAZOS_PYME = {"202": (("1P", (4, 20)), ("2P", (10, 20)), ("3P", (12, 20))), "200": (("Anual", (7, 25)),)}


# ============================================================================================ utilidades
def trimestre_de(fecha) -> Optional[str]:
    f = a_fecha(fecha)
    return f"{(f.month - 1) // 3 + 1}T" if f else None


def fin_trimestre(ejercicio: int, periodo: str) -> date:
    mes = TRIMESTRES.index(periodo) * 3 + 3
    return (date(ejercicio + (mes == 12), 1 if mes == 12 else mes + 1, 1) - timedelta(days=1))


def plazo_trimestral(ejercicio: int, periodo: str) -> date:
    return {"1T": date(ejercicio, 4, 20), "2T": date(ejercicio, 7, 20), "3T": date(ejercicio, 10, 20),
            "4T": date(ejercicio + 1, 1, 30)}[periodo]


def _num(v) -> float:
    return float(v) if v not in (None, "") else 0.0


def _pct(v, defecto: float = 1.0) -> float:
    return defecto if v in (None, "") else tc.fraccion(v)


def cuota_iva(factura: dict) -> float:
    """Cuota de IVA de la factura; si falta y hay base y tipo, se calcula."""
    if factura.get("cuota_iva") not in (None, ""):
        return _num(factura["cuota_iva"])
    return r2(_num(factura.get("base")) * _pct(factura.get("tipo_iva"), 0.0))


def retencion(factura: dict) -> float:
    if factura.get("retencion") not in (None, ""):
        return _num(factura["retencion"])
    return r2(_num(factura.get("base")) * _pct(factura.get("tipo_retencion"), 0.0))


def iva_deducible(factura: dict) -> float:
    return r2(cuota_iva(factura) * _pct(factura.get("pct_iva_deducible")))


def gasto_irpf(factura: dict) -> float:
    """Gasto fiscal IRPF: base + IVA no deducible, por el % deducible."""
    no_deducible = cuota_iva(factura) - iva_deducible(factura)
    return r2((_num(factura.get("base")) + no_deducible) * _pct(factura.get("pct_deducible_irpf")))


def total_factura(factura: dict) -> float:
    return r2(_num(factura.get("base")) + cuota_iva(factura) - retencion(factura))


def actividad_vigente(actividades: list[dict], ejercicio: int) -> Optional[dict]:
    """La actividad de alta más reciente que estuvo vigente en algún momento del ejercicio."""
    vigentes = []
    for a in actividades:
        alta, baja = a_fecha(a.get("fecha_alta")), a_fecha(a.get("fecha_baja"))
        if (alta is None or alta <= date(ejercicio, 12, 31)) and (baja is None or baja >= date(ejercicio, 1, 1)):
            vigentes.append(a)
    return max(vigentes, key=lambda a: a_fecha(a.get("fecha_alta")) or date.min, default=None)


# ============================================================================================ cálculo puro
@dataclass
class Resultado303:
    periodo: str
    base_devengada: float
    iva_devengado: float
    base_deducible: float
    iva_deducible: float
    compensacion_anterior: float
    resultado: float
    a_ingresar: float
    a_compensar: float
    observaciones: list[str] = field(default_factory=list)


def calcular_303(emitidas: list[dict], recibidas: list[dict], periodo: str,
                 compensacion_anterior: float = 0.0) -> Resultado303:
    em = [f for f in emitidas if f.get("trimestre") == periodo]
    re_ = [f for f in recibidas if f.get("trimestre") == periodo]
    devengado = r2(sum(cuota_iva(f) for f in em))
    deducible = r2(sum(iva_deducible(f) for f in re_))
    resultado = r2(devengado - deducible - compensacion_anterior)
    obs = []
    if resultado < 0 and periodo == "4T":
        obs.append("4T negativo: elegir compensar en el ejercicio siguiente o solicitar devolución · VERIFICAR")
    return Resultado303(periodo, r2(sum(_num(f.get("base")) for f in em)), devengado,
                        r2(sum(_num(f.get("base")) for f in re_ if iva_deducible(f) > 0)), deducible,
                        r2(compensacion_anterior), resultado, max(0.0, resultado), max(0.0, r2(-resultado)), obs)


@dataclass
class Resultado130:
    periodo: str
    ingresos: float
    gastos: float
    rendimiento: float
    pago_20: float
    retenciones: float
    pagos_anteriores: float
    resultado: float
    a_ingresar: float
    observaciones: list[str] = field(default_factory=list)


def calcular_130(emitidas: list[dict], recibidas: list[dict], periodo: str, pagos_anteriores: float,
                 simplificada: bool = True) -> Resultado130:
    """Acumulado del 1 de enero al fin del trimestre ``periodo``."""
    hasta = TRIMESTRES.index(periodo)
    dentro = lambda f: f.get("trimestre") in TRIMESTRES[:hasta + 1]  # noqa: E731
    ingresos = r2(sum(_num(f.get("base")) for f in emitidas if dentro(f)))
    gastos = r2(sum(gasto_irpf(f) for f in recibidas if dentro(f)))
    obs = []
    if simplificada:
        dificil = r2(min(PCT_DIFICIL_JUSTIFICACION * max(0.0, ingresos - gastos), LIMITE_DIFICIL_JUSTIFICACION))
        if dificil:
            obs.append(f"Incluye {fmt_eur(dificil)} de gastos de difícil justificación (5 %, máx. 2.000 €)")
        gastos = r2(gastos + dificil)
    rendimiento = r2(ingresos - gastos)
    pago = r2(PCT_PAGO_130 * max(0.0, rendimiento))
    retenciones = r2(sum(retencion(f) for f in emitidas if dentro(f)))
    resultado = r2(pago - retenciones - pagos_anteriores)
    obs.append("Sin minoración por rendimientos bajos (casilla 13) · VERIFICAR")
    return Resultado130(periodo, ingresos, gastos, rendimiento, pago, retenciones, r2(pagos_anteriores),
                        resultado, max(0.0, resultado), obs)


def calcular_retenciones(recibidas: list[dict], periodo: str) -> dict[str, tuple[int, float, float]]:
    """{'111': (perceptores, base, retenciones), '115': (…)} del trimestre."""
    salida = {}
    for modelo in ("111", "115"):
        filas = [f for f in recibidas if f.get("trimestre") == periodo and retencion(f) > 0
                 and ((f.get("categoria") == CATEGORIA_ALQUILER) == (modelo == "115"))]
        if filas:
            perceptores = len({(f.get("nif_proveedor") or f.get("proveedor") or f.get("codigo")) for f in filas})
            salida[modelo] = (perceptores, r2(sum(_num(f.get("base")) for f in filas)),
                              r2(sum(retencion(f) for f in filas)))
    return salida


# ============================================================================================ informe
@dataclass
class ResumenTrimestre:
    periodo: str
    ingresos: float = 0.0
    gastos: float = 0.0
    iva_devengado: float = 0.0
    iva_deducible: float = 0.0
    a_ingresar_303: float = 0.0
    a_compensar_303: float = 0.0
    a_ingresar_130: Optional[float] = None
    retenciones_111: float = 0.0
    retenciones_115: float = 0.0
    n_emitidas: int = 0
    n_recibidas: int = 0

    @property
    def rendimiento(self) -> float:
        return r2(self.ingresos - self.gastos)


@dataclass
class InformeResidente:
    ejercicio: int
    tipo_cliente: str
    actividad: Optional[dict]
    regimen_iva: str
    regimen_irpf: str
    calcula_130: bool
    trimestres: list[ResumenTrimestre] = field(default_factory=list)
    alertas: list[Alerta] = field(default_factory=list)
    estados: list[str] = field(default_factory=list)
    pendientes: list[Pendiente] = field(default_factory=list)

    @property
    def total_ingresos(self) -> float:
        return r2(sum(t.ingresos for t in self.trimestres))

    @property
    def total_gastos(self) -> float:
        return r2(sum(t.gastos for t in self.trimestres))

    @property
    def total_a_ingresar(self) -> float:
        return r2(sum(t.a_ingresar_303 + (t.a_ingresar_130 or 0) + t.retenciones_111 + t.retenciones_115
                      for t in self.trimestres))


def _guardar(db: Database, tabla: str, cid: int, clave: str, datos: dict, estado: str,
             justificante: Optional[dict] = None) -> dict:
    """Upsert respetando una fila presentada (congelada) y los estados manuales. Devuelve la fila final.

    ``justificante``: modelo presentado subido en PDF; la fila recién calculada queda Presentada (y congelada)."""
    previa = db.get_por_clave(tabla, cid, clave)
    if tc._presentado(previa):
        return previa
    datos = {**datos, "estado": tc._estado_auto(previa, estado)}
    if justificante and estado != "No procede":
        datos.update(estado="Presentado", fecha_presentacion=justificante.get("fecha_presentacion"),
                     nrc=justificante.get("csv"), ruta=justificante.get("ruta"))
        campo = "retenciones" if tabla == "m111_115" else "a_ingresar"
        importe = justificante.get("importe")
        if importe not in (None, ""):
            if _num(datos.get(campo)) != _num(importe):
                aviso = "Importe del justificante distinto del calculado · VERIFICAR"
                datos["observaciones"] = "; ".join(filter(None, (datos.get("observaciones"), aviso)))
            datos[campo] = importe  # La declaración presentada manda, incluido importe cero.
    codigo = db.upsert(tabla, cid, datos, clave=clave)
    return db.get(tabla, cid, codigo)


def _modelos_actividad(actividad: Optional[dict], tipo: str, regimen_iva: str, calcula_130: bool) -> set[str]:
    modelos = {m.strip() for m in ((actividad or {}).get("modelos") or "").split(",") if m.strip()}
    if regimen_iva == "General":
        modelos |= {"303", "390"}
    if calcula_130:
        modelos.add("130")
    if tipo == TIPO_PYME:
        modelos |= {"202", "200"}
    return modelos


def ejercicios_encadenados(db: Database, cid: int, ejercicios: set[int] | list[int], ejercicio_activo: int,
                           hoy: Optional[date] = None) -> list[int]:
    """Años no prescritos desde el primer ejercicio con facturas/modelos hasta el último afectado o calculado."""
    anio_actual = (hoy or date.today()).year
    candidatos = {int(e) for e in ejercicios} | {int(ejercicio_activo)}
    for tabla in ("facturas_emitidas", "facturas_recibidas", "m303", "modelos_presentados"):
        for r in db.con.execute(f"SELECT DISTINCT ejercicio FROM {tabla} WHERE cliente_id=? AND ejercicio IS NOT NULL",
                                (cid,)):
            candidatos.add(int(r[0]))
    validos = {e for e in candidatos if anio_actual - 4 <= e <= anio_actual} | {int(ejercicio_activo)}
    return list(range(min(validos), max(validos) + 1))


def recalcular_residente(db: Database, cid: int, ejercicio: int, hoy: Optional[date] = None) -> InformeResidente:
    """Recalcula 303/130/111/115 del ejercicio, el calendario y los pendientes. Guarda en BD."""
    hoy = hoy or date.today()
    cliente = db.get_cliente(cid) or {}
    tipo = cliente.get("tipo_cliente") or TIPO_AUTONOMO
    actividades = db.listar("actividades", cid)
    actividad = actividad_vigente(actividades, ejercicio)
    regimen_iva = (actividad or {}).get("regimen_iva") or "General"
    regimen_irpf = (actividad or {}).get("regimen_irpf") or "Directa simplificada"
    calcula_130 = (tipo == TIPO_AUTONOMO and regimen_irpf.startswith("Directa")
                   and ((actividad or {}).get("obligado_130") or "Sí") != "No")
    informe = InformeResidente(ejercicio, tipo, actividad, regimen_iva, regimen_irpf, calcula_130)
    if actividad is None:
        informe.alertas.append(Alerta("aviso", None, "actividad", "Sin modelo 036/037: se asume IVA General"
                                      + (" y estimación directa simplificada." if tipo == TIPO_AUTONOMO else ".")))

    emitidas = db.listar("facturas_emitidas", cid, ejercicio=ejercicio)
    recibidas = db.listar("facturas_recibidas", cid, ejercicio=ejercicio)
    justificantes = {(m["modelo"], m.get("periodo")): m
                     for m in db.listar("modelos_presentados", cid, ejercicio=ejercicio)}
    for f in emitidas + recibidas:
        if not f.get("trimestre"):
            informe.alertas.append(Alerta("aviso", None, "factura",
                                          f"{f['codigo']} sin fecha: no se suma a ningún trimestre."))

    # ---- 303
    previo_4t = db.get_por_clave("m303", cid, f"303|{ejercicio - 1}|4T")
    compensar = _num((previo_4t or {}).get("a_compensar"))
    for periodo in TRIMESTRES:
        clave = f"303|{ejercicio}|{periodo}"
        if regimen_iva != "General":
            if db.get_por_clave("m303", cid, clave):
                _guardar(db, "m303", cid, clave, {"observaciones": f"Régimen de IVA {regimen_iva}: sin 303 "
                                                  "calculado · VERIFICAR"}, "No procede")
            continue
        r = calcular_303(emitidas, recibidas, periodo, compensar)
        fila = _guardar(db, "m303", cid, clave, {
            "ejercicio": ejercicio, "periodo": periodo, "base_devengada": r.base_devengada,
            "iva_devengado": r.iva_devengado, "base_deducible": r.base_deducible, "iva_deducible": r.iva_deducible,
            "compensacion_anterior": r.compensacion_anterior, "resultado": r.resultado, "a_ingresar": r.a_ingresar,
            "a_compensar": r.a_compensar, "observaciones": "; ".join(r.observaciones) or None}, "Pendiente",
            justificantes.get(("303", periodo)))
        compensar = _num(fila.get("a_compensar"))  # una fila presentada manda en la cadena

    # ---- 130
    pagos = 0.0
    for periodo in TRIMESTRES:
        clave = f"130|{ejercicio}|{periodo}"
        if not calcula_130:
            if db.get_por_clave("m130", cid, clave):
                _guardar(db, "m130", cid, clave, {"observaciones": "Sin pago fraccionado 130 para este cliente "
                                                  f"({tipo}, {regimen_irpf}) · VERIFICAR"}, "No procede")
            continue
        r = calcular_130(emitidas, recibidas, periodo, pagos, regimen_irpf == "Directa simplificada")
        fila = _guardar(db, "m130", cid, clave, {
            "ejercicio": ejercicio, "periodo": periodo, "ingresos": r.ingresos, "gastos": r.gastos,
            "rendimiento": r.rendimiento, "pago_20": r.pago_20, "retenciones": r.retenciones,
            "pagos_anteriores": r.pagos_anteriores, "resultado": r.resultado, "a_ingresar": r.a_ingresar,
            "observaciones": "; ".join(r.observaciones)}, "Pendiente", justificantes.get(("130", periodo)))
        pagos = r2(pagos + _num(fila.get("a_ingresar")))

    # ---- 111 / 115
    for periodo in TRIMESTRES:
        calculadas = calcular_retenciones(recibidas, periodo)
        for modelo in ("111", "115"):
            clave = f"RET|{ejercicio}|{periodo}|{modelo}"
            if modelo in calculadas:
                perceptores, base, ret = calculadas[modelo]
                _guardar(db, "m111_115", cid, clave, {"ejercicio": ejercicio, "periodo": periodo, "modelo": modelo,
                                                      "perceptores": perceptores, "base": base, "retenciones": ret},
                         "Pendiente", justificantes.get((modelo, periodo)))
            elif db.get_por_clave("m111_115", cid, clave) or (modelo, periodo) in justificantes:
                justificante = justificantes.get((modelo, periodo))
                _guardar(db, "m111_115", cid, clave, {"ejercicio": ejercicio, "periodo": periodo, "modelo": modelo,
                         "perceptores": 0, "base": 0, "retenciones": 0},
                         "Pendiente" if justificante else "No procede", justificante)

    # ---- resumen por trimestre (con las filas finales, presentadas incluidas)
    m303 = {f["periodo"]: f for f in db.listar("m303", cid, ejercicio=ejercicio)}
    m130 = {f["periodo"]: f for f in db.listar("m130", cid, ejercicio=ejercicio)}
    ret = {(f["periodo"], f["modelo"]): f for f in db.listar("m111_115", cid, ejercicio=ejercicio)}
    for periodo in TRIMESTRES:
        em = [f for f in emitidas if f.get("trimestre") == periodo]
        rc = [f for f in recibidas if f.get("trimestre") == periodo]
        t = ResumenTrimestre(periodo, r2(sum(_num(f.get("base")) for f in em)), r2(sum(gasto_irpf(f) for f in rc)),
                             r2(sum(cuota_iva(f) for f in em)), r2(sum(iva_deducible(f) for f in rc)),
                             n_emitidas=len(em), n_recibidas=len(rc))
        if periodo in m303 and m303[periodo].get("estado") != "No procede":
            t.a_ingresar_303 = _num(m303[periodo].get("a_ingresar"))
            t.a_compensar_303 = _num(m303[periodo].get("a_compensar"))
        if calcula_130 and periodo in m130:
            t.a_ingresar_130 = _num(m130[periodo].get("a_ingresar"))
        t.retenciones_111 = _num((ret.get((periodo, "111")) or {}).get("retenciones"))
        t.retenciones_115 = _num((ret.get((periodo, "115")) or {}).get("retenciones"))
        informe.trimestres.append(t)

    _calendario(db, cid, ejercicio, informe, m303, m130, ret, tipo)
    informe.pendientes = revisar_pendientes_residente(db, cid, ejercicio, informe, hoy)
    return informe


def _calendario(db: Database, cid: int, ejercicio: int, informe: InformeResidente, m303: dict, m130: dict,
                ret: dict, tipo: str) -> None:
    """14_OBLIGACIONES / 09_OBLIGACIONES: trimestrales calculados y anuales del 036, con su estado."""
    presentados = {(m["modelo"], m.get("periodo")): m for m in db.listar("modelos_presentados", cid,
                                                                          ejercicio=ejercicio)}
    for periodo in TRIMESTRES:
        limite = plazo_trimestral(ejercicio, periodo)
        filas = [("303", "303 IVA", m303.get(periodo)), ("130", "130 IRPF", m130.get(periodo)),
                 ("111", "111 Retenciones", ret.get((periodo, "111"))),
                 ("115", "115 Alquileres", ret.get((periodo, "115")))]
        for modelo, tipo_ob, fila in filas:
            if not fila:
                continue
            estado = fila.get("estado") or "Pendiente"
            importe = fila.get("a_ingresar") if modelo in ("303", "130") else fila.get("retenciones")
            tc._obligacion(db, cid, ejercicio, tipo_ob, None, f"OBL|{modelo}|{ejercicio}|{periodo}", limite,
                           fila["codigo"], estado, f"Modelo {modelo} {periodo}: {fmt_eur(_num(importe))}",
                           fila.get("a_ingresar") if estado == "Presentado" and modelo in ("303", "130") else None,
                           fila.get("fecha_presentacion"))
            if estado == "No procede":
                continue
            informe.estados.append(f"Modelo {modelo} {periodo}/{ejercicio}: " + (
                "PRESENTADO [OK]" if estado == "Presentado" else
                f"{estado.lower()} · {fmt_eur(_num(importe))} · límite {limite:%d/%m/%Y}"))
    modelos = _modelos_actividad(informe.actividad, tipo, informe.regimen_iva, informe.calcula_130)
    claves_aut = set()
    for modelo in sorted(m for m in modelos if m not in MODELOS_CALCULADOS):
        plazos = ([(p, date(ejercicio + (modelo == "200"), mes, dia)) for p, (mes, dia) in PLAZOS_PYME[modelo]]
                  if modelo in PLAZOS_PYME else tc.plazos_autonomo(modelo, ejercicio))
        for periodo, limite in plazos:
            p = presentados.get((modelo, periodo))
            estado = "Presentado" if p else "Pendiente"
            clave_aut = f"AUT|{modelo}|{ejercicio}|{periodo}"
            claves_aut.add(clave_aut)
            tc._obligacion(db, cid, ejercicio, "Otra", None, clave_aut, limite,
                           p["codigo"] if p else None, estado, f"Modelo {modelo} {periodo}",
                           p.get("importe") if p else None, p.get("fecha_presentacion") if p else None)
            informe.estados.append(f"Modelo {modelo} {periodo}/{ejercicio}: "
                                   + ("PRESENTADO [OK]" if p else f"pendiente (límite {limite:%d/%m/%Y})"))
    for ob in db.listar("obligaciones", cid, ejercicio=ejercicio):
        if (ob.get("clave") or "").startswith("AUT|") and ob["clave"] not in claves_aut and not tc._presentado(ob):
            db.upsert("obligaciones", cid, {"codigo": ob["codigo"], "estado": "No procede"})


def revisar_pendientes_residente(db: Database, cid: int, ejercicio: int, informe: InformeResidente,
                                 hoy: date) -> list[Pendiente]:
    pendientes: list[Pendiente] = []
    tiene_036 = any(a.get("ruta") for a in db.listar("actividades", cid)) or any(
        d.get("tipo") == "Modelo 036/037" and d.get("estado") != "Falta" for d in db.listar("documentos", cid))
    if not tiene_036:
        pendientes.append(Pendiente("grave", "documento", "Falta el modelo 036/037",
                                    "Sube el alta censal en PDF: fija actividad, régimen de IVA/IRPF y modelos."))
    sin_datos = [f for t in ("facturas_emitidas", "facturas_recibidas") for f in
                 db.listar(t, cid, ejercicio=ejercicio) if not f.get("trimestre") or not (
                     f.get("nif_receptor") if t == "facturas_emitidas" else f.get("nif_proveedor"))]
    if sin_datos:
        pendientes.append(Pendiente("aviso", "documento", f"{len(sin_datos)} factura(s) sin fecha o sin NIF",
                                    ", ".join(f["codigo"] for f in sin_datos[:8])
                                    + (" …" if len(sin_datos) > 8 else "")))
    for ob in db.listar("obligaciones", cid, ejercicio=ejercicio):
        if ob.get("estado") in ("Presentado", "No procede"):
            continue
        limite = a_fecha(ob.get("fecha_limite"))
        if limite is None:
            continue
        dias = (limite - hoy).days
        nivel = "grave" if dias < 0 else "aviso" if dias <= 30 else "info"
        titulo = ("VENCIDA: " if dias < 0 else "Vence pronto: " if dias <= 30 else "Por presentar: ") + (
            ob.get("tipo") if ob.get("tipo") != "Otra" else (ob.get("notas") or "Modelo"))
        pendientes.append(Pendiente(nivel, "declaracion", titulo,
                                    f"{ob.get('notas') or ''}. Plazo hasta el {limite:%d/%m/%Y}.".lstrip(". "),
                                    fecha_limite=limite, codigo=ob["codigo"]))
    for t in informe.trimestres:
        if t.n_emitidas == 0 and fin_trimestre(ejercicio, t.periodo) < hoy:
            pendientes.append(Pendiente("info", "documento", f"Sin facturas emitidas en el {t.periodo}",
                                        "Si hubo actividad, sube las facturas para que cuadren el 303 y el 130."))
    orden = {"grave": 0, "aviso": 1, "info": 2}
    return sorted(pendientes, key=lambda p: (orden[p.nivel], p.fecha_limite or date.max))
