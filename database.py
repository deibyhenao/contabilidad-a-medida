"""Motor SQLite de Contabilidad a Medida.

Autor: Claude (arquitecto). Auditor: Codex.

Diseño:
- Cada pestaña de la plantilla Excel tiene una tabla espejo. ``TABLAS`` describe, columna a columna,
  qué campo de BD va en qué letra de la hoja. ``excel_generator`` usa este mapa: no hay mapeos duplicados.
- Cada fila tiene ``codigo`` (= columna A / ID de la hoja, p.ej. ``INM-001``) único por cliente.
- ``clave`` es una clave natural opcional (solo BD) que permite a la calculadora hacer upsert idempotente
  (p.ej. un 210 por inmueble y trimestre) sin duplicar filas.
- Memoria interanual: todas las tablas con datos fiscales llevan ``ejercicio``; nunca se borran años anteriores.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Optional

from carteras import NR, RAIZ, RES, PREFIJO_EXCEL, TIPO_NR, cartera_de, validar_tipo
from gastos import decimal_finito, ejercicio_registrado, fraccion, redondear_eur

from app_paths import RECURSOS as RECURSOS_DIR, carpeta_datos
from persistencia import crear_snapshot
from rutas_seguras import sanitize_safe_path

VERSION_ESQUEMA = 1

PROYECTO_DIR = carpeta_datos()
DB_PATH_DEFECTO = PROYECTO_DIR / "contabilidad.db"
CARPETA_CLIENTES_DEFECTO = PROYECTO_DIR / "Clientes"
PLANTILLA_EXCEL = RECURSOS_DIR / "plantillas" / "Plantilla_Expediente_No_Residente_v2.xlsx"
PLANTILLA_EXCEL_RES = RECURSOS_DIR / "plantillas" / "Plantilla_Expediente_Residente_v1.xlsx"
PLANTILLAS = {NR: PLANTILLA_EXCEL, RES: PLANTILLA_EXCEL_RES}

SUBCARPETA_SOPORTES = "Soportes_PDF"
SUBCARPETA_ENTRADA = "_ARRASTRA_AQUI"

# Tipos de columna:
#   txt, int  -> tal cual
#   date      -> ISO 'YYYY-MM-DD' en BD, datetime.date en Excel
#   eur, pct  -> REAL (pct como fracción: 0.5 = 50 %)
#   link      -> ruta relativa a la carpeta del cliente; en Excel se escribe como hipervínculo
#   formula   -> columna con fórmula en la plantilla: NO se guarda en BD ni se escribe nunca
_SQL_TIPO = {"txt": "TEXT", "int": "INTEGER", "date": "TEXT", "eur": "REAL", "pct": "REAL", "link": "TEXT"}


@dataclass(frozen=True)
class Columna:
    campo: str
    letra: Optional[str]  # None = solo BD (no existe en la plantilla)
    tipo: str = "txt"


@dataclass(frozen=True)
class TablaDef:
    nombre: str
    hoja: str
    prefijo: Optional[str]  # None = el código lo da el usuario (p.ej. el ejercicio en 01_RESIDENCIA_FISCAL)
    columnas: tuple[Columna, ...]
    fila_inicio: int = 2
    cartera: str = NR       # NR, RES o "AMBAS" (misma tabla y columnas en las dos plantillas)
    hoja_res: str = ""      # hoja en la plantilla de residentes cuando cartera == "AMBAS"

    def hoja_en(self, cartera: str) -> str:
        """Hoja de esta tabla en la plantilla de la cartera; "" si no aparece en ella."""
        if self.cartera == "AMBAS":
            return self.hoja if cartera == NR else self.hoja_res
        return self.hoja if self.cartera == cartera else ""

    @property
    def columnas_excel(self) -> list[Columna]:
        return [c for c in self.columnas if c.letra]

    @property
    def columnas_bd(self) -> list[Columna]:
        return [c for c in self.columnas if c.tipo != "formula"]


def _c(campo: str, letra: Optional[str], tipo: str = "txt") -> Columna:
    return Columna(campo, letra, tipo)


TABLAS: dict[str, TablaDef] = {
    t.nombre: t
    for t in [
        TablaDef("residencias", "01_RESIDENCIA_FISCAL", None, (
            _c("codigo", "A"), _c("ejercicio", None, "int"), _c("pais", "B"), _c("grupo", "C"),
            _c("nif_extranjero", "D"), _c("certificado", "E"), _c("fecha_certificado", "F", "date"),
            _c("valido_hasta", "G", "date"), _c("normativa_ip", "H"), _c("ccaa_ip", "I"),
            _c("gastos_deducibles", "J"), _c("ruta", "K", "link"), _c("observaciones", "L"),
        )),
        TablaDef("inmuebles", "02_INMUEBLES", "INM", (
            _c("codigo", "A"), _c("direccion", "B"), _c("municipio", "C"), _c("provincia", "D"), _c("cp", "E"),
            _c("ref_catastral", "F"), _c("fecha_adquisicion", "G", "date"), _c("pct_titularidad", "H", "pct"),
            _c("tipo_adquisicion", "I"), _c("precio_escritura", "J", "eur"), _c("itp_iva_ajd", "K", "eur"),
            _c("notaria", "L", "eur"), _c("registro", "M", "eur"), _c("agencia", "N", "eur"),
            _c("abogado_gestoria", "O", "eur"), _c("otros_gastos", "P", "eur"),
            _c("valor_adquisicion_total", "Q", "formula"),
            _c("valor_referencia", "R", "eur"), _c("base_impuesto_adquisicion", "S", "eur"),
            _c("uso_valor_referencia", "T"), _c("n_protocolo", "U"), _c("notario", "V"),
            _c("estado", "W"), _c("fecha_venta", "X", "date"), _c("precio_venta", "Y", "eur"),
            _c("carpeta", "Z", "link"), _c("observaciones", "AA"),
            # Solo BD
            _c("gananciales_forzado", None, "int"),   # 1 = forzar 50 % sociedad conyugal
            _c("pct_titularidad_escritura", None, "pct"),  # % de la escritura guardado al forzar el 50 %
            _c("valor_comprobado", None, "eur"),      # valor comprobado por la Administración
            _c("gastos_venta", None, "eur"),
        )),
        TablaDef("catastro", "03_CATASTRO_IBI", "CAT", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("inmueble", "C"), _c("ref_catastral", "D"),
            _c("valor_catastral", "E", "eur"), _c("valor_suelo", "F", "eur"), _c("valor_construccion", "G", "eur"),
            _c("fecha_efecto", "H"), _c("anio_revision", "I", "int"), _c("pct_imputacion", "J", "pct"),
            _c("fuente", "K"), _c("fecha_comprobacion", "L", "date"), _c("ibi_ejercicio", "M", "eur"),
            _c("ibi_pagado", "N", "eur"), _c("ruta", "O", "link"), _c("observaciones", "P"),
            _c("valor_referencia", None, "eur"),  # valor de referencia Catastro del ejercicio
        )),
        TablaDef("contratos", "04_CONTRATOS", "CTR", (
            _c("codigo", "A"), _c("inmueble", "B"), _c("fecha_inicio", "C", "date"),
            _c("fecha_fin_prevista", "D", "date"), _c("fecha_fin_real", "E", "date"), _c("estado", "F"),
            _c("tipo", "G"), _c("renta_inicial", "H", "eur"), _c("periodicidad", "I"), _c("fianza", "J", "eur"),
            _c("motivo_fin", "K"), _c("fecha_ultima_modificacion", "L", "date"), _c("ruta", "M", "link"),
            _c("observaciones", "N"),
        )),
        TablaDef("inquilinos", "05_INQUILINOS", "INQ", (
            _c("codigo", "A"), _c("contrato", "B"), _c("inmueble", "C"), _c("nombre", "D"),
            _c("tipo_persona", "E"), _c("documento", "F"), _c("pais", "G"), _c("fecha_alta", "H", "date"),
            _c("fecha_baja", "I", "date"), _c("ruta", "J", "link"), _c("observaciones", "K"),
        )),
        TablaDef("rentas", "06_RENTAS_CONTRATO", "RNT", (
            _c("codigo", "A"), _c("contrato", "B"), _c("inmueble", "C"), _c("desde", "D", "date"),
            _c("hasta", "E", "date"), _c("renta", "F", "eur"), _c("periodicidad", "G"), _c("motivo", "H"),
            _c("ruta", "I", "link"), _c("observaciones", "J"),
        )),
        TablaDef("cobros", "07_COBROS", "COB", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("inmueble", "C"), _c("contrato", "D"),
            _c("periodo_desde", "E", "date"), _c("periodo_hasta", "F", "date"), _c("fecha_cobro", "G", "date"),
            _c("importe_contractual", "H", "eur"), _c("importe_cobrado", "I", "eur"),
            _c("diferencia", "J", "formula"), _c("estado", "K"), _c("ruta", "L", "link"), _c("observaciones", "M"),
        )),
        TablaDef("gastos", "08_GASTOS", "GAS", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("inmueble", "C"), _c("contrato", "D"),
            _c("fecha", "E", "date"), _c("periodo_desde", "F", "date"), _c("periodo_hasta", "G", "date"),
            _c("categoria", "H"), _c("proveedor", "I"), _c("nif_proveedor", "J"), _c("n_factura", "K"),
            _c("base", "L", "eur"), _c("iva", "M", "eur"), _c("total", "N", "eur"),
            _c("pct_inmueble", "O", "pct"), _c("pct_alquiler", "P", "pct"),
            _c("importe_potencial", "Q", "formula"), _c("importe_deducible", "R", "eur"),
            _c("incluido_210", "S"), _c("id_210", "T"), _c("ruta", "U", "link"), _c("observaciones", "V"),
            _c("origen", None),  # 'app' | 'excel' | 'pdf'
        )),
        TablaDef("m210_alquiler", "09_210_ALQUILER", "210A", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("inmueble", "C"), _c("contrato", "D"),
            _c("periodo", "E"), _c("desde", "F", "date"), _c("hasta", "G", "date"), _c("pais", "H"),
            _c("ingresos", "I", "eur"), _c("gastos", "J", "eur"), _c("base", "K", "eur"), _c("tipo", "L", "pct"),
            _c("cuota", "M", "eur"), _c("pagado", "N", "eur"), _c("fecha_presentacion", "O", "date"),
            _c("fecha_pago", "P", "date"), _c("nrc", "Q"), _c("estado", "R"), _c("ruta", "S", "link"),
            _c("observaciones", "T"), _c("detalle_limite", None),
        )),
        TablaDef("arrastres_gastos", "", "ARR", (
            _c("codigo", None), _c("inmueble", None), _c("ejercicio", None, "int"),
            _c("generado", None, "eur"), _c("aplicado", None, "eur"), _c("pendiente", None, "eur"),
            _c("saldos", None), _c("aplicaciones", None), _c("caducados", None),
            _c("estado", None), _c("observaciones", None),
        )),
        TablaDef("m210_imputacion", "10_210_IMPUTACION", "210I", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("inmueble", "C"), _c("dias", "D", "int"),
            _c("valor_catastral", "E", "eur"), _c("pct_imputacion", "F", "pct"), _c("pct_titularidad", "G", "pct"),
            _c("renta_imputada_formula", "H", "formula"), _c("tipo", "I", "pct"),
            _c("cuota_formula", "J", "formula"), _c("pagado", "K", "eur"), _c("fecha_presentacion", "L", "date"),
            _c("fecha_pago", "M", "date"), _c("nrc", "N"), _c("estado", "O"), _c("ruta", "P", "link"),
            _c("observaciones", "Q"),
            # Solo BD: el mismo cálculo que hacen las fórmulas H y J, para mostrarlo en la app
            _c("renta_imputada", None, "eur"), _c("cuota", None, "eur"),
        )),
        TablaDef("patrimonio_714", "11_PATRIMONIO_714", "714", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("pais", "C"), _c("ccaa", "D"), _c("normativa", "E"),
            _c("valor_bienes", "F", "eur"), _c("deudas", "G", "eur"), _c("base", "H", "eur"),
            _c("minimo_exento", "I", "eur"), _c("base_liquidable", "J", "eur"), _c("cuota_integra", "K", "eur"),
            _c("bonificacion", "L", "eur"), _c("resultado", "M", "eur"), _c("pagado", "N", "eur"),
            _c("fecha_presentacion", "O", "date"), _c("fecha_pago", "P", "date"), _c("csv", "Q"),
            _c("estado", "R"), _c("ruta", "S", "link"), _c("observaciones", "T"),
        )),
        TablaDef("its_718", "12_ITS_718", "718", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("pais", "C"), _c("criterio", "D"),
            _c("base", "E", "eur"), _c("cuota", "F", "eur"), _c("deducciones", "G", "eur"),
            _c("resultado", "H", "eur"), _c("pagado", "I", "eur"), _c("fecha_presentacion", "J", "date"),
            _c("fecha_pago", "K", "date"), _c("csv", "L"), _c("estado", "M"), _c("ruta", "N", "link"),
            _c("observaciones", "O"),
        )),
        TablaDef("documentos", "13_DOCUMENTACION", "DOC", (
            _c("codigo", "A"), _c("inmueble", "B"), _c("contrato", "C"), _c("ejercicio", "D", "int"),
            _c("tipo", "E"), _c("descripcion", "F"), _c("estado", "G"), _c("fecha_documento", "H", "date"),
            _c("fecha_caducidad", "I", "date"), _c("ruta", "J", "link"), _c("ultima_revision", "K", "date"),
            _c("observaciones", "L"),
            _c("sha256", None), _c("nombre_original", None),
        )),
        TablaDef("obligaciones", "14_OBLIGACIONES", "OBL", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("tipo", "C"), _c("inmueble", "D"),
            _c("estado", "E"), _c("fecha_limite", "F", "date"), _c("fecha_presentacion", "G", "date"),
            _c("pagado", "H", "eur"), _c("doc_faltante", "I"), _c("id_modelo", "J"), _c("notas", "K"),
        )),
        TablaDef("notas", "15_NOTAS", "NOT", (
            _c("codigo", "A"), _c("fecha", "B", "date"), _c("categoria", "C"), _c("asunto", "D"),
            _c("nota", "E"), _c("proxima_accion", "F"), _c("fecha_proxima", "G", "date"), _c("responsable", "H"),
        )),
        # ---- Residentes fiscales (autónomos y pymes): plantilla Plantilla_Expediente_Residente_v1.xlsx ----
        TablaDef("actividades", "01_ACTIVIDAD", "ACT", (
            _c("codigo", "A"), _c("fecha_alta", "B", "date"), _c("epigrafe_iae", "C"), _c("descripcion", "D"),
            _c("regimen_iva", "E"), _c("regimen_irpf", "F"),
            _c("modelos", "G"),  # "130,303,111,115" separados por coma
            _c("obligado_130", "H"), _c("fecha_baja", "I", "date"), _c("ruta", "J", "link"),
            _c("observaciones", "K"),
        ), cartera=RES),
        TablaDef("facturas_emitidas", "02_FACTURAS_EMITIDAS", "FE", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("trimestre", "C"), _c("fecha", "D", "date"),
            _c("numero", "E"), _c("receptor", "F"), _c("nif_receptor", "G"), _c("concepto", "H"),
            _c("base", "I", "eur"), _c("tipo_iva", "J", "pct"), _c("cuota_iva", "K", "eur"),
            _c("tipo_retencion", "L", "pct"), _c("retencion", "M", "eur"), _c("total", "N", "formula"),
            _c("estado_cobro", "O"), _c("ruta", "P", "link"), _c("observaciones", "Q"), _c("origen", None),
        ), cartera=RES),
        TablaDef("facturas_recibidas", "03_FACTURAS_RECIBIDAS", "FR", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("trimestre", "C"), _c("fecha", "D", "date"),
            _c("numero", "E"), _c("proveedor", "F"), _c("nif_proveedor", "G"), _c("concepto", "H"),
            _c("categoria", "I"), _c("base", "J", "eur"), _c("tipo_iva", "K", "pct"), _c("cuota_iva", "L", "eur"),
            _c("pct_iva_deducible", "M", "pct"), _c("tipo_retencion", "N", "pct"), _c("retencion", "O", "eur"),
            _c("total", "P", "formula"), _c("pct_deducible_irpf", "Q", "pct"), _c("gasto_irpf", "R", "formula"),
            _c("ruta", "S", "link"), _c("observaciones", "T"), _c("origen", None),
        ), cartera=RES),
        TablaDef("m303", "04_MODELO_303", "303", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("periodo", "C"), _c("base_devengada", "D", "eur"),
            _c("iva_devengado", "E", "eur"), _c("base_deducible", "F", "eur"), _c("iva_deducible", "G", "eur"),
            _c("compensacion_anterior", "H", "eur"), _c("resultado", "I", "eur"), _c("a_ingresar", "J", "eur"),
            _c("a_compensar", "K", "eur"), _c("estado", "L"), _c("fecha_presentacion", "M", "date"),
            _c("nrc", "N"), _c("ruta", "O", "link"), _c("observaciones", "P"),
        ), cartera=RES),
        TablaDef("m130", "05_MODELO_130", "130", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("periodo", "C"), _c("ingresos", "D", "eur"),
            _c("gastos", "E", "eur"), _c("rendimiento", "F", "eur"), _c("pago_20", "G", "eur"),
            _c("retenciones", "H", "eur"), _c("pagos_anteriores", "I", "eur"), _c("resultado", "J", "eur"),
            _c("a_ingresar", "K", "eur"), _c("estado", "L"), _c("fecha_presentacion", "M", "date"),
            _c("nrc", "N"), _c("ruta", "O", "link"), _c("observaciones", "P"),
        ), cartera=RES),
        TablaDef("m111_115", "06_RETENCIONES_111_115", "RET", (
            _c("codigo", "A"), _c("ejercicio", "B", "int"), _c("periodo", "C"), _c("modelo", "D"),
            _c("perceptores", "E", "int"), _c("base", "F", "eur"), _c("retenciones", "G", "eur"), _c("estado", "H"),
            _c("fecha_presentacion", "I", "date"), _c("nrc", "J"), _c("ruta", "K", "link"),
            _c("observaciones", "L"),
        ), cartera=RES),
        TablaDef("modelos_presentados", "07_MODELOS_PRESENTADOS", "MOD", (
            _c("codigo", "A"), _c("modelo", "B"), _c("ejercicio", "C", "int"), _c("periodo", "D"),
            _c("fecha_presentacion", "E", "date"), _c("importe", "F", "eur"), _c("csv", "G"),
            _c("estado", "H"), _c("ruta", "I", "link"), _c("observaciones", "J"),
        ), cartera=RES),
    ]
}

# Documentación, obligaciones y notas existen en las dos plantillas con las mismas columnas.
for _tabla, _hoja in (("documentos", "08_DOCUMENTACION"), ("obligaciones", "09_OBLIGACIONES"),
                      ("notas", "10_NOTAS")):
    TABLAS[_tabla] = replace(TABLAS[_tabla], cartera="AMBAS", hoja_res=_hoja)


def hojas_cartera(cartera: str) -> list[tuple[TablaDef, str]]:
    """(tabla, hoja) de la plantilla de una cartera, en el orden de TABLAS."""
    return [(t, t.hoja_en(cartera)) for t in TABLAS.values() if t.hoja_en(cartera)]


# Celdas de 00_RESUMEN que la app rellena (datos del cliente). El resto son fórmulas de la plantilla.
RESUMEN_CELDAS = {
    "nombre_completo": "B4", "nif": "B5", "pasaporte": "B6", "nacionalidad": "B7", "email": "B8",
    "telefono": "B9", "responsable": "B10", "carpeta": "B11", "fecha_revision": "B12", "ejercicio": "E4",
}
RESUMEN_CELDAS_RES = {
    "nombre_completo": "B4", "nif": "B5", "tipo_cliente": "B6", "email": "B7", "telefono": "B8",
    "responsable": "B9", "carpeta": "B10", "fecha_revision": "B11", "ejercicio": "E4",
}
CELDAS_RESUMEN = {NR: RESUMEN_CELDAS, RES: RESUMEN_CELDAS_RES}

# Listas de la plantilla (validaciones de datos). Los valores que escribe la app deben salir de aquí.
ESTADOS_MODELO = ("Pendiente", "En preparación", "Falta documentación", "Revisar", "Presentado", "No procede")
ESTADOS_INMUEBLE = ("Activo", "Vendido", "Baja", "En revisión")
ESTADOS_CONTRATO = ("Vigente", "Finalizado", "Rescindido anticipadamente", "Prorrogado", "Pendiente firma", "Cancelado")
ESTADOS_DOCUMENTO = ("Disponible", "Falta", "Pendiente actualizar", "No procede", "Caducado")
ESTADOS_COBRO = ("Cobrado", "Parcial", "Pendiente", "Impagado", "Compensado")
TIPOS_OBLIGACION = ("210 Alquiler", "210 Imputación", "Patrimonio 714", "ITS 718", "Otra")
TIPOS_OBLIGACION_RES = ("303 IVA", "130 IRPF", "111 Retenciones", "115 Alquileres", "Otra")
CATEGORIAS_FACTURA_RECIBIDA = ("Compras", "Alquiler local", "Suministros", "Servicios profesionales", "Seguros",
                               "Seguridad Social", "Material de oficina", "Reparación/conservación",
                               "Publicidad", "Viajes y transporte", "Comisiones bancarias", "Otros")
ESTADOS_COBRO_FACTURA = ("Cobrada", "Pendiente", "Parcial", "Impagada")
CATEGORIAS_GASTO = ("IBI", "Comunidad", "Seguro", "Reparación/conservación", "Administración", "Agencia",
                    "Suministros", "Intereses/financiación", "Amortización", "Servicios profesionales",
                    "Tasa/basura", "Otros")
PERIODICIDADES = ("Mensual", "Trimestral", "Semestral", "Anual", "Otro")
FUENTES_CATASTRO = ("Catastro", "IBI Ayuntamiento", "SUMA", "Diputación", "Otro")


# --------------------------------------------------------------------------------------------- utilidades
def slug(texto: str, mayusculas: bool = False) -> str:
    """'Pepito Pérez' -> 'Pepito_Perez'. Sin tildes ni caracteres no válidos en Windows."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")
    return t.upper() if mayusculas else t



