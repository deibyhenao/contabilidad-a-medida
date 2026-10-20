"""Tests de chat_correcciones (solo biblioteca estándar; válidos con unittest y pytest).

Autor: Claude. Auditor: Codex.
"""
from __future__ import annotations

import dataclasses
import itertools
import threading
import time
import unittest
from decimal import Decimal

import chat_correcciones as cc

CID, EJ = 1, 2026
CAMPO = "valor_adquisicion"


class Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def recibo_ok(p: cc.Propuesta) -> cc.ReciboGuardado:
    return cc.ReciboGuardado(p.id, p.cliente_id, p.ejercicio, p.codigo, p.campo, p.valor, 1)


class Base(unittest.TestCase):
    def setUp(self):
        self.activo = [(CID, EJ)]
        self.reloj = Reloj()
        contador = itertools.count(1)
        self.g = cc.GestorCorrecciones(lambda: self.activo[0], ttl=120, reloj=self.reloj,
                                       generar_token=lambda: f"{next(contador):016x}")

    def proponer(self, codigo="INM-001", valor="250000,00", cid=CID, ej=EJ):
        return self.g.proponer(cid, ej, codigo, CAMPO, valor)


# ------------------------------------------------------------------------------------------- comandos
class TestInterpretar(Base):
    def test_texto_normal_va_a_la_ia(self):
        for texto in ("¿Cuánto vale INM-001?", "", "   ", None, "/ayuda", "corregir INM-001 x 1",
                      "Por favor /corregir INM-001 valor_adquisicion 1"):
            self.assertIsNone(cc.interpretar(texto))

    def test_corregir(self):
        c = cc.interpretar("  /corregir INM-001 valor_adquisicion 250.000,00 €  ")
        self.assertEqual(c, cc.Comando("corregir", ("INM-001", "valor_adquisicion", "250.000,00 €")))
        self.assertEqual(cc.interpretar("/CORREGIR a b c").accion, "corregir")

    def test_confirmar(self):
        self.assertEqual(cc.interpretar("/confirmar abc"), cc.Comando("confirmar", ("abc",)))

    def test_comandos_mal_formados(self):
        for texto in ("/corregir", "/corregir INM-001 valor_adquisicion", "/confirmar", "/confirmar a b",
                      "/corregir INM-001 valor_adquisicion 1\n/confirmar x",
                      "/corregir INM-001 valor_adquisicion 1 x",
                      "/corregir INM-001 valor_adquisicion " + "1" * 300):
            with self.assertRaises(cc.ErrorCorreccion, msg=texto):
                cc.interpretar(texto)


# ------------------------------------------------------------------------------------------- validación
class TestValidacion(Base):
    def test_importes_validos(self):
        casos = {"250000,00": "250000.00", "250.000,00": "250000.00", "250.000,00 €": "250000.00",
                 "250000€": "250000.00", "0": "0.00", "0,5": "0.50", "1.234.567,89": "1234567.89",
                 250000: "250000.00", Decimal("12.3"): "12.30", 99.99: "99.99"}
        for entrada, esperado in casos.items():
            self.assertEqual(cc.normalizar_importe(entrada), Decimal(esperado), entrada)

    def test_cero_negativo_se_normaliza(self):
        self.assertEqual(str(cc.normalizar_importe(-0.0)), "0.00")
        self.assertEqual(str(cc.normalizar_importe(Decimal("-0"))), "0.00")

    def test_importes_invalidos(self):
        malos = ["NaN", "nan", "inf", "Infinity", "-1", "-0,01", "+5", "1e5", "250000.00", "250.00",
                 "1,234", "12.34,5", "", " ", "1 000", "١٢٣", "２５０", "1.000.000.000,00",
                 float("nan"), float("inf"), float("-inf"), -0.01, -1, True, False, None, [1],
                 Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-1"), Decimal("1.001"),
                 0.1 + 0.2, 1e16]
        for valor in malos:
            with self.assertRaises(cc.ErrorCorreccion, msg=repr(valor)):
                cc.normalizar_importe(valor)

    def test_codigos(self):
        self.assertEqual(cc.normalizar_codigo(" inm-001 "), "INM-001")
        self.assertEqual(cc.normalizar_codigo("A_1"), "A_1")
        for malo in ("", "INM 001", "../x", "INM-001;DROP", "INM-", "-INM", "inmß", "ınm", "Ｉ1", "١٢",
                     "A" * 33, None, 1):
            with self.assertRaises(cc.ErrorCorreccion, msg=repr(malo)):
                cc.normalizar_codigo(malo)

    def test_campos(self):
        self.assertEqual(cc.normalizar_campo(" VALOR_ADQUISICION "), CAMPO)
        for malo in ("valor_catastral", "id", "cliente_id", "codigo", "", None, "valor_adquisicion;"):
            with self.assertRaises(cc.ErrorCorreccion):
                cc.normalizar_campo(malo)

    def test_cliente_y_ejercicio_no_validos(self):
        for cid, ej in ((0, EJ), (-1, EJ), (True, EJ), ("1", EJ), (1.0, EJ), (CID, 1800), (CID, "2026"),
                        (CID, True)):
            with self.assertRaises(cc.ErrorCorreccion):
                self.g.proponer(cid, ej, "INM-001", CAMPO, "1")

    def test_importe_leido(self):
        self.assertEqual(cc.importe_leido(250000.0), Decimal("250000.00"))
        self.assertEqual(cc.importe_leido(1234.56), Decimal("1234.56"))
        for malo in ("250000", float("nan"), None):
            with self.assertRaises(cc.ErrorCorreccion):
                cc.importe_leido(malo)

    def test_ttl_no_valido(self):
        for ttl in (0, -1, 601, True, "120"):
            with self.assertRaises(ValueError):
                cc.GestorCorrecciones(lambda: (CID, EJ), ttl=ttl)


