import json, folium

def color_tren(t):
    if t["antiguedad_s"] is None or t["antiguedad_s"] > 120:
        return "gray", "Dato antiguo"
    if t["relacion_viaje"] == "CANCELED":
        return "red", "Cancelado"
    d = t["retraso_s"]
    if d is None:
        return "gray", "Sin retraso publicado"
    if d >= 600:
        return "red", f"Retraso {d // 60} min"
    if d >= 180:
        return "orange", f"Retraso {d // 60} min"
    return "green", f"Retraso {d // 60} min"

def construir_mapa(estado, estaciones, centro=(40.4168, -3.7038), zoom=10):
    m = folium.Map(location=centro, zoom_start=zoom, tiles="OpenStreetMap")
    m.get_root().header.add_child(folium.Element(
        "<style>.leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.85)}</style>"))
    for f in estaciones["features"]:
        lon, lat = f["geometry"]["coordinates"]
        folium.CircleMarker([lat, lon], radius=2, color="#888", fill=True,
                            tooltip=f["properties"]["nombre"]).add_to(m)
    for t in estado["trenes"]:
        color, etiqueta = color_tren(t)
        folium.CircleMarker(
            [t["lat"], t["lon"]], radius=6, color=color, fill=True, fill_opacity=0.9,
            tooltip=f'{t["linea"]} · {etiqueta}',
            popup=(f'{t["linea"]} → {t["destino"] or "?"}<br>{etiqueta}<br>'
                   f'Próxima: {t["proxima_parada_nombre"]}<br>Dato de hace {t["antiguedad_s"]} s'),
        ).add_to(m)
    return m