def componente_ruta(texto: str, limite: int = 64) -> str:
    """Componente Unicode válido en Windows; conserva nombres internacionales."""
    normal = unicodedata.normalize("NFKD", texto or "")
    normal = "".join(c for c in normal if not unicodedata.combining(c))
    normal = re.sub(r"[^\w-]+", "_", normal, flags=re.UNICODE).strip("_") or "Cliente"
    if normal.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)],
                          *[f"LPT{i}" for i in range(10)]}:
        normal = "_" + normal
    if len(normal) > limite:
        sufijo = hashlib.sha256(texto.encode("utf-8")).hexdigest()[:10]
        normal = normal[:limite - 11] + "_" + sufijo
    return normal


def subcarpeta_cliente(carpeta: Path, nombre: str = SUBCARPETA_SOPORTES) -> Path:
    """Rechaza junctions/enlaces que redirigen una subcarpeta del expediente."""
    if nombre not in {SUBCARPETA_SOPORTES, SUBCARPETA_ENTRADA}:
        raise ValueError("Subcarpeta de expediente no permitida.")
    base = Path(carpeta).absolute()
    for parte in (base, *base.parents):
        if parte.is_symlink() or (parte.exists() and getattr(parte.lstat(), "st_file_attributes", 0) & 1024):
            raise ValueError("La carpeta del expediente contiene un enlace o una junction.")
    if base.resolve() != base:
        raise ValueError('La carpeta del expediente apunta a otra ubicación.')
    base = base.resolve()
    esperada = base / nombre
    try:
        if esperada.resolve() != esperada:
            raise ValueError("La carpeta del expediente apunta a otra ubicación. Revisa sus permisos.")
    except (OSError, RuntimeError):
        raise ValueError("No se puede verificar la carpeta del expediente.") from None
    return esperada


