import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from streamlit_folium import st_folium

import railops_logica as L
from railops_granite import informe
from railops_mapa import construir_mapa


BASE = Path(__file__).resolve().parent
DATOS = BASE / "enlace_datos"
SALIDA = DATOS / "salida"

st.set_page_config(
    page_title="RailOps",
    layout="wide",
)

st.title("RailOps · Centro de observación de Cercanías")


def leer_json(ruta):
    with ruta.open(encoding="utf-8") as archivo:
        return json.load(archivo)


def leer_fecha_utc(valor):
    if not valor:
        return None

    try:
        fecha = datetime.fromisoformat(
            str(valor).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None

    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)

    return fecha.astimezone(timezone.utc)


def edad_segundos(valor, ahora):
    fecha = leer_fecha_utc(valor)

    if fecha is None:
        return None

    return (ahora - fecha).total_seconds()


def texto_edad(segundos):
    if segundos is None:
        return "desconocida"

    if segundos < 0:
        return "fecha futura: revisar relojes"

    segundos = int(segundos)
    minutos, resto = divmod(segundos, 60)

    return f"{minutos} min {resto} s"


# Actualización manual: no hay bucles ni st.rerun().
st.button("Actualizar datos", key="actualizar_datos")


try:
    estado = leer_json(SALIDA / "estado_actual.json")
    estaciones = leer_json(SALIDA / "estaciones.geojson")
except (OSError, ValueError) as error:
    st.error(f"No se pueden leer los archivos de estado: {error}")
    st.info(
        "Comprueba que el recolector está funcionando y que "
        "ha generado los archivos de salida."
    )
    st.stop()


ahora = datetime.now(timezone.utc)

edad_archivo = edad_segundos(
    estado.get("generado_utc"),
    ahora,
)

edad_posiciones = edad_segundos(
    estado.get("cabecera_posiciones_utc"),
    ahora,
)

edad_actualizaciones = edad_segundos(
    estado.get("cabecera_actualizaciones_utc"),
    ahora,
)


st.caption(
    f"Modo declarado: {estado.get('modo', 'desconocido')} · "
    f"Pantalla leída a las {ahora:%H:%M:%S} UTC"
)

col1, col2, col3 = st.columns(3)

col1.metric(
    "Archivo generado hace",
    texto_edad(edad_archivo),
)

col2.metric(
    "Publicación de posiciones hace",
    texto_edad(edad_posiciones),
)

col3.metric(
    "Publicación de llegadas/retrasos hace",
    texto_edad(edad_actualizaciones),
)

st.caption(
    "Las edades se recalculan al actualizar la pantalla. "
    "No son un contador automático."
)


for nombre, edad in (
    ("archivo de estado", edad_archivo),
    ("feed de posiciones", edad_posiciones),
    ("feed de actualizaciones", edad_actualizaciones),
):
    if edad is None:
        st.warning(f"No hay una fecha válida para el {nombre}.")
    elif edad < 0:
        st.warning(
            f"La fecha del {nombre} está en el futuro. "
            "Comprueba los relojes."
        )
    elif edad > 120:
        st.warning(
            f"El {nombre} tiene más de 2 minutos de antigüedad."
        )