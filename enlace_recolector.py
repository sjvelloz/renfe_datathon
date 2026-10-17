#!/usr/bin/env python3
"""
enlace_recolector.py  ·  Proyecto Enlace (IBM Z Datathon 2026)
=================================================================
Recolector continuo + auditor de cobertura para dejar corriendo en LinuxONE.

Qué hace
--------
1. CATÁLOGO (al arrancar y cada --horas-catalogo):
   - Comprueba TODOS los enlaces del "Plan definitivo" (datos y documentación).
   - Para las fichas de data.renfe.com consulta su API CKAN y descubre los
     recursos reales que publican (JSON, .pb, ZIP). Si aparece uno nuevo, lo avisa.
2. GTFS ESTÁTICO (al arrancar y cada --horas-gtfs; solo recarga si cambia):
   - Descarga fomento_transit.zip, lo versiona por hash y carga en SQLite:
     stops, routes, trips, stop_times, calendar, calendar_dates (y shapes si existe).
3. TIEMPO REAL (cada --intervalo segundos):
   - vehicle_positions.json, trip_updates.json, alerts.json.
   - Guarda el JSON original comprimido (raw/AAAA-MM-DD/...json.gz).
   - Normaliza según el contrato común del plan (sección 4) en SQLite:
       hora de captura UTC, fecha de cabecera, timestamp individual, trip_id,
       fecha de servicio si existe, stop_id, route_id si se resuelve,
       coordenadas, retraso, llegada, salida, estado. IDs como texto.
       AUSENTE = NULL, nunca 0.
   - Registra TODOS los campos que aparecen en cada feed (tabla campos_vistos)
     para detectar datos nuevos o inesperados (andén, ocupación, energía...).
   - Cada --cada-pb ciclos descarga también las versiones .pb y, si está
     instalado gtfs-realtime-bindings, compara sus campos con los del JSON.
4. ESTADO PARA EL MAPA (cada ciclo, escritura atómica):
   - salida/estado_actual.json  -> trenes con posición, retraso, parada, línea,
     coordenadas de la parada, antigüedad y origen de cada dato.
   - salida/estaciones.geojson  -> estaciones GTFS (se regenera con cada GTFS).
5. INFORME (cada --minutos-informe):
   - salida/informe_cobertura.md / .json con veredicto por punto:
       llegadas/retrasos/cancelaciones · posiciones · historial · estaciones ·
       recorrido/horario · vías/andenes/señalización · ocupación · energía ·
       causas · y estado de cada enlace del plan.

Requisitos: Python 3.8+ y biblioteca estándar. Opcional:
    pip install gtfs-realtime-bindings     (para decodificar los .pb)

Arranque recomendado en el mainframe (sigue corriendo al cerrar la sesión):
    nohup python3 enlace_recolector.py --datos ./enlace_datos > recolector.out 2>&1 &
    tail -f enlace_datos/recolector.log
Parada limpia:  kill -TERM <pid>   (o Ctrl+C si está en primer plano)

Un único escritor en SQLite (este proceso). El mapa y los notebooks deben
abrir la base en solo lectura:  sqlite3.connect("file:...db?mode=ro", uri=True)
o leer salida/estado_actual.json.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta

# =====================================================================
# ENLACES DEL PLAN DEFINITIVO (sección 2)
# =====================================================================
ENLACES_PLAN = [
    # (nombre, url, tipo)  tipo: datos_rt | datos_gtfs | ficha_ckan | documentacion
    ("Posiciones (JSON)", "https://gtfsrt.renfe.com/vehicle_positions.json", "datos_rt"),
    ("Llegadas y retrasos (JSON)", "https://gtfsrt.renfe.com/trip_updates.json", "datos_rt"),
    ("GTFS oficial (ficha)", "https://data.renfe.com/dataset/horarios-cercanias", "ficha_ckan"),
    ("Descarga GTFS (ZIP)", "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip", "datos_gtfs"),
    ("Ficha de posiciones", "https://data.renfe.com/dataset/ubicacion-vehiculos", "ficha_ckan"),
    ("Ficha de actualizaciones", "https://data.renfe.com/dataset/horarios-viaje-cercanias", "ficha_ckan"),
    ("Referencia GTFS estático", "https://gtfs.org/documentation/schedule/reference/", "documentacion"),
    ("Referencia GTFS-Realtime", "https://gtfs.org/documentation/realtime/reference/", "documentacion"),
    ("Folium", "https://python-visualization.github.io/folium/", "documentacion"),
    ("Leaflet", "https://leafletjs.com/reference.html", "documentacion"),
    ("Granite 4.0", "https://www.ibm.com/granite/docs/models/granite4-0", "documentacion"),
    ("GGUF Granite micro", "https://huggingface.co/ibm-granite/granite-4.0-micro-GGUF", "documentacion"),
    ("Evento Datathon", "https://hack2skill.com/event/datathon-2026", "documentacion"),
    # Ficha extra (avisos): no está en el plan pero aporta la única fuente de "causas"
    ("Ficha de avisos (extra)", "https://data.renfe.com/dataset/incidencias-avisos", "ficha_ckan"),
]

FEEDS_JSON = {
    "vehicle_positions": "https://gtfsrt.renfe.com/vehicle_positions.json",
    "trip_updates": "https://gtfsrt.renfe.com/trip_updates.json",
    "alerts": "https://gtfsrt.renfe.com/alerts.json",
}
FEEDS_PB = {
    "vehicle_positions.pb": "https://gtfsrt.renfe.com/vehicle_positions.pb",
    "trip_updates.pb": "https://gtfsrt.renfe.com/trip_updates.pb",
    "alerts.pb": "https://gtfsrt.renfe.com/alerts.pb",
}
GTFS_ZIP = "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip"
CKAN_API = "https://data.renfe.com/api/3/action/package_show?id={}"

USER_AGENT = "enlace-recolector/2.0 (IBM Z Datathon 2026, uso academico)"

KW = {
    "vias": re.compile(r"(platform|track|via\b|vía|anden|andén|signal|senal|señal|enclav|interlock)", re.I),
    "ocupacion": re.compile(r"(occupancy|ocupaci|passenger|pasajer|crowd|carriage)", re.I),
    "energia": re.compile(r"(energy|energ|power|potencia|consum|kwh|mwh|voltage|tension|tensión)", re.I),
    "causa": re.compile(r"(cause|causa|motivo|reason)", re.I),
}

PARAR = False
log = logging.getLogger("enlace")


# =====================================================================
# Utilidades
# =====================================================================
def utc_now():
    return datetime.now(timezone.utc)


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def a_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def a_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def txt(x):
    return None if x is None or x == "" else str(x)


def g(d, *claves):
    """Lee un campo admitiendo camelCase (JSON de Renfe) o snake_case (protobuf)."""
    if not isinstance(d, dict):
        return None
    for c in claves:
        if c in d:
            return d[c]
        s = re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), c)
        if s in d:
            return d[s]
    return None


def descargar(url, timeout=30, intentos=3, cabeceras=None):
    """GET con reintentos y backoff. Devuelve (bytes, status, ms, headers)."""
    ultimo = None
    for i in range(intentos):
        try:
            h = {"User-Agent": USER_AGENT}
            if cabeceras:
                h.update(cabeceras)
            req = urllib.request.Request(url, headers=h)
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data, r.status, (time.perf_counter() - t0) * 1000, dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return b"", 304, 0, dict(e.headers)
            ultimo = e
        except Exception as e:  # noqa: BLE001
            ultimo = e
        time.sleep(2 * (i + 1))
    raise ultimo


def rutas_claves(obj, ruta="", out=None):
    if out is None:
        out = Counter()
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = f"{ruta}.{k}" if ruta else k
            out[r] += 1
            rutas_claves(v, r, out)
    elif isinstance(obj, list):
        for v in obj:
            rutas_claves(v, ruta + "[]", out)
    return out


def escribir_atomico(ruta, contenido, binario=False):
    tmp = ruta + ".tmp"
    with open(tmp, "wb" if binario else "w", **({} if binario else {"encoding": "utf-8"})) as f:
        f.write(contenido)
    os.replace(tmp, ruta)


# =====================================================================
# Base de datos (único escritor)
# =====================================================================
ESQUEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS capturas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed TEXT, captura_utc TEXT, captura_epoch INTEGER,
  cabecera_ts INTEGER, http_status INTEGER, latencia_ms REAL,
  bytes INTEGER, entidades INTEGER, sha1 TEXT, repetida INTEGER,
  ruta_raw TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_capt_feed ON capturas(feed, captura_epoch);

-- Contrato común: una fila por vehículo y captura
CREATE TABLE IF NOT EXISTS obs_posicion (
  captura_id INTEGER, captura_utc TEXT, cabecera_ts INTEGER, entidad_id TEXT,
  vehiculo_id TEXT, vehiculo_label TEXT, trip_id TEXT, route_id_feed TEXT,
  route_id_gtfs TEXT, fecha_servicio TEXT, hora_inicio TEXT, relacion_viaje TEXT,
  lat REAL, lon REAL, bearing REAL, speed REAL, odometer REAL,
  estado TEXT, stop_id TEXT, secuencia_parada INTEGER, ts_individual INTEGER,
  antiguedad_s INTEGER, ocupacion_estado TEXT, ocupacion_pct REAL,
  congestion TEXT, accesible TEXT
);
CREATE INDEX IF NOT EXISTS ix_pos_trip ON obs_posicion(trip_id, captura_id);
CREATE INDEX IF NOT EXISTS ix_pos_veh ON obs_posicion(vehiculo_id, captura_id);

-- Contrato común: una fila por parada actualizada (o por viaje si no hay paradas)
CREATE TABLE IF NOT EXISTS obs_actualizacion (
  captura_id INTEGER, captura_utc TEXT, cabecera_ts INTEGER, entidad_id TEXT,
  trip_id TEXT, route_id_feed TEXT, route_id_gtfs TEXT, fecha_servicio TEXT,
  hora_inicio TEXT, relacion_viaje TEXT, vehiculo_id TEXT, ts_individual INTEGER,
  retraso_viaje_s INTEGER, stop_id TEXT, secuencia_parada INTEGER,
  relacion_parada TEXT, llegada_epoch INTEGER, llegada_retraso_s INTEGER,
  llegada_incert INTEGER, salida_epoch INTEGER, salida_retraso_s INTEGER,
  salida_incert INTEGER, n_paradas_en_update INTEGER
);
CREATE INDEX IF NOT EXISTS ix_act_trip ON obs_actualizacion(trip_id, captura_id);
CREATE INDEX IF NOT EXISTS ix_act_stop ON obs_actualizacion(stop_id, captura_id);

-- Avisos: una fila por versión distinta del aviso
CREATE TABLE IF NOT EXISTS avisos (
  aviso_id TEXT, sha1 TEXT, primera_captura_utc TEXT, ultima_captura_utc TEXT,
  veces INTEGER, causa TEXT, efecto TEXT, inicio INTEGER, fin INTEGER,
  cabecera TEXT, descripcion TEXT, url TEXT, entidades_json TEXT,
  n_trip INTEGER, n_route INTEGER, n_stop INTEGER,
  PRIMARY KEY (aviso_id, sha1)
);

-- Inventario de todos los campos vistos en cada feed (JSON y .pb)
CREATE TABLE IF NOT EXISTS campos_vistos (
  feed TEXT, ruta TEXT, veces INTEGER, primera_utc TEXT, ultima_utc TEXT,
  PRIMARY KEY (feed, ruta)
);

CREATE TABLE IF NOT EXISTS enlaces (
  comprobado_utc TEXT, nombre TEXT, url TEXT, tipo TEXT, http_status INTEGER,
  latencia_ms REAL, content_type TEXT, bytes INTEGER, error TEXT, recursos_json TEXT
);

CREATE TABLE IF NOT EXISTS gtfs_versiones (
  sha1 TEXT PRIMARY KEY, descargado_utc TEXT, bytes INTEGER, last_modified TEXT,
  archivos TEXT, resumen_json TEXT, activa INTEGER
);

-- GTFS estático (se reemplaza en cada versión nueva)
CREATE TABLE IF NOT EXISTS gtfs_stops (stop_id TEXT PRIMARY KEY, stop_code TEXT, stop_name TEXT,
  stop_lat REAL, stop_lon REAL, location_type TEXT, parent_station TEXT,
  platform_code TEXT, wheelchair_boarding TEXT, extra_json TEXT);
CREATE TABLE IF NOT EXISTS gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT,
  route_short_name TEXT, route_long_name TEXT, route_type TEXT, route_color TEXT,
  route_text_color TEXT, extra_json TEXT);
CREATE TABLE IF NOT EXISTS gtfs_trips (trip_id TEXT PRIMARY KEY, route_id TEXT,
  service_id TEXT, trip_headsign TEXT, direction_id TEXT, shape_id TEXT,
  wheelchair_accessible TEXT, extra_json TEXT);
CREATE TABLE IF NOT EXISTS gtfs_stop_times (trip_id TEXT, arrival_time TEXT,
  departure_time TEXT, stop_id TEXT, stop_sequence INTEGER, pickup_type TEXT,
  drop_off_type TEXT);
CREATE INDEX IF NOT EXISTS ix_st_trip ON gtfs_stop_times(trip_id, stop_sequence);
CREATE INDEX IF NOT EXISTS ix_st_stop ON gtfs_stop_times(stop_id);
CREATE TABLE IF NOT EXISTS gtfs_calendar (service_id TEXT PRIMARY KEY, monday TEXT,
  tuesday TEXT, wednesday TEXT, thursday TEXT, friday TEXT, saturday TEXT,
  sunday TEXT, start_date TEXT, end_date TEXT);
CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (service_id TEXT, date TEXT, exception_type TEXT);
CREATE TABLE IF NOT EXISTS gtfs_shapes (shape_id TEXT, lat REAL, lon REAL, seq INTEGER);
CREATE INDEX IF NOT EXISTS ix_shape ON gtfs_shapes(shape_id, seq);
"""