def ruta_soporte(carpeta: Path, relativa: str) -> Path:
    """Solo archivos PDF directos de Soportes_PDF, sin rutas absolutas ni escapes."""
    ruta = Path(str(relativa).replace("\\", "/"))
    if ruta.is_absolute() or PureWindowsPath(str(relativa)).drive or ".." in ruta.parts:
        raise ValueError("La ruta del PDF sale de los soportes del cliente.")
    soportes = subcarpeta_cliente(carpeta)
    if len(ruta.parts) != 2 or ruta.parts[0].casefold() != SUBCARPETA_SOPORTES.casefold():
        raise ValueError("El documento debe estar dentro de Soportes_PDF.")
    if re.search(r'[<>:"|?*]', ruta.name) or ruta.name.endswith((" ", ".")):
        raise ValueError("El nombre del PDF contiene caracteres no permitidos en Windows.")
    candidato = soportes / ruta.name
    if (any(unicodedata.category(c).startswith('C') for c in ruta.name)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]", ruta.name.split('.', 1)[0].rstrip(' '), re.I)
            or candidato.is_symlink()
            or (candidato.exists() and getattr(candidato.lstat(), "st_file_attributes", 0) & 1024)):
        raise ValueError("Nombre reservado o enlace de PDF no permitido.")
    destino = candidato.resolve()
    if destino.parent != soportes or destino.suffix.lower() != ".pdf":
        raise ValueError("La ruta del PDF sale de los soportes del cliente.")
    return destino


