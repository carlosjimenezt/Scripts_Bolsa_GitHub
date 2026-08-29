"""Calendario vertical de catalizadores para el cuaderno operativo V2.4.

El módulo no genera señales ni transmite órdenes. Resume acontecimientos públicos,
calcula reacciones bursátiles y conserva las variables que utiliza el panel del
notebook: ``FECHAS_RESULTADOS_GRAFICOS`` y ``DATOS_ALERTA_RESULTADOS``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import escape, unescape
import json
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import pandas as pd
import yfinance as yf
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from IPython.display import HTML, display


VERSION_ESTADO = 3
MESES_HISTORICOS = 6
MESES_PROXIMOS = 2
UMBRAL_MOVIMIENTO_PCT = 7.0

CARPETA_CALENDARIO = Path("salida_operativa") / "calendario_resultados"
ARCHIVO_ESTADO = CARPETA_CALENDARIO / "estado_fechas_v2_4.json"
ARCHIVO_HTML = CARPETA_CALENDARIO / "informe_resultados.html"
CARPETA_CALENDARIO.mkdir(parents=True, exist_ok=True)
# Evita que yfinance intente crear su base SQLite fuera del proyecto.
yf.set_tz_cache_location(str(Path(tempfile.gettempdir()) / "yf_calendario_v24_cache"))

FUENTES_IR = {
    "META": "https://investor.atmeta.com/investor-events/default.aspx",
    "SHOP": "https://www.shopify.com/investors/events",
    "NVDA": "https://investor.nvidia.com/events-and-presentations/events-and-presentations/default.aspx",
    "MSFT": "https://www.microsoft.com/en-us/investor/events/events-upcoming",
    "AAPL": "https://investor.apple.com/investor-relations/default.aspx",
    "AMZN": "https://ir.aboutamazon.com/events/",
    "GOOGL": "https://abc.xyz/investor/",
    "GOOG": "https://abc.xyz/investor/",
    "TSLA": "https://ir.tesla.com/",
    "AVGO": "https://investors.broadcom.com/events-and-presentations",
}

NOMBRES_EMPRESA = {
    "META": "Meta Platforms",
    "SHOP": "Shopify",
    "NVDA": "NVIDIA",
    "MSFT": "Microsoft",
    "AAPL": "Apple",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet Google",
    "GOOG": "Alphabet Google",
    "TSLA": "Tesla",
    "AVGO": "Broadcom",
}

CIK_SEC = {
    "META": 1326801,
    "SHOP": 1594805,
    "NVDA": 1045810,
    "MSFT": 789019,
    "AAPL": 320193,
    "AMZN": 1018724,
    "GOOGL": 1652044,
    "GOOG": 1652044,
    "TSLA": 1318605,
    "AVGO": 1730168,
}

FUENTES_EMPRESA = {
    "META": {"meta investor relations", "meta"},
    "SHOP": {"shopify"},
    "NVDA": {"nvidia investor relations", "nvidia newsroom", "nvidia"},
    "MSFT": {"microsoft investor relations", "microsoft"},
    "AAPL": {"apple"},
    "AMZN": {"amazon investor relations", "about amazon", "amazon"},
    "GOOGL": {"alphabet investor relations", "alphabet", "google"},
    "GOOG": {"alphabet investor relations", "alphabet", "google"},
    "TSLA": {"tesla investor relations", "tesla"},
    "AVGO": {"broadcom investor relations", "broadcom"},
}

MEDIOS_PRIORITARIOS = (
    "Reuters", "AP News", "CNBC", "MarketWatch", "WSJ", "Bloomberg",
    "Financial Times", "Barron's", "U.S. News & World Report", "Fortune",
    "Morningstar", "Yahoo Finance",
)
DISTRIBUIDORES_OFICIALES = {"PR Newswire", "GlobeNewswire", "Business Wire"}

PALABRAS_RESULTADOS = re.compile(
    r"earnings|financial results|quarterly results|earnings call|results conference|"
    r"reports? .{0,45}(?:quarter|results)|resultados|conference call",
    re.IGNORECASE,
)
PALABRAS_EVENTO_IR = re.compile(
    r"earnings|results|conference|investor day|shareholder|annual meeting|"
    r"presentaci[oó]n|resultados|conferencia|accionistas",
    re.IGNORECASE,
)
PALABRAS_LEGALES = re.compile(
    r"trial|hearing|court|jury|appeal|bellwether|lawsuit|litigation|"
    r"commission|regulator|antitrust|settlement|juicio|audiencia|tribunal|"
    r"jurado|apelaci[oó]n|demanda|regulador|acuerdo",
    re.IGNORECASE,
)

PATRONES_FECHA = (
    re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+20\d{2}\b",
        re.I,
    ),
    re.compile(
        r"\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}\b",
        re.I,
    ),
    re.compile(
        r"\b\d{1,2}\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|"
        r"agosto|septiembre|octubre|noviembre|diciembre)\s+de\s+20\d{2}\b",
        re.I,
    ),
)

MESES_ES = {
    "enero": "January", "febrero": "February", "marzo": "March",
    "abril": "April", "mayo": "May", "junio": "June", "julio": "July",
    "agosto": "August", "septiembre": "September", "octubre": "October",
    "noviembre": "November", "diciembre": "December",
}


def _descargar(url: str, timeout: int = 25) -> tuple[bytes, str]:
    agente = "CalendarioEventosV2.4/1.0 analisis-inversion-local"
    req = Request(url, headers={"User-Agent": agente, "Accept-Language": "es,en;q=0.8"})
    with urlopen(req, timeout=timeout) as respuesta:
        return respuesta.read(), respuesta.headers.get_content_charset() or "utf-8"


def _texto_web(url: str, timeout: int = 25) -> str:
    contenido, codificacion = _descargar(url, timeout)
    raw = contenido.decode(codificacion, errors="replace")
    raw = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", raw, flags=re.I)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def _url_feed(consulta: str) -> str:
    return "https://news.google.com/rss/search?" + urlencode(
        {"q": consulta, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )


def _leer_feed(consulta: str) -> list[dict[str, Any]]:
    contenido, _ = _descargar(_url_feed(consulta))
    raiz = ET.fromstring(contenido)
    noticias = []
    for item in raiz.findall("./channel/item"):
        nodo_fuente = item.find("source")
        fuente = (nodo_fuente.text or "").strip() if nodo_fuente is not None else ""
        titulo = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or "").strip()
        try:
            fecha_publicacion = parsedate_to_datetime(item.findtext("pubDate")).date()
        except (TypeError, ValueError, OverflowError):
            continue
        if titulo and enlace:
            noticias.append(
                {"fecha": fecha_publicacion, "fuente": fuente, "titulo": titulo, "enlace": enlace}
            )
    return sorted(noticias, key=lambda n: n["fecha"], reverse=True)


def _limpiar_titulo(titulo: str, fuente: str) -> str:
    titulo = titulo.replace("\ufffd", "'")
    sufijo = f" - {fuente}"
    return titulo[:-len(sufijo)].strip() if fuente and titulo.endswith(sufijo) else titulo.strip()


def _limpiar_contexto(texto: str, maximo: int = 360) -> str:
    texto = re.sub(r"\s+", " ", texto).strip(" -|.;:")
    if len(texto) <= maximo:
        return texto
    return texto[:maximo].rsplit(" ", 1)[0] + "…"


def _normalizar_traduccion(original: str, traducido: str) -> str:
    """Corrige giros recurrentes de la traducción automática financiera."""
    salida = traducido.strip()
    m = re.match(
        r"^Meta Stock Drops? (\d+(?:\.\d+)?)% on Steeper AI Costs, Missed Forecast[.!]?$",
        original,
        re.I,
    )
    if m:
        return (
            f"La acción de Meta cae un {m.group(1)} % por mayores costes de IA "
            "y una previsión incumplida"
        )
    patrones = (
        (r"^Meta Reports (First|Second|Third|Fourth) Quarter (20\d{2}) Results$", None),
        (r"^(.+?) Earnings Call Transcript$", r"Transcripción de la presentación de resultados de \1"),
    )
    m = re.match(patrones[0][0], original, re.I)
    if m:
        trimestre = {"first": "primer", "second": "segundo", "third": "tercer", "fourth": "cuarto"}[m.group(1).lower()]
        return f"Meta presenta los resultados del {trimestre} trimestre de {m.group(2)}"
    for patron, reemplazo in patrones[1:]:
        if re.match(patron, original, re.I):
            return re.sub(patron, reemplazo, original, flags=re.I)
    salida = re.sub(r"\bMeta Reports\b", "Meta presenta", salida, flags=re.I)
    salida = re.sub(r"\bMeta Stock\b", "la acción de Meta", salida, flags=re.I)
    salida = re.sub(r"\b(?:Las|Los) la acción\b", "La acción", salida, flags=re.I)
    salida = re.sub(
        r"\bMeta cráteres de flujo de efectivo\b",
        "El flujo de caja de Meta se desploma",
        salida,
        flags=re.I,
    )
    salida = re.sub(r"\bMeta creación de negocios\b", "Meta crea un negocio", salida, flags=re.I)
    return salida or original


def _traducir_es(texto: str, cache: dict[str, str]) -> str:
    if not texto:
        return texto
    clave = f"v3|{texto}"
    if clave in cache:
        # También se normalizan las traducciones ya almacenadas, de modo que las
        # mejoras de estilo se apliquen sin invalidar toda la caché de red.
        cache[clave] = _normalizar_traduccion(texto, cache[clave])
        return cache[clave]
    try:
        url = "https://api.mymemory.translated.net/get?" + urlencode(
            {"q": texto[:480], "langpair": "en|es"}
        )
        contenido, codificacion = _descargar(url, timeout=20)
        respuesta = json.loads(contenido.decode(codificacion, errors="replace"))
        traducido = unescape(respuesta["responseData"]["translatedText"]).strip()
        cache[clave] = _normalizar_traduccion(texto, traducido)
    except Exception:
        # Nunca se muestra como resumen un titular inglés sin identificar.
        cache[clave] = "Traducción temporalmente no disponible; consulte la fuente enlazada."
    return cache[clave]


def _a_fecha(valor: Any) -> date | None:
    try:
        marca = pd.Timestamp(valor)
        return None if pd.isna(marca) else marca.date()
    except (TypeError, ValueError, OverflowError):
        return None


def _parsear_fecha(texto: str) -> date | None:
    limpio = re.sub(r"(\d)(st|nd|rd|th)", r"\1", texto, flags=re.I)
    if " de " in limpio.lower():
        for es, en in MESES_ES.items():
            limpio = re.sub(rf"\b{es}\b", en, limpio, flags=re.I)
        limpio = re.sub(r"\bde\b", " ", limpio, flags=re.I)
    try:
        return date_parser.parse(limpio, fuzzy=True, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def _fechas_con_contexto(texto: str, desde: date, hasta: date) -> list[tuple[date, str]]:
    encontrados: list[tuple[date, str]] = []
    vistos = set()
    for patron in PATRONES_FECHA:
        for coincidencia in patron.finditer(texto):
            fecha = _parsear_fecha(coincidencia.group(0))
            if not fecha or not (desde <= fecha <= hasta):
                continue
            contexto = texto[max(0, coincidencia.start() - 260): coincidencia.end() + 320]
            clave = (fecha, _limpiar_contexto(contexto, 180))
            if clave not in vistos:
                vistos.add(clave)
                encontrados.append((fecha, contexto))
    return encontrados


def _noticia_es_comunicado(ticker: str, noticia: dict[str, Any]) -> bool:
    fuente = noticia["fuente"].strip()
    return (
        fuente.lower() in FUENTES_EMPRESA.get(ticker, set()) or fuente in DISTRIBUIDORES_OFICIALES
    ) and bool(PALABRAS_RESULTADOS.search(noticia["titulo"]))


def _seleccionar_noticias(
    ticker: str,
    noticias: list[dict[str, Any]],
    fecha_evento: date,
    es_resultado: bool,
    impacto: float | None = None,
    maximo: int = 3,
) -> list[dict[str, Any]]:
    candidatas = []
    nombre = NOMBRES_EMPRESA.get(ticker, ticker).lower().split()[0]
    for noticia in noticias:
        distancia = (noticia["fecha"] - fecha_evento).days
        if not (-1 <= distancia <= 3):
            continue
        if nombre not in noticia["titulo"].lower() and ticker.lower() not in noticia["titulo"].lower():
            continue
        titulo_minusculas = noticia["titulo"].lower()
        alcista = bool(re.search(r"\brise|rises|rose|jump|jumps|surge|surges|soar|soars|gain|gains\b", titulo_minusculas))
        bajista = bool(re.search(r"\bfall|falls|fell|drop|drops|plunge|plunges|slump|slumps|sink|sinks|tumble|tumbles\b", titulo_minusculas))
        if not es_resultado and impacto is not None:
            if impacto > 0 and bajista and not alcista:
                continue
            if impacto < 0 and alcista and not bajista:
                continue
        oficial = _noticia_es_comunicado(ticker, noticia)
        prioridad = MEDIOS_PRIORITARIOS.index(noticia["fuente"]) if noticia["fuente"] in MEDIOS_PRIORITARIOS else 99
        if oficial or noticia["fuente"] in MEDIOS_PRIORITARIOS:
            candidatas.append((0 if oficial else 1, abs(distancia), prioridad, noticia))
    candidatas.sort(key=lambda x: x[:3])
    elegidas, fuentes_vistas = [], set()
    for _, _, _, noticia in candidatas:
        etiqueta = "Comunicado" if _noticia_es_comunicado(ticker, noticia) else noticia["fuente"]
        if etiqueta in fuentes_vistas:
            continue
        elegidas.append(noticia)
        fuentes_vistas.add(etiqueta)
        if len(elegidas) >= maximo:
            break
    return elegidas


def _resumen_noticias(
    ticker: str,
    fecha_evento: date,
    es_resultado: bool,
    cache_traducciones: dict[str, str],
    impacto: float | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    nombre = NOMBRES_EMPRESA.get(ticker, ticker)
    despues = (fecha_evento - timedelta(days=2)).isoformat()
    antes = (fecha_evento + timedelta(days=4)).isoformat()
    consultas = [f'"{nombre}" earnings OR results after:{despues} before:{antes}'] if es_resultado else [
        f'"{nombre}" stock after:{despues} before:{antes}',
        f'"{nombre}" shares after:{despues} before:{antes}',
    ]
    noticias, claves_vistas = [], set()
    for consulta in consultas:
        try:
            lote = _leer_feed(consulta)
        except Exception:
            lote = []
        for noticia in lote:
            clave_noticia = (noticia["titulo"], noticia["fuente"])
            if clave_noticia not in claves_vistas:
                claves_vistas.add(clave_noticia)
                noticias.append(noticia)
    elegidas = _seleccionar_noticias(ticker, noticias, fecha_evento, es_resultado, impacto)
    resumen = []
    if es_resultado:
        resumen.append({"etiqueta": "Evento", "texto": "Presentación de resultados trimestrales."})
    for noticia in elegidas:
        original = _limpiar_titulo(noticia["titulo"], noticia["fuente"])
        etiqueta = "Comunicado" if _noticia_es_comunicado(ticker, noticia) else noticia["fuente"]
        resumen.append({"etiqueta": etiqueta, "texto": _traducir_es(original, cache_traducciones)})
    if not resumen:
        resumen = [{"etiqueta": "Evento", "texto": "Movimiento relevante sin noticia fiable localizada automáticamente."}]
    return resumen, elegidas


def _historial_precios(objeto_yf: yf.Ticker, inicio: date, fin: date) -> pd.DataFrame:
    try:
        precios = objeto_yf.history(
            start=inicio - timedelta(days=15), end=fin + timedelta(days=2),
            auto_adjust=False, actions=False,
        )
    except Exception:
        return pd.DataFrame()
    if not isinstance(precios, pd.DataFrame) or precios.empty or "Close" not in precios:
        return pd.DataFrame()
    precios = precios.copy()
    indice = pd.DatetimeIndex(precios.index)
    if indice.tz is not None:
        indice = indice.tz_localize(None)
    precios.index = indice.normalize()
    precios["Variacion_pct"] = precios["Close"].pct_change() * 100
    return precios


def _impacto_resultado(precios: pd.DataFrame, fecha_anuncio: date) -> float | None:
    if precios.empty:
        return None
    cierres = precios["Close"].dropna()
    dia = pd.Timestamp(fecha_anuncio)
    previos, posteriores = cierres[cierres.index < dia], cierres[cierres.index > dia]
    if previos.empty or posteriores.empty:
        return None
    return (float(posteriores.iloc[0]) / float(previos.iloc[-1]) - 1.0) * 100


def _movimientos_grandes(precios: pd.DataFrame, inicio: date, fin: date) -> list[tuple[date, float]]:
    if precios.empty:
        return []
    salida = []
    for indice, valor in precios["Variacion_pct"].dropna().items():
        fecha = indice.date()
        porcentaje = float(valor)
        if inicio <= fecha <= fin and abs(porcentaje) > UMBRAL_MOVIMIENTO_PCT:
            salida.append((fecha, porcentaje))
    return salida


def _fechas_resultados_yahoo(objeto_yf: yf.Ticker, hoy: date) -> tuple[list[date], date | None]:
    pasadas, futuras = [], []
    try:
        tabla = objeto_yf.get_earnings_dates(limit=24)
        if isinstance(tabla, pd.DataFrame):
            for indice, fila in tabla.iterrows():
                fecha = _a_fecha(indice)
                if not fecha:
                    continue
                publicado = pd.notna(fila.get("Reported EPS"))
                if fecha <= hoy and publicado:
                    pasadas.append(fecha)
                elif fecha >= hoy and not publicado:
                    futuras.append(fecha)
    except Exception:
        pass
    try:
        calendario = objeto_yf.calendar
        valor = calendario.get("Earnings Date") if isinstance(calendario, dict) else None
        valores = valor if isinstance(valor, (list, tuple)) else [valor]
        futuras.extend(f for f in (_a_fecha(v) for v in valores) if f and f >= hoy)
    except Exception:
        pass
    return sorted(set(pasadas), reverse=True), min(futuras) if futuras else None


def _eventos_ir(ticker: str, hoy: date, limite: date) -> tuple[list[dict[str, Any]], list[date]]:
    url = FUENTES_IR.get(ticker)
    if not url:
        return [], []
    try:
        texto = _texto_web(url)
    except Exception:
        return [], []
    proximos, resultados_pasados = [], []
    inicio_historico = hoy - relativedelta(months=MESES_HISTORICOS)
    # En las páginas de inversores suele aparecer "fecha + nombre del evento".
    # Se corta cada bloque justo antes de la siguiente fecha para no atribuir a
    # una conferencia las palabras "Financial Results" del evento anterior.
    coincidencias = []
    for patron in PATRONES_FECHA:
        for coincidencia in patron.finditer(texto):
            fecha = _parsear_fecha(coincidencia.group(0))
            if fecha and inicio_historico <= fecha <= limite:
                coincidencias.append((coincidencia.start(), coincidencia.end(), fecha))
    coincidencias = sorted(set(coincidencias), key=lambda x: (x[0], x[1]))
    for indice, (posicion, final_fecha, fecha) in enumerate(coincidencias):
        siguiente = coincidencias[indice + 1][0] if indice + 1 < len(coincidencias) else final_fecha + 260
        contexto = texto[posicion:min(siguiente, final_fecha + 260)]
        if not PALABRAS_EVENTO_IR.search(contexto):
            continue
        es_resultado = bool(PALABRAS_RESULTADOS.search(contexto))
        if fecha <= hoy and es_resultado:
            resultados_pasados.append(fecha)
        elif fecha > hoy:
            tipo = "Presentación de resultados" if es_resultado else "Conferencia / evento para inversores"
            proximos.append({
                "fecha": fecha,
                "tipo": tipo,
                "resumen": tipo,
                "certeza": "Exacta — web oficial",
                "fuente": url,
            })
    return proximos, sorted(set(resultados_pasados), reverse=True)


def _ultimo_documento_sec(ticker: str) -> tuple[str | None, str | None]:
    cik = CIK_SEC.get(ticker)
    if not cik:
        return None, None
    try:
        url_json = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        contenido, codificacion = _descargar(url_json)
        datos = json.loads(contenido.decode(codificacion, errors="replace"))
        recientes = datos["filings"]["recent"]
        for i, formulario in enumerate(recientes["form"]):
            if formulario in {"10-Q", "10-K", "20-F", "40-F"}:
                acceso = recientes["accessionNumber"][i].replace("-", "")
                documento = recientes["primaryDocument"][i]
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acceso}/{documento}"
                return _texto_web(url, timeout=30), url
    except Exception:
        pass
    return None, None


def _eventos_sec(
    ticker: str,
    hoy: date,
    limite: date,
    cache_traducciones: dict[str, str],
) -> list[dict[str, Any]]:
    texto, url = _ultimo_documento_sec(ticker)
    if not texto or not url:
        return []
    eventos = []
    for fecha, contexto in _fechas_con_contexto(texto, hoy + timedelta(days=1), limite):
        if not PALABRAS_LEGALES.search(contexto):
            continue
        oraciones = re.split(r"(?<=[.!?])\s+", contexto)
        mes_ingles = fecha.strftime("%B").lower()
        patron_dia = re.compile(rf"\b{fecha.day}(?:st|nd|rd|th)?\b", re.I)
        candidata = next((
            o for o in oraciones
            if mes_ingles in o.lower() and patron_dia.search(o)
            and str(fecha.year) in o and PALABRAS_LEGALES.search(o)
        ), contexto)
        candidata = _limpiar_contexto(candidata)
        eventos.append({
            "fecha": fecha,
            "tipo": "Juicio / procedimiento regulatorio",
            "resumen": _traducir_es(candidata, cache_traducciones),
            "certeza": "Exacta — último informe SEC; confirmar posibles cambios",
            "fuente": url,
        })
    return eventos


def _eventos_futuros_noticias(
    ticker: str,
    hoy: date,
    limite: date,
    cache_traducciones: dict[str, str],
) -> list[dict[str, Any]]:
    nombre = NOMBRES_EMPRESA.get(ticker, ticker)
    consulta = f'"{nombre}" scheduled trial hearing investor conference event when:60d'
    try:
        noticias = _leer_feed(consulta)
    except Exception:
        return []
    eventos = []
    for noticia in noticias[:35]:
        titulo = _limpiar_titulo(noticia["titulo"], noticia["fuente"])
        for fecha, _ in _fechas_con_contexto(titulo, hoy + timedelta(days=1), limite):
            if not (PALABRAS_LEGALES.search(titulo) or PALABRAS_EVENTO_IR.search(titulo)):
                continue
            eventos.append({
                "fecha": fecha,
                "tipo": "Juicio / regulación" if PALABRAS_LEGALES.search(titulo) else "Evento corporativo",
                "resumen": _traducir_es(titulo, cache_traducciones),
                "certeza": "Fecha publicada en prensa — confirmar",
                "fuente": noticia["enlace"],
            })
    return eventos


def _estimar_proximo_resultado(fechas: list[date], hoy: date, limite: date) -> date | None:
    if not fechas:
        return None
    candidato = max(fechas) + timedelta(days=91)
    while candidato <= hoy:
        candidato += timedelta(days=91)
    return candidato if candidato <= limite else None


def _normalizar_eventos_proximos(eventos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    orden_certeza = {
        "Exacta — web oficial": 0,
        "Exacta — último informe SEC; confirmar posibles cambios": 1,
        "Fecha publicada en prensa — confirmar": 2,
        "Aproximada": 3,
    }
    salida: list[dict[str, Any]] = []
    for evento in sorted(eventos, key=lambda e: (e["fecha"], orden_certeza.get(e["certeza"], 9))):
        duplicado = next((
            existente for existente in salida
            if existente["tipo"] == evento["tipo"]
            and abs((existente["fecha"] - evento["fecha"]).days) <= 2
        ), None)
        if duplicado is None:
            salida.append(evento)
        elif orden_certeza.get(evento["certeza"], 9) < orden_certeza.get(duplicado["certeza"], 9):
            salida[salida.index(duplicado)] = evento
    return salida


def _cargar_estado() -> dict[str, Any]:
    if not ARCHIVO_ESTADO.exists():
        return {}
    try:
        datos = json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        return datos if isinstance(datos, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _fecha_html(fecha: date | None) -> str:
    return fecha.strftime("%d/%m/%Y") if fecha else "Fecha por confirmar"


def _impacto_visual(porcentaje: float | None, futuro: bool = False) -> tuple[str, str, str]:
    if futuro:
        return "Pendiente", "#5f6368", "El sentido del impacto todavía es desconocido"
    if porcentaje is None:
        return "Sin datos", "#5f6368", "No hay cotización suficiente"
    if porcentaje > 0:
        return f"↑ {porcentaje:+.2f} %", "#188038", "Subida"
    if porcentaje < 0:
        return f"↓ {porcentaje:+.2f} %", "#d93025", "Bajada"
    return f"→ {porcentaje:+.2f} %", "#5f6368", "Sin variación"


def _enlaces_noticias(noticias: list[dict[str, Any]]) -> str:
    enlaces, vistos = [], set()
    for noticia in noticias:
        etiqueta = noticia.get("fuente") or "Fuente"
        if etiqueta in vistos or not noticia.get("enlace"):
            continue
        vistos.add(etiqueta)
        enlaces.append(
            f'<a href="{escape(noticia["enlace"])}" target="_blank" rel="noopener noreferrer">'
            f'{escape(etiqueta)}</a>'
        )
    return " · ".join(enlaces) if enlaces else "—"


def _bloque_html(fila: dict[str, Any]) -> str:
    historicos_html = []
    for evento in fila["historicos"]:
        resumen = "".join(
            f'<div><b>{escape(item["etiqueta"])}:</b> {escape(item["texto"])}</div>'
            for item in evento["resumen"]
        )
        impacto, color, direccion = _impacto_visual(evento["impacto"])
        historicos_html.append(f"""
          <tr>
            <td class="fecha">{_fecha_html(evento['fecha'])}</td>
            <td><span class="tipo pasado">{escape(evento['tipo'])}</span></td>
            <td class="resumen">{resumen}</td>
            <td class="impacto" style="color:{color}" title="{direccion}">{escape(impacto)}</td>
            <td class="fuente">{_enlaces_noticias(evento['noticias'])}</td>
          </tr>""")
    if not historicos_html:
        historicos_html.append(
            '<tr><td colspan="5" class="vacio">No hubo sesiones con variación superior al 7 % '
            'ni resultados localizados durante esta ventana.</td></tr>'
        )

    proximos_html = []
    for evento in fila["proximos"]:
        proximos_html.append(f"""
          <tr>
            <td class="fecha">{_fecha_html(evento['fecha'])}</td>
            <td><span class="tipo futuro">{escape(evento['tipo'])}</span></td>
            <td class="resumen">{escape(evento['resumen'])}</td>
            <td><span class="certeza">{escape(evento['certeza'])}</span></td>
            <td class="fuente"><a href="{escape(evento['fuente'])}" target="_blank"
              rel="noopener noreferrer">Fuente</a></td>
          </tr>""")
    if not proximos_html:
        proximos_html.append(
            '<tr><td colspan="5" class="vacio">No se localizaron eventos importantes publicados '
            'para los próximos dos meses.</td></tr>'
        )

    aviso = " | ".join(fila["avisos"])
    return f"""
      <section class="bloque" title="{escape(aviso)}">
        <h3>{escape(fila['ticker'])}</h3>
        <h4>Próximos eventos importantes — dos meses</h4>
        <div class="tabla-wrap"><table>
          <thead><tr><th>Fecha prevista</th><th>Tipo</th><th>Evento próximo</th>
          <th>Precisión</th><th>Fuente</th></tr></thead>
          <tbody>{''.join(proximos_html)}</tbody>
        </table></div>
        <h4 class="proximos-titulo">Eventos pasados — últimos seis meses</h4>
        <div class="tabla-wrap"><table>
          <thead><tr><th>Fecha del evento</th><th>Tipo</th><th>Resumen del evento</th>
          <th>Impacto bursátil</th><th>Fuentes</th></tr></thead>
          <tbody>{''.join(historicos_html)}</tbody>
        </table></div>
      </section>"""


def _informe_html(filas: list[dict[str, Any]], hoy: date) -> str:
    bloques = "".join(_bloque_html(fila) for fila in filas)
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calendario dinámico de eventos V2.4</title><style>
 body {{ margin:0;color:#202124;background:#fff;font-family:Arial,sans-serif }}
 .calendario {{ max-width:1500px;margin:0 auto;padding:10px }}
 h2 {{ font-size:18px;margin:0 0 4px }}
 .leyenda,.nota {{ color:#5f6368;font-size:11px;margin:4px 0 10px }}
 .bloque {{ border:1px solid #d8dee4;border-radius:9px;padding:10px;margin:12px 0 18px;
            box-shadow:0 1px 2px rgba(0,0,0,.05) }}
 h3 {{ font-size:18px;margin:0 0 8px;color:#174ea6 }}
 h4 {{ font-size:12px;margin:5px 0;color:#3c4043 }} .proximos-titulo {{ margin-top:12px }}
 .tabla-wrap {{ overflow-x:auto;border:1px solid #e3e7eb;border-radius:6px }}
 table {{ width:100%;border-collapse:collapse;font-size:11px;line-height:1.3 }}
 th {{ background:#f6f8fa;color:#3c4043;text-align:left;padding:7px;
       border-bottom:1px solid #cfd4da;white-space:nowrap }}
 td {{ padding:7px;border-bottom:1px solid #eceff1;vertical-align:top }}
 tbody tr:last-child td {{ border-bottom:0 }} tbody tr:hover {{ background:#f8fbff }}
 .fecha {{ width:94px;white-space:nowrap;font-weight:600 }}
 .resumen {{ min-width:360px }} .resumen div+div {{ margin-top:4px }}
 .impacto {{ width:100px;white-space:nowrap;font-size:13px;font-weight:800;text-align:center }}
 .fuente {{ min-width:120px }} .fuente a {{ white-space:nowrap }}
 .tipo,.certeza {{ display:inline-block;padding:2px 7px;border-radius:10px;
                   white-space:nowrap;font-size:10px;font-weight:700 }}
 .pasado {{ color:#3c4043;background:#eef1f4 }} .futuro {{ color:#174ea6;background:#e8f0fe }}
 .certeza {{ color:#8a4b00;background:#fef7e0;white-space:normal }}
 .vacio {{ color:#6b7280;font-style:italic }} a {{ color:#1967d2;text-decoration:none }}
 a:hover {{ text-decoration:underline }}
</style></head><body><div class="calendario">
 <h2>Calendario dinámico de eventos — {hoy:%d/%m/%Y}</h2>
 <p class="leyenda">Cada valor tiene su propio bloque. El histórico incluye todas las presentaciones
 de resultados y las sesiones cuya variación cierre a cierre superó el 7 %. Los eventos futuros
 pueden tener fecha exacta o aproximada.</p>
{bloques}
 <p class="nota">La reacción de resultados compara el cierre anterior con el primer cierre posterior
 al anuncio; los demás impactos son variaciones de cierre a cierre. Una fecha futura no anticipa
 la dirección del movimiento. Verifique siempre la fuente original.</p>
</div></body></html>"""