def abrir_db(ruta):
    con = sqlite3.connect(ruta, timeout=60)
    con.executescript(ESQUEMA)
    return con


def registrar_campos(con, feed, contador, ahora):
    con.executemany(
        """INSERT INTO campos_vistos(feed, ruta, veces, primera_utc, ultima_utc)
           VALUES (?,?,?,?,?)
           ON CONFLICT(feed, ruta) DO UPDATE SET veces = veces + excluded.veces,
                                                 ultima_utc = excluded.ultima_utc""",
        [(feed, r, n, ahora, ahora) for r, n in contador.items()],
    )


# =====================================================================
# 1. Catálogo de enlaces
# =====================================================================
def comprobar_enlaces(con, carpeta):
    ahora = utc_now().isoformat()
    recursos_descubiertos = []
    for nombre, url, tipo in ENLACES_PLAN:
        st = ms = nbytes = None
        ctype = err = None
        recursos = None
        try:
            if tipo == "datos_gtfs":
                # HEAD-like: solo cabeceras (el ZIP se descarga en otra fase)
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
                t0 = time.perf_counter()
                with urllib.request.urlopen(req, timeout=30) as r:
                    st, ctype = r.status, r.headers.get("Content-Type")
                    nbytes = a_int(r.headers.get("Content-Length"))
                ms = (time.perf_counter() - t0) * 1000
            else:
                data, st, ms, h = descargar(url, timeout=30, intentos=2)
                ctype, nbytes = h.get("Content-Type"), len(data)
            if tipo == "ficha_ckan":
                slug = url.rstrip("/").split("/")[-1]
                data, _, _, _ = descargar(CKAN_API.format(slug), timeout=30, intentos=2)
                res = json.loads(data)["result"]
                recursos = [{"formato": r.get("format"), "nombre": r.get("name"),
                             "url": r.get("url"), "modificado": r.get("last_modified") or r.get("metadata_modified")}
                            for r in res.get("resources", [])]
                recursos_descubiertos += recursos
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        con.execute("INSERT INTO enlaces VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ahora, nombre, url, tipo, st, ms, ctype, nbytes, err,
                     json.dumps(recursos, ensure_ascii=False) if recursos is not None else None))
        log.info("Enlace %-30s %s %s", nombre, st or "-", err or "")
    con.commit()

    conocidos = set(FEEDS_JSON.values()) | set(FEEDS_PB.values()) | {GTFS_ZIP}
    nuevos = [r for r in recursos_descubiertos if r["url"] and r["url"] not in conocidos]
    if nuevos:
        log.warning("Recursos NUEVOS en data.renfe.com no incluidos en el recolector: %s", nuevos)
    escribir_atomico(os.path.join(carpeta, "salida", "catalogo_recursos.json"),
                     json.dumps({"comprobado_utc": ahora, "recursos": recursos_descubiertos,
                                 "nuevos_no_recolectados": nuevos}, ensure_ascii=False, indent=2))


