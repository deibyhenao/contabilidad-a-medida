"""Normativa NR por ejercicio, confirmada por el asesor (DA 4.ª LIP).

Fuentes: BOE-A-2024-6951 art. 621-1 y DT primera; BOE-A-1998-8202
arts. 8/9, redacciones Ley 5/2025 y Ley 5/2026. Verificación: 04/11/2026.
No determina la localización de cuentas/valores ni beneficios particulares.
"""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

NOMBRES = {'ESTATAL': 'Estatal (no residente)', 'CATALUNA': 'Cataluña',
           'VALENCIANA': 'Comunitat Valenciana', 'MADRID': 'Comunidad de Madrid',
           'ANDALUCIA': 'Andalucía', 'ARAGON': 'Aragón', 'ASTURIAS': 'Principado de Asturias',
           'BALEARES': 'Illes Balears', 'CANARIAS': 'Canarias', 'CANTABRIA': 'Cantabria',
           'CASTILLA_LA_MANCHA': 'Castilla-La Mancha', 'CASTILLA_LEON': 'Castilla y León',
           'EXTREMADURA': 'Extremadura', 'GALICIA': 'Galicia', 'MURCIA': 'Región de Murcia',
           'RIOJA': 'La Rioja', 'NAVARRA': 'Comunidad Foral de Navarra',
           'PAIS_VASCO': 'País Vasco', 'CEUTA': 'Ceuta', 'MELILLA': 'Melilla',
           'OTRA': 'Otra comunidad'}
CATALUNA = (
    ('0', '0', '.00210'), ('167129.45', '350.97', '.00315'),
    ('334252.88', '877.41', '.00525'), ('668499.75', '2632.21', '.00945'),
    ('1336999.75', '8949.54', '.01365'), ('2673999.01', '27199.58', '.01785'),
    ('5347998.03', '74930.46', '.02205'), ('10695996.06', '192853.82', '.02750'),
    ('20000000', '448713.93', '.03480'))
VALENCIANA = (
    ('0', '0', '.0025'), ('167129.45', '417.82', '.0037'),
    ('334252.88', '1036.18', '.0062'), ('668499.75', '3108.51', '.0112'),
    ('1336999.51', '10595.71', '.0162'), ('2673999.01', '32255.10', '.0212'),
    ('5347998.03', '88943.88', '.0262'), ('10695996.06', '229061.43', '.035'))

# Anclas de las escalas oficiales AEAT Patrimonio 2025, capítulo 4.
ASTURIAS = (('0','0','.0022'), ('167129.45','367.68','.0033'),
    ('334252.88','919.19','.0056'), ('668499.75','2790.97','.0102'),
    ('1336999.51','9609.67','.0148'), ('2673999.01','29397.26','.0197'),
    ('5347998.03','82075.05','.0248'), ('10695996.06','214705.40','.03'))
BALEARES = (('0','0','.0028'), ('170472.04','477.32','.0041'),
    ('340937.04','1176.23','.0069'), ('681869.75','3528.67','.0124'),
    ('1336739.51','11649.06','.0179'), ('2727479','36543.30','.0235'),
    ('5454958','100639.06','.029'), ('10909915.99','258832.84','.0345'))
CANTABRIA = (('0','0','.0024'), ('167129.45','401.11','.0036'),
    ('334252.88','1002.75','.0061'), ('668499.75','3041.66','.0109'),
    ('1336999.51','10328.31','.0157'), ('2673999.01','31319.20','.0206'),
    ('5347998.03','86403.58','.0254'), ('10695996.06','222242.73','.0303'))
EXTREMADURA = (('0','0','.003'), ('167129.45','501.39','.0045'),
    ('334252.88','1253.44','.0075'), ('668499.75','3760.30','.0135'),
    ('1336999.51','12785.04','.0195'), ('2673999.01','38856.53','.0255'),
    ('5347998.03','107043.51','.0315'), ('10695996.06','275505.45','.0375'))


def clave(cid, ejercicio):
    return f'normativa714|{int(cid)}|{int(ejercicio)}'


def leer(db, cid, ejercicio):
    raw = db.get_config(clave(cid, ejercicio), '')
    origen = None
    if not raw:
        prefijo = f'normativa714|{int(cid)}|'
        anteriores = []
        # Rango sobre la clave primaria: usa el índice en lugar de recorrer toda la tabla de configuración.
        for r in db.con.execute('SELECT clave,valor FROM config WHERE clave>=? AND clave<?',
                                (prefijo, prefijo[:-1] + chr(ord(prefijo[-1]) + 1))):
            try:
                anno = int(r[0].rsplit('|', 1)[1])
                contenido = json.loads(r[1])
                if anno < int(ejercicio) and isinstance(contenido, dict):
                    anteriores.append((anno, r[1]))
            except (ValueError, TypeError, AttributeError):
                continue
        if anteriores:
            origen, raw = max(anteriores)
    try:
        datos = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        datos = {}
    if not isinstance(datos, dict):
        datos = {}
    manual = db.get_config(f'bonificacion714_manual|{cid}|{ejercicio}', '')
    minimo_manual = db.get_config(f'minimo714_manual|{cid}|{ejercicio}', '')
    return {'normativa': 'ESTATAL', 'comunidad': '', 'justificacion': '', 'confirmado': False,
            **datos, 'heredada_de': origen,
            'bonificacion_manual': bool(manual),
            'minimo_manual': bool(minimo_manual),
            'version': hashlib.sha256(f'{ejercicio}|{origen}|{raw}|{manual}|{minimo_manual}'.encode('utf-8')).hexdigest()}


