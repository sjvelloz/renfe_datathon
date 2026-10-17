"""Diagnostico RailOps. Ejecutar: streamlit run diagnostico_railops.py"""
import sys
import json
import platform
import sqlite3
import traceback
import importlib
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from datetime import datetime, timezone
import streamlit as st

st.set_page_config(page_title="Diagnóstico RailOps", layout="wide")
st.title("Diagnóstico RailOps · IBM / local")
st.caption("No modifica la base de datos ni ejecuta el recolector o Granite. Las tablas interactivas son pruebas opcionales.")
st.button("Repetir comprobaciones")
BASE = Path(__file__).resolve().parent
DB = BASE / "enlace_datos" / "enlace.db"
report = {"fecha_utc": datetime.now(timezone.utc).isoformat(), "entorno": {}, "pruebas": {}}

def show(name, value):
    report["pruebas"][name] = value
    st.subheader(name)
    st.code(json.dumps(value, ensure_ascii=False, indent=2, default=str), language="json")

def failure(name):
    text = traceback.format_exc()
    report["pruebas"][name] = {"error": text}
    st.error(name + ": error")
    st.code(text)

def markdown_table(rows):
    if not rows:
        st.info("Sin filas en esta ventana.")
        return
    columns = list(rows[0])
    def cell(v):
        return str(v if v is not None else "—").replace("|", "\|").replace("\n", " ")
    text = ["| " + " | ".join(map(cell, columns)) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    text += ["| " + " | ".join(cell(row.get(c)) for c in columns) + " |" for row in rows]
    st.markdown("\n".join(text))

packages = {}
for name in ("streamlit", "pyarrow", "numpy", "pandas", "folium", "streamlit-folium", "protobuf"):
    try:
        packages[name] = version(name)
    except PackageNotFoundError:
        packages[name] = "no instalado"
report["entorno"] = {
    "python": sys.version, "ejecutable": sys.executable,
    "arquitectura": platform.machine(), "orden_bytes": sys.byteorder,
    "sistema": platform.platform(), "carpeta_script": str(BASE),
    "carpeta_ejecucion": str(Path.cwd()), "paquetes": packages,
}
st.header("1. Entorno")
st.code(json.dumps(report["entorno"], ensure_ascii=False, indent=2), language="json")
if sys.byteorder == "big":
    st.warning("Este Python usa big-endian. Es una pista para investigar la compatibilidad de Arrow, no una confirmación del fallo.")

st.header("2. Archivos y frescura")
files = {}
for path in (DB, BASE / "enlace_datos/salida/estado_actual.json", BASE / "enlace_datos/salida/estaciones.geojson"):
    files[str(path.relative_to(BASE))] = {"existe": path.exists(), "bytes": path.stat().st_size if path.exists() else None}
show("Archivos", files)
try:
    state = json.loads((BASE / "enlace_datos/salida/estado_actual.json").read_text(encoding="utf-8"))
    times = {k: state.get(k) for k in ("modo", "generado_utc", "cabecera_posiciones_utc", "cabecera_actualizaciones_utc")}
    times["numero_trenes"] = len(state.get("trenes", []))
    show("Estado del recolector", times)
except Exception:
    failure("Estado del recolector")

st.header("3. SQLite y lógica: sin tabla Arrow")
rows = []
if not DB.exists():
    st.error("No se encuentra la base de datos. Coloca este archivo junto a app.py, dentro del proyecto.")
else:
    con = None
    try:
        con = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        show("SQLite", {"version": sqlite3.sqlite_version, "apertura": "solo lectura", "query_only": con.execute("PRAGMA query_only").fetchone()[0]})
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        show("Tablas disponibles", tables)
        if "obs_actualizacion" in tables:
            show("Rango de capturas", dict(con.execute("SELECT MIN(captura_utc) AS primera, MAX(captura_utc) AS ultima FROM obs_actualizacion").fetchone()))
        logic = importlib.import_module("railops_logica")
        rows = logic.zonas_por_linea(con, 15)
        show("Resultado real de zonas_por_linea (primeras 10)", rows[:10])
        show("Tipos Python de la primera fila", {k: type(v).__name__ for k, v in rows[0].items()} if rows else {})
        st.subheader("Mismos datos en Markdown, sin Arrow")
        markdown_table(rows[:10])
    except Exception:
        failure("Consulta de métricas")
    finally:
        if con is not None:
            con.close()

st.header("4. Prueba aislada con valores conocidos")
st.write("Los valores correctos son C7 / 3 / 1 y C5 / 12 / 2. Esta prueba no depende de Renfe ni de SQLite.")
sample = [{"linea": "C7", "viajes": 3, "con_retraso_5min": 1}, {"linea": "C5", "viajes": 12, "con_retraso_5min": 2}]
show("Datos de control", sample)
try:
    import pyarrow as pa
    table = pa.Table.from_pylist(sample)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    restored = pa.ipc.open_stream(sink.getvalue()).read_all().to_pylist()
    show("Ida y vuelta Arrow en servidor", {"coincide": restored == sample, "datos": restored, "schema": str(table.schema)})
except Exception:
    failure("Ida y vuelta Arrow en servidor")

if st.checkbox("Mostrar tablas interactivas para reproducir el fallo", value=False):
    st.warning("Un error que ocurra en el navegador puede no aparecer en el informe descargado. Guarda una captura si la tabla falla.")
    try:
        st.subheader("Control: números conocidos")
        st.dataframe(sample, use_container_width=True)
        if rows:
            st.subheader("Datos de la aplicación")
            st.dataframe(rows[:10], use_container_width=True)
    except Exception:
        failure("Tablas interactivas")

st.header("5. Cómo interpretar el resultado")
st.markdown("""- Si JSON y Markdown son correctos pero la tabla interactiva no, el fallo está en la ruta de visualización/serialización, no en la consulta SQL.
- Si también falla la tabla de control, se reproduce sin datos de Renfe.
- Si falla la ida y vuelta de Arrow en el servidor, revisa el error registrado y las versiones.
- Si Arrow funciona en el servidor pero la tabla falla, todavía puede haber un problema entre la serialización y el cliente. El test del servidor no lo descarta.
- Si el JSON ya contiene cifras incorrectas, hay que investigar SQLite y la lógica antes de culpar a la tabla.
- Si no hay filas, comprueba la fecha de la última captura: la consulta utiliza una ventana de 15 minutos.

No se aplican parches automáticos ni cambios de dependencias: primero se recoge evidencia.
""")
st.text_area("Observaciones: ¿qué aparece en las tablas interactivas?", key="observaciones", help="Estas observaciones se incluirán en el informe.")
report["observaciones"] = st.session_state.get("observaciones", "")
st.download_button("Descargar informe de diagnóstico", data=json.dumps(report, ensure_ascii=False, indent=2, default=str), file_name="informe_railops.json", mime="application/json")
st.caption("El informe incluye rutas locales y versiones. Revísalo antes de compartirlo; no incluye credenciales ni el contenido completo de la base de datos.")