# =====================================================================
# 2. GTFS estático
# =====================================================================
def _leer_csv(z, nombre):
    cand = [n for n in z.namelist() if n.lower().split("/")[-1] == nombre]
    if not cand:
        return None, None
    f = io.TextIOWrapper(z.open(cand[0]), encoding="utf-8-sig", errors="replace")
    r = csv.DictReader(f)
    r.fieldnames = [(c or "").strip() for c in (r.fieldnames or [])]
    return r.fieldnames, ({k: (v or "").strip() for k, v in row.items() if k} for row in r)


def actualizar_gtfs(con, carpeta, forzar=False):
    try:
        data, st, ms, h = descargar(GTFS_ZIP, timeout=300, intentos=3)
    except Exception as e:  # noqa: BLE001
        log.error("GTFS no descargado: %s", e)
        return False
    sha = hashlib.sha1(data).hexdigest()
    ya = con.execute("SELECT 1 FROM gtfs_versiones WHERE sha1=? AND activa=1", (sha,)).fetchone()
    if ya and not forzar:
        log.info("GTFS sin cambios (sha1 %s)", sha[:10])
        return False

    os.makedirs(os.path.join(carpeta, "gtfs"), exist_ok=True)
    ruta_zip = os.path.join(carpeta, "gtfs", f"fomento_transit_{utc_now():%Y%m%d}_{sha[:10]}.zip")
    escribir_atomico(ruta_zip, data, binario=True)
    z = zipfile.ZipFile(io.BytesIO(data))
    log.info("GTFS nuevo (%d KB, %s). Cargando en SQLite...", len(data) // 1024, sha[:10])

    resumen = {"archivos": sorted(z.namelist()), "columnas": {}}
    tablas = ["gtfs_stops", "gtfs_routes", "gtfs_trips", "gtfs_stop_times",
              "gtfs_calendar", "gtfs_calendar_dates", "gtfs_shapes"]
    for t in tablas:
        con.execute(f"DELETE FROM {t}")

    def extra(row, base):
        e = {k: v for k, v in row.items() if k not in base and v}
        return json.dumps(e, ensure_ascii=False) if e else None

    # stops
    cols, filas = _leer_csv(z, "stops.txt")
    resumen["columnas"]["stops"] = cols
    base = ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "location_type",
            "parent_station", "platform_code", "wheelchair_boarding"]
    if filas:
        con.executemany("INSERT OR REPLACE INTO gtfs_stops VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ((txt(r.get("stop_id")), txt(r.get("stop_code")), txt(r.get("stop_name")),
                          a_float(r.get("stop_lat")), a_float(r.get("stop_lon")), txt(r.get("location_type")),
                          txt(r.get("parent_station")), txt(r.get("platform_code")),
                          txt(r.get("wheelchair_boarding")), extra(r, base)) for r in filas))
    # routes
    cols, filas = _leer_csv(z, "routes.txt")
    resumen["columnas"]["routes"] = cols
    base = ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type",
            "route_color", "route_text_color"]
    if filas:
        con.executemany("INSERT OR REPLACE INTO gtfs_routes VALUES (?,?,?,?,?,?,?,?)",
                        ((txt(r.get("route_id")), txt(r.get("agency_id")), txt(r.get("route_short_name")),
                          txt(r.get("route_long_name")), txt(r.get("route_type")), txt(r.get("route_color")),
                          txt(r.get("route_text_color")), extra(r, base)) for r in filas))
    # trips
    cols, filas = _leer_csv(z, "trips.txt")
    resumen["columnas"]["trips"] = cols
    base = ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id", "shape_id",
            "wheelchair_accessible"]
    if filas:
        con.executemany("INSERT OR REPLACE INTO gtfs_trips VALUES (?,?,?,?,?,?,?,?)",
                        ((txt(r.get("trip_id")), txt(r.get("route_id")), txt(r.get("service_id")),
                          txt(r.get("trip_headsign")), txt(r.get("direction_id")), txt(r.get("shape_id")),
                          txt(r.get("wheelchair_accessible")), extra(r, base)) for r in filas))
    # stop_times (grande: por lotes)
    cols, filas = _leer_csv(z, "stop_times.txt")
    resumen["columnas"]["stop_times"] = cols
    if filas:
        lote = []
        for r in filas:
            lote.append((txt(r.get("trip_id")), txt(r.get("arrival_time")), txt(r.get("departure_time")),
                         txt(r.get("stop_id")), a_int(r.get("stop_sequence")), txt(r.get("pickup_type")),
                         txt(r.get("drop_off_type"))))
            if len(lote) >= 50000:
                con.executemany("INSERT INTO gtfs_stop_times VALUES (?,?,?,?,?,?,?)", lote)
                lote = []
        if lote:
            con.executemany("INSERT INTO gtfs_stop_times VALUES (?,?,?,?,?,?,?)", lote)
    # calendar
    cols, filas = _leer_csv(z, "calendar.txt")
    resumen["columnas"]["calendar"] = cols
    if filas:
        dias = ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday", "start_date", "end_date"]
        con.executemany("INSERT OR REPLACE INTO gtfs_calendar VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tuple(txt(r.get(c)) for c in dias) for r in filas))
    cols, filas = _leer_csv(z, "calendar_dates.txt")
    resumen["columnas"]["calendar_dates"] = cols
    if filas:
        con.executemany("INSERT INTO gtfs_calendar_dates VALUES (?,?,?)",
                        ((txt(r.get("service_id")), txt(r.get("date")), txt(r.get("exception_type"))) for r in filas))
    cols, filas = _leer_csv(z, "shapes.txt")
    resumen["columnas"]["shapes"] = cols
    if filas:
        con.executemany("INSERT INTO gtfs_shapes VALUES (?,?,?,?)",
                        ((txt(r.get("shape_id")), a_float(r.get("shape_pt_lat")), a_float(r.get("shape_pt_lon")),
                          a_int(r.get("shape_pt_sequence"))) for r in filas))

    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    resumen.update({
        "stops": q("SELECT COUNT(*) FROM gtfs_stops"),
        "stops_con_coord": q("SELECT COUNT(*) FROM gtfs_stops WHERE stop_lat IS NOT NULL AND stop_lon IS NOT NULL"),
        "stops_con_platform_code": q("SELECT COUNT(*) FROM gtfs_stops WHERE platform_code IS NOT NULL"),
        "routes": q("SELECT COUNT(*) FROM gtfs_routes"),
        "trips": q("SELECT COUNT(*) FROM gtfs_trips"),
        "trips_con_shape": q("SELECT COUNT(*) FROM gtfs_trips WHERE shape_id IS NOT NULL"),
        "stop_times": q("SELECT COUNT(*) FROM gtfs_stop_times"),
        "stop_times_con_salida": q("SELECT COUNT(*) FROM gtfs_stop_times WHERE departure_time IS NOT NULL"),
        "calendar": q("SELECT COUNT(*) FROM gtfs_calendar"),
        "calendar_dates": q("SELECT COUNT(*) FROM gtfs_calendar_dates"),
        "shape_puntos": q("SELECT COUNT(*) FROM gtfs_shapes"),
        "calendario_min": q("SELECT MIN(start_date) FROM gtfs_calendar"),
        "calendario_max": q("SELECT MAX(end_date) FROM gtfs_calendar"),
        "ruta_zip": ruta_zip,
    })
    con.execute("UPDATE gtfs_versiones SET activa=0")
    con.execute("INSERT OR REPLACE INTO gtfs_versiones VALUES (?,?,?,?,?,?,1)",
                (sha, utc_now().isoformat(), len(data), h.get("Last-Modified"),
                 json.dumps(resumen["archivos"]), json.dumps(resumen, ensure_ascii=False)))
    con.commit()

    # GeoJSON de estaciones para el mapa
    feats = []
    for sid, nom, lat, lon, lt, par, plat in con.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station, platform_code "
            "FROM gtfs_stops WHERE stop_lat IS NOT NULL"):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"stop_id": sid, "nombre": nom, "location_type": lt,
                                     "parent_station": par, "platform_code": plat}})
    escribir_atomico(os.path.join(carpeta, "salida", "estaciones.geojson"),
                     json.dumps({"type": "FeatureCollection", "features": feats,
                                 "fuente": GTFS_ZIP, "gtfs_sha1": sha}, ensure_ascii=False))
    log.info("GTFS cargado: %d paradas, %d viajes, %d stop_times, shapes=%d puntos",
             resumen["stops"], resumen["trips"], resumen["stop_times"], resumen["shape_puntos"])
    return True


