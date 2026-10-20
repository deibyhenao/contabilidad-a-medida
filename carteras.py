"""Carteras de clientes: No Residentes y Residentes (autónomos y pymes).

Funciones puras, sin BD ni disco. Cada cartera tiene su carpeta raíz física, su plantilla Excel y sus
pestañas; un expediente nunca vive ni lee archivos de la otra cartera.

Autor: Claude (arquitecto). Auditor: Codex.
"""
from __future__ import annotations

NR = "NR"
RES = "RES"
CARTERAS = (NR, RES)

TIPO_NR = "No residente"
TIPO_AUTONOMO = "Autónomo"
TIPO_PYME = "Pyme"
TIPOS = {NR: (TIPO_NR,), RES: (TIPO_AUTONOMO, TIPO_PYME)}
TIPOS_CLIENTE = (TIPO_NR, TIPO_AUTONOMO, TIPO_PYME)

RAIZ = {NR: "Clientes_No_Residentes", RES: "Clientes_Residentes"}
PREFIJO_EXCEL = {NR: "NR", RES: "RES"}
ETIQUETA = {NR: "No residentes", RES: "Residentes"}

REGIMENES_IVA = ("General", "Recargo de equivalencia", "Exento", "Simplificado")
REGIMENES_IRPF = ("Directa simplificada", "Directa normal", "Objetiva")


def cartera_de(tipo_cliente: str | None) -> str:
    """Autónomo y Pyme son residentes; cualquier otro valor (incluido vacío) es No residente."""
    return RES if (tipo_cliente or "").strip() in TIPOS[RES] else NR


def tipo_por_defecto(cartera: str) -> str:
    return TIPOS[cartera][0] if cartera in TIPOS else TIPO_NR


def validar_tipo(tipo_cliente: str) -> str:
    tipo = (tipo_cliente or "").strip()
    if tipo not in TIPOS_CLIENTE:
        raise ValueError(f"Tipo de cliente no válido: {tipo_cliente!r}. Usa {', '.join(TIPOS_CLIENTE)}.")
    return tipo
