"""Casos de uso de la app, sin interfaz gráfica (testeable).

Autores: Antigravity (versión inicial en main.py), Claude (refactor, edición manual, IA, pendientes).
Orden obligatorio en toda escritura: importar Excel -> cambiar BD -> recalcular -> exportar Excel.
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import threading
from contextlib import nullcontext
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import carteras
import database
import documentos
import excel_generator
import fiscal_residentes
import ia_extractor
import pdf_processor
import tax_calculator
from carteras import NR, RES, cartera_de
from database import TABLAS, Database, a_fecha

ConfirmarFn = Callable[[pdf_processor.Extraccion], pdf_processor.Extraccion]


def _carpeta_sin_enlaces(carpeta: Path) -> Path:
    """Rechaza una junction tanto en la carpeta como en cualquiera de sus padres."""
    carpeta = Path(os.path.abspath(carpeta))
    try:
        if carpeta.resolve() != carpeta:
            raise ValueError("La carpeta apunta a otra ubicación (enlace). No se usa.")
    except (OSError, RuntimeError):
        raise ValueError("No se puede verificar la ubicación de la carpeta.") from None
    return carpeta


class CanceladoPorUsuario(Exception):
    """El usuario descartó el documento en el diálogo de revisión."""


# Etiquetas de los campos que no tienen columna en la plantilla (solo BD)
ETIQUETAS_SOLO_BD = {
    "ejercicio": "Ejercicio", "gananciales_forzado": "Gananciales 50 % forzado",
    "pct_titularidad_escritura": "% titularidad según escritura", "valor_comprobado": "Valor comprobado Administración",
    "gastos_venta": "Gastos de venta", "valor_referencia": "Valor de referencia", "origen": "Origen",
    "renta_imputada": "Renta imputada", "cuota": "Cuota", "sha256": "Huella", "nombre_original": "Nombre original",
}

SI_NO = ("Sí", "No", "Revisar")
OPCIONES: dict[tuple[str, str], tuple[str, ...]] = {
    ("inmuebles", "estado"): database.ESTADOS_INMUEBLE,
    ("inmuebles", "uso_valor_referencia"): SI_NO,
    ("inmuebles", "tipo_adquisicion"): ("Compraventa", "Herencia", "Donación", "Obra nueva", "Otro"),
    ("contratos", "estado"): database.ESTADOS_CONTRATO,
    ("contratos", "periodicidad"): database.PERIODICIDADES,
    ("contratos", "tipo"): ("Vivienda", "Temporada", "Uso distinto de vivienda", "Habitación", "Otro"),
    ("rentas", "periodicidad"): database.PERIODICIDADES,
    ("inquilinos", "tipo_persona"): ("Persona física", "Persona jurídica"),
    ("catastro", "fuente"): database.FUENTES_CATASTRO,
    ("gastos", "categoria"): database.CATEGORIAS_GASTO,
    ("gastos", "incluido_210"): ("Sí", "No", "Pendiente"),
    ("cobros", "estado"): database.ESTADOS_COBRO,
    ("m210_alquiler", "estado"): database.ESTADOS_MODELO,
    ("m210_imputacion", "estado"): database.ESTADOS_MODELO,
    ("patrimonio_714", "estado"): database.ESTADOS_MODELO,
    ("its_718", "estado"): database.ESTADOS_MODELO,
    ("obligaciones", "estado"): database.ESTADOS_MODELO,
    ("obligaciones", "tipo"): database.TIPOS_OBLIGACION,
    ("documentos", "estado"): database.ESTADOS_DOCUMENTO,
    ("residencias", "certificado"): ("Sí", "No", "Pendiente", "No procede"),
    ("residencias", "gastos_deducibles"): SI_NO,
    # ---- residentes
    ("actividades", "regimen_iva"): carteras.REGIMENES_IVA,
    ("actividades", "regimen_irpf"): carteras.REGIMENES_IRPF,
    ("actividades", "obligado_130"): ("Sí", "No"),
    ("facturas_emitidas", "estado_cobro"): database.ESTADOS_COBRO_FACTURA,
    ("facturas_recibidas", "categoria"): database.CATEGORIAS_FACTURA_RECIBIDA,
    ("m303", "estado"): database.ESTADOS_MODELO,
    ("m130", "estado"): database.ESTADOS_MODELO,
    ("m111_115", "estado"): database.ESTADOS_MODELO,
    ("m111_115", "modelo"): ("111", "115"),
    ("modelos_presentados", "estado"): database.ESTADOS_MODELO,
}
OPCIONES[("obligaciones", "tipo")] = database.TIPOS_OBLIGACION + database.TIPOS_OBLIGACION_RES[:-1]
# Campos que la app calcula sola: se muestran pero se avisa de que se recalculan
CALCULADOS = {
    "m210_alquiler": {"ingresos", "gastos", "base", "tipo", "cuota", "desde", "hasta", "pais", "contrato"},
    "m210_imputacion": {"dias", "valor_catastral", "pct_imputacion", "pct_titularidad", "tipo", "renta_imputada", "cuota"},
    "patrimonio_714": {"valor_bienes", "base", "minimo_exento", "base_liquidable", "cuota_integra", "resultado"},
    "its_718": {"base", "cuota", "deducciones", "resultado"},
    "cobros": {"importe_contractual"},
    "facturas_emitidas": {"ejercicio", "trimestre"},
    "facturas_recibidas": {"ejercicio", "trimestre"},
    "m303": {"base_devengada", "iva_devengado", "base_deducible", "iva_deducible", "compensacion_anterior",
             "resultado", "a_ingresar", "a_compensar"},
    "m130": {"ingresos", "gastos", "rendimiento", "pago_20", "retenciones", "pagos_anteriores", "resultado",
             "a_ingresar"},
    "m111_115": {"perceptores", "base", "retenciones"},
}
OCULTOS = {"codigo", "sha256", "nombre_original", "origen", "pct_titularidad_escritura", "renta_imputada", "cuota",
           "detalle_limite"}
CAMPOS_PRESENTACION = {
    "m210_alquiler": ("fecha_presentacion", "pagado", "nrc"),
    "m210_imputacion": ("fecha_presentacion", "pagado", "nrc"),
    "patrimonio_714": ("fecha_presentacion", "pagado", "csv"),
    "its_718": ("fecha_presentacion", "pagado", "csv"),
    "obligaciones": ("fecha_presentacion", "pagado"),
    "m303": ("fecha_presentacion", "nrc"),
    "m130": ("fecha_presentacion", "nrc"),
    "m111_115": ("fecha_presentacion", "nrc"),
}
# Declaraciones de residentes: el recálculo los congela al estar presentados
_DECLARACIONES_RES = ("m303", "m130", "m111_115")
_PREFIJO_A_TABLA = {t.prefijo: t.nombre for t in TABLAS.values() if t.prefijo}


@lru_cache(maxsize=1)
def _cabeceras_plantilla() -> dict[str, dict[str, str]]:
    """{hoja: {letra: texto de la fila 1}} leído de la plantilla (modo solo lectura, rápido)."""
    import openpyxl
    from openpyxl.utils import get_column_letter
    out = {}
    for plantilla in database.PLANTILLAS.values():  # las hojas no se repiten entre las dos plantillas
        if not Path(plantilla).exists():
            continue
        wb = openpyxl.load_workbook(plantilla, read_only=True)
        try:
            for t in TABLAS.values():
                for hoja in {t.hoja, t.hoja_res} - {""}:
                    if hoja in wb.sheetnames and hoja not in out:
                        fila = next(wb[hoja].iter_rows(min_row=1, max_row=1, values_only=True), ())
                        out[hoja] = {get_column_letter(i): str(v) for i, v in enumerate(fila, 1) if v}
        finally:
            wb.close()
    return out


def etiquetas(tabla: str) -> dict[str, str]:
    t = TABLAS[tabla]
    cab = _cabeceras_plantilla().get(t.hoja, {})
    return {c.campo: (cab.get(c.letra) if c.letra else None) or ETIQUETAS_SOLO_BD.get(c.campo)
            or c.campo.replace("_", " ").capitalize() for c in t.columnas_bd}


def campos_editables(tabla: str) -> list[database.Columna]:
    return [c for c in TABLAS[tabla].columnas_bd if c.campo not in OCULTOS]


class Controlador:
    """Orquestador de casos de uso. Seguro entre hilos: escrituras bajo ``_lock``; la UI lee con ``lector``."""

    def __init__(self, db_path: Path | None = None):
        ruta = Path(db_path) if db_path is not None else database.DB_PATH_DEFECTO
        self.db = Database(ruta, multihilo=True)
        self.lector = Database(ruta, multihilo=True)  # conexión aparte para que la UI lea sin bloquear
        self._lock = threading.RLock()
        self._firmas: dict[int, Optional[str]] = {}  # firma del Excel leída en la última importación
        self.aviso_migracion: Optional[str] = None
        try:  # una sola vez: cada expediente a la raíz de su cartera (todo o nada, con copia de la BD)
            self.migracion = self.db.migrar_carteras()
        except Exception as exc:  # la app sigue con las rutas antiguas y se reintenta al reabrir
            self.migracion = None
            detalle = ("La recuperación quedó incompleta: revisa la copia de la BD y el manifiesto de "
                       "recuperación antes de continuar." if getattr(exc, "requiere_recuperacion", False)
                       else "No se ha movido nada; se reintentará al volver a abrir la app.")
            self.aviso_migracion = ("No se pudieron ordenar las carpetas en No residentes / Residentes "
                                    f"({exc.__class__.__name__}: {exc}). {detalle}")

    def cerrar(self) -> None:
        with self._lock:
            self.db.cerrar()
            self.lector.cerrar()

    # ------------------------------------------------------------------ configuración
    def cambiar_carpeta_clientes(self, ruta: Path) -> None:
        with self._lock:
            self.db.carpeta_clientes = Path(ruta)

    def ajustes_ia(self) -> ia_extractor.AjustesIA:
        return ia_extractor.AjustesIA.desde_db(self.lector)

    def guardar_ajustes_ia(self, ajustes: ia_extractor.AjustesIA, clave: Optional[str] = None) -> None:
        with self._lock:
            ajustes.guardar(self.db)
            if clave:
                ia_extractor.guardar_clave(clave)

    def borrar_clave_ia(self) -> None:
        ia_extractor.borrar_clave()

    def estado_ia(self) -> tuple[bool, str]:
        aj = self.ajustes_ia()
        if not aj.activada:
            return False, "Lectura con IA desactivada · lector local"
        if not ia_extractor.hay_clave():
            return False, "Falta la clave API de Claude · lector local"
        return True, f"Lectura con IA activa · {aj.modelo}"

    # ------------------------------------------------------------------ clientes
    def crear_cliente(self, nombre: str, apellidos: str, nif: str, tipo_cliente: str = "No residente",
                      pais: str | None = None, ejercicio: int | None = None, regimen_iva: str | None = None,
                      regimen_irpf: str | None = None) -> dict:
        """Alta en la carpeta de su cartera. NR: país de residencia. Residente: régimen de IVA e IRPF."""
        with self._lock:
            if self.db.get_cliente_por_nif((nif or "").strip().upper()):
                raise ValueError("Ya existe un cliente con ese NIF/NIE. Abre su ficha para corregir sus datos; no se ha creado ni modificado ningún expediente.")
            carteras.validar_tipo(tipo_cliente)
            if pais and ejercicio is not None:
                documentos._validar_ejercicio(ejercicio)
            cliente = self.db.crear_cliente(nombre, apellidos, nif, tipo_cliente=tipo_cliente)
            if cartera_de(cliente.get("tipo_cliente")) == RES:
                if (regimen_iva or regimen_irpf) and not self.db.listar("actividades", cliente["id"]):
                    self.db.upsert("actividades", cliente["id"], {
                        "descripcion": "Actividad principal", "regimen_iva": regimen_iva,
                        "regimen_irpf": regimen_irpf if cliente["tipo_cliente"] == carteras.TIPO_AUTONOMO else None,
                        "observaciones": "Datos indicados al crear el cliente; sube el 036/037 para completarlos."})
                return cliente
            if pais:
                ej = int(ejercicio or date.today().year)
                grupo = tax_calculator.grupo_territorio(pais)
                self.db.guardar_residencia(cliente["id"], ej, pais=pais, grupo=grupo,
                                           gastos_deducibles="Sí" if grupo == tax_calculator.GRUPO_UE else "No")
            return cliente

    def eliminar_cliente(self, cid: int) -> Path:
        """Quita al cliente de la app. Su carpeta (PDFs y Excel) NO se borra: se mueve a
        ``<carpeta de clientes>/_Eliminados/<carpeta>_<fecha-hora>`` para poder recuperarla.

        Orden seguro: primero se renombra la carpeta, sin copiar ni borrar archivos parcialmente.
        Si el sistema rechaza el traslado (p. ej. entre volúmenes), no se toca la BD.
        Después se borra de la BD; si la BD fallara, la carpeta vuelve a su sitio."""
        from datetime import datetime
        with self._lock:
            cliente = self.db.get_cliente(cid)
            if not cliente:
                raise ValueError("El cliente ya no existe.")
            origen = self.db.verificar_cartera(cliente)
            # La papelera tampoco puede redirigir los datos a otra cartera mediante una junction.
            papelera = _carpeta_sin_enlaces(origen.parent / "_Eliminados")
            destino = papelera / f"{origen.name}_{datetime.now():%Y%m%d_%H%M%S}"
            n = 2
            while destino.exists():
                destino = papelera / f"{origen.name}_{datetime.now():%Y%m%d_%H%M%S}_{n}"
                n += 1
            movida = False
            if origen.exists():
                papelera.mkdir(parents=True, exist_ok=True)
                try:
                    origen.rename(destino)
                except (PermissionError, OSError) as exc:
                    raise ValueError("No se pudo mover la carpeta del cliente: cierra su Excel o sus PDFs "
                                     "abiertos y vuelve a intentarlo. Comprueba también los permisos y que "
                                     "_Eliminados esté en la misma unidad. "
                                     f"({exc.__class__.__name__})") from None
                movida = True
            try:
                # Las tablas del cliente tienen ON DELETE CASCADE hacia clientes
                self.db.con.execute("DELETE FROM clientes WHERE id=?", (cid,))
                self.db.con.execute("DELETE FROM config WHERE clave IN (?,?,?)",
                                    (f"excel_firma|{cid}", f"excel_pendiente|{cid}", f"excel_resumen|{cid}"))
                self.db.con.commit()
            except Exception:
                self.db.con.rollback()
                if movida:
                    destino.rename(origen)
                raise
            self._firmas.pop(cid, None)
            return destino

    def abrir_cliente(self, cid: int, ejercicio: int) -> dict:
        """Carga el expediente. Si el usuario tocó el Excel fuera de la app, sincroniza primero (A3-P3)."""
        with self._lock:
            documentos.recuperar_archivos(self.db, cid)
            if excel_generator.excel_modificado(self.db, cid):
                return self.sincronizar(cid, ejercicio)
            if self._es_residente(cid):
                ej = int(ejercicio)
                todos = fiscal_residentes.ejercicios_encadenados(self.db, cid, {ej}, ej)
                with self.db.transaccion():
                    informes = {e: fiscal_residentes.recalcular_residente(self.db, cid, e) for e in todos}
                return {"informe": informes[ej], "estados_autonomo": informes[ej].estados, "excel": None,
                        "error_excel": None}
            with self.db.transaccion():
                informe = tax_calculator.recalcular_cliente(self.db, cid, int(ejercicio))
                estados_aut = self._autonomo(cid, int(ejercicio))
                if estados_aut:
                    informe.pendientes = tax_calculator.revisar_pendientes(self.db, cid, int(ejercicio))
            return {"informe": informe, "estados_autonomo": estados_aut,
                    "excel": None, "error_excel": None}

    def _es_residente(self, cid: int) -> bool:
        cliente = self.db.get_cliente(cid)
        return bool(cliente) and cartera_de(cliente.get("tipo_cliente")) == RES

    def _autonomo(self, cid: int, ej: int) -> list[str]:
        cliente = self.db.get_cliente(cid)
        if cliente and cliente.get("tipo_cliente") == "Autónomo":
            return tax_calculator.recalcular_autonomo(self.db, cid, ej)
        return []

    # ------------------------------------------------------------------ flujo central
    def _recalcular_bd(self, cid: int, ejercicios: set[int], ejercicio_activo: int) -> dict:
        ej = int(ejercicio_activo)
        # Solo años no prescritos (4) y no futuros, más el activo y declaraciones existentes desde el más antiguo
        hoy = date.today().year
        desde = min({int(e) for e in ejercicios} | {ej})
        tablas_modelo = (_DECLARACIONES_RES if self._es_residente(cid) else
                         ("m210_alquiler", "m210_imputacion", "patrimonio_714", "its_718"))
        # Un modelo ya calculado debe seguir coherente aunque sea histórico o
        # una previsión futura. No crear años nuevos por una etiqueta del PDF.
        existentes = {int(r[0]) for tabla in (*tablas_modelo, "obligaciones", "modelos_presentados")
            for r in self.db.con.execute(f"SELECT DISTINCT ejercicio FROM {tabla} WHERE cliente_id=? AND ejercicio>=?",
                                         (cid, desde)) if r[0] is not None}
        if self._es_residente(cid):
            # El 303 encadena lo «a compensar» de un año al siguiente: se recalcula en orden
            encadenados = set(fiscal_residentes.ejercicios_encadenados(self.db, cid, ejercicios, ej))
            # Completar los años intermedios necesarios para arrastrar saldos
            # hasta una previsión que ya existe, sin crear previsiones por etiquetas.
            hasta = max(encadenados | existentes)
            todos = sorted(encadenados | existentes | set(range(max(encadenados) + 1, hasta + 1)))
            with self.db.transaccion():
                informes = {e: fiscal_residentes.recalcular_residente(self.db, cid, e) for e in todos}
            estados_aut = informes[ej].estados
        else:
            todos = sorted({int(e) for e in ejercicios if hoy - 4 <= int(e) <= hoy}
                           | existentes | {ej})
            with self.db.transaccion():
                informes = tax_calculator.recalcular_ejercicios(self.db, cid, todos)
                estados_aut: list[str] = []
                for e in todos:
                    est = self._autonomo(cid, e)
                    if est:
                        informes[e].pendientes = tax_calculator.revisar_pendientes(self.db, cid, e)
                    if e == ej:
                        estados_aut = est
        return {"informe": informes[ej], "estados_autonomo": estados_aut}

    def _exportar_informe(self, cid: int, ejercicio_activo: int, resultado: dict) -> dict:
        ej = int(ejercicio_activo)
        self.db.set_config(f"excel_pendiente|{cid}", "1")
        try:
            if self._excel_cambiado_durante(cid):
                # El usuario guardó el Excel mientras trabajábamos (A4-P01): no se pisa su versión
                cliente = self.db.get_cliente(cid)
                base = self.db.ruta_excel(cliente)
                pendiente = base.with_name(f"{base.stem}_PENDIENTE_{date.today():%Y%m%d}{base.suffix}")
                ruta_excel = excel_generator.exportar_cliente(self.db, cid, ej, destino=pendiente)
                error_excel = ("Guardaste el Excel mientras se procesaba: tu versión se ha conservado. La versión "
                               f"actualizada está en {pendiente.name}. Pulsa Sincronizar para unirlas.")
            else:
                ruta_excel, error_excel = excel_generator.exportar_cliente(self.db, cid, ej), None
        except excel_generator.ExcelBloqueadoError as exc:
            ruta_excel, error_excel = exc.ruta_pendiente, str(exc)
        if error_excel:
            self.db.set_config(f"excel_pendiente|{cid}", str(ruta_excel))
        return {**resultado, "excel": ruta_excel, "error_excel": error_excel}

    def _recalcular_y_exportar(self, cid: int, ejercicios: set[int], ejercicio_activo: int) -> dict:
        return self._exportar_informe(cid, ejercicio_activo, self._recalcular_bd(cid, ejercicios, ejercicio_activo))

    def _importar(self, cid: int) -> None:
        """Importa lo manual del Excel y recuerda qué versión del archivo se leyó."""
        cliente = self.db.get_cliente(cid)
        ruta = self.db.ruta_excel(cliente) if cliente else None
        # Captura ANTES: un guardado externo durante la importación debe detectarse al exportar.
        firma = excel_generator._firma_excel(ruta) if ruta and ruta.exists() else None
        excel_generator.import_excel_to_db(self.db, cid)
        self._firmas[cid] = firma

    def _excel_cambiado_durante(self, cid: int) -> bool:
        if cid not in self._firmas:
            return False
        cliente = self.db.get_cliente(cid)
        ruta = self.db.ruta_excel(cliente) if cliente else None
        actual = excel_generator._firma_excel(ruta) if ruta and ruta.exists() else None
        return self._firmas.pop(cid) != actual

    # --------------------------------------------------------------- gestión de documentos
    def listar_documentos(self, cid: int, papelera: bool = False, limite: int = 51, desplazamiento: int = 0) -> list[dict]:
        return documentos.listar(self.lector, cid, papelera, limite, desplazamiento)

    def obtener_documento(self, cid: int, codigo: str) -> dict:
        return documentos._documento(self.lector, cid, codigo)

    def _version_documental(self, cid: int, codigo: str) -> str:
        doc = self.db.get("documentos", cid, codigo)
        if not doc:
            raise ValueError("El documento no pertenece a este cliente.")
        maximo = self.db.con.execute("SELECT COALESCE(MAX(id),0) FROM documentos_cambios WHERE cliente_id=?", (cid,)).fetchone()[0]
        cliente = self.db.get_cliente(cid)
        ruta = self.db.ruta_excel(cliente)
        meta = documentos._documento(self.db, cid, codigo)
        pdf = documentos._ruta(self.db, cid, meta['ruta_papelera'] if meta['en_papelera'] else doc['ruta'], papelera=meta['en_papelera'])
        datos = (doc, documentos._huella(pdf) if pdf.is_file() else None, documentos.revision(self.db, cid), maximo,
                 excel_generator._firma_excel(ruta) if ruta.exists() else None)
        return hashlib.sha256(json.dumps(datos, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _preparar_documento(self, cid: int, codigo: str, version_esperada=None) -> None:
        cliente = self.db.get_cliente(cid)
        if not cliente:
            raise ValueError("El cliente ya no existe.")
        self.db.verificar_cartera(cliente)
        documentos.recuperar_archivos(self.db, cid)
        self._importar(cid)
        if version_esperada is not None and self._version_documental(cid, codigo) != version_esperada:
            raise ValueError("Los datos o el Excel han cambiado desde la previsualización. Revisa de nuevo el documento antes de confirmar.")
        if not self.db.get("documentos", cid, codigo):
            raise ValueError("El documento no pertenece a este cliente.")

    def previsualizar_documento(self, cid: int, codigo: str, accion: str, ejercicio_destino=None) -> dict:
        with self._lock:
            self._preparar_documento(cid, codigo)
            accion_backend = "cambiar_ejercicio" if accion == "corregir_ejercicio" else accion
            vista = documentos.previsualizar(self.db, cid, codigo, accion_backend, ejercicio_destino)
            doc = self.db.get("documentos", cid, codigo)
            cliente = self.db.get_cliente(cid)
            return {**vista, "version": self._version_documental(cid, codigo), "documento": doc,
                    "cliente": {k: cliente[k] for k in ("id", "nombre", "apellidos", "nif")}}

    def _terminar_documentos(self, cid: int, ejercicio: int, calculo: dict, resultado=None) -> dict:
        avisos = []
        try:
            documentos.recuperar_archivos(self.db, cid)
        except Exception as exc:
            avisos.append("La operación está guardada. El movimiento del archivo queda pendiente de recuperación (" + type(exc).__name__ + ").")
        try:
            res = self._exportar_informe(cid, ejercicio, calculo)
        except Exception as exc:
            res = {**calculo, "excel": None, "error_excel": "Los datos se guardaron y recalcularon, pero no se pudo actualizar el Excel. Conserva el archivo y pulsa Sincronizar cuando esté disponible (" + type(exc).__name__ + ")."}
        return {**res, "resultado": resultado, "documentos_actualizados": True, "cliente_documentos": cid,
                "avisos_documentales": avisos}

    def _operar_documento(self, cid, codigo, ejercicio, accion, destino=None, version_esperada=None):
        with self._lock:
            self._preparar_documento(cid, codigo, version_esperada)
            proteccion = (documentos.proteger_movimiento(self.db, cid, codigo, accion == "retirar")
                          if accion in ("retirar", "restaurar") else nullcontext())
            with proteccion as copias, self.db.transaccion():
                if accion == "retirar":
                    afectados = documentos.retirar(self.db, cid, codigo, copias=copias)
                elif accion == "restaurar":
                    afectados = documentos.restaurar(self.db, cid, codigo, copias=copias)
                else:
                    afectados = documentos.cambiar_ejercicio(self.db, cid, codigo, destino)
                calculo = self._recalcular_bd(cid, set(afectados) | {int(ejercicio)}, int(ejercicio))
            return self._terminar_documentos(cid, int(ejercicio), calculo)

    def retirar_documento(self, cid, codigo, ejercicio_activo, version_esperada=None):
        return self._operar_documento(cid, codigo, ejercicio_activo, "retirar", version_esperada=version_esperada)

    def restaurar_documento(self, cid, codigo, ejercicio_activo, version_esperada=None):
        return self._operar_documento(cid, codigo, ejercicio_activo, "restaurar", version_esperada=version_esperada)

    def corregir_ejercicio_documento(self, cid, codigo, nuevo_ejercicio, ejercicio_activo, version_esperada=None):
        return self._operar_documento(cid, codigo, ejercicio_activo, "cambiar_ejercicio", nuevo_ejercicio, version_esperada)

    def preparar_accion_documental(self, cid, plan):
        from acciones_documentales import validar_plan
        import gestion_archivos
        plan = validar_plan(plan)
        with self._lock:
            self._preparar_documento(cid, plan['codigo'])
            doc = self.db.get('documentos', cid, plan['codigo'])
            if plan['accion'] == 'cambiar_ejercicio':
                vista = documentos.previsualizar(self.db, cid, plan['codigo'], 'cambiar_ejercicio', plan['ejercicio_destino'])
                documentos._exigir(vista)
                nombre = f"{plan['ejercicio_destino']}_{plan['codigo']}{Path(doc['ruta']).suffix.lower()}"
            else:
                vista = {'advertencias': [], 'ejercicio_origen': doc.get('ejercicio')}
                nombre = plan['nombre']
            _, _, relativa, _, _ = gestion_archivos.preparar(self.db, cid, plan['codigo'], nombre)
            return {**vista, 'version': self._version_documental(cid, plan['codigo']),
                    'ruta_origen': doc['ruta'], 'ruta_destino': relativa}

    def ejecutar_accion_documental(self, propuesta):
        from acciones_documentales import Propuesta, validar_plan
        import gestion_archivos
        if type(propuesta) is not Propuesta:
            raise ValueError('Se requiere una propuesta documental confirmada.')
        cid, ejercicio = propuesta.cliente_id, propuesta.ejercicio
        plan = validar_plan(dict(propuesta.plan))
        codigo = plan['codigo']
        with self._lock:
            self._preparar_documento(cid, codigo, propuesta.version)
            nombre = (f"{plan['ejercicio_destino']}_{codigo}{Path(self.db.get('documentos', cid, codigo)['ruta']).suffix.lower()}"
                      if plan['accion'] == 'cambiar_ejercicio' else plan['nombre'])
            with gestion_archivos.proteger(self.db, cid, codigo, nombre) as copias, self.db.transaccion():
                afectados = set()
                if plan['accion'] == 'cambiar_ejercicio':
                    afectados |= documentos.cambiar_ejercicio(self.db, cid, codigo, plan['ejercicio_destino'])
                afectados |= gestion_archivos.renombrar(self.db, cid, codigo, nombre, plan.get('descripcion'), copias=copias)
                self.db.con.execute('INSERT INTO documentos_operaciones(cliente_id,documento,accion,detalle) VALUES(?,?,?,?)',
                                    (cid, codigo, plan['accion'], documentos._json(plan)))
                calculo = self._recalcular_bd(cid, afectados | {ejercicio}, ejercicio)
            return self._terminar_documentos(cid, ejercicio, calculo)

    def sustituir_documento(self, cid, codigo, ruta_pdf, ejercicio_activo, confirmar=None, version_esperada=None):
        with self._lock:
            self._preparar_documento(cid, codigo, version_esperada)
            doc = self.db.get("documentos", cid, codigo)
            ruta_pdf = Path(ruta_pdf)
            # Revisar el nuevo PDF antes de modificar el soporte anterior.
            huella_nueva = pdf_processor._hash(ruta_pdf)
            ext = self.extraer(ruta_pdf)
            if confirmar is not None:
                ext = confirmar(ext)
                if not isinstance(ext, pdf_processor.Extraccion):
                    raise TypeError("La revisión debe devolver una extracción válida.")
            if pdf_processor._hash(ruta_pdf) != huella_nueva:
                raise ValueError("El PDF cambió durante la revisión. Vuelve a seleccionarlo antes de sustituir el documento.")
            self._preparar_documento(cid, codigo, version_esperada)
            if huella_nueva == doc.get("sha256"):
                raise ValueError("Has elegido el mismo PDF. El documento anterior sigue activo.")
            cliente = self.db.get_cliente(cid)
            soportes = database.subcarpeta_cliente(Path(cliente["carpeta"]))
            anteriores = set(soportes.glob("*"))
            try:
                with documentos.proteger_movimiento(self.db, cid, codigo, True) as copias, self.db.transaccion():
                    with documentos.conservar_correcciones(self.db, cid, codigo):
                        afectados = documentos.retirar(self.db, cid, codigo, copias=copias)
                        resultado = pdf_processor.aplicar(self.db, cid, ext, ruta_pdf, ejercicio=int(ejercicio_activo),
                                                           huella_esperada=huella_nueva)
                        if resultado.duplicado:
                            raise ValueError("El PDF elegido ya está registrado. El documento anterior se conserva activo.")
                    calculo = self._recalcular_bd(cid, set(afectados) | set(resultado.ejercicios_afectados) | {int(ejercicio_activo)}, int(ejercicio_activo))
            except BaseException:
                # Solo retirar copias nuevas creadas por esta importación fallida y verificadas por contenido.
                for archivo in set(soportes.glob("*")) - anteriores:
                    relativo = archivo.relative_to(Path(cliente["carpeta"])).as_posix()
                    registrado = self.db.con.execute("SELECT 1 FROM documentos WHERE cliente_id=? AND ruta=?", (cid, relativo)).fetchone()
                    if not registrado and archivo.resolve().parent == soportes and pdf_processor._hash(archivo) == huella_nueva:
                        archivo.unlink()
                raise
            return self._terminar_documentos(cid, int(ejercicio_activo), calculo, resultado)

    def extraer(self, ruta_pdf: Path) -> pdf_processor.Extraccion:
        return pdf_processor.extraer(Path(ruta_pdf), self.ajustes_ia())

    def procesar_pdf(self, cid: int, ruta_pdf: Path | str, ejercicio: int, confirmar: ConfirmarFn | None = None,
                     inmueble_destino: str | None = None, comentario_guia: str = "") -> dict:
        with self._lock:
            ej = int(ejercicio)
            cliente = self.db.get_cliente(cid)
            if cliente is None:
                raise ValueError(f"Cliente inexistente: {cid}")
            self.db.verificar_cartera(cliente)  # nunca se archiva un soporte fuera de la cartera del cliente
            self._importar(cid)
            resultado = pdf_processor.procesar_pdf(self.db, cid, Path(ruta_pdf), confirmar=confirmar,
                                                   inmueble_destino=inmueble_destino, ejercicio=ej,
                                                   **({"comentario_guia": comentario_guia} if comentario_guia else {}))
            return {"resultado": resultado,
                    **self._recalcular_y_exportar(cid, set(resultado.ejercicios_afectados) | {ej}, ej)}

    def sincronizar(self, cid: int, ejercicio: int) -> dict:
        with self._lock:
            self._importar(cid)
            existentes = {int(f["ejercicio"]) for f in self.db.listar("m210_alquiler", cid) if f.get("ejercicio")}
            return self._recalcular_y_exportar(cid, existentes | {int(ejercicio)}, int(ejercicio))

    def procesar_bandeja(self, cid: int, ejercicio: int, confirmar: ConfirmarFn | None = None,
                         comentario_guia: str = "", rutas: list[Path] | None = None) -> list[dict]:
        with self._lock:
            cliente = self.db.get_cliente(cid)
            if cliente is None:
                raise ValueError(f"Cliente inexistente: {cid}")
            bandeja = database.subcarpeta_cliente(Path(cliente["carpeta"]), database.SUBCARPETA_ENTRADA)
            bandeja.mkdir(parents=True, exist_ok=True)
            resultados: list[dict] = []
            elegidas = None if rutas is None else {Path(r).absolute() for r in rutas}
            for pdf_path in sorted(p for p in bandeja.iterdir() if p.is_file()
                    and p.suffix.lower() in {".pdf", ".xlsx", ".xls", ".docx", ".csv"} and p.resolve().parent == bandeja
                    and (elegidas is None or p.absolute() in elegidas)):
                error_msg: Optional[str] = None
                try:
                    res = self.procesar_pdf(cid, pdf_path, ejercicio, confirmar=confirmar,
                                            **({"comentario_guia": comentario_guia} if comentario_guia else {}))
                    pdf_path.unlink(missing_ok=True)
                    resultados.append({**res, "archivo": pdf_path.name, "ok": True, "error": None})
                except CanceladoPorUsuario:
                    # Se aparta: si se queda, el vigilante vuelve a abrir el diálogo cada 2 s (A3-01)
                    destino = self._mover_unico(pdf_path, bandeja / "_descartados")
                    resultados.append({"archivo": pdf_path.name, "ok": False, "cancelado": True,
                                       "error": "Descartado por el usuario", "ruta_descartado": destino})
                except Exception as exc:
                    error_msg = str(exc) or exc.__class__.__name__
                if error_msg is not None:
                    # Fuera del except: la traza ya no retiene el PDF abierto por PyMuPDF (WinError 32)
                    gc.collect()
                    destino = self._mover_unico(pdf_path, bandeja / "_errores")
                    txt = destino.with_suffix(".txt")
                    txt.write_text(f"Archivo: {pdf_path.name}\nError al procesar PDF: {error_msg}\n", encoding="utf-8")
                    resultados.append({"archivo": pdf_path.name, "ok": False, "error": error_msg,
                                       "ruta_error": destino, "txt_error": txt})
            return resultados

    @staticmethod
    def _mover_unico(origen: Path, carpeta: Path) -> Path:
        if carpeta.resolve() != origen.parent.resolve() / carpeta.name:
            raise ValueError("La carpeta de destino sale de la bandeja del cliente.")
        carpeta.mkdir(parents=True, exist_ok=True)
        destino, n = carpeta / origen.name, 2
        while destino.exists():
            destino = carpeta / f"{origen.stem}_{n}{origen.suffix}"
            n += 1
        shutil.move(str(origen), str(destino))
        return destino

    # ------------------------------------------------------------------ edición manual
    def validar(self, tabla: str, datos: dict) -> dict[str, str]:
        """{campo: mensaje} con los valores que no se pueden interpretar. {} = todo correcto."""
        columnas = {c.campo: c for c in TABLAS[tabla].columnas_bd}
        nombres = etiquetas(tabla)
        errores = {}
        for campo, valor in datos.items():
            col = columnas.get(campo)
            if col is None or valor in (None, ""):
                continue
            try:
                database._normalizar(col, valor)
            except (ValueError, TypeError):
                ayuda = {"date": "usa dd/mm/aaaa", "eur": "usa un importe como 1.234,56",
                         "pct": "usa un porcentaje entre 0 % y 100 % (por ejemplo 50 % o 0,5)",
                         "int": "usa un número entero"}.get(col.tipo, "valor no válido")
                errores[campo] = f"{nombres.get(campo, campo)}: {ayuda}"
        return errores

    def editar(self, cid: int, tabla: str, codigo: Optional[str], datos: dict, ejercicio: int) -> dict:
        """Alta (codigo=None) o corrección manual de una fila. Un campo enviado vacío se borra."""
        if tabla == "documentos" and codigo and "ejercicio" in datos:
            previo = self.db.get(tabla, cid, codigo)
            if previo and str(datos["ejercicio"]) != str(previo.get("ejercicio")):
                if set(datos) != {"ejercicio"}:
                    raise ValueError("Corrige el ejercicio con su acción específica antes de editar otros datos del documento.")
                return self.corregir_ejercicio_documento(cid, codigo, int(datos["ejercicio"]), ejercicio)
        errores = self.validar(tabla, datos)
        if errores:
            raise ValueError("; ".join(errores.values()))
        with self._lock:
            self._importar(cid)
            validos = {c.campo for c in TABLAS[tabla].columnas_bd}
            limpios = {k: v for k, v in datos.items() if k in validos and k != "codigo"}
            if tabla in ("gastos", "facturas_emitidas", "facturas_recibidas") and not codigo:
                limpios.setdefault("origen", "app")
            if tabla == "residencias" and limpios.get("ejercicio"):
                codigo = codigo or str(int(limpios["ejercicio"]))
            previa = self.db.get(tabla, cid, codigo) if codigo else None
            with self.db.transaccion():
                if codigo:
                    nuevo = self.db.upsert(tabla, cid, {"codigo": codigo, **limpios}, respetar_manuales=False)
                    actual = self.db.get(tabla, cid, nuevo) or {}
                    for campo, valor in limpios.items():
                        if valor in (None, "") and actual.get(campo) not in (None, ""):
                            self.db.vaciar_campo(tabla, cid, nuevo, campo)
                else:
                    nuevo = self.db.upsert(tabla, cid, limpios, respetar_manuales=False)
                fila = self.db.get(tabla, cid, nuevo) or {}
                if tabla == "inmuebles":
                    self.db.proteger_campos_inmueble(cid, nuevo, (
                        k for k in limpios if (previa or {}).get(k) != fila.get(k)))
            afectados = {int(ejercicio)}
            if tabla == "gastos":
                afectados |= tax_calculator.ejercicios_gasto(previa or {}) | tax_calculator.ejercicios_gasto(fila)
            elif tabla in ("inmuebles", "contratos", "rentas", "residencias"):
                afectados |= {int(f["ejercicio"]) for f in self.db.listar("m210_alquiler", cid) if f.get("ejercicio")}
            if isinstance((previa or {}).get("ejercicio"), int):
                afectados.add(previa["ejercicio"])
            if isinstance(fila.get("ejercicio"), int):
                afectados.add(fila["ejercicio"])
            return {"codigo": nuevo, **self._recalcular_y_exportar(cid, afectados, int(ejercicio))}

    def aplicar_correccion_chat(self, propuesta, cliente_activo: int, ejercicio_activo: int, al_guardar=None):
        """Solo comandos confirmados del usuario; nunca se invoca desde una respuesta del modelo."""
        import chat_correcciones as cc
        if type(propuesta) is not cc.Propuesta or (propuesta.cliente_id, propuesta.ejercicio) != (cliente_activo, ejercicio_activo):
            raise cc.ErrorCorreccion("La propuesta no corresponde al expediente activo.")
        if propuesta.campo != "valor_adquisicion":
            raise cc.ErrorCorreccion("Campo de corrección no permitido.")
        valor = cc.normalizar_importe(propuesta.valor)
        cid, ej = propuesta.cliente_id, propuesta.ejercicio
        with self._lock:
            cliente = self.db.get_cliente(cid)
            if not cliente or cartera_de(cliente.get("tipo_cliente")) != NR:
                raise cc.ErrorCorreccion("La corrección de inmuebles requiere un expediente de No residentes.")
            self.db.verificar_cartera(cliente)
            self._importar(cid)
            if self.db.get("inmuebles", cid, propuesta.codigo) is None:
                raise cc.ErrorCorreccion("El inmueble no pertenece a este cliente.")
            # Un precio es interanual: no reinterpretar ningún histórico presentado.
            for tabla in ("m210_alquiler", "m210_imputacion", "patrimonio_714", "its_718", "obligaciones"):
                if any(tax_calculator._presentado(f) for f in self.db.iterar(tabla, cid)):
                    raise cc.ErrorCorreccion("El expediente tiene declaraciones presentadas. Revisa la corrección manualmente.")
            resultado = self.editar(cid, "inmuebles", propuesta.codigo, {"precio_escritura": float(valor)}, ej)
            if resultado.get("error_excel"):
                raise cc.GuardadoNoVerificado("El dato se guardó en la BD y se recalculó, pero el Excel está pendiente. "
                                             + resultado["error_excel"])
            fila = self.db.get("inmuebles", cid, propuesta.codigo)
            if not fila or self.db.get_por_clave("patrimonio_714", cid, f"714|{ej}") is None:
                raise cc.GuardadoNoVerificado("Revisa la ficha: no se ha podido verificar el recálculo de Patrimonio.")
            if al_guardar is not None:
                al_guardar(resultado)
            return cc.ReciboGuardado(propuesta.id, cid, ej, propuesta.codigo, propuesta.campo,
                                    cc.importe_leido(fila["precio_escritura"]), 1)

    # Dependientes que se borran con su inmueble o contrato (las referencias son texto, sin claves foráneas)
    _DEPENDIENTES = {
        "inmuebles": ("inmueble", ("catastro", "contratos", "inquilinos", "rentas", "cobros", "gastos",
                                   "m210_alquiler", "m210_imputacion", "obligaciones", "arrastres_gastos")),
        "contratos": ("contrato", ("inquilinos", "rentas", "cobros")),
    }
    _DECLARACIONES = ("m210_alquiler", "m210_imputacion", "obligaciones")

    def cambiar_tipo_cliente(self, cid: int, nuevo_tipo: str, ejercicio: int) -> dict:
        """Cambia el tipo de cliente. Si cambia de cartera, mueve su carpeta a la otra raíz (todo o nada).

        El Excel anterior pasa a _backups (la otra cartera usa otra plantilla) y se genera el nuevo. Se rechaza
        si el cliente tiene declaraciones presentadas: el histórico fiscal de una cartera no se reinterpreta."""
        nuevo_tipo = carteras.validar_tipo(nuevo_tipo)
        with self._lock:
            cliente = self.db.get_cliente(cid)
            if not cliente:
                raise ValueError("El cliente ya no existe.")
            origen = self.db.verificar_cartera(cliente)
            self._importar(cid)
            antes, despues = cartera_de(cliente.get("tipo_cliente")), cartera_de(nuevo_tipo)
            if antes == despues:
                self.db.actualizar_cliente(cid, tipo_cliente=nuevo_tipo)
                return self._recalcular_y_exportar(cid, {int(ejercicio)}, int(ejercicio))
            tablas = (self._DECLARACIONES[:2] + ("patrimonio_714", "its_718")) if antes == NR else _DECLARACIONES_RES
            presentadas = [f"{t} {r['codigo']}" for t in tablas for r in self.db.listar(t, cid)
                           if r.get("estado") == "Presentado"]
            if presentadas:
                raise ValueError(f"Tiene declaraciones presentadas ({', '.join(presentadas[:3])}): no se cambia de "
                                 "cartera para conservar su histórico. Crea un expediente nuevo en la otra cartera.")
            raiz = _carpeta_sin_enlaces(self.db.raiz(despues))
            raiz.mkdir(parents=True, exist_ok=True)
            destino, n = raiz / origen.name, 2
            while destino.exists():
                destino, n = raiz / f"{origen.name}_{n}", n + 1
            destino = _carpeta_sin_enlaces(destino)
            excel_viejo = self.db.ruta_excel(cliente)
            movida, respaldo = False, None
            viejo = destino / excel_viejo.name
            try:
                with self.db.transaccion():
                    if origen.exists():
                        try:
                            origen.rename(destino)
                        except OSError as exc:
                            raise ValueError("No se pudo mover la carpeta del cliente: cierra su Excel o sus "
                                             f"PDFs y vuelve a intentarlo ({exc.__class__.__name__}).") from None
                        movida = True
                    self.db.actualizar_cliente(cid, tipo_cliente=nuevo_tipo, carpeta=str(destino))
                    self.db.asegurar_carpetas(self.db.get_cliente(cid))
                    if viejo.exists():
                        copias = _carpeta_sin_enlaces(destino / "_backups")
                        copias.mkdir(exist_ok=True)
                        copia = copias / f"{viejo.stem}_antes_cambio_cartera{viejo.suffix}"
                        numero = 2
                        while copia.exists():
                            copia = copias / f"{viejo.stem}_antes_cambio_cartera_{numero}{viejo.suffix}"
                            numero += 1
                        _carpeta_sin_enlaces(copia)
                        viejo.rename(copia)
                        respaldo = copia
                    self.db.con.execute("DELETE FROM config WHERE clave=?", (f"excel_firma|{cid}",))
            except Exception as exc:
                fallos = []
                if respaldo is not None:
                    try:
                        respaldo.rename(viejo)
                    except OSError as error:
                        fallos.append(f"Excel en {respaldo}: {error.__class__.__name__}")
                if movida:
                    try:
                        destino.rename(origen)
                    except OSError as error:
                        fallos.append(f"carpeta en {destino}: {error.__class__.__name__}")
                if fallos:
                    raise RuntimeError("No se pudo restaurar por completo el cambio de cartera; requiere "
                                       "recuperación manual. " + "; ".join(fallos)) from exc
                raise
            self._firmas.pop(cid, None)
            return self._recalcular_y_exportar(cid, {int(ejercicio)}, int(ejercicio))

    def borrar_registro(self, cid: int, tabla: str, codigo: str, ejercicio: int) -> dict:
        if tabla == "documentos":
            return self.retirar_documento(cid, codigo, ejercicio)
        """Borra una fila. Inmuebles y contratos arrastran sus datos dependientes (A4-P04), salvo que el
        inmueble tenga declaraciones ya presentadas: entonces se rechaza y se propone marcarlo como Baja."""
        with self._lock:
            self._importar(cid)
            previa = self.db.get(tabla, cid, codigo)
            if tabla in self._DEPENDIENTES:
                campo, tablas = self._DEPENDIENTES[tabla]
                presentadas = [f"{t} {r['codigo']}" for t in tablas if t in self._DECLARACIONES
                               for r in self.db.listar(t, cid, **{campo: codigo}) if r.get("estado") == "Presentado"]
                if presentadas:
                    raise ValueError(f"{codigo} tiene declaraciones presentadas ({', '.join(presentadas[:3])}). "
                                     "No se borra para conservar el histórico: cambia su estado a Baja o Vendido.")
                contratos = [r["codigo"] for r in self.db.listar("contratos", cid, inmueble=codigo)]                     if tabla == "inmuebles" else [codigo]
                for t in tablas:
                    for r in self.db.listar(t, cid, **{campo: codigo}):
                        self.db.borrar(t, cid, r["codigo"])
                for ctr in contratos:  # inquilinos/rentas/cobros enlazados por contrato
                    for t in ("inquilinos", "rentas", "cobros"):
                        for r in self.db.listar(t, cid, contrato=ctr):
                            self.db.borrar(t, cid, r["codigo"])
            self.db.borrar(tabla, cid, codigo)
            afectados = {int(ejercicio)}
            if tabla == "gastos":
                afectados |= tax_calculator.ejercicios_gasto(previa or {})
            return self._recalcular_y_exportar(cid, afectados, int(ejercicio))

    def marcar_presentado(self, cid: int, tabla: str, codigo: str, ejercicio: int,
                          fecha_presentacion: Any = None, pagado: Any = None, justificante: Optional[str] = None) -> dict:
        """Marca una declaración como presentada (y su obligación/modelo enlazado)."""
        if tabla not in CAMPOS_PRESENTACION:
            raise ValueError(f"{tabla} no es una declaración")
        datos: dict[str, Any] = {"estado": "Presentado", "fecha_presentacion": fecha_presentacion or date.today(),
                                 "pagado": pagado}
        campo_just = next((c for c in CAMPOS_PRESENTACION[tabla] if c in ("nrc", "csv")), None)
        if campo_just and justificante:
            datos[campo_just] = justificante
        errores = self.validar(tabla, datos)
        if errores:
            raise ValueError("; ".join(errores.values()))
        with self._lock:
            self._importar(cid)
            self.db.upsert(tabla, cid, {"codigo": codigo, **datos})
            enlazados = []
            if tabla == "obligaciones":
                obl = self.db.get("obligaciones", cid, codigo) or {}
                modelo = obl.get("id_modelo")
                destino = _PREFIJO_A_TABLA.get((modelo or "").rsplit("-", 1)[0]) if modelo else None
                if destino in CAMPOS_PRESENTACION:
                    enlazados.append((destino, modelo))
            else:
                enlazados += [("obligaciones", o["codigo"]) for o in self.db.listar("obligaciones", cid, id_modelo=codigo)]
            for t, cod in enlazados:
                self.db.upsert(t, cid, {"codigo": cod, "estado": "Presentado",
                                        "fecha_presentacion": datos["fecha_presentacion"], "pagado": pagado})
            return self._recalcular_y_exportar(cid, {int(ejercicio)}, int(ejercicio))

    # ------------------------------------------------------------------ atajos usados por la UI y los tests
    def _datos_gananciales(self, cid: int, inmueble: str, activo: bool) -> dict:
        """Al forzar el 50 % se guarda el % de la escritura (H muestra 50 % en Excel); al quitarlo se restaura."""
        fila = self.db.get("inmuebles", cid, inmueble) or {}
        if activo:
            datos = {"codigo": inmueble, "gananciales_forzado": 1, "pct_titularidad": 0.5}
            if not fila.get("gananciales_forzado"):
                datos["pct_titularidad_escritura"] = fila.get("pct_titularidad") if fila.get("pct_titularidad") is not None else 1.0
            return datos
        original = fila.get("pct_titularidad_escritura")
        return {"codigo": inmueble, "gananciales_forzado": 0,
                "pct_titularidad": original if original is not None else fila.get("pct_titularidad")}

    def set_gananciales(self, cid: int, inmueble: str, activo: bool, ejercicio: int) -> dict:
        with self._lock:
            self._importar(cid)
            datos = self._datos_gananciales(cid, inmueble, activo)
            with self.db.transaccion():
                codigo = self.db.upsert("inmuebles", cid, datos, respetar_manuales=False)
                self.db.proteger_campos_inmueble(cid, codigo, datos.keys())
            existentes = {int(f["ejercicio"]) for f in self.db.listar("m210_alquiler", cid) if f.get("ejercicio")}
            return {"inmueble": codigo, **self._recalcular_y_exportar(cid, existentes | {int(ejercicio)}, int(ejercicio))}

    def marcar_vendido(self, cid: int, inmueble: str, fecha_venta: str | date, precio_venta: float | str,
                       ejercicio: int, gastos_venta: float | None = None) -> dict:
        with self._lock:
            self._importar(cid)
            datos: dict[str, Any] = {"codigo": inmueble, "estado": "Vendido", "fecha_venta": fecha_venta,
                                     "precio_venta": precio_venta}
            if gastos_venta is not None:
                datos["gastos_venta"] = gastos_venta
            with self.db.transaccion():
                codigo = self.db.upsert("inmuebles", cid, datos, respetar_manuales=False)
                self.db.proteger_campos_inmueble(cid, codigo, datos.keys())
            fv = a_fecha(fecha_venta)
            afectados = {int(ejercicio), fv.year} if fv else {int(ejercicio)}
            return {"inmueble": codigo, **self._recalcular_y_exportar(cid, afectados, int(ejercicio))}

    def alta_gasto(self, cid: int, ejercicio: int, inmueble: str, categoria: str, fecha: str | date,
                   total: float | str, **extra: Any) -> dict:
        datos = {"ejercicio": int(ejercicio), "inmueble": inmueble, "categoria": categoria, "fecha": fecha,
                 "total": total, "origen": "app", "incluido_210": extra.pop("incluido_210", "Pendiente"), **extra}
        res = self.editar(cid, "gastos", None, datos, ejercicio)
        return {"gasto": res["codigo"], **res}

    # ------------------------------------------------------------------ vistas de lectura para la UI
    def vista_inmuebles(self, cid: int, ejercicio: int, limite: Optional[int] = None,
                        desplazamiento: int = 0) -> list[dict]:
        out = []
        inmuebles = (self.lector.listar_pagina("inmuebles", cid, limite, desplazamiento)
                     if limite is not None else self.lector.iterar("inmuebles", cid))
        for inm in inmuebles:
            cat = self.lector.catastro(cid, inm["codigo"], int(ejercicio))
            vp = tax_calculator.valor_patrimonial(inm, cat)
            tit = tax_calculator.pct_titularidad(inm)
            out.append({**inm, "_vp": vp, "_tit": tit, "_computado": round(vp.valor * tit, 2), "_catastro": cat})
        return out
