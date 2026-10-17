import argparse
import gzip
import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
# Este es el código que va recopilando los datos de la renfe

FEEDS = {
    "vehicle_positions":
        "https://gtfsrt.renfe.com/vehicle_positions.json",
    "trip_updates":
        "https://gtfsrt.renfe.com/trip_updates.json",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def integer(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def setup_database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            feed TEXT NOT NULL,
            captured_at_utc TEXT NOT NULL,
            feed_timestamp INTEGER,
            entity_count INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            UNIQUE(feed, sha256)
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY,
            attempted_at_utc TEXT NOT NULL,
            feed TEXT NOT NULL,
            http_status INTEGER,
            elapsed_ms INTEGER,
            snapshot_id INTEGER,
            outcome TEXT NOT NULL,
            error TEXT,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
        );

        CREATE TABLE IF NOT EXISTS vehicles (
            snapshot_id INTEGER NOT NULL,
            entity_id TEXT,
            trip_id TEXT,
            start_date TEXT,
            start_time TEXT,
            route_id TEXT,
            vehicle_id TEXT,
            vehicle_label TEXT,
            stop_id TEXT,
            stop_sequence INTEGER,
            latitude REAL,
            longitude REAL,
            current_status TEXT,
            vehicle_timestamp INTEGER,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
        );

        CREATE TABLE IF NOT EXISTS trip_updates (
            snapshot_id INTEGER NOT NULL,
            entity_id TEXT,
            trip_id TEXT,
            start_date TEXT,
            start_time TEXT,
            route_id TEXT,
            schedule_relationship TEXT,
            update_timestamp INTEGER,
            reported_delay_seconds INTEGER,
            vehicle_id TEXT,
            stop_updates_json TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
        );

        CREATE TABLE IF NOT EXISTS stop_updates (
            snapshot_id INTEGER NOT NULL,
            entity_id TEXT,
            trip_id TEXT,
            start_date TEXT,
            stop_id TEXT,
            stop_sequence INTEGER,
            schedule_relationship TEXT,
            arrival_time INTEGER,
            arrival_delay_seconds INTEGER,
            arrival_uncertainty INTEGER,
            departure_time INTEGER,
            departure_delay_seconds INTEGER,
            departure_uncertainty INTEGER,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
        );

        CREATE INDEX IF NOT EXISTS idx_vehicles_trip
            ON vehicles(trip_id, start_date);

        CREATE INDEX IF NOT EXISTS idx_updates_trip
            ON trip_updates(trip_id, start_date);

        CREATE INDEX IF NOT EXISTS idx_stops_trip
            ON stop_updates(trip_id, start_date, stop_id);
    """)
    db.commit()
    return db


def normalize_vehicles(db, snapshot_id, entities):
    rows = []

    for entity in entities:
        vehicle = entity.get("vehicle")
        if not isinstance(vehicle, dict):
            continue

        trip = vehicle.get("trip") or {}
        position = vehicle.get("position") or {}
        descriptor = vehicle.get("vehicle") or {}

        rows.append((
            snapshot_id,
            entity.get("id"),
            trip.get("tripId"),
            trip.get("startDate"),
            trip.get("startTime"),
            trip.get("routeId"),
            descriptor.get("id"),
            descriptor.get("label"),
            vehicle.get("stopId"),
            integer(vehicle.get("currentStopSequence")),
            position.get("latitude"),
            position.get("longitude"),
            vehicle.get("currentStatus"),
            integer(vehicle.get("timestamp")),
        ))

    db.executemany(
        "INSERT INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def normalize_trip_updates(db, snapshot_id, entities):
    trips = []
    stops = []

    for entity in entities:
        update = entity.get("tripUpdate")
        if not isinstance(update, dict):
            continue

        trip = update.get("trip") or {}
        vehicle = update.get("vehicle") or {}
        stop_updates = update.get("stopTimeUpdate") or []

        trips.append((
            snapshot_id,
            entity.get("id"),
            trip.get("tripId"),
            trip.get("startDate"),
            trip.get("startTime"),
            trip.get("routeId"),
            trip.get("scheduleRelationship"),
            integer(update.get("timestamp")),
            integer(update.get("delay")),
            vehicle.get("id"),
            json_text(stop_updates),
        ))

        for stop in stop_updates:
            arrival = stop.get("arrival") or {}
            departure = stop.get("departure") or {}

            stops.append((
                snapshot_id,
                entity.get("id"),
                trip.get("tripId"),
                trip.get("startDate"),
                stop.get("stopId"),
                integer(stop.get("stopSequence")),
                stop.get("scheduleRelationship"),
                integer(arrival.get("time")),
                integer(arrival.get("delay")),
                integer(arrival.get("uncertainty")),
                integer(departure.get("time")),
                integer(departure.get("delay")),
                integer(departure.get("uncertainty")),
            ))

    db.executemany(
        "INSERT INTO trip_updates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        trips,
    )
    db.executemany(
        "INSERT INTO stop_updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        stops,
    )


def save_snapshot(db, root, feed, payload, captured_at):
    canonical = json_text(payload).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()

    existing = db.execute(
        "SELECT id FROM snapshots WHERE feed=? AND sha256=?",
        (feed, digest),
    ).fetchone()

    if existing:
        return existing[0], "unchanged"

    entities = payload["entity"]
    feed_timestamp = integer(payload["header"].get("timestamp"))

    day = captured_at[:10]
    relative = Path("raw") / feed / day / f"{digest}.json.gz"
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_suffix(".tmp")
    with gzip.open(temporary, "wb") as handle:
        handle.write(canonical)
    temporary.replace(destination)

    with db:
        cursor = db.execute("""
            INSERT INTO snapshots (
                feed, captured_at_utc, feed_timestamp,
                entity_count, sha256, raw_path
            ) VALUES (?,?,?,?,?,?)
        """, (
            feed,
            captured_at,
            feed_timestamp,
            len(entities),
            digest,
            relative.as_posix(),
        ))
        snapshot_id = cursor.lastrowid

        if feed == "vehicle_positions":
            normalize_vehicles(db, snapshot_id, entities)
        elif feed == "trip_updates":
            normalize_trip_updates(db, snapshot_id, entities)

    return snapshot_id, "saved"


def collect_feed(session, db, root, feed, url):
    attempted_at = utc_now()
    started = time.monotonic()
    http_status = None
    snapshot_id = None
    error = None

    try:
        response = session.get(url, timeout=(10, 30))
        http_status = response.status_code
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("La respuesta no es un objeto JSON")

        if not isinstance(payload.get("header"), dict):
            raise ValueError("Falta una cabecera válida")

        if not isinstance(payload.get("entity"), list):
            raise ValueError("Falta una lista entity válida")

        captured_at = utc_now()
        snapshot_id, outcome = save_snapshot(
            db, root, feed, payload, captured_at
        )

        timestamp = integer(payload["header"].get("timestamp"))
        age = (
            round(time.time() - timestamp)
            if timestamp is not None else None
        )

        logging.info(
            "%s | %s | registros=%s | antigüedad_feed_s=%s",
            feed, outcome, len(payload["entity"]), age,
        )

        if age is not None and (age > 300 or age < -60):
            logging.warning(
                "%s: revisar frescura o reloj; antigüedad=%s s",
                feed, age,
            )

    except (requests.RequestException, ValueError) as exc:
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"
        logging.warning("%s | %s", feed, error)

    except Exception as exc:
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"
        logging.exception("%s | Error al guardar o normalizar", feed)

    elapsed_ms = round((time.monotonic() - started) * 1000)

    with db:
        db.execute("""
            INSERT INTO fetch_log (
                attempted_at_utc, feed, http_status, elapsed_ms,
                snapshot_id, outcome, error
            ) VALUES (?,?,?,?,?,?,?)
        """, (
            attempted_at,
            feed,
            http_status,
            elapsed_ms,
            snapshot_id,
            outcome,
            error,
        ))


def main():
    parser = argparse.ArgumentParser(
        description="Recopilador de posiciones y actualizaciones de Renfe"
    )
    parser.add_argument("--out", default="datos_renfe")
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument(
        "--hours", type=float, default=0,
        help="Duración; 0 significa hasta pulsar Ctrl+C",
    )
    args = parser.parse_args()

    if args.interval < 20:
        parser.error("Usa un intervalo de al menos 20 segundos")
    if args.hours < 0:
        parser.error("--hours no puede ser negativo")

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(
                root / "collector.log", encoding="utf-8"
            ),
            logging.StreamHandler(),
        ],
    )

    db = setup_database(root / "renfe.sqlite")
    session = requests.Session()
    session.headers["User-Agent"] = "EnlaceResearchCollector/1.0"

    manifest = root / "sources.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps({
                "created_at_utc": utc_now(),
                "feeds": FEEDS,
                "notes": [
                    "Datos sin filtrar por núcleo.",
                    "Los timestamps publicados se conservan.",
                    "Ausencia de campo no equivale a cero.",
                    "Desaparición de un tren no equivale a cancelación.",
                ],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    deadline = (
        time.monotonic() + args.hours * 3600
        if args.hours else None
    )

    logging.info("Inicio de captura | carpeta=%s", root.resolve())

    try:
        while deadline is None or time.monotonic() < deadline:
            cycle_started = time.monotonic()

            for feed, url in FEEDS.items():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                collect_feed(session, db, root, feed, url)

            wait = max(
                0, args.interval - (time.monotonic() - cycle_started)
            )

            if deadline is not None:
                wait = min(
                    wait, max(0, deadline - time.monotonic())
                )

            time.sleep(wait)

    except KeyboardInterrupt:
        logging.info("Parada solicitada con Ctrl+C")

    finally:
        session.close()
        db.close()
        logging.info("Captura finalizada")


if __name__ == "__main__":
    main()