# ------------------------------------------------------------------------------------------- propuesta y token
class TestPropuesta(Base):
    def test_propuesta_visible_y_normalizada(self):
        e = self.proponer(codigo="inm-001", valor="250.000,00")
        self.assertEqual((e.propuesta.codigo, e.propuesta.campo, e.propuesta.valor), ("INM-001", CAMPO,
                                                                                      Decimal("250000.00")))
        for trozo in ("INM-001", "Precio de escritura", "sin gastos ni impuestos", "todos los ejercicios",
                      "250.000,00 €", f"/confirmar {e.token}", "no guardada"):
            self.assertIn(trozo, e.mensaje)
        self.assertNotIn(e.token, repr(e))
        self.assertEqual(self.g.pendientes(), 1)

    def test_token_por_defecto_aleatorio_y_no_guardado_en_claro(self):
        g = cc.GestorCorrecciones(lambda: (CID, EJ))
        tokens = {g.proponer(CID, EJ, f"INM-{i}", CAMPO, "1").token for i in range(5)}
        self.assertEqual(len(tokens), 5)
        for t in tokens:
            self.assertRegex(t, r"^[0-9a-f]{16}$")
            self.assertNotIn(t, repr(g.__dict__))

    def test_token_repetido_o_mal_formado_del_generador(self):
        g = cc.GestorCorrecciones(lambda: (CID, EJ), generar_token=lambda: "a" * 16)
        g.proponer(CID, EJ, "INM-001", CAMPO, "1")
        with self.assertRaises(cc.ErrorCorreccion):
            g.proponer(CID, EJ, "INM-002", CAMPO, "1")
        g2 = cc.GestorCorrecciones(lambda: (CID, EJ), generar_token=lambda: "corto")
        with self.assertRaises(cc.ErrorCorreccion):
            g2.proponer(CID, EJ, "INM-001", CAMPO, "1")

    def test_nueva_propuesta_sustituye_a_la_anterior_del_mismo_campo(self):
        e1, e2 = self.proponer(valor="1"), self.proponer(valor="2")
        with self.assertRaisesRegex(cc.ErrorCorreccion, "sustituida"):
            self.g.confirmar(e1.token, CID, EJ, recibo_ok)
        self.assertEqual(self.g.confirmar(e2.token, CID, EJ, recibo_ok).propuesta.valor, Decimal("2.00"))

    def test_limite_de_pendientes(self):
        for i in range(cc.MAX_PENDIENTES):
            self.proponer(codigo=f"INM-{i}")
        with self.assertRaises(cc.ErrorCorreccion):
            self.proponer(codigo="INM-X")

    def test_token_tolera_mayusculas_y_espacios(self):
        e = self.proponer()
        self.g.confirmar(f"  {e.token.upper()} ", CID, EJ, recibo_ok)

    def test_tokens_desconocidos_anulan_lo_pendiente(self):
        e = self.proponer()
        for malo in ["ffffffffffffffff", "zz", "", None] + ["eeeeeeeeeeeeeeee"] * (cc.MAX_FALLOS - 4):
            with self.assertRaises(cc.ErrorCorreccion):
                self.g.confirmar(malo, CID, EJ, recibo_ok)
        with self.assertRaisesRegex(cc.ErrorCorreccion, "intentos fallidos"):
            self.g.confirmar(e.token, CID, EJ, recibo_ok)

    def test_procesar(self):
        self.assertIsNone(self.g.procesar("¿Qué falta?", CID, EJ, recibo_ok))
        mensaje = self.g.procesar("/corregir INM-001 valor_adquisicion 250000,00", CID, EJ, recibo_ok)
        token = mensaje.split("/confirmar ")[1].split("`")[0]
        final = self.g.procesar(f"/confirmar {token}", CID, EJ, recibo_ok)
        self.assertIn("Corrección guardada", final)
        self.assertIn("250.000,00 €", final)