def generar_calendario_eventos(
    activos: list[str] | tuple[str, ...],
    eventos_manuales: dict[str, list[dict[str, Any]]] | None = None,
    mostrar: bool = True,
) -> dict[str, Any]:
    """Genera el informe y devuelve las estructuras consumidas por los gráficos.

    ``eventos_manuales`` es opcional. Cada entrada admite ``fecha``, ``tipo``,
    ``resumen``, ``certeza`` y ``fuente`` y resulta útil para calendarios locales
    de tribunales que no exponen datos estructurados.
    """
    hoy = date.today()
    inicio = hoy - relativedelta(months=MESES_HISTORICOS)
    limite = hoy + relativedelta(months=MESES_PROXIMOS)
    estado_anterior = _cargar_estado()
    filas, estado_nuevo = [], {"_version": VERSION_ESTADO}
    manuales = eventos_manuales or {}

    tickers = list(dict.fromkeys(str(t).strip().upper() for t in activos if str(t).strip()))
    for ticker in tickers:
        objeto_yf = yf.Ticker(ticker)
        previa = estado_anterior.get(ticker, {}) if isinstance(estado_anterior.get(ticker), dict) else {}
        traducciones = dict(previa.get("traducciones", {}))
        avisos: list[str] = []

        precios = _historial_precios(objeto_yf, inicio, hoy)
        if precios.empty:
            avisos.append("No se pudo descargar el historial de precios")

        fechas_yf, proxima_yf = _fechas_resultados_yahoo(objeto_yf, hoy)
        proximos_ir, fechas_ir = _eventos_ir(ticker, hoy, limite)
        fechas_resultados = sorted({
            f for f in [*fechas_yf, *fechas_ir] if inicio <= f <= hoy
        }, reverse=True)

        # Migra una sola vez las fechas verificadas del antiguo esquema V2.4.
        # En el esquema actual se recalculan para que una caché obsoleta no
        # convierta conferencias ordinarias en presentaciones de resultados.
        if estado_anterior.get("_version", 0) < VERSION_ESTADO:
            for resultado_cache in previa.get("resultados", []):
                fecha_cache = _a_fecha(resultado_cache.get("fecha")) if isinstance(resultado_cache, dict) else None
                if fecha_cache and inicio <= fecha_cache <= hoy:
                    fechas_resultados.append(fecha_cache)
        fechas_resultados = sorted(set(fechas_resultados), reverse=True)

        grandes = _movimientos_grandes(precios, inicio, hoy)
        movimientos_consumidos: set[date] = set()
        historicos: list[dict[str, Any]] = []

        for fecha_resultado in fechas_resultados:
            movimiento_asociado = min(
                (
                    (fecha_mov, pct) for fecha_mov, pct in grandes
                    if 0 <= (fecha_mov - fecha_resultado).days <= 3
                ),
                key=lambda par: (par[0] - fecha_resultado).days,
                default=None,
            )
            if movimiento_asociado:
                movimientos_consumidos.add(movimiento_asociado[0])
                impacto = movimiento_asociado[1]
            else:
                impacto = _impacto_resultado(precios, fecha_resultado)
            resumen, noticias = _resumen_noticias(
                ticker, fecha_resultado, True, traducciones, impacto
            )
            historicos.append({
                "fecha": fecha_resultado,
                "tipo": "Presentación de resultados",
                "resumen": resumen,
                "impacto": impacto,
                "noticias": noticias,
            })

        for fecha_movimiento, impacto in grandes:
            if fecha_movimiento in movimientos_consumidos:
                continue
            resumen, noticias = _resumen_noticias(
                ticker, fecha_movimiento, False, traducciones, impacto
            )
            historicos.append({
                "fecha": fecha_movimiento,
                "tipo": "Movimiento diario superior al 7 %",
                "resumen": resumen,
                "impacto": impacto,
                "noticias": noticias,
            })
        historicos.sort(key=lambda e: e["fecha"], reverse=True)

        proximos = list(proximos_ir)
        if proxima_yf and hoy < proxima_yf <= limite:
            proximos.append({
                "fecha": proxima_yf,
                "tipo": "Presentación de resultados",
                "resumen": "Próxima presentación de resultados.",
                "certeza": "Aproximada",
                "fuente": f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/analysis/",
            })
        elif not any(e["tipo"] == "Presentación de resultados" for e in proximos):
            estimada = _estimar_proximo_resultado(fechas_resultados, hoy, limite)
            if estimada:
                proximos.append({
                    "fecha": estimada,
                    "tipo": "Presentación de resultados",
                    "resumen": "Fecha estimada a partir del ritmo trimestral; pendiente de confirmación.",
                    "certeza": "Aproximada",
                    "fuente": FUENTES_IR.get(ticker) or f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/analysis/",
                })

        proximos.extend(_eventos_sec(ticker, hoy, limite, traducciones))
        proximos.extend(_eventos_futuros_noticias(ticker, hoy, limite, traducciones))
        for manual in manuales.get(ticker, []):
            fecha_manual = _a_fecha(manual.get("fecha"))
            if fecha_manual and hoy < fecha_manual <= limite:
                proximos.append({
                    "fecha": fecha_manual,
                    "tipo": manual.get("tipo", "Evento importante"),
                    "resumen": manual.get("resumen", "Evento añadido manualmente."),
                    "certeza": manual.get("certeza", "Fecha confirmada manualmente"),
                    "fuente": manual.get("fuente", FUENTES_IR.get(ticker, "#")),
                })
        proximos = _normalizar_eventos_proximos(proximos)

        estado_nuevo[ticker] = {
            "eventos": [{
                "fecha": e["fecha"].isoformat(), "tipo": e["tipo"],
                "resumen": e["resumen"], "impacto": e["impacto"],
                "noticias": [{**n, "fecha": n["fecha"].isoformat()} for n in e["noticias"]],
            } for e in historicos],
            "resultados": [{"fecha": f.isoformat()} for f in fechas_resultados],
            "proxima_fecha": next((e["fecha"].isoformat() for e in proximos if e["tipo"] == "Presentación de resultados"), None),
            "tipo": next((e["certeza"] for e in proximos if e["tipo"] == "Presentación de resultados"), "Sin anunciar"),
            "traducciones": traducciones,
            "consultado": datetime.now().isoformat(timespec="seconds"),
        }
        filas.append({
            "ticker": ticker,
            "historicos": historicos,
            "proximos": proximos,
            "fechas_resultados": fechas_resultados,
            "avisos": avisos,
        })

    CARPETA_CALENDARIO.mkdir(parents=True, exist_ok=True)
    ARCHIVO_ESTADO.write_text(json.dumps(estado_nuevo, indent=2, ensure_ascii=False), encoding="utf-8")

    fechas_graficos = {fila["ticker"]: fila["fechas_resultados"] for fila in filas}
    alertas_resultados = {}
    for fila in filas:
        proximo_resultado = next(
            (e for e in fila["proximos"] if e["tipo"] == "Presentación de resultados"), None
        )
        certeza = proximo_resultado["certeza"] if proximo_resultado else "Sin anunciar"
        tipo_compatible = "Oficial" if certeza.startswith("Exacta") else (
            "Aproximada" if proximo_resultado else "Sin anunciar"
        )
        alertas_resultados[fila["ticker"]] = {
            "publicadas": fila["fechas_resultados"],
            "proxima": proximo_resultado["fecha"] if proximo_resultado else None,
            "tipo_proxima": tipo_compatible,
        }

    html = _informe_html(filas, hoy)
    ARCHIVO_HTML.write_text(html, encoding="utf-8")
    if mostrar:
        display(HTML(html))
        print("Informe V2.4 guardado en:", ARCHIVO_HTML.resolve())

    return {
        "FECHAS_RESULTADOS_GRAFICOS": fechas_graficos,
        "DATOS_ALERTA_RESULTADOS": alertas_resultados,
        "FILAS_CALENDARIO_EVENTOS": filas,
        "ARCHIVO_HTML": ARCHIVO_HTML.resolve(),
    }


__all__ = ["generar_calendario_eventos"]
