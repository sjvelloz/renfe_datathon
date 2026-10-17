import json, sqlite3

DB = "enlace_datos/enlace.db"
UMBRAL_AMARILLO = 180   # 3 min
UMBRAL_ROJO = 600       # 10 min
DATO_VIEJO_S = 120

def conectar(db=DB):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con

def desde(minutos):
    return f"strftime('%Y-%m-%dT%H:%M:%S','now','-{int(minutos)} minutes')"

def zonas_por_linea(con, minutos=15):
    sql = f"""
    WITH ult AS (
      SELECT trip_id, route_id_gtfs, retraso_viaje_s AS d, relacion_viaje, MAX(captura_id)
      FROM obs_actualizacion WHERE captura_utc >= {desde(minutos)}
      GROUP BY trip_id)
    SELECT r.route_id, r.route_short_name AS linea, COUNT(*) AS viajes,
           SUM(d >= 300) AS con_retraso_5min,
           SUM(relacion_viaje = 'CANCELED') AS cancelados,
           ROUND(AVG(d)/60.0, 1) AS retraso_medio_min,
           MAX(d) AS retraso_max_s
    FROM ult JOIN gtfs_routes r ON r.route_id = ult.route_id_gtfs
    GROUP BY r.route_id ORDER BY con_retraso_5min DESC"""
    return [dict(x) for x in con.execute(sql)]

def zonas_por_estacion(con, minutos=15):
    sql = f"""
    WITH ult AS (
      SELECT trip_id, stop_id, retraso_viaje_s AS d, relacion_parada, MAX(captura_id)
      FROM obs_actualizacion
      WHERE captura_utc >= {desde(minutos)} AND stop_id IS NOT NULL
      GROUP BY trip_id, stop_id)
    SELECT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, COUNT(*) AS viajes,
           SUM(d >= 300) AS con_retraso_5min,
           SUM(COALESCE(relacion_parada,'') = 'SKIPPED') AS paradas_omitidas
    FROM ult JOIN gtfs_stops s ON s.stop_id = ult.stop_id
    GROUP BY s.stop_id ORDER BY con_retraso_5min DESC"""
    return [dict(x) for x in con.execute(sql)]

def evolucion_viaje(con, trip_id):
    sql = """SELECT captura_utc, retraso_viaje_s, stop_id, relacion_viaje
             FROM obs_actualizacion WHERE trip_id = ? ORDER BY captura_id"""
    return [dict(x) for x in con.execute(sql, (trip_id,))]

def empeorados(con, route_id, minutos=15):
    sql = f"""
    SELECT trip_id, MIN(retraso_viaje_s) AS d_min, MAX(retraso_viaje_s) AS d_max,
      (SELECT a2.retraso_viaje_s FROM obs_actualizacion a2 WHERE a2.trip_id=a.trip_id
         AND a2.captura_utc >= {desde(minutos)} ORDER BY a2.captura_id ASC LIMIT 1) AS d_ini,
      (SELECT a2.retraso_viaje_s FROM obs_actualizacion a2 WHERE a2.trip_id=a.trip_id
         ORDER BY a2.captura_id DESC LIMIT 1) AS d_fin
    FROM obs_actualizacion a
    WHERE route_id_gtfs = ? AND captura_utc >= {desde(minutos)}
    GROUP BY trip_id"""
    filas = [dict(x) for x in con.execute(sql, (route_id,))]
    return [f for f in filas if f["d_ini"] is not None and f["d_fin"] is not None
            and f["d_fin"] - f["d_ini"] >= 60]

def avisos_de_linea(con, route_id, minutos=15):
    sql = f"""SELECT aviso_id, cabecera, descripcion, entidades_json FROM avisos
              WHERE ultima_captura_utc >= {desde(minutos)}"""
    out = []
    for a in con.execute(sql):
        ents = json.loads(a["entidades_json"] or "[]")
        if any(e.get("routeId") == route_id for e in ents):
            out.append({"id": a["aviso_id"], "texto": a["descripcion"] or a["cabecera"]})
    return out
