"""RailOps · Analítica: comparación entre ventanas y predicción experimental.

Módulo NUEVO. No modifica ningún archivo existente ni escribe en la base de datos:
abre SQLite siempre en modo solo lectura. Solo usa la biblioteca estándar de
Python, para funcionar igual en Windows y en IBM Z (sin numpy, pandas ni Arrow).

Entrenar el modelo desde la terminal (no se entrena solo en cada recarga):

    python railops_analitica.py --entrenar
    python railops_analitica.py --entrenar --db enlace_datos/enlace.db

El modelo se guarda en modelos/railops_riesgo.json (carpeta ignorada por git).
"""

import bisect
import sys
import json
import math
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_POR_DEFECTO = BASE / "enlace_datos" / "enlace.db"
MODELO_POR_DEFECTO = BASE / "modelos" / "railops_riesgo.json"

UMBRAL_RETRASO_S = 300      # 5 minutos, igual que railops_logica
HORIZONTE_S = 600           # predicción a 10 minutos
TOLERANCIA_S = 90           # margen para encontrar la observación futura/pasada
VERSION_MODELO = 1

# Centros aproximados para poner nombre a cada núcleo según la posición media
# de sus estaciones. Solo es una etiqueta legible; el código real se muestra.
CIUDADES = [
    ("Madrid", 40.42, -3.70), ("Barcelona", 41.39, 2.17),
    ("Valencia", 39.47, -0.38), ("Sevilla", 37.39, -5.98),
    ("Málaga", 36.72, -4.42), ("Cádiz", 36.53, -6.29),
    ("Bilbao", 43.26, -2.93), ("San Sebastián", 43.32, -1.98),
    ("Santander", 43.46, -3.80), ("Asturias", 43.36, -5.85),
    ("Murcia/Alicante", 38.17, -0.80), ("Cartagena", 37.60, -0.98),
    ("Ferrol", 43.48, -8.23), ("León", 42.60, -5.57),
    ("Zaragoza", 41.65, -0.88),
]


# ---------------------------------------------------------------------------
# Acceso a datos (solo lectura)
# ---------------------------------------------------------------------------

