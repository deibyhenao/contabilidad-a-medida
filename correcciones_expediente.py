"""Correcciones propuestas por IA sobre datos de origen; nunca SQL, rutas o cálculos fiscales libres."""
import re
import unicodedata

from database import TABLAS


TABLAS_PERMITIDAS = frozenset({
    "inmuebles", "catastro", "contratos", "rentas", "inquilinos", "cobros", "gastos", "residencias",
    "actividades", "facturas_emitidas", "facturas_recibidas",
})
EXCLUIDOS = frozenset({"codigo", "cliente_id", "id", "inmueble", "contrato", "ejercicio", "origen",
    "carpeta", "ruta", "id_210", "sha256", "manual_override", "revision_pendiente", "pct_titularidad_escritura",
    "grupo", "importe_deducible", "fecha_ultima_modificacion"})
HERRAMIENTA = {
    "name": "proponer_correccion_expediente",
    "description": "Propone corregir UN dato de origen de un registro existente, por orden explícita del último mensaje del usuario. Usa código y tabla del contexto. No ejecuta nada. Pregunta por fechas o códigos ambiguos. Para dejar un trimestre sin alquiler, corrige la fecha real de fin del contrato; no inventes cuotas ni marques declaraciones presentadas.",
    "input_schema": {"type": "object", "additionalProperties": False,
        "properties": {"tabla": {"type": "string", "enum": sorted(TABLAS_PERMITIDAS)},
            "codigo": {"type": "string", "maxLength": 40},
            "campo": {"type": "string", "maxLength": 60},
            "valor": {"type": "string", "maxLength": 500,
                      "description": "Dato exacto confirmado por el usuario. Fechas ISO, importes en formato español; vacío solo si pide borrar ese dato."}},
        "required": ["tabla", "codigo", "campo", "valor"]},
}


def validar_plan(datos):
    from controlador import CALCULADOS
    if type(datos) is not dict or set(datos) not in (
            {"tabla", "codigo", "campo", "valor"}, {"accion", "tabla", "codigo", "campo", "valor"}):
        raise ValueError("La propuesta de corrección no es válida.")
    if datos.get("accion", "editar_registro") != "editar_registro":
        raise ValueError("Acción de corrección no permitida.")
    tabla, codigo, campo, valor = (datos.get(k) for k in ("tabla", "codigo", "campo", "valor"))
    if not isinstance(tabla, str) or tabla not in TABLAS_PERMITIDAS:
        raise ValueError("Esta tabla no admite correcciones desde el chat.")
    if not isinstance(codigo, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", codigo):
        raise ValueError("Usa el código de un registro existente del expediente.")
    permitidos = {c.campo for c in TABLAS[tabla].columnas_bd} - EXCLUIDOS - CALCULADOS.get(tabla, set())
    if not isinstance(campo, str) or campo not in permitidos:
        raise ValueError("Ese campo requiere su acción específica o se calcula automáticamente. Corrige sus datos de origen.")
    if (not isinstance(valor, str) or len(valor) > 500
            or any(unicodedata.category(c).startswith('C') for c in valor)):
        raise ValueError("El valor debe ser texto breve sin caracteres de control.")
    return {"accion": "editar_registro", "tabla": tabla, "codigo": codigo, "campo": campo, "valor": valor}
