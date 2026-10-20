"""Correcciones explícitas desde el chat del expediente: propuesta visible y confirmación de un solo uso.

Autor: Claude. Auditor: Codex.

Solo se interpretan comandos que el USUARIO escribe en el campo del chat:
    /corregir INM-001 valor_adquisicion 250000,00
    /confirmar <token>
Nunca se interpreta texto de respuestas de la IA, PDFs, nombres de archivo ni datos de la BD: el texto del modelo
no es un permiso. El módulo no toca BD, ficheros ni red. Valida, normaliza y guarda la propuesta en memoria; al
confirmar llama al callback del integrador y solo informa éxito si este devuelve un ``ReciboGuardado`` que coincide
exactamente con la propuesta. Autorización del inmueble, bloqueo de ejercicios presentados, validación fiscal,
transacción, recálculo y exportación son responsabilidad del callback.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

CAMPOS_PERMITIDOS = frozenset({"valor_adquisicion"})
TTL_SEGUNDOS = 120              # caducidad de una propuesta sin confirmar
MAX_TTL = 600
MAX_PENDIENTES = 10             # propuestas vivas por sesión
MAX_FALLOS = 5                  # tokens desconocidos seguidos antes de anular todo lo pendiente
MAX_CONSUMIDOS = 256            # memoria de tokens usados/caducados para explicar el rechazo
MAX_COMANDO = 200
MAX_ID = 2**63 - 1
EJERCICIO_MIN, EJERCICIO_MAX = 1990, 2100
MAX_IMPORTE = Decimal("999999999.99")  # cota de cordura, no un criterio fiscal
CENTIMOS = Decimal("0.01")

# Solo ASCII: \d y \w de Python aceptan dígitos y letras Unicode.
_CODIGO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,30}[A-Za-z0-9])?")
_IMPORTE = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]{1,12})(?:,[0-9]{1,2})?")
_TOKEN = re.compile(r"[0-9a-f]{16}")

USO_CORREGIR = "Uso: /corregir <código inmueble> valor_adquisicion <importe>. Ej.: /corregir INM-001 valor_adquisicion 250000,00"
USO_CONFIRMAR = "Uso: /confirmar <token>"


class ErrorCorreccion(Exception):
    """Mensaje apto para mostrar al usuario. Nunca implica que se haya guardado nada."""


class GuardadoNoVerificado(ErrorCorreccion):
    """El callback terminó sin recibo válido: el estado real es desconocido y el token ya está consumido."""


@dataclass(frozen=True)
class Propuesta:
    """Lo que recibe el callback. No contiene el token."""
    id: int
    cliente_id: int
    ejercicio: int
    codigo: str
    campo: str
    valor: Decimal      # finito, >= 0, exactamente 2 decimales
    caduca: float       # reloj monotónico del gestor


@dataclass(frozen=True)
class ReciboGuardado:
    """Prueba de guardado que DEBE devolver el callback, construida después del commit.

    Todos los campos deben coincidir con la propuesta; ``valor_leido`` es el valor RELEÍDO de la BD tras el commit
    (usa ``importe_leido``) y ``filas_afectadas`` debe ser exactamente 1. Cualquier otra cosa (None, False, True,
    dict, subclase, tipos distintos) se trata como guardado no verificado.
    """
    propuesta_id: int
    cliente_id: int
    ejercicio: int
    codigo: str
    campo: str
    valor_leido: Decimal
    filas_afectadas: int


@dataclass(frozen=True)
class PropuestaEmitida:
    propuesta: Propuesta
    token: str = field(repr=False)
    mensaje: str = field(default="", repr=False)


@dataclass(frozen=True)
class Resultado:
    propuesta: Propuesta
    recibo: ReciboGuardado
    mensaje: str


@dataclass(frozen=True)
class Comando:
    accion: str                 # "corregir" | "confirmar"
    argumentos: tuple[str, ...]


# ------------------------------------------------------------------------------------------- validación
def _entero(valor, nombre: str, minimo: int, maximo: int) -> int:
    if type(valor) is not int or not minimo <= valor <= maximo:
        raise ErrorCorreccion(f"{nombre} no válido.")
    return valor


def normalizar_codigo(codigo) -> str:
    """Código sencillo de inmueble (letras/dígitos ASCII, guion, guion bajo; máx. 32), en mayúsculas."""
    texto = codigo.strip() if isinstance(codigo, str) else ""
    if not _CODIGO.fullmatch(texto):
        raise ErrorCorreccion("Código de inmueble no válido. Usa el código de la ficha, p. ej. INM-001.")
    return texto.upper()


def normalizar_campo(campo) -> str:
    texto = campo.strip().lower() if isinstance(campo, str) else ""
    if texto not in CAMPOS_PERMITIDOS:
        raise ErrorCorreccion("Ese campo no se puede corregir desde el chat. Permitidos: "
                              + ", ".join(sorted(CAMPOS_PERMITIDOS)) + ".")
    return texto


def normalizar_importe(valor) -> Decimal:
    """Importe finito, no negativo, con hasta 2 decimales. Texto en formato español: 250000,00 o 250.000,00 €."""
    if isinstance(valor, bool):
        raise ErrorCorreccion("Importe no válido.")
    if isinstance(valor, str):
        texto = valor.strip()
        if texto.endswith("€"):
            texto = texto[:-1].rstrip()
        if texto.startswith("-"):
            raise ErrorCorreccion("El importe no puede ser negativo.")
        if len(texto) > 32 or not _IMPORTE.fullmatch(texto):
            raise ErrorCorreccion("Importe no válido. Usa formato español sin signo: 250000,00 o 250.000,00.")
        importe = Decimal(texto.replace(".", "").replace(",", "."))
    elif isinstance(valor, int):
        importe = Decimal(valor)
    elif isinstance(valor, float):
        importe = Decimal(repr(valor))  # nan/inf dan Decimal no finito
    elif isinstance(valor, Decimal):
        importe = valor
    else:
        raise ErrorCorreccion("Importe no válido.")
    if not importe.is_finite():
        raise ErrorCorreccion("El importe debe ser un número finito.")
    if importe < 0:
        raise ErrorCorreccion("El importe no puede ser negativo.")
    if importe > MAX_IMPORTE:
        raise ErrorCorreccion("El importe es demasiado alto. Revísalo.")
    redondeado = importe.quantize(CENTIMOS)
    if redondeado != importe:
        raise ErrorCorreccion("El importe admite como máximo 2 decimales.")
    return Decimal("0.00") if redondeado == 0 else redondeado  # nunca "-0.00"


def importe_leido(valor) -> Decimal:
    """Para el callback: convierte el valor releído de la BD (REAL/INTEGER) al Decimal del recibo."""
    if isinstance(valor, str):
        raise ErrorCorreccion("El valor releído no es numérico.")
    return normalizar_importe(valor)


def formatear_importe(valor: Decimal) -> str:
    return f"{valor:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " €"


def interpretar(texto) -> Optional[Comando]:
    """Comando explícito del usuario, o None si el texto es una pregunta normal para la IA.

    Solo debe llamarse con lo que el usuario ha escrito en el campo del chat, nunca con texto de la IA.
    """
    if not isinstance(texto, str):
        return None
    limpio = texto.strip()
    if not limpio.startswith("/"):
        return None
    partes = limpio.split(None, 1)
    orden = partes[0].lower()
    if orden not in ("/corregir", "/confirmar"):
        return None
    if len(limpio) > MAX_COMANDO or len(limpio.splitlines()) != 1:
        raise ErrorCorreccion("El comando debe ir en una sola línea y ser breve.")
    resto = partes[1] if len(partes) > 1 else ""
    if orden == "/confirmar":
        argumentos = resto.split()
        if len(argumentos) != 1:
            raise ErrorCorreccion(USO_CONFIRMAR)
        return Comando("confirmar", (argumentos[0],))
    argumentos = resto.split(None, 2)
    if len(argumentos) != 3:
        raise ErrorCorreccion(USO_CORREGIR)
    return Comando("corregir", tuple(argumentos))


def _huella(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _recibo_valido(recibo, propuesta: Propuesta) -> bool:
    return (type(recibo) is ReciboGuardado
            and type(recibo.propuesta_id) is int and recibo.propuesta_id == propuesta.id
            and type(recibo.cliente_id) is int and recibo.cliente_id == propuesta.cliente_id
            and type(recibo.ejercicio) is int and recibo.ejercicio == propuesta.ejercicio
            and type(recibo.codigo) is str and recibo.codigo == propuesta.codigo
            and type(recibo.campo) is str and recibo.campo == propuesta.campo
            and type(recibo.valor_leido) is Decimal and recibo.valor_leido.is_finite()
            and recibo.valor_leido == propuesta.valor
            and type(recibo.filas_afectadas) is int and recibo.filas_afectadas == 1)


# ------------------------------------------------------------------------------------------- gestor
class GestorCorrecciones:
    """Uno por sesión de la aplicación. Las propuestas solo valen para el cliente y ejercicio abiertos.

    ``activo`` devuelve ``(cliente_id, ejercicio)`` del expediente abierto o None. Se consulta fuera del bloqueo
    interno, así que puede leer el estado de la interfaz. Los tokens solo se guardan como SHA-256.
    """

    def __init__(self, activo: Callable[[], Optional[tuple[int, int]]], ttl: float = TTL_SEGUNDOS,
                 reloj: Callable[[], float] = time.monotonic,
                 generar_token: Callable[[], str] = lambda: secrets.token_hex(8)):
        if not callable(activo):
            raise TypeError("activo debe ser invocable")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 0 < ttl <= MAX_TTL:
            raise ValueError(f"ttl debe estar entre 0 y {MAX_TTL} segundos")
        self._activo, self._ttl, self._reloj, self._generar = activo, float(ttl), reloj, generar_token
        self._lock = threading.Lock()
        self._pendientes: dict[bytes, Propuesta] = {}
        self._consumidos: OrderedDict[bytes, str] = OrderedDict()
        self._en_curso: set[tuple] = set()
        self._fallos = 0
        self._siguiente_id = 1

    # -------------------------------------------------------------------- API pública
    def procesar(self, texto, cliente_id, ejercicio,
                 aplicar_callback: Callable[[Propuesta], object]) -> Optional[str]:
        """Mensaje para el chat si ``texto`` es un comando; None si es una pregunta normal."""
        comando = interpretar(texto)
        if comando is None:
            return None
        if comando.accion == "corregir":
            return self.proponer(cliente_id, ejercicio, *comando.argumentos).mensaje
        return self.confirmar(comando.argumentos[0], cliente_id, ejercicio, aplicar_callback).mensaje

    def proponer(self, cliente_id, ejercicio, codigo, campo, valor) -> PropuestaEmitida:
        """Valida y deja pendiente una corrección. No guarda nada. Sustituye otra pendiente del mismo campo."""
        cid, ej = self._comprobar_activo(cliente_id, ejercicio)
        codigo, campo, importe = normalizar_codigo(codigo), normalizar_campo(campo), normalizar_importe(valor)
        with self._lock:
            ahora = self._reloj()
            self._purgar(cid, ej, ahora)
            destino = (cid, ej, codigo, campo)
            for huella, p in list(self._pendientes.items()):
                if (p.cliente_id, p.ejercicio, p.codigo, p.campo) == destino:
                    self._consumir(huella, "sustituida por otra posterior")
            if len(self._pendientes) >= MAX_PENDIENTES:
                raise ErrorCorreccion("Hay demasiadas correcciones sin confirmar. Confírmalas o espera a que caduquen.")
            token, huella = self._nuevo_token()
            propuesta = Propuesta(self._siguiente_id, cid, ej, codigo, campo, importe, ahora + self._ttl)
            self._siguiente_id += 1
            self._pendientes[huella] = propuesta
        mensaje = (f"**Propuesta de corrección (no guardada todavía)**\n"
                   f"- Inmueble: {codigo}\n- Campo: Precio de escritura (sin gastos ni impuestos de adquisición)\n"
                   f"- Nuevo valor: {formatear_importe(importe)}\n- Ejercicio abierto: {ej}\n"
                   "Afecta a todos los ejercicios del inmueble.\n\n"
                   f"Para aplicarla escribe `/confirmar {token}` en los próximos {int(self._ttl)} segundos. "
                   "El token sirve una sola vez y se confirma desde este expediente y ejercicio.")
        return PropuestaEmitida(propuesta, token, mensaje)

    def confirmar(self, token, cliente_id, ejercicio,
                  aplicar_callback: Callable[[Propuesta], object]) -> Resultado:
        """Consume el token y llama a ``aplicar_callback(propuesta)`` una sola vez.

        El token queda consumido ANTES de llamar al callback: ni una excepción ni un recibo inválido permiten
        repetir la confirmación. Las excepciones del callback se propagan tal cual; un retorno que no sea un
        ``ReciboGuardado`` coincidente lanza ``GuardadoNoVerificado``. Solo se devuelve ``Resultado`` si hay recibo.
        """
        if not callable(aplicar_callback):
            raise TypeError("aplicar_callback debe ser invocable")
        cid, ej = self._comprobar_activo(cliente_id, ejercicio)
        texto = token.strip().lower() if isinstance(token, str) and len(token) <= 64 else ""
        with self._lock:
            self._purgar(cid, ej, self._reloj())
            huella = _huella(texto) if _TOKEN.fullmatch(texto) else None
            propuesta = self._pendientes.get(huella) if huella else None
            if propuesta is None:
                motivo = self._consumidos.get(huella) if huella else None
                if motivo:
                    raise ErrorCorreccion(f"Ese token no se puede usar: propuesta {motivo}. No se ha aplicado nada.")
                raise ErrorCorreccion(self._fallo())
            destino = (propuesta.cliente_id, propuesta.ejercicio, propuesta.codigo, propuesta.campo)
            if destino in self._en_curso:
                raise ErrorCorreccion("Ya se está guardando una corrección de ese campo. Espera y vuelve a intentarlo.")
            self._consumir(huella, "ya utilizada")
            self._en_curso.add(destino)
            self._fallos = 0
        try:
            recibo = aplicar_callback(propuesta)
        finally:
            with self._lock:
                self._en_curso.discard(destino)
        if not _recibo_valido(recibo, propuesta):
            raise GuardadoNoVerificado("No consta que la corrección se haya guardado. Revisa la ficha del inmueble "
                                       "antes de volver a proponerla.")
        mensaje = (f"**Corrección guardada.** Inmueble {propuesta.codigo} · Precio de escritura (sin gastos) = "
                   f"{formatear_importe(recibo.valor_leido)} (valor releído tras guardar).")
        return Resultado(propuesta, recibo, mensaje)

    def descartar(self, cliente_id: Optional[int] = None) -> None:
        """Anula lo pendiente (de un cliente o todo). Llamar en «Nueva conversación», cambio de cliente y cierre."""
        with self._lock:
            for huella, p in list(self._pendientes.items()):
                if cliente_id is None or p.cliente_id == cliente_id:
                    self._consumir(huella, "descartada")

    def pendientes(self) -> int:
        with self._lock:
            return len(self._pendientes)

    # -------------------------------------------------------------------- interno (con self._lock salvo activo)
    def _comprobar_activo(self, cliente_id, ejercicio) -> tuple[int, int]:
        cid = _entero(cliente_id, "Cliente", 1, MAX_ID)
        ej = _entero(ejercicio, "Ejercicio", EJERCICIO_MIN, EJERCICIO_MAX)
        try:
            actual = self._activo()
        except Exception:
            raise ErrorCorreccion("No se puede comprobar el expediente abierto.") from None
        if (not isinstance(actual, tuple) or len(actual) != 2
                or any(type(x) is not int for x in actual) or actual != (cid, ej)):
            raise ErrorCorreccion("La corrección no corresponde al cliente y ejercicio abiertos.")
        return cid, ej

    def _purgar(self, cid: int, ej: int, ahora: float) -> None:
        for huella, p in list(self._pendientes.items()):
            if ahora >= p.caduca:
                self._consumir(huella, "caducada")
            elif (p.cliente_id, p.ejercicio) != (cid, ej):
                self._consumir(huella, "anulada al cambiar de expediente o ejercicio")

    def _consumir(self, huella: bytes, motivo: str) -> None:
        self._pendientes.pop(huella, None)
        self._consumidos[huella] = motivo
        self._consumidos.move_to_end(huella)
        while len(self._consumidos) > MAX_CONSUMIDOS:
            self._consumidos.popitem(last=False)

    def _fallo(self) -> str:
        self._fallos += 1
        if self._fallos < MAX_FALLOS:
            return "Token desconocido. Copia el token de la última propuesta."
        for huella in list(self._pendientes):
            self._consumir(huella, "anulada por demasiados intentos fallidos")
        self._fallos = 0
        return "Demasiados tokens incorrectos: se han anulado las propuestas pendientes. Vuelve a proponerlas."

    def _nuevo_token(self) -> tuple[str, bytes]:
        for _ in range(3):
            token = self._generar()
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ErrorCorreccion("No se pudo generar el token de confirmación.")
            huella = _huella(token)
            if huella not in self._pendientes and huella not in self._consumidos:
                return token, huella
        raise ErrorCorreccion("No se pudo generar un token único. Inténtalo de nuevo.")