def mapa_trip_route(con):
    return dict(con.execute("SELECT trip_id, route_id FROM gtfs_trips"))


# =====================================================================
# 3. Tiempo real
# =====================================================================
class Recolector:
    def __init__(self, con, carpeta, guardar_raw=True):
        self.con = con
        self.carpeta = carpeta
        self.guardar_raw = guardar_raw
        self.ult_sha = {}
        self.trip2route = mapa_trip_route(con)
        self.ultimo = {}  # feed -> json último válido
        self.ultimo_ok = {}  # feed -> captura_utc
        self.pb_disponible = self._probar_pb()

    @staticmethod
    def _probar_pb():
        try:
            from google.transit import gtfs_realtime_pb2  # noqa: F401
            from google.protobuf.json_format import MessageToDict  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def refrescar_gtfs(self):
        self.trip2route = mapa_trip_route(self.con)

    def _guardar_raw(self, feed, data, ahora):
        if not self.guardar_raw:
            return None
        d = os.path.join(self.carpeta, "raw", ahora.strftime("%Y-%m-%d"))
        os.makedirs(d, exist_ok=True)
        ruta = os.path.join(d, f"{ahora:%H%M%S}_{feed}.gz")
        with gzip.open(ruta, "wb", compresslevel=6) as f:
            f.write(data)
        return ruta

    def capturar(self, feed, url):
        ahora = utc_now()
        ah_iso, ah_ep = ahora.isoformat(), int(ahora.timestamp())
        try:
            data, st, ms, _ = descargar(url, timeout=30, intentos=3)
        except Exception as e:  # noqa: BLE001
            self.con.execute("INSERT INTO capturas(feed,captura_utc,captura_epoch,error) VALUES (?,?,?,?)",
                             (feed, ah_iso, ah_ep, f"{type(e).__name__}: {e}"))
            log.warning("%s ERROR %s", feed, e)
            return None
        sha = hashlib.sha1(data).hexdigest()
        repetida = int(self.ult_sha.get(feed) == sha)
        self.ult_sha[feed] = sha

        if feed.endswith(".pb"):
            ruta = self._guardar_raw(feed.replace(".", "_"), data, ahora)
            n, cab = None, None
            if self.pb_disponible:
                from google.transit import gtfs_realtime_pb2
                from google.protobuf.json_format import MessageToDict
                m = gtfs_realtime_pb2.FeedMessage()
                m.ParseFromString(data)
                js = MessageToDict(m)
                n, cab = len(js.get("entity", [])), a_int(g(js.get("header", {}), "timestamp"))
                registrar_campos(self.con, feed, rutas_claves(js), ah_iso)
            self.con.execute("""INSERT INTO capturas(feed,captura_utc,captura_epoch,cabecera_ts,http_status,
                                latencia_ms,bytes,entidades,sha1,repetida,ruta_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                             (feed, ah_iso, ah_ep, cab, st, ms, len(data), n, sha, repetida, ruta))
            return None

        try:
            js = json.loads(data.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self.con.execute("INSERT INTO capturas(feed,captura_utc,captura_epoch,http_status,bytes,error) "
                             "VALUES (?,?,?,?,?,?)", (feed, ah_iso, ah_ep, st, len(data), f"JSON inválido: {e}"))
            return None

        ruta = self._guardar_raw(feed, data, ahora)
        cab = a_int(g(js.get("header", {}), "timestamp"))
        ents = js.get("entity", []) or []
        cur = self.con.execute("""INSERT INTO capturas(feed,captura_utc,captura_epoch,cabecera_ts,http_status,
                                  latencia_ms,bytes,entidades,sha1,repetida,ruta_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                               (feed, ah_iso, ah_ep, cab, st, ms, len(data), len(ents), sha, repetida, ruta))
        cid = cur.lastrowid
        registrar_campos(self.con, feed, rutas_claves(js), ah_iso)
        if feed == "vehicle_positions":
            self._posiciones(cid, ah_iso, ah_ep, cab, ents)
        elif feed == "trip_updates":
            self._actualizaciones(cid, ah_iso, cab, ents)
        elif feed == "alerts":
            self._avisos(ah_iso, ents)
        self.ultimo[feed] = js
        self.ultimo_ok[feed] = ah_iso
        return len(ents)

    def _posiciones(self, cid, ah_iso, ah_ep, cab, ents):
        filas = []
        for e in ents:
            v = g(e, "vehicle")
            if not isinstance(v, dict):
                continue
            pos = g(v, "position") or {}
            trip = g(v, "trip") or {}
            veh = g(v, "vehicle") or {}
            tid = txt(g(trip, "tripId"))
            ts = a_int(g(v, "timestamp"))
            ref = cab or ah_ep
            filas.append((
                cid, ah_iso, cab, txt(e.get("id")), txt(g(veh, "id")), txt(g(veh, "label")), tid,
                txt(g(trip, "routeId")), self.trip2route.get(tid), txt(g(trip, "startDate")),
                txt(g(trip, "startTime")), txt(g(trip, "scheduleRelationship")),
                a_float(g(pos, "latitude")), a_float(g(pos, "longitude")), a_float(g(pos, "bearing")),
                a_float(g(pos, "speed")), a_float(g(pos, "odometer")), txt(g(v, "currentStatus")),
                txt(g(v, "stopId")), a_int(g(v, "currentStopSequence")), ts,
                (ref - ts) if ts else None, txt(g(v, "occupancyStatus")), a_float(g(v, "occupancyPercentage")),
                txt(g(v, "congestionLevel")), txt(g(veh, "wheelchairAccessible")),
            ))
        self.con.executemany(f"INSERT INTO obs_posicion VALUES ({','.join('?' * 26)})", filas)

    def _actualizaciones(self, cid, ah_iso, cab, ents):
        filas = []
        for e in ents:
            tu = g(e, "tripUpdate")
            if not isinstance(tu, dict):
                continue
            trip = g(tu, "trip") or {}
            tid = txt(g(trip, "tripId"))
            comun = (cid, ah_iso, cab, txt(e.get("id")), tid, txt(g(trip, "routeId")),
                     self.trip2route.get(tid), txt(g(trip, "startDate")), txt(g(trip, "startTime")),
                     txt(g(trip, "scheduleRelationship")) or "SCHEDULED",
                     txt(g(g(tu, "vehicle") or {}, "id")), a_int(g(tu, "timestamp")), a_int(g(tu, "delay")))
            stus = g(tu, "stopTimeUpdate") or []
            if not stus:
                filas.append(comun + (None,) * 9 + (0,))
            for s in stus:
                arr, dep = g(s, "arrival") or {}, g(s, "departure") or {}
                filas.append(comun + (
                    txt(g(s, "stopId")), a_int(g(s, "stopSequence")), txt(g(s, "scheduleRelationship")),
                    a_int(g(arr, "time")), a_int(g(arr, "delay")), a_int(g(arr, "uncertainty")),
                    a_int(g(dep, "time")), a_int(g(dep, "delay")), a_int(g(dep, "uncertainty")),
                    len(stus)))
        self.con.executemany(f"INSERT INTO obs_actualizacion VALUES ({','.join('?' * 23)})", filas)

    def _avisos(self, ah_iso, ents):
        def texto(t):
            tr = (g(t, "translation") or []) if isinstance(t, dict) else []
            es = [x for x in tr if (g(x, "language") or "").lower().startswith("es")] or tr
            return txt(g(es[0], "text")) if es else None

        for e in ents:
            a = g(e, "alert")
            if not isinstance(a, dict):
                continue
            sha = hashlib.sha1(json.dumps(a, sort_keys=True).encode()).hexdigest()
            ies = g(a, "informedEntity") or []
            per = (g(a, "activePeriod") or [{}])[0] or {}
            self.con.execute(
                """INSERT INTO avisos VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(aviso_id, sha1) DO UPDATE SET veces = veces + 1,
                   ultima_captura_utc = excluded.ultima_captura_utc""",
                (txt(e.get("id")), sha, ah_iso, ah_iso, txt(g(a, "cause")), txt(g(a, "effect")),
                 a_int(g(per, "start")), a_int(g(per, "end")), texto(g(a, "headerText")),
                 texto(g(a, "descriptionText")), texto(g(a, "url")), json.dumps(ies, ensure_ascii=False),
                 sum(1 for i in ies if g(g(i, "trip") or {}, "tripId")),
                 sum(1 for i in ies if g(i, "routeId")), sum(1 for i in ies if g(i, "stopId"))))

    # ---------------- estado para el mapa
    def escribir_estado(self, bbox=None):
        vp = self.ultimo.get("vehicle_positions") or {}
        tu = self.ultimo.get("trip_updates") or {}
        al = self.ultimo.get("alerts") or {}
        cab_vp = a_int(g(vp.get("header", {}), "timestamp"))
        stops = {r[0]: r[1:] for r in self.con.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM gtfs_stops")}
        rutas = {r[0]: r[1:] for r in self.con.execute(
            "SELECT route_id, route_short_name, route_long_name, route_color FROM gtfs_routes")}
        heads = dict(self.con.execute("SELECT trip_id, trip_headsign FROM gtfs_trips"))

        upd = {}
        for e in tu.get("entity", []) or []:
            t = g(e, "tripUpdate") or {}
            tid = txt(g(g(t, "trip") or {}, "tripId"))
            if not tid:
                continue
            s = (g(t, "stopTimeUpdate") or [{}])[0] or {}
            arr = g(s, "arrival") or {}
            dep = g(s, "departure") or {}
            d = a_int(g(t, "delay"))
            upd[tid] = {
                "relacion_viaje": txt(g(g(t, "trip") or {}, "scheduleRelationship")) or "SCHEDULED",
                "retraso_s": d if d is not None else a_int(g(arr, "delay")),
                "proxima_parada_id": txt(g(s, "stopId")),
                "llegada_publicada_utc": iso(a_int(g(arr, "time"))),
                "salida_publicada_utc": iso(a_int(g(dep, "time"))),  # null si Renfe no la publica
            }

        trenes = []
        for e in vp.get("entity", []) or []:
            v = g(e, "vehicle") or {}
            pos = g(v, "position") or {}
            lat, lon = a_float(g(pos, "latitude")), a_float(g(pos, "longitude"))
            if lat is None or lon is None:
                continue
            if bbox and not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                continue
            tid = txt(g(g(v, "trip") or {}, "tripId"))
            rid = self.trip2route.get(tid)
            sid = txt(g(v, "stopId"))
            ts = a_int(g(v, "timestamp"))
            r = rutas.get(rid, (None, None, None))
            st = stops.get(sid, (None, None, None))
            u = upd.get(tid, {})
            trenes.append({
                "vehiculo_id": txt(g(g(v, "vehicle") or {}, "id")) or txt(e.get("id")),
                "trip_id": tid, "route_id": rid, "linea": r[0], "linea_nombre": r[1],
                "color": ("#" + r[2]) if r[2] else None, "destino": heads.get(tid),
                "lat": lat, "lon": lon, "estado": txt(g(v, "currentStatus")),
                "parada_id": sid, "parada_nombre": st[0],
                "posicion_utc": iso(ts), "antiguedad_s": (cab_vp - ts) if (cab_vp and ts) else None,
                "retraso_s": u.get("retraso_s"), "relacion_viaje": u.get("relacion_viaje"),
                "proxima_parada_id": u.get("proxima_parada_id"),
                "proxima_parada_nombre": stops.get(u.get("proxima_parada_id"), (None,))[0],
                "llegada_publicada_utc": u.get("llegada_publicada_utc"),
                "salida_publicada_utc": u.get("salida_publicada_utc"),
                "trip_en_gtfs": tid in heads,
            })
        cancelados = [{"trip_id": k, "linea": rutas.get(self.trip2route.get(k), (None,))[0],
                       "destino": heads.get(k)} for k, x in upd.items()
                      if x["relacion_viaje"] in ("CANCELED", "CANCELLED")]
        avisos = []
        for e in al.get("entity", []) or []:
            a = g(e, "alert") or {}
            tr = (g(g(a, "headerText") or {}, "translation") or [{}])
            dtr = (g(g(a, "descriptionText") or {}, "translation") or [{}])
            avisos.append({"id": txt(e.get("id")), "causa": txt(g(a, "cause")), "efecto": txt(g(a, "effect")),
                           "cabecera": txt(g(tr[0], "text")), "descripcion": txt(g(dtr[0], "text")),
                           "rutas": sorted({txt(g(i, "routeId")) for i in (g(a, "informedEntity") or []) if g(i, "routeId")}),
                           "paradas": sorted({txt(g(i, "stopId")) for i in (g(a, "informedEntity") or []) if g(i, "stopId")})})
        estado = {
            "modo": "DIRECTO",
            "generado_utc": utc_now().isoformat(),
            "cabecera_posiciones_utc": iso(cab_vp),
            "cabecera_actualizaciones_utc": iso(a_int(g(tu.get("header", {}), "timestamp"))),
            "ultima_captura_ok": self.ultimo_ok,
            "nota": "Datos publicados por Renfe (GTFS-RT). Retraso y llegada son los publicados, no medidos. "
                    "Campos nulos = no publicados. Tren ausente no implica cancelación.",
            "fuentes": list(FEEDS_JSON.values()) + [GTFS_ZIP],
            "n_trenes": len(trenes), "trenes": trenes,
            "cancelados": cancelados, "avisos": avisos,
        }
        escribir_atomico(os.path.join(self.carpeta, "salida", "estado_actual.json"),
                         json.dumps(estado, ensure_ascii=False))


# =====================================================================
# 5. Informe de cobertura (se calcula desde SQLite: sobrevive reinicios)
# =====================================================================
def informe(con, carpeta, pb_disponible):
    q1 = lambda s, p=(): con.execute(s, p).fetchone()  # noqa: E731
    qa = lambda s, p=(): con.execute(s, p).fetchall()  # noqa: E731
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "n/d"  # noqa: E731
    V = []

    def add(p, est, ev):
        V.append({"punto": p, "estado": est, "evidencia": ev})

    capt = {r[0]: r[1:] for r in qa("""SELECT feed, COUNT(*), SUM(error IS NOT NULL), SUM(repetida),
            MIN(captura_utc), MAX(captura_utc), AVG(latencia_ms), MAX(latencia_ms) FROM capturas GROUP BY feed""")}

    # 1. Llegadas / retrasos / cancelaciones
    n, ndel, narr, ndep, nstop, ntrip = q1("""SELECT COUNT(*),
        SUM(COALESCE(retraso_viaje_s, llegada_retraso_s, salida_retraso_s) IS NOT NULL),
        SUM(llegada_epoch IS NOT NULL), SUM(salida_epoch IS NOT NULL), SUM(stop_id IS NOT NULL),
        COUNT(DISTINCT trip_id) FROM obs_actualizacion""")
    rel = dict(qa("SELECT relacion_viaje, COUNT(DISTINCT trip_id) FROM obs_actualizacion GROUP BY 1"))
    relp = dict(qa("SELECT relacion_parada, COUNT(*) FROM obs_actualizacion WHERE stop_id IS NOT NULL GROUP BY 1"))
    paradas_media = q1("SELECT AVG(n_paradas_en_update) FROM obs_actualizacion")[0]
    add("Llegadas, retrasos y cancelaciones",
        "SÍ" if ndel else ("SIN DATOS" if not n else "PARCIAL"),
        f"{n or 0} filas, {ntrip or 0} viajes distintos. Con retraso {pct(ndel or 0, n)}; con llegada "
        f"{pct(narr or 0, n)}; con SALIDA {pct(ndep or 0, n)}; media de paradas por actualización "
        f"{(paradas_media or 0):.2f}. Viajes por relación {rel}. Relación de parada {relp} "
        f"(SKIPPED = parada omitida).")

    # 2. Posiciones
    r = q1("""SELECT COUNT(*), SUM(lat IS NOT NULL), COUNT(DISTINCT vehiculo_id), SUM(trip_id IS NOT NULL),
        SUM(ts_individual IS NOT NULL), SUM(speed IS NOT NULL), SUM(bearing IS NOT NULL),
        SUM(stop_id IS NOT NULL), SUM(estado IS NOT NULL) FROM obs_posicion""")
    edad = [x[0] for x in qa("SELECT antiguedad_s FROM obs_posicion WHERE antiguedad_s IS NOT NULL "
                             "ORDER BY rowid DESC LIMIT 20000")]
    edad.sort()
    add("Posiciones de trenes", "SÍ" if r[1] else "SIN DATOS",
        f"{r[0]} posiciones de {r[2]} vehículos. Coordenadas {pct(r[1] or 0, r[0])}, tripId {pct(r[3] or 0, r[0])}, "
        f"timestamp {pct(r[4] or 0, r[0])}, estado {pct(r[8] or 0, r[0])}, parada {pct(r[7] or 0, r[0])}, "
        f"velocidad {pct(r[5] or 0, r[0])}, rumbo {pct(r[6] or 0, r[0])}. "
        + (f"Antigüedad p50={edad[len(edad)//2]} s, p90={edad[int(len(edad)*0.9)]} s." if edad else ""))

    # 3. Historial
    nc = q1("SELECT COUNT(*), MIN(captura_utc), MAX(captura_utc) FROM capturas WHERE feed='vehicle_positions' AND error IS NULL")
    mov = q1("""SELECT COUNT(*) FROM (SELECT vehiculo_id FROM obs_posicion WHERE vehiculo_id IS NOT NULL
        GROUP BY vehiculo_id HAVING COUNT(DISTINCT ROUND(lat,4)||','||ROUND(lon,4)) > 1)""")[0]
    vehs = q1("SELECT COUNT(DISTINCT vehiculo_id) FROM obs_posicion")[0]
    cambia = q1("""SELECT COUNT(*) FROM (SELECT trip_id FROM obs_actualizacion WHERE retraso_viaje_s IS NOT NULL
        GROUP BY trip_id HAVING COUNT(DISTINCT retraso_viaje_s) > 1)""")[0]
    huecos = qa("""SELECT captura_epoch - LAG(captura_epoch) OVER (ORDER BY captura_epoch) FROM capturas
        WHERE feed='vehicle_positions' AND error IS NULL""")
    huecos = [h[0] for h in huecos if h[0] is not None]
    add("Historial temporal", "SÍ" if (nc[0] or 0) >= 2 and mov else "ACUMULANDO",
        f"{nc[0]} capturas válidas de posiciones entre {nc[1]} y {nc[2]}. Vehículos con >1 posición distinta "
        f"{mov}/{vehs}; viajes con retraso cambiante {cambia}. Hueco máximo entre capturas "
        f"{max(huecos) if huecos else 'n/d'} s. El historial lo construye este recolector (Renfe no lo ofrece).")

    # 4. Estaciones
    gv = q1("SELECT resumen_json FROM gtfs_versiones WHERE activa=1")
    gres = json.loads(gv[0]) if gv else {}
    rt_stops = q1("""SELECT COUNT(*), SUM(s.stop_id IS NOT NULL) FROM (
        SELECT DISTINCT stop_id FROM obs_actualizacion WHERE stop_id IS NOT NULL
        UNION SELECT DISTINCT stop_id FROM obs_posicion WHERE stop_id IS NOT NULL) x
        LEFT JOIN gtfs_stops s ON s.stop_id = x.stop_id""")
    add("Nombres y coordenadas de estaciones",
        "SÍ" if gres.get("stops_con_coord") and rt_stops[1] else ("NO VERIFICADO" if not gres else "PARCIAL"),
        f"GTFS: {gres.get('stops')} paradas, {gres.get('stops_con_coord')} con coordenadas. stop_id del tiempo "
        f"real presentes en GTFS: {rt_stops[1] or 0}/{rt_stops[0]} ({pct(rt_stops[1] or 0, rt_stops[0])}).")

    # 5. Recorrido y horario
    rt_trips = q1("""SELECT COUNT(*), SUM(t.trip_id IS NOT NULL) FROM (
        SELECT DISTINCT trip_id FROM obs_actualizacion WHERE trip_id IS NOT NULL
        UNION SELECT DISTINCT trip_id FROM obs_posicion WHERE trip_id IS NOT NULL) x
        LEFT JOIN gtfs_trips t ON t.trip_id = x.trip_id""")
    hoy = datetime.now().strftime("%Y%m%d")
    vigente = gres.get("calendario_min") and gres["calendario_min"] <= hoy <= (gres.get("calendario_max") or "")
    # ¿la parada publicada en directo pertenece al recorrido programado de ese viaje?
    coh = q1("""SELECT COUNT(*), SUM(EXISTS(SELECT 1 FROM gtfs_stop_times st WHERE st.trip_id=a.trip_id
        AND st.stop_id=a.stop_id)) FROM (SELECT DISTINCT trip_id, stop_id FROM obs_actualizacion
        WHERE stop_id IS NOT NULL ORDER BY rowid DESC LIMIT 3000) a""")
    add("Recorrido y horario de cada viaje",
        "SÍ" if rt_trips[0] and (rt_trips[1] or 0) / rt_trips[0] > 0.8 else ("NO VERIFICADO" if not gres else "PARCIAL"),
        f"trip_id del directo en GTFS: {rt_trips[1] or 0}/{rt_trips[0]} ({pct(rt_trips[1] or 0, rt_trips[0])}). "
        f"Paradas del directo que pertenecen al horario programado del viaje: {coh[1] or 0}/{coh[0]} "
        f"({pct(coh[1] or 0, coh[0])}). stop_times {gres.get('stop_times')}, shapes {gres.get('shape_puntos')} "
        f"puntos ({'dibujar recorrido real' if gres.get('shape_puntos') else 'recorrido esquemático'}). "
        f"Calendario {gres.get('calendario_min')}–{gres.get('calendario_max')} "
        f"({'VIGENTE hoy' if vigente else 'revisar calendar_dates / vigencia'}).")

    # 6–9: campos especiales en todos los feeds (JSON y .pb)
    campos = qa("SELECT feed, ruta, veces FROM campos_vistos")
    hits = {k: [f"{f}:{r} ({n})" for f, r, n in campos if rx.search(r.split(".")[-1].replace("[]", ""))]
            for k, rx in KW.items()}
    add("Vías, andenes, señalización y enclavamientos",
        "PARCIAL (andén en GTFS)" if gres.get("stops_con_platform_code") else ("REVISAR" if hits["vias"] else "NO"),
        f"Campos en feeds: {hits['vias'] or 'ninguno'}. GTFS platform_code: {gres.get('stops_con_platform_code')}. "
        f"Señalización y enclavamientos no existen en GTFS ni GTFS-RT.")
    oc = q1("SELECT SUM(ocupacion_estado IS NOT NULL), SUM(ocupacion_pct IS NOT NULL), SUM(congestion IS NOT NULL) FROM obs_posicion")
    add("Ocupación de pasajeros", "SÍ" if any(oc) else "NO",
        f"occupancyStatus {oc[0] or 0}, occupancyPercentage {oc[1] or 0}, congestionLevel {oc[2] or 0} "
        f"filas con valor. Campos relacionados: {hits['ocupacion'] or 'ninguno'}.")
    add("Consumo eléctrico", "REVISAR" if hits["energia"] else "NO",
        f"Campos relacionados: {hits['energia'] or 'ninguno'} (GTFS-RT no define energía).")
    av = q1("""SELECT COUNT(DISTINCT aviso_id), SUM(causa IS NOT NULL AND causa NOT IN ('UNKNOWN_CAUSE')),
        SUM(efecto IS NOT NULL), SUM(n_trip>0), SUM(n_route>0), SUM(n_stop>0) FROM avisos""")
    causas = dict(qa("SELECT COALESCE(causa,'(ausente)'), COUNT(*) FROM avisos GROUP BY 1"))
    add("Causas de los retrasos",
        "PARCIAL (avisos con causa)" if av[1] else ("SOLO TEXTO DE AVISOS" if av[0] else "NO"),
        f"{av[0]} avisos distintos. Causas {causas}. Con efecto {av[2] or 0}; ligados a viaje {av[3] or 0}, "
        f"a línea {av[4] or 0}, a parada {av[5] or 0}. Un aviso de línea no prueba la causa del retraso de un tren.")

    # Comparación JSON vs .pb
    comp = {}
    for base in FEEDS_JSON:
        js = {re.sub(r"_([a-z])", lambda m: m.group(1).upper(), r) for f, r, _ in campos if f == base}
        pb = {re.sub(r"_([a-z])", lambda m: m.group(1).upper(), r) for f, r, _ in campos if f == base + ".pb"}
        if pb:
            comp[base] = {"solo_en_pb": sorted(pb - js), "solo_en_json": sorted(js - pb)}

    # Enlaces
    enl = qa("""SELECT nombre, url, tipo, http_status, ROUND(latencia_ms), error, recursos_json, comprobado_utc
        FROM enlaces WHERE comprobado_utc = (SELECT MAX(comprobado_utc) FROM enlaces)""")

    # ---- Markdown
    md = ["# Enlace · Informe de cobertura de datos", "",
          f"Generado: {utc_now().isoformat(timespec='seconds')}", ""]
    md += ["## Capturas", "", "| Feed | Capturas | Errores | Repetidas | Desde | Hasta | Latencia media/máx ms |",
           "|---|---|---|---|---|---|---|"]
    for f, (c, e, rep, a, b, lm, lx) in capt.items():
        md.append(f"| {f} | {c} | {e or 0} | {rep or 0} | {a} | {b} | {round(lm or 0)} / {round(lx or 0)} |")
    md += ["", "## Veredicto por punto", "", "| Información | Estado | Evidencia |", "|---|---|---|"]
    md += [f"| {v['punto']} | **{v['estado']}** | {v['evidencia'].replace('|', '/')} |" for v in V]
    md += ["", "## Enlaces del plan", "", "| Enlace | Tipo | HTTP | ms | Qué aporta |", "|---|---|---|---|---|"]
    aporta = {"datos_rt": "Datos en tiempo real (se recolectan)", "datos_gtfs": "GTFS estático (se carga en SQLite)",
              "ficha_ckan": "Ficha: lista de recursos", "documentacion": "Documentación (no aporta datos)"}
    for nom, url, tipo, st, ms, err, recs, _ in enl:
        extra = aporta[tipo]
        if recs:
            extra += ": " + ", ".join(f"{x['formato']} {x['url']}" for x in json.loads(recs))
        md.append(f"| [{nom}]({url}) | {tipo} | {st or 'ERROR'} | {ms or '-'} | {extra}{(' · ' + err) if err else ''} |")
    md += ["", "## JSON frente a protobuf (.pb)", ""]
    if comp:
        for f, d in comp.items():
            md.append(f"- {f}: solo en .pb {d['solo_en_pb'] or 'nada'}; solo en JSON {d['solo_en_json'] or 'nada'}")
    else:
        md.append("- Sin comparación: instala `gtfs-realtime-bindings` para decodificar los .pb."
                  if not pb_disponible else "- Aún no se han decodificado capturas .pb.")
    md += ["", "## Todos los campos vistos", ""]
    for f, rr, nn in sorted(campos):
        md.append(f"- `{f}` · `{rr}` · {nn}")

    escribir_atomico(os.path.join(carpeta, "salida", "informe_cobertura.md"), "\n".join(md))
    escribir_atomico(os.path.join(carpeta, "salida", "informe_cobertura.json"),
                     json.dumps({"generado_utc": utc_now().isoformat(), "veredictos": V, "gtfs": gres,
                                 "json_vs_pb": comp,
                                 "enlaces": [dict(zip(["nombre", "url", "tipo", "http", "ms", "error"], e[:6])) for e in enl]},
                                ensure_ascii=False, indent=2, default=str))
    log.info("Informe actualizado: %s", " | ".join(f"{v['punto'].split(',')[0]}={v['estado']}" for v in V))


# =====================================================================
# Bucle principal
# =====================================================================
def _senal(signum, _frame):
    global PARAR
    PARAR = True
    log.info("Señal %s recibida: terminando tras el ciclo actual.", signum)


def main():
    ap = argparse.ArgumentParser(description="Recolector continuo de datos Renfe para Enlace")
    ap.add_argument("--datos", default="enlace_datos", help="carpeta de trabajo")
    ap.add_argument("--intervalo", type=float, default=30, help="segundos entre ciclos (mín. 15)")
    ap.add_argument("--horas", type=float, default=0, help="duración total; 0 = indefinido")
    ap.add_argument("--cada-pb", type=int, default=20, help="descargar .pb cada N ciclos (0 = nunca)")
    ap.add_argument("--minutos-informe", type=float, default=10)
    ap.add_argument("--horas-gtfs", type=float, default=12)
    ap.add_argument("--horas-catalogo", type=float, default=6)
    ap.add_argument("--sin-raw", action="store_true", help="no guardar JSON originales")
    ap.add_argument("--bbox", help="filtro SOLO para estado_actual.json: lat_min,lon_min,lat_max,lon_max")
    args = ap.parse_args()
    intervalo = max(15.0, args.intervalo)
    bbox = [float(x) for x in args.bbox.split(",")] if args.bbox else None

    carpeta = os.path.abspath(args.datos)
    for sub in ("", "salida", "raw", "gtfs"):
        os.makedirs(os.path.join(carpeta, sub), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(carpeta, "recolector.log"), encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    signal.signal(signal.SIGINT, _senal)
    signal.signal(signal.SIGTERM, _senal)

    con = abrir_db(os.path.join(carpeta, "enlace.db"))
    log.info("Inicio. Carpeta %s. Intervalo %ss.", carpeta, intervalo)

    comprobar_enlaces(con, carpeta)
    actualizar_gtfs(con, carpeta)
    rec = Recolector(con, carpeta, guardar_raw=not args.sin_raw)
    if not rec.pb_disponible:
        log.info("gtfs-realtime-bindings no instalado: los .pb se guardan pero no se comparan.")

    t_ini = time.time()
    t_inf = t_gtfs = t_cat = t_ini
    ciclo = 0
    while not PARAR:
        t0 = time.time()
        ciclo += 1
        res = {f: rec.capturar(f, u) for f, u in FEEDS_JSON.items()}
        if args.cada_pb and ciclo % args.cada_pb == 1:
            for f, u in FEEDS_PB.items():
                rec.capturar(f, u)
        con.commit()
        try:
            rec.escribir_estado(bbox)
        except Exception as e:  # noqa: BLE001
            log.exception("estado_actual.json no generado: %s", e)
        log.info("Ciclo %d · %s", ciclo, " ".join(f"{k}={v if v is not None else 'ERROR'}" for k, v in res.items()))

        ahora = time.time()
        if ahora - t_gtfs >= args.horas_gtfs * 3600:
            if actualizar_gtfs(con, carpeta):
                rec.refrescar_gtfs()
            t_gtfs = ahora
        if ahora - t_cat >= args.horas_catalogo * 3600:
            comprobar_enlaces(con, carpeta)
            t_cat = ahora
        if ciclo == 2 or ahora - t_inf >= args.minutos_informe * 60:
            try:
                informe(con, carpeta, rec.pb_disponible)
            except Exception as e:  # noqa: BLE001
                log.exception("Informe fallido: %s", e)
            t_inf = ahora
        if args.horas and ahora - t_ini >= args.horas * 3600:
            break
        while not PARAR and time.time() - t0 < intervalo:
            time.sleep(0.5)

    informe(con, carpeta, rec.pb_disponible)
    con.commit()
    con.close()
    log.info("Fin limpio.")


if __name__ == "__main__":
    main()