def a_iso(valor: Any) -> Optional[str]:
    """Normaliza date/datetime/'dd/mm/yyyy'/'yyyy-mm-dd' a 'YYYY-MM-DD'. None/'' -> None."""
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    s = str(valor).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Fecha no reconocida: {valor!r}")


def a_fecha(valor: Any) -> Optional[date]:
    iso = a_iso(valor)
    return date.fromisoformat(iso) if iso else None


def a_num(valor: Any) -> Optional[float]:
    """Acepta 1234.5, '1.234,50', '1234,5 €', '50%' (-> 0.5 si lleva %)."""
    if valor in (None, ""):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace("€", "").replace(" ", "").replace("\u00a0", "")
    pct = s.endswith("%")
    s = s.rstrip("%")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"[-+]?[1-9]\d{0,2}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    n = float(s)
    return n / 100 if pct else n


def _normalizar(col: Columna, valor: Any) -> Any:
    if valor in (None, ""):
        return None
    if col.tipo == "date":
        return a_iso(valor)
    if col.tipo in ("eur", "pct"):
        n = a_num(valor)
        decimal_finito(n)
        if col.tipo == "pct":
            explicito = isinstance(valor, str) and valor.strip().endswith("%")
            if not explicito and n > 1 and float(n).is_integer():  # compatibilidad: 50 -> 0.5
                n /= 100
            return round(fraccion(n), 6)
        return redondear_eur(n)
    if col.tipo == "int":
        return int(a_num(valor))
    return str(valor).strip() if isinstance(valor, str) else valor