# ------------------------------------------------------------------------------------------- aislamiento
class TestAislamiento(Base):
    def test_proponer_fuera_del_expediente_activo(self):
        with self.assertRaises(cc.ErrorCorreccion):
            self.proponer(cid=2)
        with self.assertRaises(cc.ErrorCorreccion):
            self.proponer(ej=EJ - 1)

    def test_activo_ausente_o_roto(self):
        for activo in (lambda: None, lambda: (CID,), lambda: [CID, EJ], lambda: (True, EJ), lambda: 1 / 0):
            g = cc.GestorCorrecciones(activo)
            with self.assertRaises(cc.ErrorCorreccion):
                g.proponer(CID, EJ, "INM-001", CAMPO, "1")

    def test_token_de_otro_cliente_no_sirve_ni_al_volver(self):
        e = self.proponer()
        llamadas = []
        self.activo[0] = (2, EJ)
        with self.assertRaises(cc.ErrorCorreccion):
            self.g.confirmar(e.token, 2, EJ, lambda p: llamadas.append(p))
        self.activo[0] = (CID, EJ)
        with self.assertRaisesRegex(cc.ErrorCorreccion, "anulada"):
            self.g.confirmar(e.token, CID, EJ, lambda p: llamadas.append(p))
        self.assertEqual(llamadas, [])

    def test_token_de_otro_ejercicio_no_sirve(self):
        e = self.proponer()
        self.activo[0] = (CID, EJ + 1)
        with self.assertRaises(cc.ErrorCorreccion):
            self.g.confirmar(e.token, CID, EJ + 1, recibo_ok)
        self.activo[0] = (CID, EJ)
        with self.assertRaises(cc.ErrorCorreccion):
            self.g.confirmar(e.token, CID, EJ, recibo_ok)

    def test_parametros_distintos_del_activo(self):
        e = self.proponer()
        with self.assertRaises(cc.ErrorCorreccion):
            self.g.confirmar(e.token, 2, EJ, recibo_ok)  # el activo sigue siendo (1, 2026)
        self.g.confirmar(e.token, CID, EJ, recibo_ok)      # no se consumió: el rechazo fue previo

    def test_descartar(self):
        e = self.proponer()
        self.g.descartar(CID)
        with self.assertRaisesRegex(cc.ErrorCorreccion, "descartada"):
            self.g.confirmar(e.token, CID, EJ, recibo_ok)


# ------------------------------------------------------------------------------------------- caducidad
class TestCaducidad(Base):
    def test_antes_del_limite_vale(self):
        e = self.proponer()
        self.reloj.t += 119.9
        self.g.confirmar(e.token, CID, EJ, recibo_ok)

    def test_en_el_limite_caduca(self):
        e = self.proponer()
        self.reloj.t += 120
        llamadas = []
        with self.assertRaisesRegex(cc.ErrorCorreccion, "caducada"):
            self.g.confirmar(e.token, CID, EJ, lambda p: llamadas.append(p))
        self.assertEqual(llamadas, [])
        self.assertEqual(self.g.pendientes(), 0)


