import re

def numeros(texto):
    return {float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", texto)}

def numeros_json(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, dict):
        for v in obj.values(): numeros_json(v, out)
    elif isinstance(obj, list):
        for v in obj: numeros_json(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(float(obj))
    elif isinstance(obj, str):
        out |= numeros(obj)
    return out

def informe_valido(texto, datos):
    inventados = numeros(texto) - numeros_json(datos)
    return len(inventados) == 0, inventados

def plantilla_respaldo(d):
    return (f"[Plantilla] En la línea {d['linea']}, {d['viajes_retraso_5min']} de "
            f"{d['viajes_observados']} viajes observados en los últimos {d['ventana_min']} minutos "
            f"superan 5 minutos de retraso publicado y {d['viajes_empeorados_1min']} han empeorado. "
            f"Avisos activos en la línea: {d['n_avisos']}. "
            f"Los datos disponibles no permiten atribuir una causa.")
