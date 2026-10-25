"""Estado de un lote de documentos mientras se procesa (vista «Leyendo N documentos»). Sin Flet: se prueba solo."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

EN_COLA, LEYENDO, REVISION = "En cola", "Leyendo…", "En revisión"
ARCHIVADO, DUPLICADO, DESCARTADO, ERROR = "Archivado", "Ya estaba archivado", "Descartado", "Error"
FINALES = (ARCHIVADO, DUPLICADO, DESCARTADO, ERROR)


@dataclass
class FilaCola:
    nombre: str
    estado: str = EN_COLA
    detalle: str = ""                 # código del documento o motivo del error
    coste_usd: Optional[float] = None
    coste_incompleto: bool = False    # alguna lectura sin uso registrado: el coste es un mínimo
    modelos: list[str] = field(default_factory=list)   # modelos que leyeron de verdad (no el configurado)

    @property
    def extension(self) -> str:
        return (Path(self.nombre).suffix.lstrip(".") or "doc").upper()[:4]


@dataclass
class ColaDocumentos:
    nombres: list[str]
    reloj: Callable[[], float] = time.monotonic
    filas: list[FilaCola] = field(init=False)
    inicio: float = field(init=False)
    fin: Optional[float] = field(default=None, init=False)

    def __post_init__(self):
        self.filas = [FilaCola(n) for n in self.nombres]
        self.inicio = self.reloj()

    # ------------------------------------------------------------------ transiciones
    def leyendo(self, i: int) -> None:
        self.filas[i].estado = LEYENDO

    def en_revision(self, i: int, coste_usd: Optional[float] = None, incompleto: bool = False,
                    modelos: tuple[str, ...] | list[str] = ()) -> None:
        fila = self.filas[i]
        fila.estado = REVISION
        if coste_usd is not None:
            fila.coste_usd = round((fila.coste_usd or 0) + float(coste_usd), 8)
        fila.coste_incompleto = fila.coste_incompleto or bool(incompleto)
        fila.modelos += [m for m in modelos if m and m not in fila.modelos]

    def terminar(self, i: int, estado: str, detalle: str = "") -> None:
        if estado not in FINALES:
            raise ValueError(f"Estado final no válido: {estado}")
        self.filas[i].estado, self.filas[i].detalle = estado, detalle
        if all(f.estado in FINALES for f in self.filas):
            self.fin = self.reloj()

    def detener(self) -> None:
        """Lote cortado (cambio de expediente o cierre): lo pendiente queda en cola y el reloj se para."""
        for f in self.filas:
            if f.estado in (LEYENDO, REVISION):
                f.estado = EN_COLA
        self.fin = self.reloj()

    # ------------------------------------------------------------------ lectura
    @property
    def total(self) -> int:
        return len(self.filas)

    @property
    def terminados(self) -> int:
        return sum(f.estado in FINALES for f in self.filas)

    @property
    def activo(self) -> bool:
        return self.fin is None

    @property
    def progreso(self) -> float:
        """0..1. El documento en curso cuenta medio, para que la barra avance mientras la IA lee."""
        if not self.total:
            return 1.0
        en_curso = sum(f.estado in (LEYENDO, REVISION) for f in self.filas) * 0.5
        return min(1.0, (self.terminados + en_curso) / self.total)

    @property
    def coste_usd(self) -> Optional[float]:
        costes = [f.coste_usd for f in self.filas if f.coste_usd is not None]
        return round(sum(costes), 8) if costes else None

    @property
    def coste_incompleto(self) -> bool:
        return any(f.coste_incompleto for f in self.filas)

    def modelos_usados(self) -> list[str]:
        vistos: list[str] = []
        for f in self.filas:
            vistos += [m for m in f.modelos if m not in vistos]
        return vistos

    def segundos(self) -> int:
        return int((self.fin if self.fin is not None else self.reloj()) - self.inicio)

    def tiempo(self) -> str:
        s = self.segundos()
        return f"{s // 60}:{s % 60:02d}"

    def titulo(self) -> str:
        n = self.total
        if self.activo:
            return f"Leyendo {n} documento{'s' if n != 1 else ''}"
        return f"{self.contar(ARCHIVADO)} de {n} archivado{'s' if n != 1 else ''}"

    def contar(self, estado: str) -> int:
        return sum(f.estado == estado for f in self.filas)

    def resumen(self) -> str:
        partes = [(ARCHIVADO, "archivado", "archivados"), (DUPLICADO, "ya estaba", "ya estaban"),
                  (DESCARTADO, "descartado", "descartados"), (ERROR, "con error", "con error")]
        textos = [f"{n} {uno if n == 1 else varios}" for estado, uno, varios in partes if (n := self.contar(estado))]
        pendientes = self.total - self.terminados
        if pendientes and not self.activo:
            textos.append(f"{pendientes} sin procesar")
        return " · ".join(textos)