# ------------------------------------------------------------------------------------------- callback
class TestCallback(Base):
    def test_exito_solo_con_recibo(self):
        e = self.proponer()
        recibidas = []

        def aplicar(p):
            recibidas.append(p)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                p.valor = Decimal("1")
            return recibo_ok(p)
        r = self.g.confirmar(e.token, CID, EJ, aplicar)
        self.assertEqual(recibidas, [e.propuesta])
        self.assertIs(type(r.recibo), cc.ReciboGuardado)
        self.assertFalse(hasattr(e.propuesta, "token"))

    def test_doble_envio(self):
        e = self.proponer()
        llamadas = []

        def aplicar(p):
            llamadas.append(p)
            return recibo_ok(p)
        self.g.confirmar(e.token, CID, EJ, aplicar)
        with self.assertRaisesRegex(cc.ErrorCorreccion, "ya utilizada"):
            self.g.confirmar(e.token, CID, EJ, aplicar)
        self.assertEqual(len(llamadas), 1)

    def test_excepcion_se_propaga_y_no_permite_reintento(self):
        e = self.proponer()
        llamadas = []

        def falla(p):
            llamadas.append(p)
            raise RuntimeError("BD bloqueada")
        with self.assertRaisesRegex(RuntimeError, "BD bloqueada"):
            self.g.confirmar(e.token, CID, EJ, falla)
        with self.assertRaisesRegex(cc.ErrorCorreccion, "ya utilizada"):
            self.g.confirmar(e.token, CID, EJ, recibo_ok)
        self.assertEqual(len(llamadas), 1)
        # El campo no queda bloqueado como "en curso" tras la excepción.
        self.g.confirmar(self.proponer().token, CID, EJ, recibo_ok)

    def test_retornos_sin_prueba_de_guardado(self):
        class Subclase(cc.ReciboGuardado):
            pass
        malos = [
            lambda p: None, lambda p: False, lambda p: True, lambda p: 1, lambda p: "ok",
            lambda p: {"guardado": True}, lambda p: dataclasses.asdict(recibo_ok(p)),
            lambda p: Subclase(*dataclasses.astuple(recibo_ok(p))),
            lambda p: dataclasses.replace(recibo_ok(p), filas_afectadas=0),
            lambda p: dataclasses.replace(recibo_ok(p), filas_afectadas=2),
            lambda p: dataclasses.replace(recibo_ok(p), filas_afectadas=True),
            lambda p: dataclasses.replace(recibo_ok(p), propuesta_id=p.id + 1),
            lambda p: dataclasses.replace(recibo_ok(p), cliente_id=2),
            lambda p: dataclasses.replace(recibo_ok(p), ejercicio=EJ - 1),
            lambda p: dataclasses.replace(recibo_ok(p), codigo="INM-002"),
            lambda p: dataclasses.replace(recibo_ok(p), campo="valor_catastral"),
            lambda p: dataclasses.replace(recibo_ok(p), valor_leido=p.valor + Decimal("0.01")),
            lambda p: dataclasses.replace(recibo_ok(p), valor_leido=float(p.valor)),
            lambda p: dataclasses.replace(recibo_ok(p), valor_leido=Decimal("NaN")),
        ]
        for i, aplicar in enumerate(malos):
            e = self.proponer()
            with self.assertRaises(cc.GuardadoNoVerificado, msg=str(i)) as ctx:
                self.g.confirmar(e.token, CID, EJ, aplicar)
            self.assertIn("No consta", str(ctx.exception))
            self.assertNotIn("guardada.", str(ctx.exception))
            with self.assertRaisesRegex(cc.ErrorCorreccion, "ya utilizada"):
                self.g.confirmar(e.token, CID, EJ, recibo_ok)

    def test_callback_no_invocable_no_consume_token(self):
        e = self.proponer()
        with self.assertRaises(TypeError):
            self.g.confirmar(e.token, CID, EJ, None)
        self.g.confirmar(e.token, CID, EJ, recibo_ok)


# ------------------------------------------------------------------------------------------- concurrencia
class TestConcurrencia(Base):
    def test_confirmaciones_simultaneas_aplican_una_vez(self):
        e = self.proponer()
        llamadas, exitos, errores = [], [], []
        candado = threading.Lock()
        barrera = threading.Barrier(8)

        def aplicar(p):
            with candado:
                llamadas.append(p.id)
            time.sleep(0.02)
            return recibo_ok(p)

        def hilo():
            barrera.wait(5)
            try:
                exitos.append(self.g.confirmar(e.token, CID, EJ, aplicar))
            except cc.ErrorCorreccion as exc:
                errores.append(exc)
        hilos = [threading.Thread(target=hilo) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(5)
        self.assertEqual((len(exitos), len(errores), len(llamadas)), (1, 7, 1))

    def test_propuestas_simultaneas_tienen_token_e_id_unicos(self):
        g = cc.GestorCorrecciones(lambda: (CID, EJ))
        barrera = threading.Barrier(cc.MAX_PENDIENTES)
        emitidas = []

        def hilo(i):
            barrera.wait(5)
            emitidas.append(g.proponer(CID, EJ, f"INM-{i}", CAMPO, "1"))
        hilos = [threading.Thread(target=hilo, args=(i,)) for i in range(cc.MAX_PENDIENTES)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(5)
        self.assertEqual(len({x.token for x in emitidas}), cc.MAX_PENDIENTES)
        self.assertEqual(len({x.propuesta.id for x in emitidas}), cc.MAX_PENDIENTES)

    def test_no_se_aplican_dos_correcciones_del_mismo_campo_a_la_vez(self):
        e1 = self.proponer(valor="1")
        dentro, soltar = threading.Event(), threading.Event()
        resultados = []

        def lento(p):
            dentro.set()
            soltar.wait(5)
            return recibo_ok(p)
        h = threading.Thread(target=lambda: resultados.append(self.g.confirmar(e1.token, CID, EJ, lento)))
        h.start()
        self.assertTrue(dentro.wait(5))
        e2 = self.proponer(valor="2")
        with self.assertRaisesRegex(cc.ErrorCorreccion, "Ya se está guardando"):
            self.g.confirmar(e2.token, CID, EJ, recibo_ok)
        soltar.set()
        h.join(5)
        self.assertEqual(len(resultados), 1)
        self.assertEqual(self.g.confirmar(e2.token, CID, EJ, recibo_ok).propuesta.valor, Decimal("2.00"))


if __name__ == "__main__":
    unittest.main()