def codigo_desde_nombre(texto):
    import unicodedata
    def normalizar(s):
        return ''.join(c for c in unicodedata.normalize('NFD', str(s or '').casefold())
                       if not unicodedata.combining(c)).strip()
    nombres = {normalizar(v): k for k, v in NOMBRES.items()}
    nombres.update({'estatal': 'ESTATAL', 'catalunya': 'CATALUNA', 'valencia': 'VALENCIANA', 'madrid': 'MADRID'})
    return nombres.get(normalizar(texto))


def fijar_herencia(db, cid, ejercicio):
    """Los borradores heredan; solo una elección explícita crea configuración anual.

    Un año explícito posterior manda sobre los anteriores. Los 714 presentados
    ya congelan sus datos en el motor y nunca se reescriben por esta herencia.
    Mantiene el nombre interno por compatibilidad con las primeras pruebas.
    """
    return leer(db, cid, ejercicio)


def cambio_desde_excel(db, cid, ejercicio, texto):
    """Una corrección explícita del libro invalida la confirmación anterior.

    La comunidad se propone en el selector, pero no se afirma el punto de
    conexión legal por una cadena de texto ni se descarta la elección anterior.
    """
    texto = str(texto or '').strip()
    codigo = codigo_desde_nombre(texto) or 'OTRA'
    anterior = leer(db, cid, ejercicio)
    db.set_config(clave(cid, ejercicio), json.dumps(dict(normativa=codigo,
        comunidad=texto if codigo == 'OTRA' else '', confirmado=False,
        justificacion='Normativa corregida en Excel: confirmar el punto de conexión legal.',
        anterior_confirmada=anterior.get('normativa')), ensure_ascii=False))


def regla(codigo, ejercicio):
    """None significa sin motor validado; nunca sustituir por estatal sin aviso."""
    if codigo == 'ESTATAL':
        return 700000, None
    if ejercicio not in (2024, 2025, 2026):
        return None
    if codigo == 'CATALUNA':
        return 500000, CATALUNA
    if codigo == 'VALENCIANA':
        return {2024: 500000, 2025: 1000000, 2026: 2000000}[ejercicio], VALENCIANA
    if codigo == 'MADRID':
        # DT séptima DLeg.1/2010: régimen transitorio mientras siga vigente ITS.
        return 700000, None
    if ejercicio == 2025:
        especiales = {'ASTURIAS': (700000, ASTURIAS), 'BALEARES': (3000000, BALEARES),
            'CANTABRIA': (700000, CANTABRIA), 'EXTREMADURA': (500000, EXTREMADURA)}
        if codigo in especiales:
            return especiales[codigo]
        if codigo in ('ANDALUCIA', 'ARAGON', 'CANARIAS', 'CASTILLA_LA_MANCHA',
                      'CASTILLA_LEON', 'GALICIA', 'MADRID', 'MURCIA', 'RIOJA'):
            return 700000, None
    # Regímenes forales, Ceuta/Melilla y años sin matriz contrastada requieren
    # sus puntos de conexión/beneficios específicos: no se asimilan al común.
    return None


def cuota(base, tarifa):
    importe = Decimal(str(base))
    if importe <= 0:
        return 0.0
    desde, fija, tipo = next(t for t in reversed(tarifa) if importe >= Decimal(t[0]))
    return float((Decimal(fija) + (importe - Decimal(desde)) * Decimal(tipo))
                 .quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def bonificacion_general(codigo, ejercicio, cuota_ip, cuota_its):
    """Estimación general ANTES de límites conjuntos y beneficios particulares.

    AEAT Patrimonio 2025, capítulo 5. Galicia usa la secuencia específica de
    cumplimentación 4.3.9.8: IP inicial al 50 %, ITS simulado con ese IP y
    ajuste de la bonificación. No sustituye el pago acreditado del 714 en el 718.
    """
    if ejercicio != 2025 and not (codigo == 'MADRID' and ejercicio in (2024, 2026)):
        return None
    ip, its = Decimal(str(cuota_ip)), Decimal(str(cuota_its))
    if codigo in ('MADRID', 'ANDALUCIA', 'CANTABRIA', 'MURCIA', 'RIOJA'):
        valor = max(Decimal(0), ip - its)
    elif codigo == 'EXTREMADURA':
        valor = ip
    elif codigo == 'GALICIA':
        inicial = (ip * Decimal('.5')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        ip_inicial = max(Decimal(0), ip - inicial)
        its_inicial = max(Decimal(0), its - ip_inicial)
        valor = max(Decimal(0), inicial - its_inicial)
    elif codigo in ('ESTATAL', 'CATALUNA', 'VALENCIANA', 'ASTURIAS', 'BALEARES',
                    'ARAGON', 'CANARIAS', 'CASTILLA_LA_MANCHA', 'CASTILLA_LEON'):
        valor = Decimal(0)
    else:
        return None
    return float(valor.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