# --------------------------------------------------------------------------------------------- motor
class Database:
    def __init__(self, db_path: Path | str = DB_PATH_DEFECTO, multihilo: bool = False,
                 solo_lectura: bool = False):
        """``multihilo=True``: la conexión se usa desde varios hilos (UI Flet). Activa WAL para que una
        conexión de lectura no choque con la de escritura."""
        ruta = Path(db_path).absolute()
        self.db_path = sanitize_safe_path(ruta.parent, ruta)
        self._nivel_transaccion = 0
        self._documento_origen = None
        self._registrando_documentos = False
        if not solo_lectura:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        destino = self.db_path.resolve().as_uri() + "?mode=ro" if solo_lectura else str(self.db_path)
        self.con = sqlite3.connect(destino, uri=solo_lectura, check_same_thread=not multihilo, timeout=30)
        try:
            self.con.create_function("casefold", 1, lambda v: str(v or "").casefold(), deterministic=True)
            self.con.row_factory = sqlite3.Row
            self.con.execute("PRAGMA foreign_keys = ON")
            self.con.execute("PRAGMA temp_store = MEMORY")
            # Tope por conexión, no reserva anticipada de 64 MiB.
            self.con.execute("PRAGMA cache_size = -64000")
            version = self.con.execute("PRAGMA user_version").fetchone()[0]
            if version > VERSION_ESQUEMA:
                raise RuntimeError("Esta base de datos requiere una versión más reciente del programa.")
            self.con.execute("PRAGMA synchronous = NORMAL")
            if solo_lectura:
                self.con.execute("PRAGMA query_only = ON")
            else:
                # Antes de cualquier DDL/PRAGMA persistente sobre una BD existente.
                tiene_datos = self.con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone() is not None
                if version < VERSION_ESQUEMA and tiene_datos:
                    crear_snapshot(self.db_path)
                self.con.execute("PRAGMA journal_mode = WAL")
                if version < VERSION_ESQUEMA:
                    with self.transaccion():
                        # Otra instancia puede haber migrado mientras se creaba la copia.
                        version = self.con.execute("PRAGMA user_version").fetchone()[0]
                        if version > VERSION_ESQUEMA:
                            raise RuntimeError("La versión de la base de datos no es compatible.")
                        if version < VERSION_ESQUEMA:
                            self._crear_esquema()
                            self.con.execute(f"PRAGMA user_version = {VERSION_ESQUEMA}")
        except BaseException:
            self.con.close()
            raise

    def _confirmar(self) -> None:
        if self._nivel_transaccion == 0:
            self.con.commit()

    @contextmanager
    def transaccion(self):
        """Agrupa CRUD sin commits por fila. Anidar usa savepoints; no confirma una transacción ajena.

        El controlador mantiene su bloqueo durante el lote. No hacer commit/rollback directo de la
        conexión dentro del bloque. Los archivos Excel se escriben después de confirmar el recálculo.
        """
        propia = self._nivel_transaccion == 0 and not self.con.in_transaction
        nombre = f"lote_{self._nivel_transaccion + 1}"
        self.con.execute("BEGIN IMMEDIATE" if propia else f"SAVEPOINT {nombre}")
        self._nivel_transaccion += 1
        try:
            yield self
            if propia:
                self.con.commit()
            else:
                self.con.execute(f"RELEASE SAVEPOINT {nombre}")
        except BaseException:
            if self.con.in_transaction:
                if propia:
                    self.con.rollback()
                else:
                    self.con.execute(f"ROLLBACK TO SAVEPOINT {nombre}")
                    self.con.execute(f"RELEASE SAVEPOINT {nombre}")
            raise
        finally:
            self._nivel_transaccion -= 1

    def cerrar(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cerrar()

    # ------------------------------------------------------------------ esquema
    def _crear_esquema(self) -> None:
        cur = self.con.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT)")
        cur.execute(
            """CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL, apellidos TEXT NOT NULL DEFAULT '', nif TEXT NOT NULL UNIQUE,
                pasaporte TEXT, nacionalidad TEXT, email TEXT, telefono TEXT, responsable TEXT,
                tipo_cliente TEXT NOT NULL DEFAULT 'No residente',  -- 'No residente' | 'Autónomo' | 'Pyme'
                carpeta TEXT NOT NULL, fecha_revision TEXT, creado TEXT NOT NULL,
                cartera TEXT NOT NULL DEFAULT 'NR')"""
        )
        if "cartera" not in {r["name"] for r in cur.execute("PRAGMA table_info(clientes)")}:
            # Migración: la cartera se deriva del tipo y se guarda para listarla con un índice exacto
            cur.execute("ALTER TABLE clientes ADD COLUMN cartera TEXT NOT NULL DEFAULT 'NR'")
            cur.execute("UPDATE clientes SET cartera='RES' WHERE tipo_cliente IN ('Autónomo','Pyme')")
        for t in TABLAS.values():
            cols = [f"{c.campo} {_SQL_TIPO[c.tipo]}" for c in t.columnas_bd if c.campo != "codigo"]
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {t.nombre} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                    codigo TEXT NOT NULL, clave TEXT, actualizado TEXT,
                    {", ".join(cols)},
                    UNIQUE(cliente_id, codigo))"""
            )
            cur.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{t.nombre}_clave ON {t.nombre}(cliente_id, clave) "
                "WHERE clave IS NOT NULL"
            )
            # Migración suave: columnas añadidas en versiones posteriores
            existentes = {r["name"] for r in cur.execute(f"PRAGMA table_info({t.nombre})")}
            for c in t.columnas_bd:
                if c.campo not in existentes and c.campo != "codigo":
                    cur.execute(f"ALTER TABLE {t.nombre} ADD COLUMN {c.campo} {_SQL_TIPO[c.tipo]}")
        # Metadatos de corrección solo en BD: ninguna columna nueva en las plantillas.
        if "manual_override" not in {r["name"] for r in cur.execute("PRAGMA table_info(inmuebles)")}:
            cur.execute("ALTER TABLE inmuebles ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")
        cur.execute("""CREATE TABLE IF NOT EXISTS overrides_inmuebles (
            cliente_id INTEGER NOT NULL, codigo TEXT NOT NULL, campo TEXT NOT NULL,
            actualizado TEXT NOT NULL, PRIMARY KEY(cliente_id,codigo,campo),
            FOREIGN KEY(cliente_id,codigo) REFERENCES inmuebles(cliente_id,codigo) ON DELETE CASCADE)""")
        # Historial documental independiente del formato sagrado de las plantillas.
        from documentos import crear_esquema
        crear_esquema(self)
        self._confirmar()

        # Los UNIQUE existentes ya indexan nif y (cliente_id, codigo): no duplicarlos.
        cur.execute("CREATE INDEX IF NOT EXISTS ix_clientes_orden ON clientes(apellidos,nombre,id)")
        # Cartera + orden: listar 600 clientes de una cartera sin tabla temporal de ordenación
        cur.execute("CREATE INDEX IF NOT EXISTS ix_clientes_cartera_orden ON clientes(cartera,apellidos,nombre,id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_clientes_tipo ON clientes(tipo_cliente)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_inmuebles_override ON inmuebles(cliente_id,manual_override,codigo)")
        for t in TABLAS.values():
            campos = {c.campo for c in t.columnas_bd}
            grupos = []
            if "ejercicio" in campos:
                grupos.append(("ejercicio", "codigo"))
            if "inmueble" in campos:
                grupos.append(("inmueble", "ejercicio", "codigo") if "ejercicio" in campos
                              else ("inmueble", "codigo"))
            if "contrato" in campos:
                grupos.append(("contrato", "codigo"))
            if "ref_catastral" in campos:
                grupos.append(("ref_catastral",))
            if t.nombre == "documentos":
                grupos.append(("sha256",))
            for columnas in grupos:
                nombre = f"ix_{t.nombre}_{'_'.join(columnas)}"
                cur.execute(f"CREATE INDEX IF NOT EXISTS {nombre} ON {t.nombre}"
                            f"(cliente_id,{','.join(columnas)})")
        self._confirmar()

    # ------------------------------------------------------------------ config
    def get_config(self, clave: str, defecto: Optional[str] = None) -> Optional[str]:
        r = self.con.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
        return r["valor"] if r else defecto

    def set_config(self, clave: str, valor: str) -> None:
        self.con.execute("INSERT INTO config(clave, valor) VALUES(?,?) "
                         "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (clave, str(valor)))
        self._confirmar()

    @property
    def carpeta_base(self) -> Path:
        """Carpeta que contiene las dos raíces Clientes_No_Residentes y Clientes_Residentes."""
        return Path(self.get_config("carpeta_base_clientes", str(PROYECTO_DIR)))

    @carpeta_base.setter
    def carpeta_base(self, ruta: Path | str) -> None:
        base = Path(ruta)
        for cartera in RAIZ:
            (base / RAIZ[cartera]).mkdir(parents=True, exist_ok=True)
        self.set_config("carpeta_base_clientes", str(base))

    # Compatibilidad: «carpeta de clientes» es ahora la carpeta base de las dos carteras
    carpeta_clientes = carpeta_base

    def raiz(self, cartera: str) -> Path:
        return self.carpeta_base / RAIZ[cartera]

    def verificar_cartera(self, cliente: dict) -> Path:
        """Carpeta del cliente si vive en la raíz de SU cartera y sin enlaces; si no, ValueError.

        Se compara el nombre de la raíz (no la ruta completa) para que siga valiendo si cambia la base.
        """
        carpeta = Path(cliente["carpeta"])
        esperada = RAIZ[cartera_de(cliente.get("tipo_cliente"))]
        try:
            real = carpeta.resolve()
        except (OSError, RuntimeError):
            raise ValueError("No se puede verificar la carpeta del expediente.") from None
        if os.path.normcase(str(real)) != os.path.normcase(os.path.abspath(carpeta)):
            raise ValueError("La carpeta del expediente apunta a otra ubicación (enlace). No se usa.")
        if real.parent.name != esperada:
            raise ValueError(f"La carpeta del expediente no está en {esperada}: no se usa para no mezclar "
                             "carteras.")
        return real

    def migrar_carteras(self) -> dict:
        """Una sola vez: mueve cada expediente a la raíz de su cartera. Todo o nada.

        Copia la BD antes. Si un movimiento falla, deshace los anteriores, la BD no cambia y se reintenta en
        el siguiente arranque. Devuelve {"movidos": n, "copia": ruta | None}.
        """
        pendientes = list((self.db_path.parent / "backups").glob(f"{self.db_path.stem}_antes_carteras_*.recuperacion.json"))
        if pendientes:
            error = RuntimeError(f"Hay una migración pendiente de recuperación manual: {pendientes[0]}")
            error.requiere_recuperacion = True
            raise error
        if self.get_config("version_carteras") == "1":
            return {"movidos": 0, "copia": None}
        legado = self.get_config("carpeta_clientes")
        base = Path(self.get_config("carpeta_base_clientes", str(PROYECTO_DIR)))
        if legado:
            try:
                defecto = Path(legado).resolve() == CARPETA_CLIENTES_DEFECTO.resolve()
            except OSError:
                defecto = False
            if not defecto:
                base = Path(legado)
        for cartera in RAIZ:  # las dos raíces existen siempre, aunque una cartera esté vacía
            try:
                (base / RAIZ[cartera]).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        clientes = [dict(r) for r in self.con.execute("SELECT * FROM clientes ORDER BY id")]
        if not clientes:
            with self.transaccion():
                self.set_config("carpeta_base_clientes", str(base))
                self.set_config("version_carteras", "1")
            return {"movidos": 0, "copia": None}
        copia = self.db_path.parent / "backups" / f"{self.db_path.stem}_antes_carteras_{datetime.now():%Y%m%d_%H%M%S_%f}.db"
        copia.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(copia)) as destino_copia:
            self.con.backup(destino_copia)
        plan, reservados, origenes = [], set(), set()
        for c in clientes:
            cartera = cartera_de(c.get("tipo_cliente"))
            origen = Path(c["carpeta"])
            if origen.resolve() != Path(os.path.abspath(origen)):
                raise ValueError("El origen de una migración no puede ser un enlace o junction.")
            if origen in origenes:
                raise ValueError("Dos expedientes comparten la misma carpeta: revisa las rutas antes de migrar.")
            origenes.add(origen)
            raiz = base / RAIZ[cartera]
            if raiz.resolve() != Path(os.path.abspath(raiz)):
                raise ValueError("La raíz de destino de una migración no puede ser un enlace o junction.")
            if origen.parent.name == RAIZ[cartera]:
                continue
            destino, n = base / RAIZ[cartera] / origen.name, 2
            while destino.exists() or destino in reservados:
                destino, n = base / RAIZ[cartera] / f"{origen.name}_{n}", n + 1
            reservados.add(destino)
            plan.append((c, origen, destino, cartera))
        hechos = []
        try:
            for c, origen, destino, cartera in plan:
                destino.parent.mkdir(parents=True, exist_ok=True)
                if origen.exists():
                    origen.rename(destino)
                    hechos.append((origen, destino))
            with self.transaccion():
                for c in clientes:
                    self.con.execute("UPDATE clientes SET cartera=? WHERE id=?",
                                     (cartera_de(c.get("tipo_cliente")), c["id"]))
                for c, origen, destino, cartera in plan:
                    self.con.execute("UPDATE clientes SET carpeta=?, cartera=? WHERE id=?",
                                     (str(destino), cartera, c["id"]))
                self.set_config("carpeta_base_clientes", str(base))
                self.set_config("version_carteras", "1")
        except BaseException as causa:
            sin_revertir = []
            for origen, destino in reversed(hechos):
                try:
                    destino.rename(origen)
                except OSError as exc:
                    sin_revertir.append({"origen": str(origen), "destino": str(destino), "error": type(exc).__name__})
            if sin_revertir:
                registro = copia.with_suffix(".recuperacion.json")
                detalle = ""
                try:
                    registro.write_text(json.dumps({"copia_bd": str(copia), "movimientos_pendientes": sin_revertir},
                                                   ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    detalle = f" No se pudo guardar el registro: {sin_revertir!r}."
                error = RuntimeError(f"La reversión de carpetas quedó incompleta. Revisa la copia {copia} y "
                                     f"el registro {registro} antes de reintentar.{detalle}")
                error.requiere_recuperacion = True
                raise error from causa
            raise
        for c, origen, destino, cartera in plan:
            try:
                self.asegurar_carpetas({**c, "carpeta": str(destino)})
                if cartera == RES:  # el Excel NR de un autónomo pasa a copias: tendrá su plantilla RES
                    for viejo in destino.glob("NR_*.xlsx"):
                        respaldo = destino / "_backups" / f"{viejo.stem}_antes_residentes{viejo.suffix}"
                        respaldo.parent.mkdir(exist_ok=True)
                        if not respaldo.exists():
                            viejo.rename(respaldo)
            except (OSError, ValueError):
                pass  # el expediente ya está en su sitio; lo accesorio no deshace la migración
        if legado:
            antigua = Path(legado)
            try:
                eliminados = antigua / "_Eliminados"
                if eliminados.is_dir() and not (base / RAIZ[NR] / "_Eliminados").exists():
                    eliminados.rename(base / RAIZ[NR] / "_Eliminados")
                if antigua.is_dir() and antigua != base and not any(antigua.iterdir()):
                    antigua.rmdir()
            except OSError:
                pass
        return {"movidos": len(hechos), "copia": copia}

    # ------------------------------------------------------------------ clientes
    def crear_cliente(self, nombre: str, apellidos: str, nif: str, **campos) -> dict:
        """Crea el cliente y su carpeta ``<raiz>/<Nombre_Apellidos>_<NIF>/`` con Soportes_PDF y _ARRASTRA_AQUI.

        Si el NIF ya existe devuelve el cliente existente (idempotente)."""
        nif = nif.strip().upper()
        existente = self.get_cliente_por_nif(nif)
        if existente:
            self.asegurar_carpetas(existente)
            return existente
        if not nif or not slug(nif):
            raise ValueError("Indica un NIF o identificador válido para crear el expediente.")
        identificador = slug(nif)
        if identificador != nif:
            identificador += "_" + hashlib.sha256(nif.encode("utf-8")).hexdigest()[:10]
        campos["tipo_cliente"] = validar_tipo(campos.get("tipo_cliente") or TIPO_NR)
        cartera = cartera_de(campos["tipo_cliente"])
        carpeta = self.raiz(cartera) / componente_ruta(f"{nombre} {apellidos}_{identificador}")
        # La raíz y el propio expediente tampoco pueden ser junctions de otro cliente.
        self.verificar_cartera({"carpeta": str(carpeta), "tipo_cliente": campos["tipo_cliente"]})
        inicial, numero = carpeta, 2
        while carpeta.exists() or self.con.execute(
                "SELECT 1 FROM clientes WHERE carpeta=? COLLATE NOCASE LIMIT 1", (str(carpeta),)).fetchone():
            carpeta = inicial.with_name(componente_ruta(f"{inicial.name}_{numero}"))
            self.verificar_cartera({"carpeta": str(carpeta), "tipo_cliente": campos["tipo_cliente"]})
            numero += 1
        subcarpeta_cliente(carpeta)
        subcarpeta_cliente(carpeta, SUBCARPETA_ENTRADA)
        permitidos = {"pasaporte", "nacionalidad", "email", "telefono", "responsable", "tipo_cliente",
                      "fecha_revision"}
        extra = {k: v for k, v in campos.items() if k in permitidos}
        cols = ["nombre", "apellidos", "nif", "carpeta", "creado", "cartera", *extra]
        vals = [nombre.strip(), apellidos.strip(), nif, str(carpeta), datetime.now().isoformat(timespec="seconds"),
                cartera, *extra.values()]
        cur = self.con.execute(f"INSERT INTO clientes({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        self._confirmar()
        cliente = self.get_cliente(cur.lastrowid)
        self.asegurar_carpetas(cliente)
        return cliente

    def asegurar_carpetas(self, cliente: dict) -> None:
        base = self.verificar_cartera(cliente)
        for nombre in (SUBCARPETA_SOPORTES, SUBCARPETA_ENTRADA):
            subcarpeta_cliente(base, nombre).mkdir(parents=True, exist_ok=True)

    def get_cliente(self, cliente_id: int) -> Optional[dict]:
        r = self.con.execute("SELECT * FROM clientes WHERE id=?", (cliente_id,)).fetchone()
        return dict(r) if r else None

    def get_cliente_por_nif(self, nif: str) -> Optional[dict]:
        r = self.con.execute("SELECT * FROM clientes WHERE nif=?", (nif.strip().upper(),)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def _limites(limite: int, desplazamiento: int) -> tuple[int, int]:
        if (isinstance(limite, bool) or not isinstance(limite, int) or not 1 <= limite <= 200
                or isinstance(desplazamiento, bool) or not isinstance(desplazamiento, int)
                or desplazamiento < 0):
            raise ValueError("Página no válida: límite 1–200 y desplazamiento no negativo.")
        return limite, desplazamiento

    def listar_clientes(self, limite: int = 50, desplazamiento: int = 0, busqueda: str = "",
                        cartera: Optional[str] = None) -> list[dict]:
        """Página acotada; búsqueda Unicode literal, sin comodines ni SQL del usuario.

        ``cartera`` (NR / RES) limita a esa cartera con el índice ix_clientes_cartera_orden."""
        self._limites(limite, desplazamiento)
        condiciones, valores = [], []
        if cartera is not None:
            if cartera not in RAIZ:
                raise ValueError(f"Cartera desconocida: {cartera!r}")
            condiciones.append("cartera=?")
            valores.append(cartera)
        if busqueda.strip():
            condiciones.append("instr(casefold(nombre || ' ' || apellidos || ' ' || nif), ?) > 0")
            valores.append(busqueda.strip().casefold())
        filtro = (" WHERE " + " AND ".join(condiciones)) if condiciones else ""
        sql = "SELECT * FROM clientes" + filtro + " ORDER BY apellidos,nombre,id LIMIT ? OFFSET ?"
        return [dict(r) for r in self.con.execute(sql, (*valores, limite, desplazamiento))]

    def contar_clientes(self, cartera: Optional[str] = None) -> int:
        if cartera is None:
            return self.con.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
        if cartera not in RAIZ:
            raise ValueError(f"Cartera desconocida: {cartera!r}")
        return self.con.execute("SELECT COUNT(*) FROM clientes WHERE cartera=?", (cartera,)).fetchone()[0]

    def actualizar_cliente(self, cliente_id: int, **campos) -> dict:
        permitidos = {"nombre", "apellidos", "pasaporte", "nacionalidad", "email", "telefono", "responsable",
                      "tipo_cliente", "carpeta", "fecha_revision"}
        campos = {k: v for k, v in campos.items() if k in permitidos}
        if "tipo_cliente" in campos:
            campos["tipo_cliente"] = validar_tipo(campos["tipo_cliente"])
            campos["cartera"] = cartera_de(campos["tipo_cliente"])
        if campos:
            sets = ", ".join(f"{k}=?" for k in campos)
            self.con.execute(f"UPDATE clientes SET {sets} WHERE id=?", (*campos.values(), cliente_id))
            self._confirmar()
        return self.get_cliente(cliente_id)

    @staticmethod
    def nombre_excel(cliente: dict) -> str:
        """Nombre recomendado: NR_APELLIDOS_NOMBRE_NIE.xlsx (no residentes) o RES_… (residentes)."""
        prefijo = PREFIJO_EXCEL[cartera_de(cliente.get("tipo_cliente"))]
        nombre = (f"{prefijo}_{slug(cliente['apellidos'], True)}_{slug(cliente['nombre'], True)}_"
                  f"{slug(cliente['nif'], True)}")
        return componente_ruta(nombre, 64) + ".xlsx"

    @staticmethod
    def plantilla(cliente: dict) -> Path:
        return PLANTILLAS[cartera_de(cliente.get("tipo_cliente"))]

    def ruta_excel(self, cliente: dict) -> Path:
        base = Path(cliente["carpeta"])
        prefijo = PREFIJO_EXCEL[cartera_de(cliente.get("tipo_cliente"))]
        anterior = (f"{prefijo}_{slug(cliente['apellidos'], True)}_{slug(cliente['nombre'], True)}_"
                    f"{slug(cliente['nif'], True)}.xlsx")
        try:
            if (base / anterior).is_file():
                return base / anterior
        except OSError:
            pass  # nombre legado demasiado largo
        return base / self.nombre_excel(cliente)

    # ------------------------------------------------------------------ CRUD genérico
    def _tabla(self, tabla: str) -> TablaDef:
        if tabla not in TABLAS:
            raise KeyError(f"Tabla desconocida: {tabla}")
        return TABLAS[tabla]

    def siguiente_codigo(self, cliente_id: int, tabla: str) -> str:
        t = self._tabla(tabla)
        if not t.prefijo:
            raise ValueError(f"La tabla {tabla} no genera códigos: hay que pasar 'codigo'")
        patron = re.compile(rf"^{re.escape(t.prefijo)}-(\d+)$")
        mayor = 0
        for r in self.con.execute(f"SELECT codigo FROM {tabla} WHERE cliente_id=?", (cliente_id,)):
            m = patron.match(r["codigo"] or "")
            if m:
                mayor = max(mayor, int(m.group(1)))
        return f"{t.prefijo}-{mayor + 1:03d}"

    def upsert(self, tabla: str, cliente_id: int, datos: dict, clave: Optional[str] = None,
               solo_si_vacio: Iterable[str] = (), respetar_manuales: bool = True) -> str:
        """Inserta o actualiza una fila y devuelve su ``codigo``.

        Búsqueda de la fila existente: por ``datos['codigo']`` y, si no, por ``clave``.
        - Campos desconocidos o de fórmula se ignoran.
        - ``None`` en ``datos`` NO borra el valor existente (una extracción parcial no machaca datos buenos).
          Para borrar un campo usa ``vaciar_campo``.
        - ``solo_si_vacio``: campos que solo se escriben si en BD están vacíos (respeta lo que metió el usuario).
        """
        t = self._tabla(tabla)
        validos = {c.campo: c for c in t.columnas_bd}
        limpio = {k: _normalizar(validos[k], v) for k, v in datos.items() if k in validos}
        if tabla == "facturas_recibidas" and limpio.get("categoria"):
            categorias = {c.casefold(): c for c in CATEGORIAS_FACTURA_RECIBIDA}
            limpio["categoria"] = categorias.get(str(limpio["categoria"]).casefold(), "Otros")
        if tabla == "gastos" and limpio.get("categoria"):
            categorias = {c.casefold(): c for c in CATEGORIAS_GASTO}
            limpio["categoria"] = categorias.get(limpio["categoria"].casefold(), limpio["categoria"])
        codigo = limpio.pop("codigo", None)

        fila = None
        if codigo:
            fila = self.get(tabla, cliente_id, codigo)
        if fila is None and clave:
            r = self.con.execute(f"SELECT * FROM {tabla} WHERE cliente_id=? AND clave=?", (cliente_id, clave)).fetchone()
            fila = dict(r) if r else None
        if tabla in ("facturas_emitidas", "facturas_recibidas"):
            fecha = limpio.get("fecha") or (fila or {}).get("fecha")
            if fecha:
                f = date.fromisoformat(fecha)
                limpio.update(ejercicio=f.year, trimestre=f"{(f.month - 1) // 3 + 1}T")
            else:
                limpio["trimestre"] = None  # Sin fecha queda pendiente de clasificar.
        if (tabla == "gastos" and limpio.get("ejercicio") is None
                and (fila or {}).get("ejercicio") is None):
            inferido = ejercicio_registrado({**(fila or {}), **limpio})
            if inferido is not None:
                limpio["ejercicio"] = inferido
        ahora = datetime.now().isoformat(timespec="seconds")

        if fila is None:
            codigo = codigo or self.siguiente_codigo(cliente_id, tabla)
            limpio = {k: v for k, v in limpio.items() if v is not None}
            cols = ["cliente_id", "codigo", "clave", "actualizado", *limpio]
            vals = [cliente_id, str(codigo), clave, ahora, *limpio.values()]
            self.con.execute(f"INSERT INTO {tabla}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        else:
            codigo = fila["codigo"]
            solo_si_vacio = set(solo_si_vacio)
            protegidos = self.campos_manuales_inmueble(cliente_id, codigo) if tabla == "inmuebles" and respetar_manuales else set()
            limpio = {k: v for k, v in limpio.items() if k not in protegidos}
            cambios = {k: v for k, v in limpio.items()
                       if (v is not None or (k == "trimestre" and tabla in ("facturas_emitidas", "facturas_recibidas")))
                       and not (k in solo_si_vacio and fila.get(k) not in (None, ""))}
            if clave and not fila.get("clave"):
                cambios["clave"] = clave
            if cambios:
                cambios["actualizado"] = ahora
                sets = ", ".join(f"{k}=?" for k in cambios)
                self.con.execute(f"UPDATE {tabla} SET {sets} WHERE id=?", (*cambios.values(), fila["id"]))
        from documentos import registrar_mutacion
        registrar_mutacion(self, tabla, cliente_id, str(codigo), fila,
                           self.get(tabla, cliente_id, str(codigo)), set(limpio))
        self._confirmar()
        return str(codigo)

    def campos_manuales_inmueble(self, cliente_id: int, codigo: str) -> set[str]:
        """Protección por campo, persistente y aislada por cliente."""
        return {r["campo"] for r in self.con.execute(
            "SELECT campo FROM overrides_inmuebles WHERE cliente_id=? AND codigo=?", (cliente_id, codigo))}

    def proteger_campos_inmueble(self, cliente_id: int, codigo: str, campos: Iterable[str]) -> None:
        validos = {c.campo for c in TABLAS["inmuebles"].columnas_bd} - {"codigo", "carpeta"}
        seleccion = set(campos) & validos
        if not seleccion:
            return
        with self.transaccion():
            if self.get("inmuebles", cliente_id, codigo) is None:
                raise ValueError("Inmueble inexistente para este cliente")
            self.con.executemany("INSERT INTO overrides_inmuebles(cliente_id,codigo,campo,actualizado) VALUES(?,?,?,?) "
                                 "ON CONFLICT(cliente_id,codigo,campo) DO UPDATE SET actualizado=excluded.actualizado",
                                 [(cliente_id, codigo, c, datetime.now().isoformat(timespec="seconds")) for c in seleccion])
            self.con.execute("UPDATE inmuebles SET manual_override=1 WHERE cliente_id=? AND codigo=?", (cliente_id, codigo))

    def vaciar_campo(self, tabla: str, cliente_id: int, codigo: str, campo: str) -> None:
        t = self._tabla(tabla)
        if campo not in {c.campo for c in t.columnas_bd} or campo == "codigo":
            raise KeyError(campo)
        derivado = ", trimestre=NULL" if campo == "fecha" and tabla in ("facturas_emitidas", "facturas_recibidas") else ""
        previa = self.get(tabla, cliente_id, codigo)
        self.con.execute(f"UPDATE {tabla} SET {campo}=NULL{derivado} WHERE cliente_id=? AND codigo=?", (cliente_id, codigo))
        from documentos import registrar_mutacion
        registrar_mutacion(self, tabla, cliente_id, codigo, previa, self.get(tabla, cliente_id, codigo), {campo})
        self._confirmar()

    def get(self, tabla: str, cliente_id: int, codigo: str) -> Optional[dict]:
        self._tabla(tabla)
        r = self.con.execute(f"SELECT * FROM {tabla} WHERE cliente_id=? AND codigo=?", (cliente_id, str(codigo))).fetchone()
        return dict(r) if r else None

    def get_por_clave(self, tabla: str, cliente_id: int, clave: str) -> Optional[dict]:
        self._tabla(tabla)
        r = self.con.execute(f"SELECT * FROM {tabla} WHERE cliente_id=? AND clave=?", (cliente_id, clave)).fetchone()
        return dict(r) if r else None

    def _consulta_listado(self, tabla: str, cliente_id: int, orden: str, filtros: dict, incluir_retirados=False):
        t = self._tabla(tabla)
        validos = {c.campo for c in t.columnas_bd} | {"clave"}
        if orden not in validos | {"id"}:
            raise KeyError(orden)
        where, vals = ["cliente_id=?"], [cliente_id]
        if tabla == "documentos" and not incluir_retirados:
            where.append("NOT EXISTS (SELECT 1 FROM documentos_gestion dg WHERE dg.cliente_id=documentos.cliente_id "
                         "AND dg.codigo=documentos.codigo AND dg.en_papelera=1)")
        for k, v in filtros.items():
            if k not in validos:
                raise KeyError(k)
            where.append(f"{k} IS NULL" if v is None else f"{k}=?")
            if v is not None:
                vals.append(v)
        sql = f"SELECT * FROM {tabla} WHERE {' AND '.join(where)} ORDER BY {orden}, id"
        return sql, vals

    def iterar(self, tabla: str, cliente_id: int, orden: str = "codigo", tamano: int = 200,
               incluir_retirados: bool = False, **filtros):
        """Recorre todas las filas de un cliente con memoria acotada, para exportaciones."""
        self._limites(tamano, 0)
        sql, vals = self._consulta_listado(tabla, cliente_id, orden, filtros, incluir_retirados)
        cursor = self.con.execute(sql, vals)
        try:
            while True:
                lote = cursor.fetchmany(tamano)
                if not lote:
                    break
                yield from (dict(r) for r in lote)
        finally:
            cursor.close()

    def listar(self, tabla: str, cliente_id: int, orden: str = "codigo", **filtros) -> list[dict]:
        # Compatibilidad: los cálculos fiscales deben recibir todas las filas del cliente.
        return list(self.iterar(tabla, cliente_id, orden, **filtros))

    def listar_pagina(self, tabla: str, cliente_id: int, limite: int = 50, desplazamiento: int = 0,
                      orden: str = "codigo", **filtros) -> list[dict]:
        self._limites(limite, desplazamiento)
        sql, vals = self._consulta_listado(tabla, cliente_id, orden, filtros)
        return [dict(r) for r in self.con.execute(sql + " LIMIT ? OFFSET ?", (*vals, limite, desplazamiento))]

    def borrar(self, tabla: str, cliente_id: int, codigo: str) -> None:
        self._tabla(tabla)
        self.con.execute(f"DELETE FROM {tabla} WHERE cliente_id=? AND codigo=?", (cliente_id, codigo))
        self._confirmar()

    # ------------------------------------------------------------------ atajos de dominio
    def residencia(self, cliente_id: int, ejercicio: int) -> Optional[dict]:
        """Residencia del ejercicio; si no hay fila para ese año usa la más reciente anterior (memoria interanual)."""
        r = self.con.execute("SELECT * FROM residencias WHERE cliente_id=? AND ejercicio=? "
                             "ORDER BY codigo,id LIMIT 1", (cliente_id, ejercicio)).fetchone()
        if r is None:
            r = self.con.execute("SELECT * FROM residencias WHERE cliente_id=? AND ejercicio<? "
                                 "ORDER BY ejercicio DESC,codigo DESC,id DESC LIMIT 1",
                                 (cliente_id, ejercicio)).fetchone()
        return dict(r) if r else None

    def guardar_residencia(self, cliente_id: int, ejercicio: int, **campos) -> str:
        campos.update(codigo=str(ejercicio), ejercicio=ejercicio)
        return self.upsert("residencias", cliente_id, campos)

    def catastro(self, cliente_id: int, inmueble: str, ejercicio: int) -> Optional[dict]:
        """Catastro del ejercicio; si falta, el del año anterior más próximo (y lo indica con '_heredado')."""
        actual = self.con.execute("SELECT * FROM catastro WHERE cliente_id=? AND inmueble=? AND ejercicio=? "
                                  "ORDER BY codigo DESC,id DESC LIMIT 1", (cliente_id, inmueble, ejercicio)).fetchone()
        if actual is not None:
            r = dict(actual)
            if not r.get("valor_catastral"):
                previa = self.con.execute("SELECT * FROM catastro WHERE cliente_id=? AND inmueble=? "
                    "AND ejercicio<? AND valor_catastral IS NOT NULL AND valor_catastral!=0 "
                    "ORDER BY ejercicio DESC,codigo DESC,id DESC LIMIT 1", (cliente_id, inmueble, ejercicio)).fetchone()
                if previa:
                    for campo in ("valor_catastral", "valor_suelo", "valor_construccion", "anio_revision"):
                        if r.get(campo) in (None, ""):
                            r[campo] = previa[campo]
                    r["_heredado"] = previa["ejercicio"]
            return r
        previa = self.con.execute("SELECT * FROM catastro WHERE cliente_id=? AND inmueble=? AND ejercicio<? "
                                  "ORDER BY ejercicio DESC,codigo DESC,id DESC LIMIT 1",
                                  (cliente_id, inmueble, ejercicio)).fetchone()
        if previa:
            return {**dict(previa), "_heredado": previa["ejercicio"]}
        return None

    def registrar_documento(self, cliente_id: int, ruta_relativa: str, tipo: str, sha256: str,
                            nombre_original: str, **campos) -> tuple[str, bool]:
        """Alta en 13_DOCUMENTACION. Devuelve (codigo, es_nuevo). Mismo sha256 => no duplica."""
        previo = self.con.execute("SELECT codigo FROM documentos WHERE cliente_id=? AND sha256=?",
                                  (cliente_id, sha256)).fetchone()
        if previo:
            retirado = self.con.execute("SELECT en_papelera FROM documentos_gestion WHERE cliente_id=? AND codigo=?",
                                        (cliente_id, previo["codigo"])).fetchone()
            if retirado and retirado["en_papelera"]:
                raise ValueError("Este PDF está en la papelera. Restáuralo explícitamente antes de volver a importarlo.")
            return previo["codigo"], False
        datos = {"ruta": ruta_relativa, "tipo": tipo, "sha256": sha256, "nombre_original": nombre_original,
                 "estado": "Disponible", "ultima_revision": date.today(), **campos}
        return self.upsert("documentos", cliente_id, datos), True

    def nota(self, cliente_id: int, categoria: str, asunto: str, texto: str, clave: Optional[str] = None,
             proxima_accion: Optional[str] = None) -> str:
        return self.upsert("notas", cliente_id, {"fecha": date.today(), "categoria": categoria, "asunto": asunto,
                                                  "nota": texto, "proxima_accion": proxima_accion}, clave=clave)