def conectar_ro(db):
    ruta = Path(db).resolve()
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe la base de datos: {ruta}")
    con = sqlite3.connect(ruta.as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def huella_db(db):
    """Identificador barato del contenido actual (para la caché de Streamlit)."""
    con = conectar_ro(db)
    try:
        fila = con.execute(
            "SELECT MAX(rowid) AS n FROM obs_actualizacion"
        ).fetchone()
        return int(fila["n"] or 0)
    finally:
        con.close()


class Obs:
    """Observación compacta (__slots__ ocupa ~3 veces menos memoria que un dict).
    Se accede como un diccionario: o["t"], o["d"]..."""
    __slots__ = ("trip", "route", "nucleo", "grupo", "t", "d", "cancelado")

    def __init__(self, trip, route, nucleo, grupo, t, d, cancelado):
        self.trip, self.route, self.nucleo, self.grupo = trip, route, nucleo, grupo
        self.t, self.d, self.cancelado = t, d, cancelado

    def __getitem__(self, clave):
        return getattr(self, clave)


def _nombre_nucleo(lat, lon):
    mejor = min(CIUDADES, key=lambda c: (c[1] - lat) ** 2 + (c[2] - lon) ** 2)
    return mejor[0]


def cargar_datos(db, horas=24):
    """Carga las observaciones compactas necesarias para todo el panel.

    Una observación = (viaje, publicación del feed). Si un viaje trae varias
    paradas en la misma captura, se toma la primera fila guardada (próxima
    parada). Las publicaciones repetidas del feed se eliminan. Solo se cargan
    las últimas `horas` horas para limitar la memoria.
    """
    con = conectar_ro(db)
    try:
        rutas = {
            r["route_id"]: r["route_short_name"] or r["route_id"]
            for r in con.execute(
                "SELECT route_id, route_short_name FROM gtfs_routes"
            )
        }

        centros = {}
        for r in con.execute(
            """
            SELECT substr(a.route_id_gtfs, 1, 2) AS nucleo,
                   AVG(s.stop_lat) AS lat, AVG(s.stop_lon) AS lon
            FROM (SELECT DISTINCT route_id_gtfs, stop_id
                  FROM obs_actualizacion
                  WHERE route_id_gtfs IS NOT NULL) a
            JOIN gtfs_stops s ON s.stop_id = a.stop_id
            GROUP BY 1
            """
        ):
            if r["lat"] is not None:
                centros[r["nucleo"]] = _nombre_nucleo(r["lat"], r["lon"])

        filas = con.execute(
            """
            SELECT trip_id, route_id_gtfs, cabecera_ts, retraso_viaje_s,
                   relacion_viaje, MIN(rowid) AS primera
            FROM obs_actualizacion
            WHERE route_id_gtfs IS NOT NULL
              AND cabecera_ts IS NOT NULL
              AND trip_id IS NOT NULL
              AND captura_id >= (
                  SELECT COALESCE(MIN(id), 0) FROM capturas
                  WHERE captura_epoch >= (SELECT MAX(captura_epoch) FROM capturas) - ?
              )
            GROUP BY captura_id, trip_id
            """,
            (int(horas * 3600),),
        )

        vistos = set()
        obs = []
        textos = {}

        def unico(v):  # comparte cadenas repetidas para ahorrar memoria
            return textos.setdefault(v, sys.intern(v) if isinstance(v, str) else v)

        for f in filas:
            clave = (f["trip_id"], f["cabecera_ts"])
            if clave in vistos:
                continue
            vistos.add(clave)
            route_id = unico(f["route_id_gtfs"])
            nucleo = unico(route_id[:2])
            corto = rutas.get(route_id, route_id)
            obs.append(Obs(
                unico(f["trip_id"]), route_id, nucleo, unico(f"{nucleo}|{corto}"),
                int(f["cabecera_ts"]), f["retraso_viaje_s"],
                (f["relacion_viaje"] or "") == "CANCELED",
            ))

        capturas = [
            r[0] for r in con.execute(
                """
                SELECT captura_epoch FROM capturas
                WHERE feed = 'trip_updates' AND error IS NULL
                ORDER BY captura_epoch
                """
            )
        ]

        avisos = [dict(r) for r in con.execute(
            """
            SELECT aviso_id, primera_captura_utc, ultima_captura_utc,
                   cabecera, descripcion, entidades_json
            FROM avisos
            """
        )]
    finally:
        con.close()

    obs.sort(key=lambda o: o.t)

    nucleos = {}
    for o in obs:
        nucleos.setdefault(o["nucleo"], set()).add(o["grupo"])

    for a in avisos:
        a["inicio_ts"] = _iso_a_epoch(a["primera_captura_utc"])
        a["fin_ts"] = _iso_a_epoch(a["ultima_captura_utc"])
        try:
            ents = json.loads(a["entidades_json"] or "[]")
        except ValueError:
            ents = []
        a["rutas"] = {e.get("routeId") for e in ents if e.get("routeId")}

    return {
        "obs": obs,
        "tiempos": sorted({o["t"] for o in obs}),
        "capturas": capturas,
        "nucleos": {
            n: {"nombre": centros.get(n, f"Núcleo {n}"),
                "grupos": sorted(g, key=_orden_linea)}
            for n, g in nucleos.items()
        },
        "avisos": avisos,
    }


def _iso_a_epoch(valor):
    if not valor:
        return None
    try:
        f = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    if f.tzinfo is None:
        f = f.replace(tzinfo=timezone.utc)
    return int(f.timestamp())


def _orden_linea(grupo):
    corto = grupo.split("|", 1)[1]
    letras = "".join(c for c in corto if c.isalpha())
    numeros = "".join(c for c in corto if c.isdigit())
    return (letras[:1], int(numeros) if numeros else 999, corto)


def nombre_linea(grupo):
    return grupo.split("|", 1)[1]


# ---------------------------------------------------------------------------
# Comparación entre ventanas
# ---------------------------------------------------------------------------

def _ultimas_por_viaje(obs, desde, hasta, filtro):
    """Última observación de cada viaje en (desde, hasta]. Evita contar
    varias veces el mismo tren por la frecuencia del recolector."""
    ultimas = {}
    for o in obs:
        if o["t"] <= desde:
            continue
        if o["t"] > hasta:
            break
        if filtro(o):
            ultimas[o["trip"]] = o
    return ultimas


def _metricas(ultimas):
    validos = [o for o in ultimas.values() if not o["cancelado"] and o["d"] is not None]
    n5 = sum(1 for o in validos if o["d"] >= UMBRAL_RETRASO_S)
    return {
        "viajes": len(ultimas),
        "con_dato": len(validos),
        "retraso_5": n5,
        "cancelados": sum(1 for o in ultimas.values() if o["cancelado"]),
        "pct_5": round(100.0 * n5 / len(validos), 1) if validos else None,
        "medio_min": (round(sum(o["d"] for o in validos) / len(validos) / 60.0, 1)
                      if validos else None),
        "max_min": (round(max(o["d"] for o in validos) / 60.0, 1)
                    if validos else None),
    }


def clasificar(prev, act, minimo):
    """Reglas explícitas de prioridad (documentadas en la pantalla)."""
    if act["con_dato"] < minimo or prev["con_dato"] < minimo:
        return "insuficiente", "Datos insuficientes"
    cambio = act["pct_5"] - prev["pct_5"]
    if cambio >= 10 and act["pct_5"] >= 20:
        return "alta", "Prioridad alta"
    if cambio >= 5:
        return "media", "Prioridad media"
    if cambio <= -5:
        return "mejora", "Mejora"
    if act["pct_5"] >= 30:
        return "media", "Nivel alto sostenido"
    return "estable", "Estable"


ORDEN_PRIORIDAD = {"alta": 0, "media": 1, "estable": 2, "mejora": 3, "insuficiente": 4}


def comparar_ventanas(datos, nucleo, ref_ts, minutos=15, minimo=8):
    obs = _recorte(datos, ref_ts - 2 * minutos * 60, ref_ts)
    ini_act = ref_ts - minutos * 60
    ini_prev = ref_ts - 2 * minutos * 60

    def filtro(o):
        return o["nucleo"] == nucleo

    prev_u = _ultimas_por_viaje(obs, ini_prev, ini_act, filtro)
    act_u = _ultimas_por_viaje(obs, ini_act, ref_ts, filtro)

    lineas = []
    for g in datos["nucleos"][nucleo]["grupos"]:
        p = {k: v for k, v in prev_u.items() if v["grupo"] == g}
        a = {k: v for k, v in act_u.items() if v["grupo"] == g}
        if not p and not a:
            continue
        mp, ma = _metricas(p), _metricas(a)
        nivel, etiqueta = clasificar(mp, ma, minimo)
        comunes = len(set(p) & set(a))
        cambio = (round(ma["pct_5"] - mp["pct_5"], 1)
                  if ma["pct_5"] is not None and mp["pct_5"] is not None else None)
        lineas.append({
            "grupo": g, "linea": nombre_linea(g),
            "rutas": sorted({o["route"] for o in list(p.values()) + list(a.values())}),
            "anterior": mp, "actual": ma, "cambio_pp": cambio,
            "viajes_comunes": comunes,
            "nivel": nivel, "etiqueta": etiqueta,
        })

    lineas.sort(key=lambda l: (
        ORDEN_PRIORIDAD[l["nivel"]],
        -(l["cambio_pp"] if l["cambio_pp"] is not None else -999),
        -(l["actual"]["pct_5"] or 0),
    ))

    capt_act = _contar_capturas(datos, ini_act, ref_ts)
    capt_prev = _contar_capturas(datos, ini_prev, ini_act)
    esperadas = max(1, int(minutos * 60 / 30))

    return {
        "ref_ts": ref_ts, "minutos": minutos, "minimo": minimo,
        "total_anterior": _metricas(prev_u),
        "total_actual": _metricas(act_u),
        "viajes_comunes": len(set(prev_u) & set(act_u)),
        "capturas_actual": capt_act,
        "capturas_anterior": capt_prev,
        "capturas_esperadas": esperadas,
        "lineas": lineas,
    }


def _recorte(datos, desde, hasta):
    """Sublista de observaciones con desde < t <= hasta (búsqueda binaria)."""
    tiempos = datos.setdefault("_t_obs", [o["t"] for o in datos["obs"]])
    i = bisect.bisect_right(tiempos, desde)
    j = bisect.bisect_right(tiempos, hasta)
    return datos["obs"][i:j]


def _contar_capturas(datos, desde, hasta):
    t = datos["tiempos"]  # publicaciones distintas del feed con datos
    return bisect.bisect_right(t, hasta) - bisect.bisect_right(t, desde)


def serie_temporal(datos, nucleo, grupos=None, cubo_min=15, hasta=None):
    """Porcentaje de viajes con retraso >=5 min por intervalo de cubo_min.
    Dentro de cada intervalo se usa la última observación de cada viaje.
    Intervalos sin publicaciones del feed devuelven None (hueco visible)."""
    if not datos["tiempos"]:
        return {"cubos": [], "series": {}}
    paso = cubo_min * 60
    t0 = datos["tiempos"][0] // paso * paso
    t1 = (hasta or datos["tiempos"][-1])
    cubos = list(range(t0, t1 + 1, paso))
    claves = ["__total__"] + list(grupos or [])
    acum = {c: {} for c in claves}
    for o in datos["obs"]:
        if o["nucleo"] != nucleo or o["t"] > t1:
            continue
        b = (o["t"] - t0) // paso
        acum["__total__"].setdefault(b, {})[o["trip"]] = o
        if o["grupo"] in acum:
            acum[o["grupo"]].setdefault(b, {})[o["trip"]] = o
    series = {}
    for c in claves:
        valores = []
        for b in range(len(cubos)):
            m = _metricas(acum[c].get(b, {}))
            valores.append({"pct": m["pct_5"], "n": m["con_dato"]})
        series[c] = valores
    return {"cubos": cubos, "series": series, "paso": paso}


def avisos_activos(datos, rutas, desde, hasta):
    rutas = set(rutas)
    out = []
    for a in datos["avisos"]:
        if not (a["rutas"] & rutas):
            continue
        if a["inicio_ts"] is not None and a["inicio_ts"] > hasta:
            continue
        if a["fin_ts"] is not None and a["fin_ts"] < desde:
            continue
        texto = (a["descripcion"] or a["cabecera"] or "").strip()
        out.append({"id": a["aviso_id"], "texto": texto})
    return out


# ---------------------------------------------------------------------------
# Aprendizaje automático: riesgo de superar 5 min dentro de 10 min
# ---------------------------------------------------------------------------

CARACTERISTICAS = [
    ("retraso_min", "Retraso publicado ahora (min)"),
    ("cambio_5_min", "Cambio en los últimos 5 min (min)"),
    ("cambio_10_min", "Cambio en los últimos 10 min (min)"),
    ("linea_medio_min", "Retraso medio de la línea, minuto anterior (min)"),
    ("linea_pct5", "% de la línea con retraso ≥5 min, minuto anterior"),
    ("linea_viajes", "Viajes de la línea observados, minuto anterior"),
    ("min_en_feed", "Minutos que lleva el viaje en el feed"),
]
NOMBRES = [c[0] for c in CARACTERISTICAS]


def _indices(datos):
    """Series por viaje y contexto por línea y minuto (se calcula una vez)."""
    if "_ml" in datos:
        return datos["_ml"]
    por_viaje = {}
    contexto = {}
    for o in datos["obs"]:
        if o["cancelado"] or o["d"] is None:
            continue
        s = por_viaje.setdefault(o["trip"], {"t": [], "d": [], "grupo": o["grupo"]})
        s["t"].append(o["t"])
        s["d"].append(o["d"])
        c = contexto.setdefault((o["grupo"], o["t"] // 60), {})
        c[o["trip"]] = o["d"]
    resumen = {}
    for k, v in contexto.items():
        vals = list(v.values())
        resumen[k] = (
            sum(vals) / len(vals) / 60.0,
            100.0 * sum(1 for x in vals if x >= UMBRAL_RETRASO_S) / len(vals),
            len(vals),
        )
    datos["_ml"] = (por_viaje, resumen)
    return datos["_ml"]


def _valor_cercano(ts, ds, objetivo, tolerancia=TOLERANCIA_S, hasta=None):
    """Valor más cercano a 'objetivo' (±tolerancia). Con 'hasta' solo mira
    índices < hasta, para no usar información futura."""
    n = len(ts) if hasta is None else hasta
    i = bisect.bisect_left(ts, objetivo, 0, n)
    mejor = None
    for j in (i - 1, i):
        if 0 <= j < n and abs(ts[j] - objetivo) <= tolerancia:
            if mejor is None or abs(ts[j] - objetivo) < abs(ts[mejor] - objetivo):
                mejor = j
    return None if mejor is None else ds[mejor]


def _fila(serie, i, resumen):
    """Características de un viaje en su observación i. Solo usa el pasado."""
    ts, ds = serie["t"], serie["d"]
    t, d = ts[i], ds[i]
    d5 = _valor_cercano(ts, ds, t - 300, hasta=i + 1)
    if d5 is None:
        return None
    d10 = _valor_cercano(ts, ds, t - 600, hasta=i + 1)
    if d10 is None:
        d10 = d5
    ctx = resumen.get((serie["grupo"], t // 60 - 1))
    if ctx is None:
        ctx = (d / 60.0, 100.0 if d >= UMBRAL_RETRASO_S else 0.0, 0)
    return [
        d / 60.0, (d - d5) / 60.0, (d - d10) / 60.0,
        ctx[0], ctx[1], float(ctx[2]),
        min((t - ts[0]) / 60.0, 180.0),
    ]


def construir_ejemplos(datos, paso_s=60):
    """Ejemplos de viajes con retraso < 5 min. Etiqueta: retraso ≥ 5 min
    publicado unos 10 minutos después (±90 s). Se toma como mucho una
    muestra por viaje y minuto para no inflar el conjunto."""
    por_viaje, resumen = _indices(datos)
    ejemplos = []
    for trip, s in por_viaje.items():
        ultimo = None
        for i, (t, d) in enumerate(zip(s["t"], s["d"])):
            if d >= UMBRAL_RETRASO_S:
                continue
            if ultimo is not None and t - ultimo < paso_s:
                continue
            futuro = _valor_cercano(s["t"], s["d"], t + HORIZONTE_S)
            if futuro is None:
                continue
            x = _fila(s, i, resumen)
            if x is None:
                continue
            ultimo = t
            ejemplos.append({
                "t": t, "trip": trip, "grupo": s["grupo"], "x": x,
                "y": 1 if futuro >= UMBRAL_RETRASO_S else 0,
                "d": d, "futuro": futuro,
            })
    ejemplos.sort(key=lambda e: e["t"])
    return ejemplos


# --- Gradient boosting con árboles pequeños, en Python puro -----------------

def _bordes(columna, n_bins=32):
    valores = sorted(columna)
    bordes = []
    for k in range(1, n_bins):
        v = valores[int(k * (len(valores) - 1) / n_bins)]
        if not bordes or v > bordes[-1]:
            bordes.append(v)
    return bordes


def _sigmoide(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def entrenar_gbm(X, y, arboles=80, profundidad=3, aprendizaje=0.1,
                 min_hoja=60, lambda_l2=1.0, semilla=7, progreso=None):
    n, m = len(X), len(X[0])
    bordes = [_bordes([x[j] for x in X]) for j in range(m)]
    binned = [[bisect.bisect_right(bordes[j], x[j]) for x in X] for j in range(m)]
    nb = [len(b) + 1 for b in bordes]

    pos = sum(y)
    p0 = min(max(pos / n, 1e-4), 1 - 1e-4)
    base = math.log(p0 / (1 - p0))
    f = [base] * n
    modelo_arboles = []
    rnd = random.Random(semilla)

    for k in range(arboles):
        g = [0.0] * n
        h = [0.0] * n
        for i in range(n):
            p = _sigmoide(f[i])
            g[i] = p - y[i]
            h[i] = max(p * (1 - p), 1e-6)

        # Submuestreo del 80 % de filas por árbol (reduce sobreajuste).
        idx = [i for i in range(n) if rnd.random() < 0.8]
        arbol = _crecer(idx, binned, bordes, nb, g, h, profundidad,
                        min_hoja, lambda_l2, aprendizaje)
        modelo_arboles.append(arbol)
        for i in range(n):
            f[i] += _evaluar_bin(arbol, binned, i)
        if progreso:
            progreso(k + 1, arboles)

    return {"base": base, "arboles": modelo_arboles}


def _crecer(idx, binned, bordes, nb, g, h, prof, min_hoja, lam, lr):
    G = sum(g[i] for i in idx)
    H = sum(h[i] for i in idx)
    hoja = {"valor": -lr * G / (H + lam)}
    if prof == 0 or len(idx) < 2 * min_hoja:
        return hoja

    mejor = None
    padre = G * G / (H + lam)
    for j in range(len(binned)):
        col = binned[j]
        hg = [0.0] * nb[j]
        hh = [0.0] * nb[j]
        hc = [0] * nb[j]
        for i in idx:
            b = col[i]
            hg[b] += g[i]
            hh[b] += h[i]
            hc[b] += 1
        gl = hl = 0.0
        cl = 0
        for b in range(nb[j] - 1):
            gl += hg[b]
            hl += hh[b]
            cl += hc[b]
            if cl < min_hoja or len(idx) - cl < min_hoja:
                continue
            gr, hr = G - gl, H - hl
            ganancia = gl * gl / (hl + lam) + gr * gr / (hr + lam) - padre
            if mejor is None or ganancia > mejor[0]:
                mejor = (ganancia, j, b)

    if mejor is None or mejor[0] <= 1e-6:
        return hoja

    _, j, b = mejor
    izq = [i for i in idx if binned[j][i] <= b]
    der = [i for i in idx if binned[j][i] > b]
    return {
        "j": j, "bin": b, "umbral": bordes[j][b],
        "izq": _crecer(izq, binned, bordes, nb, g, h, prof - 1, min_hoja, lam, lr),
        "der": _crecer(der, binned, bordes, nb, g, h, prof - 1, min_hoja, lam, lr),
    }


def _evaluar_bin(nodo, binned, i):
    while "valor" not in nodo:
        nodo = nodo["izq"] if binned[nodo["j"]][i] <= nodo["bin"] else nodo["der"]
    return nodo["valor"]


def predecir(modelo, x):
    z = modelo["base"]
    for nodo in modelo["arboles"]:
        while "valor" not in nodo:
            # bisect_right en el entrenamiento: valor < umbral -> bin <= b
            nodo = nodo["izq"] if x[nodo["j"]] < nodo["umbral"] else nodo["der"]
        z += nodo["valor"]
    return _sigmoide(z)


# --- Métricas ---------------------------------------------------------------

def auc(y, s):
    pares = sorted(zip(s, y))
    rangos = [0.0] * len(pares)
    i = 0
    while i < len(pares):
        j = i
        while j + 1 < len(pares) and pares[j + 1][0] == pares[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            rangos[k] = r
        i = j + 1
    pos = sum(y)
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return None
    suma = sum(r for r, (_, yy) in zip(rangos, pares) if yy == 1)
    return (suma - pos * (pos + 1) / 2.0) / (pos * neg)


def precision_superior(y, s, fraccion=0.10):
    k = max(1, int(len(y) * fraccion))
    orden = sorted(range(len(y)), key=lambda i: -s[i])[:k]
    aciertos = sum(y[i] for i in orden)
    total_pos = max(1, sum(y))
    return aciertos / k, aciertos / total_pos, k


def calibracion(y, p, grupos=10):
    orden = sorted(range(len(y)), key=lambda i: p[i])
    out = []
    for g in range(grupos):
        tramo = orden[g * len(orden) // grupos:(g + 1) * len(orden) // grupos]
        if not tramo:
            continue
        out.append({
            "pred": sum(p[i] for i in tramo) / len(tramo),
            "obs": sum(y[i] for i in tramo) / len(tramo),
            "n": len(tramo),
        })
    return out


# --- Entrenamiento completo ---------------------------------------------------

def entrenar_y_evaluar(datos, fraccion_entreno=0.70, max_filas=40000,
                       progreso=None, origen_db=None):
    ejemplos = construir_ejemplos(datos)
    if len(ejemplos) < 2000:
        raise ValueError(
            f"Solo hay {len(ejemplos)} ejemplos. Deja el recolector "
            "funcionando más tiempo antes de entrenar."
        )
    t_ini, t_fin = ejemplos[0]["t"], ejemplos[-1]["t"]
    corte = t_ini + fraccion_entreno * (t_fin - t_ini)
    # Separación temporal con margen: la etiqueta mira 10 min (+90 s) adelante,
    # así que ningún ejemplo de entrenamiento "ve" el periodo de prueba.
    margen = HORIZONTE_S + TOLERANCIA_S
    entreno = [e for e in ejemplos if e["t"] + margen < corte]
    prueba = [e for e in ejemplos if e["t"] >= corte]
    if len(prueba) < 500 or sum(e["y"] for e in prueba) < 20:
        raise ValueError("El periodo de prueba no tiene ejemplos suficientes.")

    rnd = random.Random(11)
    muestra = entreno if len(entreno) <= max_filas else rnd.sample(entreno, max_filas)

    inicio = time.perf_counter()
    gbm = entrenar_gbm([e["x"] for e in muestra], [e["y"] for e in muestra],
                       progreso=progreso)
    segundos = time.perf_counter() - inicio

    y = [e["y"] for e in prueba]
    p = [predecir(gbm, e["x"]) for e in prueba]
    base = [e["x"][0] + 1e-3 * e["x"][1] for e in prueba]  # retraso actual

    prec_m, rec_m, k = precision_superior(y, p)
    prec_b, rec_b, _ = precision_superior(y, base)
    tasa = sum(y) / len(y)
    brier_m = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / len(y)
    brier_b = sum((tasa - yi) ** 2 for yi in y) / len(y)

    # Importancia por permutación (en una muestra de la prueba).
    sub = prueba if len(prueba) <= 6000 else random.Random(3).sample(prueba, 6000)
    ys = [e["y"] for e in sub]
    auc_ref = auc(ys, [predecir(gbm, e["x"]) for e in sub])
    importancia = []
    for j, (nombre, texto) in enumerate(CARACTERISTICAS):
        col = [e["x"][j] for e in sub]
        random.Random(j).shuffle(col)
        ps = []
        for e, v in zip(sub, col):
            x = list(e["x"])
            x[j] = v
            ps.append(predecir(gbm, x))
        importancia.append({"nombre": nombre, "texto": texto,
                            "caida_auc": round(auc_ref - auc(ys, ps), 4)})
    importancia.sort(key=lambda r: -r["caida_auc"])

    evaluacion = {
        "ejemplos_total": len(ejemplos),
        "ejemplos_entreno": len(entreno),
        "ejemplos_usados_entreno": len(muestra),
        "ejemplos_prueba": len(prueba),
        "viajes_prueba": len({e["trip"] for e in prueba}),
        "entreno_desde": entreno[0]["t"], "entreno_hasta": entreno[-1]["t"],
        "prueba_desde": prueba[0]["t"], "prueba_hasta": prueba[-1]["t"],
        "tasa_positivos_prueba": round(tasa, 4),
        "auc_modelo": round(auc(y, p), 4),
        "auc_referencia": round(auc(y, base), 4),
        "precision_top10_modelo": round(prec_m, 4),
        "precision_top10_referencia": round(prec_b, 4),
        "recall_top10_modelo": round(rec_m, 4),
        "recall_top10_referencia": round(rec_b, 4),
        "alertas_top10": k,
        "exactitud_persistencia": round(1 - tasa, 4),
        "brier_modelo": round(brier_m, 4),
        "brier_tasa_constante": round(brier_b, 4),
        "calibracion": calibracion(y, p),
        "importancia": importancia,
        "segundos_entreno": round(segundos, 1),
    }
    return {
        "version": VERSION_MODELO,
        "creado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origen_db": str(origen_db) if origen_db else None,
        "objetivo": ("Viaje con retraso publicado < 5 min: ¿publicará ≥ 5 min "
                     "dentro de 10 min (±90 s)?"),
        "caracteristicas": CARACTERISTICAS,
        "hiperparametros": {"arboles": 80, "profundidad": 3, "aprendizaje": 0.1,
                             "min_hoja": 60, "bins": 32},
        "evaluacion": evaluacion,
        "modelo": gbm,
    }


def guardar_modelo(resultado, ruta=MODELO_POR_DEFECTO):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    temporal.replace(ruta)
    return ruta


def cargar_modelo(ruta=MODELO_POR_DEFECTO):
    ruta = Path(ruta)
    if not ruta.is_file():
        return None
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if datos.get("version") != VERSION_MODELO:
        return None
    return datos


def predicciones_en(datos, modelo, ref_ts, nucleo=None, frescura_s=180):
    """Riesgo para los viajes con retraso < 5 min observados justo antes de
    ref_ts. Si ref_ts es histórico, añade lo que ocurrió después."""
    por_viaje, resumen = _indices(datos)
    out = []
    for trip, s in por_viaje.items():
        if nucleo and not s["grupo"].startswith(nucleo + "|"):
            continue
        i = bisect.bisect_right(s["t"], ref_ts) - 1
        if i < 0 or ref_ts - s["t"][i] > frescura_s:
            continue
        if s["d"][i] >= UMBRAL_RETRASO_S:
            continue
        x = _fila(s, i, resumen)
        if x is None:
            continue
        futuro = _valor_cercano(s["t"], s["d"], s["t"][i] + HORIZONTE_S)
        out.append({
            "trip": trip, "grupo": s["grupo"], "linea": nombre_linea(s["grupo"]),
            "t": s["t"][i], "retraso_min": round(x[0], 1),
            "cambio_5_min": round(x[1], 1), "linea_pct5": round(x[4], 1),
            "riesgo": predecir(modelo["modelo"], x),
            "futuro_min": None if futuro is None else round(futuro / 60.0, 1),
        })
    out.sort(key=lambda r: -r["riesgo"])
    return out


# ---------------------------------------------------------------------------
# Línea de comandos
# ---------------------------------------------------------------------------

def _main():
    import argparse

    ap = argparse.ArgumentParser(description="RailOps · analítica y modelo")
    ap.add_argument("--db", default=str(DB_POR_DEFECTO))
    ap.add_argument("--modelo", default=str(MODELO_POR_DEFECTO))
    ap.add_argument("--entrenar", action="store_true")
    ap.add_argument("--horas", type=float, default=24,
                    help="Horas de histórico a usar (por defecto 24)")
    args = ap.parse_args()

    t0 = time.perf_counter()
    print(f"Leyendo {args.db} (solo lectura)...", flush=True)
    datos = cargar_datos(args.db, args.horas)
    print(f"  {len(datos['obs'])} observaciones, "
          f"{len({o['trip'] for o in datos['obs']})} viajes, "
          f"{time.perf_counter() - t0:.1f} s", flush=True)

    if not args.entrenar:
        print("Usa --entrenar para entrenar y guardar el modelo.")
        return

    def prog(k, n):
        if k % 10 == 0 or k == n:
            print(f"  árbol {k}/{n}", flush=True)

    r = entrenar_y_evaluar(datos, progreso=prog, origen_db=args.db)
    ruta = guardar_modelo(r, args.modelo)
    ev = r["evaluacion"]
    print(json.dumps({k: v for k, v in ev.items()
                      if k not in ("calibracion", "importancia")},
                     indent=2, ensure_ascii=False))
    print(f"Modelo guardado en {ruta}")


if __name__ == "__main__":
    _main()
