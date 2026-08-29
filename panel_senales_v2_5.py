"""Panel V2.5 de señales LONG/SHORT independientes de las posiciones abiertas."""

from __future__ import annotations

import base64
from html import escape
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable
import warnings

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta
from IPython.display import HTML, display


CRITERIOS_LONG_VALIDOS = {"RSI_SOBREVENTA", "BOLLINGER_INFERIOR"}
CRITERIOS_SALIDA_LONG_VALIDOS = {"RSI_RECUPERADO", "BOLLINGER_MEDIA", "STOP_PROTECCION"}
SISTEMAS_SHORT_VALIDOS = {"RSI", "RUPTURA"}
CRITERIOS_SHORT_RSI_VALIDOS = {"RSI_SOBRECOMPRA"}
CRITERIOS_SHORT_RUPTURA_VALIDOS = {"RUPTURA_MINIMO", "VOLUMEN_RUPTURA"}
CRITERIOS_SALIDA_SHORT_RSI_VALIDOS = {"RSI_NORMALIZADO", "STOP_PROTECCION"}
CRITERIOS_SALIDA_SHORT_RUPTURA_VALIDOS = {
    "DOS_CIERRES_EMA",
    "RSI_FIN_RUPTURA",
    "MAX_SESIONES",
    "TRAILING_ATR",
}


def _lista_unica(valores: Iterable[str], nombre: str, validos: set[str]) -> list[str]:
    lista = [str(valor).strip().upper() for valor in valores if str(valor).strip()]
    repetidos = sorted({valor for valor in lista if lista.count(valor) > 1})
    desconocidos = sorted(set(lista) - validos)
    if repetidos:
        raise ValueError(f"{nombre} contiene valores repetidos: {', '.join(repetidos)}")
    if desconocidos:
        raise ValueError(f"{nombre} contiene criterios desconocidos: {', '.join(desconocidos)}")
    return lista


def _descargar_datos(
    activos: Iterable[str], fecha_fin: str | pd.Timestamp | None, ajustar_dividendos: bool
) -> tuple[list[str], dict[str, pd.DataFrame], pd.Timestamp]:
    tickers = [str(ticker).strip().upper() for ticker in activos if str(ticker).strip()]
    if not tickers or len(tickers) != len(set(tickers)):
        raise ValueError("ACTIVOS debe contener tickers únicos y al menos uno.")

    hoy = pd.Timestamp.today().normalize()
    fin_solicitado = (
        hoy
        if fecha_fin is None or str(fecha_fin).strip().upper() == "HOY"
        else pd.Timestamp(fecha_fin).normalize()
    )
    if fin_solicitado > hoy:
        raise ValueError("FECHA_FIN no puede ser posterior a hoy.")

    inicio = fin_solicitado - relativedelta(years=5)
    datos_raw: dict[str, pd.DataFrame] = {}
    errores = []
    yf.set_tz_cache_location(str(Path(tempfile.gettempdir()) / "yf_senales_v25_cache"))
    for ticker in tickers:
        try:
            datos = yf.download(
                ticker,
                start=inicio,
                end=fin_solicitado + pd.Timedelta(days=1),
                interval="1d",
                auto_adjust=ajustar_dividendos,
                progress=False,
            )
            if datos.empty:
                raise RuntimeError("descarga vacía")
            if isinstance(datos.columns, pd.MultiIndex):
                datos.columns = datos.columns.get_level_values(0)
            datos = datos[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
            datos.index = pd.DatetimeIndex(datos.index).tz_localize(None).normalize()
            datos_raw[ticker] = datos[~datos.index.duplicated(keep="last")].sort_index()
        except Exception as exc:  # pragma: no cover - depende del proveedor externo
            errores.append(f"{ticker}: {exc}")
    if errores:
        raise RuntimeError("Error de descarga:\n" + "\n".join(errores))

    sesiones_comunes = set.intersection(*(set(datos.index) for datos in datos_raw.values()))
    if not sesiones_comunes:
        raise RuntimeError("Los activos no tienen sesiones comunes.")
    ultima_fecha_comun = max(sesiones_comunes)
    return tickers, datos_raw, ultima_fecha_comun


def _preparar_indicadores(
    datos: pd.DataFrame,
    rsi_periodo: int,
    bollinger_periodo: int,
    bollinger_desviaciones: float,
    periodo_ruptura: int,
    ema_short_periodo: int,
    atr_periodo: int,
) -> pd.DataFrame:
    d = datos.copy()
    delta = d["Close"].diff()
    subida, bajada = delta.clip(lower=0), -delta.clip(upper=0)
    rs = subida.rolling(rsi_periodo).mean() / bajada.rolling(rsi_periodo).mean()
    d["RSI"] = 100 - 100 / (1 + rs)

    d["BB_MIDDLE"] = d["Close"].rolling(bollinger_periodo).mean()
    desviacion = d["Close"].rolling(bollinger_periodo).std()
    d["BB_LOWER"] = d["BB_MIDDLE"] - bollinger_desviaciones * desviacion
    d["MINIMO_RUPTURA_PREV"] = d["Low"].rolling(periodo_ruptura).min().shift(1)
    d["VOLUMEN_RUPTURA_PREV"] = d["Volume"].rolling(periodo_ruptura).mean().shift(1)
    d["EMA_SHORT"] = d["Close"].ewm(span=ema_short_periodo, adjust=False).mean()
    d["DOS_CIERRES_SOBRE_EMA"] = (d["Close"] > d["EMA_SHORT"]) & (
        d["Close"].shift(1) > d["EMA_SHORT"].shift(1)
    )
    cierre_previo = d["Close"].shift(1)
    rango_real = pd.concat(
        [
            d["High"] - d["Low"],
            (d["High"] - cierre_previo).abs(),
            (d["Low"] - cierre_previo).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["ATR"] = rango_real.ewm(alpha=1 / atr_periodo, adjust=False).mean()
    return d


def _criterio(nombre: str, detalle: str, cumple: bool) -> dict[str, Any]:
    return {"nombre": nombre, "detalle": detalle, "cumple": bool(cumple)}


def _evaluar_senales(
    actual: pd.Series,
    criterios_long: list[str],
    criterios_salida_long: list[str],
    sistemas_short: list[str],
    criterios_short_rsi: list[str],
    criterios_short_ruptura: list[str],
    criterios_salida_short_rsi: list[str],
    criterios_salida_short_ruptura: list[str],
    *,
    rsi_periodo: int,
    umbral_rsi_long: float,
    bollinger_periodo: int,
    bollinger_desviaciones: float,
    umbral_salida_long_rsi: float,
    umbral_rsi_short: float,
    umbral_salida_short_rsi: float,
    umbral_fin_ruptura_rsi: float,
    periodo_ruptura: int,
    multiplicador_volumen: float,
    ema_short_periodo: int,
) -> dict[str, Any]:
    rsi = float(actual["RSI"])
    cierre = float(actual["Close"])
    banda_inferior = float(actual["BB_LOWER"])
    banda_media = float(actual["BB_MIDDLE"])
    minimo_previo = float(actual["MINIMO_RUPTURA_PREV"])
    volumen = float(actual["Volume"])
    volumen_requerido = float(actual["VOLUMEN_RUPTURA_PREV"]) * multiplicador_volumen
    ema_short = float(actual["EMA_SHORT"])
    dos_cierres_sobre_ema = bool(actual["DOS_CIERRES_SOBRE_EMA"])

    catalogo_long = {
        "RSI_SOBREVENTA": _criterio(
            "RSI",
            f"RSI {rsi_periodo} = {rsi:.1f} < {umbral_rsi_long:g}",
            rsi < umbral_rsi_long,
        ),
        "BOLLINGER_INFERIOR": _criterio(
            "Bollinger",
            (
                f"cierre {cierre:,.2f} ≤ banda inferior {banda_inferior:,.2f} "
                f"({bollinger_periodo}, {bollinger_desviaciones:g}σ)"
            ),
            cierre <= banda_inferior,
        ),
    }
    long_items = [catalogo_long[nombre] for nombre in criterios_long]
    senal_long = bool(long_items) and all(item["cumple"] for item in long_items)

    catalogo_salida_long = {
        "RSI_RECUPERADO": _criterio(
            "RSI",
            f"RSI {rsi_periodo} = {rsi:.1f} ≥ {umbral_salida_long_rsi:g}",
            rsi >= umbral_salida_long_rsi,
        ),
        "BOLLINGER_MEDIA": _criterio(
            "Bollinger",
            f"cierre {cierre:,.2f} ≥ banda media {banda_media:,.2f} ({bollinger_periodo})",
            cierre >= banda_media,
        ),
    }
    salida_long_items = [catalogo_salida_long[nombre] for nombre in criterios_salida_long]
    salida_long = bool(salida_long_items) and any(item["cumple"] for item in salida_long_items)

    catalogo_short_rsi = {
        "RSI_SOBRECOMPRA": _criterio(
            "Sobrecompra",
            f"RSI {rsi_periodo} = {rsi:.1f} > {umbral_rsi_short:g}",
            rsi > umbral_rsi_short,
        )
    }
    catalogo_short_ruptura = {
        "RUPTURA_MINIMO": _criterio(
            "Precio",
            f"cierre {cierre:,.2f} < mínimo previo {periodo_ruptura} = {minimo_previo:,.2f}",
            cierre < minimo_previo,
        ),
        "VOLUMEN_RUPTURA": _criterio(
            "Volumen",
            (
                f"volumen {volumen:,.0f} > media {periodo_ruptura} × "
                f"{multiplicador_volumen:g} = {volumen_requerido:,.0f}"
            ),
            volumen > volumen_requerido,
        ),
    }

    rutas_short = []
    if "RSI" in sistemas_short and criterios_short_rsi:
        items = [catalogo_short_rsi[nombre] for nombre in criterios_short_rsi]
        rutas_short.append({"sistema": "RSI", "criterios": items, "cumple": all(i["cumple"] for i in items)})
    if "RUPTURA" in sistemas_short and criterios_short_ruptura:
        items = [catalogo_short_ruptura[nombre] for nombre in criterios_short_ruptura]
        rutas_short.append(
            {"sistema": "RUPTURA", "criterios": items, "cumple": all(i["cumple"] for i in items)}
        )
    senal_short = any(ruta["cumple"] for ruta in rutas_short)

    catalogo_salida_short_rsi = {
        "RSI_NORMALIZADO": _criterio(
            "RSI normalizado",
            f"RSI {rsi_periodo} = {rsi:.1f} < {umbral_salida_short_rsi:g}",
            rsi < umbral_salida_short_rsi,
        )
    }
    catalogo_salida_short_ruptura = {
        "DOS_CIERRES_EMA": _criterio(
            "Confirmación EMA",
            f"dos cierres sobre EMA {ema_short_periodo}; EMA actual = {ema_short:,.2f}",
            dos_cierres_sobre_ema,
        ),
        "RSI_FIN_RUPTURA": _criterio(
            "Fin por RSI",
            f"RSI {rsi_periodo} = {rsi:.1f} > {umbral_fin_ruptura_rsi:g}",
            rsi > umbral_fin_ruptura_rsi,
        ),
    }
    rutas_salida_short = []
    if "RSI" in sistemas_short and criterios_salida_short_rsi:
        items = [catalogo_salida_short_rsi[nombre] for nombre in criterios_salida_short_rsi]
        rutas_salida_short.append(
            {"sistema": "RSI", "criterios": items, "cumple": any(i["cumple"] for i in items)}
        )
    if "RUPTURA" in sistemas_short and criterios_salida_short_ruptura:
        items = [
            catalogo_salida_short_ruptura[nombre]
            for nombre in criterios_salida_short_ruptura
        ]
        rutas_salida_short.append(
            {"sistema": "RUPTURA", "criterios": items, "cumple": any(i["cumple"] for i in items)}
        )
    salida_short = any(ruta["cumple"] for ruta in rutas_salida_short)
    return {
        "long": {
            "entrar": senal_long,
            "criterios_entrada": long_items,
            "salir": salida_long,
            "criterios_salida": salida_long_items,
        },
        "short": {
            "entrar": senal_short,
            "rutas_entrada": rutas_short,
            "salir": salida_short,
            "rutas_salida": rutas_salida_short,
        },
    }


def _disparadores(estado: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Expone cada ruta como disparador independiente para detectar activaciones."""
    disparadores: dict[tuple[str, str, str], dict[str, Any]] = {}
    long_entrada = estado["long"]["criterios_entrada"]
    disparadores[("LONG", "ENTRAR", "RSI + BOLLINGER")] = {
        "activo": estado["long"]["entrar"],
        "criterios": long_entrada,
    }
    long_salida = estado["long"]["criterios_salida"]
    disparadores[("LONG", "SALIR", "RSI / BOLLINGER")] = {
        "activo": estado["long"]["salir"],
        "criterios": long_salida,
    }
    for ruta in estado["short"]["rutas_entrada"]:
        disparadores[("SHORT", "ENTRAR", ruta["sistema"])] = {
            "activo": ruta["cumple"],
            "criterios": ruta["criterios"],
        }
    for ruta in estado["short"]["rutas_salida"]:
        disparadores[("SHORT", "SALIR", ruta["sistema"])] = {
            "activo": ruta["cumple"],
            "criterios": ruta["criterios"],
        }
    return disparadores


def _historial_senales(
    datos: pd.DataFrame,
    evaluar_fila: Any,
    ventana_dias: int,
) -> list[dict[str, Any]]:
    """Registra el día de activación, no cada día que la condición siga vigente."""
    inicio = datos.index[-1] - pd.Timedelta(days=ventana_dias)
    primera_posicion = max(1, int(datos.index.searchsorted(inicio)))
    eventos = []
    estado_previo = _disparadores(evaluar_fila(datos.iloc[primera_posicion - 1]))
    for posicion in range(primera_posicion, len(datos)):
        fecha = datos.index[posicion]
        estado_actual = _disparadores(evaluar_fila(datos.iloc[posicion]))
        for clave, disparador in estado_actual.items():
            estaba_activo = estado_previo.get(clave, {}).get("activo", False)
            if disparador["activo"] and not estaba_activo:
                lado, accion, sistema = clave
                eventos.append(
                    {
                        "fecha": fecha,
                        "lado": lado,
                        "accion": accion,
                        "sistema": sistema,
                        "parametros": "; ".join(
                            criterio["detalle"] for criterio in disparador["criterios"]
                        ),
                    }
                )
        estado_previo = estado_actual
    return eventos


def _seleccionar_criterios(
    catalogo: dict[str, dict[str, Any]], nombres: list[str]
) -> list[dict[str, Any]]:
    return [catalogo[nombre] for nombre in nombres]


def _entrada_long(fila: pd.Series, configuracion: dict[str, Any]) -> list[dict[str, Any]]:
    rsi = float(fila["RSI"])
    cierre = float(fila["Close"])
    banda = float(fila["BB_LOWER"])
    catalogo = {
        "RSI_SOBREVENTA": _criterio(
            "RSI", f"RSI {configuracion['rsi_periodo']} = {rsi:.1f} < {configuracion['umbral_rsi_long']:g}",
            rsi < configuracion["umbral_rsi_long"],
        ),
        "BOLLINGER_INFERIOR": _criterio(
            "Bollinger",
            f"cierre {cierre:,.2f} ≤ banda inferior {banda:,.2f} "
            f"({configuracion['bollinger_periodo']}, {configuracion['bollinger_desviaciones']:g}σ)",
            cierre <= banda,
        ),
    }
    return _seleccionar_criterios(catalogo, configuracion["criterios_long"])


def _salida_long(
    fila: pd.Series, operacion: dict[str, Any] | None, configuracion: dict[str, Any]
) -> list[dict[str, Any]]:
    rsi = float(fila["RSI"])
    cierre = float(fila["Close"])
    banda_media = float(fila["BB_MIDDLE"])
    stop = operacion.get("stop") if operacion else None
    detalle_stop = (
        f"cierre {cierre:,.2f} ≤ stop {float(stop):,.2f}"
        if stop is not None
        else "sin operación virtual abierta"
    )
    catalogo = {
        "RSI_RECUPERADO": _criterio(
            "RSI", f"RSI {configuracion['rsi_periodo']} = {rsi:.1f} ≥ "
            f"{configuracion['umbral_salida_long_rsi']:g}",
            rsi >= configuracion["umbral_salida_long_rsi"],
        ),
        "BOLLINGER_MEDIA": _criterio(
            "Bollinger",
            f"cierre {cierre:,.2f} ≥ banda media {banda_media:,.2f} "
            f"({configuracion['bollinger_periodo']})",
            cierre >= banda_media,
        ),
        "STOP_PROTECCION": _criterio(
            "Stop", detalle_stop, stop is not None and cierre <= float(stop)
        ),
    }
    return _seleccionar_criterios(catalogo, configuracion["criterios_salida_long"])


def _entrada_short_rsi(fila: pd.Series, configuracion: dict[str, Any]) -> list[dict[str, Any]]:
    rsi = float(fila["RSI"])
    catalogo = {
        "RSI_SOBRECOMPRA": _criterio(
            "Sobrecompra",
            f"RSI {configuracion['rsi_periodo']} = {rsi:.1f} > {configuracion['umbral_rsi_short']:g}",
            rsi > configuracion["umbral_rsi_short"],
        )
    }
    return _seleccionar_criterios(catalogo, configuracion["criterios_short_rsi"])


def _salida_short_rsi(
    fila: pd.Series, operacion: dict[str, Any] | None, configuracion: dict[str, Any]
) -> list[dict[str, Any]]:
    rsi = float(fila["RSI"])
    cierre = float(fila["Close"])
    stop = operacion.get("stop") if operacion else None
    detalle_stop = (
        f"cierre {cierre:,.2f} ≥ stop {float(stop):,.2f}"
        if stop is not None
        else "sin operación virtual abierta"
    )
    catalogo = {
        "RSI_NORMALIZADO": _criterio(
            "RSI normalizado",
            f"RSI {configuracion['rsi_periodo']} = {rsi:.1f} < "
            f"{configuracion['umbral_salida_short_rsi']:g}",
            rsi < configuracion["umbral_salida_short_rsi"],
        ),
        "STOP_PROTECCION": _criterio(
            "Stop", detalle_stop, stop is not None and cierre >= float(stop)
        ),
    }
    return _seleccionar_criterios(catalogo, configuracion["criterios_salida_short_rsi"])


def _entrada_short_ruptura(
    fila: pd.Series, configuracion: dict[str, Any]
) -> list[dict[str, Any]]:
    cierre = float(fila["Close"])
    minimo = float(fila["MINIMO_RUPTURA_PREV"])
    volumen = float(fila["Volume"])
    volumen_requerido = float(fila["VOLUMEN_RUPTURA_PREV"]) * configuracion["multiplicador_volumen"]
    catalogo = {
        "RUPTURA_MINIMO": _criterio(
            "Precio",
            f"cierre {cierre:,.2f} < mínimo previo {configuracion['periodo_ruptura']} = {minimo:,.2f}",
            cierre < minimo,
        ),
        "VOLUMEN_RUPTURA": _criterio(
            "Volumen",
            f"volumen {volumen:,.0f} > media {configuracion['periodo_ruptura']} × "
            f"{configuracion['multiplicador_volumen']:g} = {volumen_requerido:,.0f}",
            volumen > volumen_requerido,
        ),
    }
    return _seleccionar_criterios(catalogo, configuracion["criterios_short_ruptura"])


def _salida_short_ruptura(
    fila: pd.Series, operacion: dict[str, Any] | None, configuracion: dict[str, Any]
) -> list[dict[str, Any]]:
    rsi = float(fila["RSI"])
    cierre = float(fila["Close"])
    ema = float(fila["EMA_SHORT"])
    sesiones = int(operacion.get("sesiones", 0)) if operacion else 0
    trailing = operacion.get("trailing") if operacion else None
    detalle_sesiones = (
        f"{sesiones} sesiones desde la entrada ≥ {configuracion['ruptura_max_sesiones']}"
        if operacion
        else "sin operación virtual abierta"
    )
    detalle_trailing = (
        f"cierre {cierre:,.2f} ≥ stop/trailing {float(trailing):,.2f}; "
        f"ATR {configuracion['atr_periodo']} = {float(fila['ATR']):,.2f}"
        if trailing is not None
        else "sin operación virtual abierta"
    )
    catalogo = {
        "DOS_CIERRES_EMA": _criterio(
            "Confirmación EMA",
            f"dos cierres sobre EMA {configuracion['ema_short_periodo']}; EMA actual = {ema:,.2f}",
            bool(fila["DOS_CIERRES_SOBRE_EMA"]),
        ),
        "RSI_FIN_RUPTURA": _criterio(
            "Fin por RSI",
            f"RSI {configuracion['rsi_periodo']} = {rsi:.1f} > "
            f"{configuracion['umbral_fin_ruptura_rsi']:g}",
            rsi > configuracion["umbral_fin_ruptura_rsi"],
        ),
        "MAX_SESIONES": _criterio(
            "Duración máxima", detalle_sesiones,
            operacion is not None and sesiones >= configuracion["ruptura_max_sesiones"],
        ),
        "TRAILING_ATR": _criterio(
            "Stop/trailing ATR", detalle_trailing,
            trailing is not None and cierre >= float(trailing),
        ),
    }
    return _seleccionar_criterios(catalogo, configuracion["criterios_salida_short_ruptura"])


def _actualizar_ruptura(
    fila: pd.Series, operacion: dict[str, Any], configuracion: dict[str, Any]
) -> None:
    operacion["sesiones"] += 1
    operacion["minimo_desde_entrada"] = min(
        float(operacion["minimo_desde_entrada"]), float(fila["Low"])
    )
    if pd.notna(fila["ATR"]):
        candidato = operacion["minimo_desde_entrada"] + (
            configuracion["ruptura_atr"] * float(fila["ATR"])
        )
        operacion["trailing"] = min(float(operacion["trailing"]), candidato)


def _simular_estrategia(
    datos: pd.DataFrame,
    lado: str,
    sistema: str,
    entrada_fn: Any,
    salida_fn: Any,
    configuracion: dict[str, Any],
    actualizar_fn: Any | None = None,
) -> dict[str, Any]:
    """Reconstruye una estrategia virtual; no consulta operaciones reales ni otras estrategias."""
    operaciones: list[dict[str, Any]] = []
    eventos: list[dict[str, Any]] = []
    activa: dict[str, Any] | None = None
    contador = 0

    for fecha, fila in datos.iterrows():
        entrada_items = entrada_fn(fila, configuracion)
        if activa is not None:
            if actualizar_fn is not None:
                actualizar_fn(fila, activa, configuracion)
            salida_items = salida_fn(fila, activa, configuracion)
            if any(item["cumple"] for item in salida_items):
                activa["fecha_salida"] = fecha
                activa["precio_salida"] = float(fila["Close"])
                activa["parametros_salida"] = [item.copy() for item in salida_items]
                activa["activadores_salida"] = [
                    item["nombre"] for item in salida_items if item["cumple"]
                ]
                activa["estado"] = "CERRADA"
                eventos.append(
                    {
                        "fecha": fecha,
                        "lado": lado,
                        "accion": "SALIR",
                        "sistema": sistema,
                        "operacion_id": activa["id"],
                        "parametros": "; ".join(item["detalle"] for item in salida_items),
                    }
                )
                activa = None
                # Una salida al cierre no permite reabrir la misma estrategia en ese cierre.
                continue

        if activa is None and entrada_items and all(item["cumple"] for item in entrada_items):
            contador += 1
            precio = float(fila["Close"])
            stop_pct = configuracion["stop_long"] if lado == "LONG" else configuracion["stop_short"]
            stop = precio * (1 - stop_pct) if lado == "LONG" else precio * (1 + stop_pct)
            activa = {
                "id": f"{lado}-{sistema}-{contador}",
                "lado": lado,
                "sistema": sistema,
                "fecha_entrada": fecha,
                "precio_entrada": precio,
                "parametros_entrada": [item.copy() for item in entrada_items],
                "fecha_salida": None,
                "precio_salida": None,
                "parametros_salida": [],
                "activadores_salida": [],
                "estado": "ABIERTA",
                "stop": stop,
            }
            if sistema == "RUPTURA":
                activa.update(
                    {
                        "sesiones": 1,
                        "minimo_desde_entrada": min(precio, float(fila["Low"])),
                        "trailing": stop,
                    }
                )
                if pd.notna(fila["ATR"]):
                    candidato = activa["minimo_desde_entrada"] + (
                        configuracion["ruptura_atr"] * float(fila["ATR"])
                    )
                    activa["trailing"] = min(float(activa["trailing"]), candidato)
            operaciones.append(activa)
            eventos.append(
                {
                    "fecha": fecha,
                    "lado": lado,
                    "accion": "ENTRAR",
                    "sistema": sistema,
                    "operacion_id": activa["id"],
                    "parametros": "; ".join(item["detalle"] for item in entrada_items),
                }
            )

    ultima_fecha = datos.index[-1]
    ultima_fila = datos.iloc[-1]
    evento_entrada_hoy = next(
        (evento for evento in reversed(eventos) if evento["fecha"] == ultima_fecha and evento["accion"] == "ENTRAR"),
        None,
    )
    evento_salida_hoy = next(
        (evento for evento in reversed(eventos) if evento["fecha"] == ultima_fecha and evento["accion"] == "SALIR"),
        None,
    )
    if evento_salida_hoy:
        operacion_salida = next(
            operacion for operacion in operaciones if operacion["id"] == evento_salida_hoy["operacion_id"]
        )
        salida_actual = operacion_salida["parametros_salida"]
    else:
        salida_actual = salida_fn(ultima_fila, activa, configuracion)
    return {
        "lado": lado,
        "sistema": sistema,
        "entrada_actual": entrada_fn(ultima_fila, configuracion),
        "salida_actual": salida_actual,
        "entrar_hoy": evento_entrada_hoy is not None,
        "salir_hoy": evento_salida_hoy is not None,
        "operacion_activa": activa,
        "operaciones": operaciones,
        "eventos": eventos,
    }


def _dibujar_periodo(
    ax_precio: Any,
    datos: pd.DataFrame,
    inicio: pd.Timestamp,
    titulo: str,
    fechas_resultados: Iterable[Any] | None = None,
    senales: Iterable[dict[str, Any]] | None = None,
    eventos_calendario: Iterable[dict[str, Any]] | None = None,
) -> None:
    tramo = datos.loc[datos.index >= inicio]
    ax_volumen = ax_precio.twinx()
    ax_volumen.bar(tramo.index, tramo["Volume"], color="#aeb8c2", alpha=0.32, width=0.75, zorder=1)
    ax_precio.plot(tramo.index, tramo["Close"], color="#1769aa", lw=2.2, zorder=3)
    for indice, fecha_resultado in enumerate(fechas_resultados or []):
        marca = pd.Timestamp(fecha_resultado)
        if marca < tramo.index.min() or marca > tramo.index.max():
            continue
        color = ("#ef6c00", "#7b1fa2")[indice % 2]
        ax_precio.axvline(marca, color=color, lw=1.8, ls="--", alpha=0.9, zorder=4)
        ax_precio.annotate(
            marca.strftime("%d/%m/%Y"),
            xy=(marca, 0),
            xycoords=("data", "axes fraction"),
            xytext=(0, -31),
            textcoords="offset points",
            ha="center",
            va="top",
            fontsize=8,
            fontweight="bold",
            color=color,
            annotation_clip=False,
        )
    for evento in eventos_calendario or []:
        marca = pd.Timestamp(evento["fecha"])
        if marca < tramo.index.min() or marca > tramo.index.max():
            continue
        es_resultado = "resultado" in str(evento.get("tipo", "")).lower()
        impacto = evento.get("impacto")
        if not es_resultado and (impacto is None or pd.isna(impacto) or float(impacto) == 0):
            continue
        color = "#ef6c00" if es_resultado else "#111111"
        grosor = 2.0 if es_resultado else 0.65
        estilo = "--" if es_resultado else "-"
        ax_precio.axvline(
            marca, color=color, lw=grosor, ls=estilo,
            alpha=0.95 if es_resultado else 0.72, zorder=5,
        )
        ax_precio.annotate(
            str(evento.get("etiqueta") or evento.get("tipo", "Evento")),
            xy=(marca, 0), xycoords=("data", "axes fraction"),
            xytext=(2, -8), textcoords="offset points",
            ha="left", va="top", rotation=45, fontsize=7, color=color,
            annotation_clip=False,
        )
    desplazamientos: dict[pd.Timestamp, int] = {}
    for senal in senales or []:
        marca = pd.Timestamp(senal["fecha"])
        if marca not in tramo.index:
            continue
        precio = float(tramo.loc[marca, "Close"])
        accion = senal["accion"]
        lado = senal["lado"]
        color, _, marcador, _ = _estilo_senal(lado, accion)
        repeticion = desplazamientos.get(marca, 0)
        desplazamientos[marca] = repeticion + 1
        desplazamiento_y = 13 + repeticion * 12 if marcador == "^" else -17 - repeticion * 12
        ax_precio.scatter(
            marca, precio, marker=marcador, s=135, color=color,
            edgecolor="white", linewidth=0.8, zorder=6,
        )
        ax_precio.annotate(
            f"{lado} {accion}",
            xy=(marca, precio),
            xytext=(0, desplazamiento_y),
            textcoords="offset points",
            ha="center",
            va="bottom" if desplazamiento_y > 0 else "top",
            fontsize=7,
            fontweight="bold",
            color=color,
            annotation_clip=True,
        )
    leyenda_senales = []
    for lado, accion, texto in (
        ("LONG", "ENTRAR", "LONG entrada ▼"),
        ("LONG", "SALIR", "LONG salida ▲"),
        ("SHORT", "ENTRAR", "SHORT entrada ▼"),
        ("SHORT", "SALIR", "SHORT salida ▲"),
    ):
        color, _, marcador, _ = _estilo_senal(lado, accion)
        leyenda_senales.append(
            Line2D([0], [0], marker=marcador, color="none", markerfacecolor=color,
                   markeredgecolor="white", markersize=9, label=texto)
        )
    ax_precio.legend(
        handles=leyenda_senales, loc="upper left", ncol=2, fontsize=7,
        frameon=True, framealpha=0.88, borderpad=0.5,
    )
    ax_precio.set_title(titulo, fontsize=13, fontweight="bold")
    ax_precio.set_ylabel("Precio", color="#1769aa")
    ax_volumen.set_ylabel("Volumen", color="#66717c")
    ax_volumen.tick_params(axis="y", labelsize=8, colors="#66717c")
    ax_volumen.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))
    ax_precio.grid(alpha=0.25)
    ax_precio.xaxis.set_major_locator(mdates.MonthLocator(bymonthday=(1, 15)))
    ax_precio.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m-%Y"))
    ax_precio.tick_params(axis="x", rotation=38, labelsize=9)
    ax_precio.margins(x=0.03)


def _estilo_senal(lado: str, accion: str) -> tuple[str, str, str, str]:
    estilos = {
        ("LONG", "ENTRAR"): ("#188038", "#e6f4ea", "v", "▼"),
        ("LONG", "SALIR"): ("#f9ab00", "#fef7e0", "^", "▲"),
        ("SHORT", "ENTRAR"): ("#1967d2", "#e8f0fe", "v", "▼"),
        ("SHORT", "SALIR"): ("#d93025", "#fce8e6", "^", "▲"),
    }
    return estilos.get((lado, accion), ("#5f6368", "#f1f3f4", "o", "●"))


def _etiqueta_evento_grafico(evento: dict[str, Any]) -> str:
    tipo = str(evento.get("tipo", ""))
    resumenes = [
        str(item.get("texto", "")).strip()
        for item in evento.get("resumen", [])
        if str(item.get("texto", "")).strip()
    ]
    textos = " ".join(resumenes)
    texto = f"{tipo} {textos}".lower()
    if "resultado" in tipo.lower():
        return "Resultados"
    if "arancel" in texto and "trump" in texto:
        return "Aranceles Trump"
    if "arancel" in texto:
        return "Nuevos aranceles"
    if any(palabra in texto for palabra in ("juicio", "tribunal", "veredicto", "ensayo clave")):
        return "Juicio / veredicto"
    if "nube" in texto or "cloud" in texto:
        return "Negocio en la nube"
    if "inteligencia artificial" in texto or re.search(r"\bia\b", texto):
        return "Impacto de IA"
    if any(palabra in texto for palabra in ("antimonopolio", "regulador", "investigación sec")):
        return "Investigación regulatoria"
    if "demanda" in texto:
        return "Demanda judicial"
    if any(palabra in texto for palabra in ("previsión", "pronóstico", "perspectivas")):
        return "Cambio de previsiones"

    titular = next(
        (valor for valor in resumenes if "presentación de resultados trimestrales" not in valor.lower()),
        tipo or "Evento",
    )
    titular = re.split(r"[,;:]", titular, maxsplit=1)[0].strip("¿? .")
    titular = re.sub(
        r"^(las?|los?)\s+(acciones?|títulos?)\s+de\s+\S+\s+", "", titular,
        flags=re.IGNORECASE,
    )
    palabras = titular.split()
    return " ".join(palabras[:6]) + ("…" if len(palabras) > 6 else "")


def _eventos_para_graficos(
    ticker: str, filas_calendario: Iterable[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    fila = next((f for f in filas_calendario or [] if f.get("ticker") == ticker), None)
    if not fila:
        return []
    return [
        {**evento, "etiqueta": _etiqueta_evento_grafico(evento)}
        for evento in fila.get("historicos", [])
    ]


def _grafico_ampliable(ruta: Path, ticker: str) -> str:
    imagen = base64.b64encode(ruta.read_bytes()).decode("ascii")
    origen = f"data:image/png;base64,{imagen}"
    archivo_local = escape(ruta.resolve().as_uri(), quote=True)
    titulo = escape(f"{ticker} — pulsa para ampliar")
    return f"""
      <div style='position:relative;margin:8px 0 14px'>
        <a href='{archivo_local}' target='_blank' rel='noopener'
           style='display:block;text-decoration:none'>
          <img src='{origen}' alt='{titulo}' title='{titulo}'
               style='display:block;width:100%;height:auto;cursor:zoom-in;border:1px solid #d8dee4;
                      border-radius:7px' />
        </a>
        <div style='font:12px Arial;color:#5f6368;margin-top:4px'>
          Pulsa sobre el gráfico para abrirlo en una pestaña nueva y ampliarlo.
        </div>
      </div>"""


def _alerta_resultados(ticker: str, fecha: pd.Timestamp, datos_alerta: dict[str, Any]) -> dict[str, str]:
    datos = datos_alerta.get(ticker, {})
    referencia = pd.Timestamp(fecha).date()
    publicadas = sorted(
        (pd.Timestamp(valor).date() for valor in datos.get("publicadas", []) if valor), reverse=True
    )
    ultima = publicadas[0] if publicadas else None
    proxima = pd.Timestamp(datos["proxima"]).date() if datos.get("proxima") else None
    dias_desde = (referencia - ultima).days if ultima else None
    if dias_desde is not None and 0 <= dias_desde <= 6:
        distancia = dias_desde
        mensaje = "Se produjeron HOY" if distancia == 0 else f"Se produjeron hace {distancia} día" + ("" if distancia == 1 else "s")
    elif proxima is not None and proxima >= referencia:
        distancia = (proxima - referencia).days
        mensaje = "Se producen HOY" if distancia == 0 else f"Dentro de {distancia} día" + ("" if distancia == 1 else "s")
    elif ultima is not None:
        distancia = abs(dias_desde or 0)
        mensaje = f"Se produjeron hace {distancia} días"
    else:
        return {"mensaje": "Sin alertas de resultados", "color": "#5f6368"}
    color = "#c62828" if distancia <= 3 else ("#1565c0" if distancia in (5, 6) else "#5f6368")
    return {"mensaje": mensaje, "color": color}


def _chip(
    criterio: dict[str, Any],
    prefijo: str = "",
    tipo: str = "entrada",
    resaltar: bool = True,
) -> str:
    cumple = criterio["cumple"]
    activo_visual = cumple and resaltar
    color_activo = "#137333" if tipo == "entrada" else "#b3261e"
    borde_activo = "#34a853" if tipo == "entrada" else "#d93025"
    fondo_activo = "#d9f7df" if tipo == "entrada" else "#fce8e6"
    fondo = fondo_activo if activo_visual else "#f1f3f4"
    color = color_activo if activo_visual else "#4b5563"
    borde = borde_activo if activo_visual else "#c7cdd3"
    marca = "✓" if cumple else "✗"
    texto = f"{prefijo}{criterio['nombre']}: {criterio['detalle']}"
    return (
        f"<span style='display:inline-block;background:{fondo};color:{color};border:1px solid {borde};"
        f"border-radius:12px;padding:3px 8px;margin:2px 3px 2px 0'>{marca} {escape(texto)}</span>"
    )


def _panel_accion(titulo: str, accion: str, activo: bool, contenido: str) -> str:
    es_entrada = accion == "ENTRAR"
    fondo = ("#e6f4ea" if es_entrada else "#fce8e6") if activo else "#f8f9fa"
    borde = ("#188038" if es_entrada else "#d93025") if activo else "#c7cdd3"
    color = ("#137333" if es_entrada else "#b3261e") if activo else "#5f6368"
    etiqueta = accion if activo else "—"
    return f"""
      <div style='display:grid;grid-template-columns:minmax(0,1fr) 78px;gap:8px;align-items:center;
                  background:{fondo};border:1px solid {borde};border-radius:6px;padding:8px 9px'>
        <div><div style='font-size:11px;font-weight:800;color:{color};margin-bottom:3px'>{titulo}</div>
          {contenido or '<span style="color:#777">Sin criterios activos</span>'}
        </div>
        <div style='text-align:right;font-size:15px;font-weight:900;color:{color}'>{etiqueta}</div>
      </div>"""


def _fila_senal(lado: str, entrada: str, salida: str) -> str:
    return f"""
      <div style='display:grid;grid-template-columns:72px minmax(0,1fr) minmax(0,1fr);
                  gap:8px;align-items:stretch;margin:6px 0'>
        <div style='display:flex;align-items:center;font-size:16px;font-weight:900;
                    color:#202124;padding-left:4px'>{lado}</div>
        {entrada}
        {salida}
      </div>"""


def _contenido_rutas(rutas: list[dict[str, Any]], tipo: str) -> str:
    chips = []
    for ruta in rutas:
        for indice, item in enumerate(ruta["criterios"]):
            prefijo = f"{ruta['sistema']} — " if indice == 0 else ""
            chips.append(_chip(item, prefijo, tipo))
    return ", ".join(chips)


def _tabla_historial(historial: list[dict[str, Any]], ventana_dias: int) -> str:
    if not historial:
        return (
            f"<div style='font-size:12px;color:#5f6368;margin-top:10px'>"
            f"No se activaron señales durante los últimos {ventana_dias} días.</div>"
        )
    filas = []
    for evento in sorted(historial, key=lambda item: item["fecha"], reverse=True):
        color = "#137333" if evento["accion"] == "ENTRAR" else "#b3261e"
        fondo = "#e6f4ea" if evento["accion"] == "ENTRAR" else "#fce8e6"
        filas.append(
            "<tr>"
            f"<td>{evento['fecha']:%d/%m/%Y}</td><td><b>{escape(evento['lado'])}</b></td>"
            f"<td><span style='color:{color};background:{fondo};font-weight:800;"
            f"padding:2px 6px;border-radius:10px'>{evento['accion']}</span></td>"
            f"<td>{escape(evento['sistema'])}</td><td>{escape(evento['parametros'])}</td>"
            "</tr>"
        )
    return f"""
      <div style='font-size:13px;font-weight:800;margin:11px 0 5px'>
        Señales activadas durante los últimos {ventana_dias} días
      </div>
      <div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:11px'>
        <thead><tr style='background:#f1f3f4;text-align:left'>
          <th style='padding:5px'>Fecha</th><th>Lado</th><th>Señal</th><th>Sistema</th><th>Valores al activarse</th>
        </tr></thead><tbody>{''.join(filas)}</tbody>
      </table></div>"""


def _tarjeta(
    ticker: str,
    estado: dict[str, Any],
    historial: list[dict[str, Any]],
    ventana_dias: int,
    fecha: pd.Timestamp,
    cierre: float,
    datos_alerta: dict[str, Any],
) -> str:
    alerta = _alerta_resultados(ticker, fecha, datos_alerta)

    entrada_long_contenido = ", ".join(
        _chip(item, tipo="entrada") for item in estado["long"]["criterios_entrada"]
    )
    salida_long_contenido = ", ".join(
        _chip(item, tipo="salida") for item in estado["long"]["criterios_salida"]
    )
    entrada_long = _panel_accion(
        "PARÁMETROS DE ENTRADA", "ENTRAR", estado["long"]["entrar"], entrada_long_contenido
    )
    salida_long = _panel_accion(
        "PARÁMETROS DE SALIDA", "SALIR", estado["long"]["salir"], salida_long_contenido
    )

    entrada_short = _panel_accion(
        "PARÁMETROS DE ENTRADA",
        "ENTRAR",
        estado["short"]["entrar"],
        _contenido_rutas(estado["short"]["rutas_entrada"], "entrada"),
    )
    salida_short = _panel_accion(
        "PARÁMETROS DE SALIDA",
        "SALIR",
        estado["short"]["salir"],
        _contenido_rutas(estado["short"]["rutas_salida"], "salida"),
    )
    historial_html = _tabla_historial(historial, ventana_dias)
    return f"""
    <section style='border:1px solid #d8dee4;border-radius:8px;padding:13px 15px;
                    margin:10px 0 26px;font-family:Arial;background:#fff'>
      <div style='font-size:21px;font-weight:800'>{escape(ticker)} — señales al cierre</div>
      <div style='font-size:14px;font-weight:700;color:{alerta["color"]};margin:4px 0 9px'>
        Alerta resultados: {escape(alerta["mensaje"])}.
      </div>
      {_fila_senal('LONG', entrada_long, salida_long)}
      {_fila_senal('SHORT', entrada_short, salida_short)}
      {historial_html}
      <div style='font-size:12px;color:#5f6368;margin-top:9px'>
        Cierre analizado: {fecha:%Y-%m-%d} a {cierre:,.2f}. Las señales no comprueban si existe
        una posición. El historial registra el primer día en que cada condición pasa a estar activa.
      </div>
    </section>"""


def _panel_estrategia_virtual(estrategia: dict[str, Any]) -> str:
    entrada_contenido = ", ".join(
        _chip(item, tipo="entrada", resaltar=estrategia["entrar_hoy"])
        for item in estrategia["entrada_actual"]
    )
    salida_contenido = ", ".join(
        _chip(item, tipo="salida", resaltar=estrategia["salir_hoy"])
        for item in estrategia["salida_actual"]
    )
    entrada = _panel_accion(
        "PARÁMETROS DE ENTRADA",
        "ENTRAR",
        estrategia["entrar_hoy"],
        entrada_contenido,
    )
    salida = _panel_accion(
        "PARÁMETROS DE SALIDA",
        "SALIR",
        estrategia["salir_hoy"],
        salida_contenido,
    )
    operacion = estrategia["operacion_activa"]
    if operacion:
        estado = (
            f"Operación virtual <b>ABIERTA</b> desde {operacion['fecha_entrada']:%d/%m/%Y} "
            f"a {operacion['precio_entrada']:,.2f} (referencia: cierre de la señal)."
        )
        color_estado = "#174ea6"
    elif estrategia["salir_hoy"]:
        estado = "La operación virtual asociada ha generado hoy su señal de salida."
        color_estado = "#b3261e"
    else:
        estado = "Sin operación virtual abierta para este sistema."
        color_estado = "#5f6368"
    etiqueta = f"{estrategia['lado']} — {estrategia['sistema']}"
    return f"""
      <div style='display:grid;grid-template-columns:145px minmax(0,1fr) minmax(0,1fr);
                  gap:8px;align-items:stretch;margin:7px 0'>
        <div style='display:flex;flex-direction:column;justify-content:center;padding:7px;
                    background:#f1f3f4;border-radius:6px'>
          <div style='font-size:14px;font-weight:900'>{escape(etiqueta)}</div>
          <div style='font-size:10px;color:{color_estado};margin-top:4px'>{estado}</div>
        </div>
        {entrada}{salida}
      </div>"""


def _tabla_operaciones_virtuales(
    operaciones: list[dict[str, Any]], ventana_dias: int
) -> str:
    if not operaciones:
        return (
            f"<div style='font-size:12px;color:#5f6368;margin-top:10px'>"
            f"No hubo entradas ni salidas virtuales durante los últimos {ventana_dias} días.</div>"
        )
    filas = []
    for operacion in sorted(operaciones, key=lambda item: item["fecha_entrada"], reverse=True):
        lado = operacion["lado"]
        color_entrada, fondo_entrada, _, simbolo_entrada = _estilo_senal(lado, "ENTRAR")
        color_salida, fondo_salida, _, simbolo_salida = _estilo_senal(lado, "SALIR")
        parametros_entrada = "; ".join(
            f"{'✓' if item['cumple'] else '✗'} {item['detalle']}"
            for item in operacion["parametros_entrada"]
        )
        if operacion["fecha_salida"] is not None:
            activadores = ", ".join(operacion["activadores_salida"])
            salida = (
                f"<span style='display:inline-block;color:{color_salida};background:{fondo_salida};"
                f"font-weight:900;padding:3px 7px;border-radius:10px'>"
                f"{simbolo_salida} SALIR {operacion['fecha_salida']:%d/%m/%Y}</span> a "
                f"{operacion['precio_salida']:,.2f}<br><small>Activó: {escape(activadores)}</small>"
            )
            parametros_salida = "; ".join(
                f"{'✓' if item['cumple'] else '✗'} {item['detalle']}"
                for item in operacion["parametros_salida"]
            )
        else:
            salida = "<span style='color:#174ea6;font-weight:800'>ABIERTA</span>"
            parametros_salida = "Aún no se ha activado una salida."
        filas.append(
            "<tr>"
            f"<td><b>{escape(operacion['lado'])}</b></td><td>{escape(operacion['sistema'])}</td>"
            f"<td><span style='display:inline-block;color:{color_entrada};background:{fondo_entrada};"
            f"font-weight:900;padding:3px 7px;border-radius:10px'>{simbolo_entrada} ENTRAR "
            f"{operacion['fecha_entrada']:%d/%m/%Y}</span> a {operacion['precio_entrada']:,.2f}</td>"
            f"<td>{escape(parametros_entrada)}</td><td>{salida}</td>"
            f"<td>{escape(parametros_salida)}</td>"
            "</tr>"
        )
    return f"""
      <div style='font-size:13px;font-weight:800;margin:12px 0 5px'>
        Operaciones virtuales relacionadas con los últimos {ventana_dias} días
      </div>
      <div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:11px'>
        <thead><tr style='background:#f1f3f4;text-align:left'>
          <th style='padding:5px'>Lado</th><th>Sistema</th><th>Entrada virtual</th>
          <th>Valores de entrada</th><th>Salida virtual</th><th>Valores de salida</th>
        </tr></thead><tbody>{''.join(filas)}</tbody>
      </table></div>"""


def _tarjeta_virtual(
    ticker: str,
    estrategias: list[dict[str, Any]],
    operaciones: list[dict[str, Any]],
    ventana_dias: int,
    fecha: pd.Timestamp,
    cierre: float,
    datos_alerta: dict[str, Any],
) -> str:
    alerta = _alerta_resultados(ticker, fecha, datos_alerta)
    paneles = "".join(_panel_estrategia_virtual(estrategia) for estrategia in estrategias)
    tabla = _tabla_operaciones_virtuales(operaciones, ventana_dias)
    return f"""
    <section style='border:1px solid #d8dee4;border-radius:8px;padding:13px 15px;
                    margin:10px 0 26px;font-family:Arial;background:#fff'>
      <div style='font-size:21px;font-weight:800'>
        {escape(ticker)} — Señales — {fecha:%d-%m-%Y}
      </div>
      <div style='font-size:12px;color:#5f6368;margin-top:2px'>
        Estrategias virtuales independientes
      </div>
      <div style='font-size:14px;font-weight:700;color:{alerta["color"]};margin:4px 0 9px'>
        Alerta resultados: {escape(alerta["mensaje"])}.
      </div>
      {paneles}
      {tabla}
      <div style='font-size:12px;color:#5f6368;margin-top:9px'>
        Cierre analizado: {fecha:%Y-%m-%d} a {cierre:,.2f}. LONG, SHORT RSI y SHORT ruptura
        evolucionan por separado. Cada SALIR pertenece a la entrada virtual de su mismo sistema.
      </div>
    </section>"""


def generar_panel_senales(
    activos: Iterable[str],
    *,
    fecha_fin: str | pd.Timestamp | None = None,
    ajustar_dividendos: bool = True,
    criterios_long: Iterable[str] = ("RSI_SOBREVENTA", "BOLLINGER_INFERIOR"),
    criterios_salida_long: Iterable[str] = (
        "RSI_RECUPERADO", "BOLLINGER_MEDIA", "STOP_PROTECCION"
    ),
    sistemas_short: Iterable[str] = ("RSI", "RUPTURA"),
    criterios_short_rsi: Iterable[str] = ("RSI_SOBRECOMPRA",),
    criterios_short_ruptura: Iterable[str] = ("RUPTURA_MINIMO", "VOLUMEN_RUPTURA"),
    criterios_salida_short_rsi: Iterable[str] = ("RSI_NORMALIZADO", "STOP_PROTECCION"),
    criterios_salida_short_ruptura: Iterable[str] = (
        "DOS_CIERRES_EMA", "RSI_FIN_RUPTURA", "MAX_SESIONES", "TRAILING_ATR"
    ),
    rsi_periodo: int = 14,
    umbral_rsi_long: float = 30,
    bollinger_periodo: int = 20,
    bollinger_desviaciones: float = 2.0,
    umbral_salida_long_rsi: float = 65,
    umbral_rsi_short: float = 70,
    umbral_salida_short_rsi: float = 50,
    umbral_fin_ruptura_rsi: float = 55,
    periodo_ruptura: int = 20,
    multiplicador_volumen: float = 1.0,
    ema_short_periodo: int = 10,
    atr_periodo: int = 14,
    stop_long: float = 0.07,
    stop_short: float = 0.07,
    ruptura_atr: float = 2.0,
    ruptura_max_sesiones: int = 20,
    ventana_senales_dias: int = 30,
    fechas_resultados: dict[str, Iterable[Any]] | None = None,
    datos_alerta_resultados: dict[str, Any] | None = None,
    filas_calendario_eventos: Iterable[dict[str, Any]] | None = None,
    carpeta_salida: str | Path = "salida_operativa",
    mostrar: bool = True,
) -> dict[str, Any]:
    """Simula señales LONG/SHORT independientes sin consultar posiciones reales."""
    warnings.filterwarnings("ignore")
    if not mostrar:
        # Las validaciones automáticas y servidores sin escritorio no deben
        # intentar abrir el backend gráfico Tk. Jupyter mantiene su backend
        # interactivo cuando ``mostrar=True``.
        plt.switch_backend("Agg")
    criterios_long = _lista_unica(criterios_long, "CRITERIOS_LONG", CRITERIOS_LONG_VALIDOS)
    criterios_salida_long = _lista_unica(
        criterios_salida_long, "CRITERIOS_SALIDA_LONG", CRITERIOS_SALIDA_LONG_VALIDOS
    )
    sistemas_short = _lista_unica(sistemas_short, "SISTEMAS_SHORT_ACTIVOS", SISTEMAS_SHORT_VALIDOS)
    criterios_short_rsi = _lista_unica(
        criterios_short_rsi, "CRITERIOS_SHORT_RSI", CRITERIOS_SHORT_RSI_VALIDOS
    )
    criterios_short_ruptura = _lista_unica(
        criterios_short_ruptura,
        "CRITERIOS_SHORT_RUPTURA",
        CRITERIOS_SHORT_RUPTURA_VALIDOS,
    )
    criterios_salida_short_rsi = _lista_unica(
        criterios_salida_short_rsi,
        "CRITERIOS_SALIDA_SHORT_RSI",
        CRITERIOS_SALIDA_SHORT_RSI_VALIDOS,
    )
    criterios_salida_short_ruptura = _lista_unica(
        criterios_salida_short_ruptura,
        "CRITERIOS_SALIDA_SHORT_RUPTURA",
        CRITERIOS_SALIDA_SHORT_RUPTURA_VALIDOS,
    )
    if not criterios_long:
        raise ValueError("CRITERIOS_LONG debe contener al menos un criterio activo.")
    if not criterios_salida_long:
        raise ValueError("CRITERIOS_SALIDA_LONG debe contener al menos un criterio activo.")
    if not sistemas_short:
        raise ValueError("SISTEMAS_SHORT_ACTIVOS debe contener al menos un sistema activo.")
    if "RSI" in sistemas_short and not criterios_short_rsi:
        raise ValueError("Activa al menos un criterio RSI o comenta el sistema SHORT 'RSI'.")
    if "RSI" in sistemas_short and not criterios_salida_short_rsi:
        raise ValueError("Activa una salida RSI o comenta el sistema SHORT 'RSI'.")
    if "RUPTURA" in sistemas_short and not criterios_short_ruptura:
        raise ValueError("Activa al menos un criterio de ruptura o comenta el sistema SHORT 'RUPTURA'.")
    if "RUPTURA" in sistemas_short and not criterios_salida_short_ruptura:
        raise ValueError("Activa una salida de ruptura o comenta el sistema SHORT 'RUPTURA'.")
    if min(rsi_periodo, bollinger_periodo, periodo_ruptura, ema_short_periodo, atr_periodo) < 2:
        raise ValueError("Los periodos de indicadores deben ser iguales o superiores a 2.")
    if bollinger_desviaciones <= 0 or multiplicador_volumen <= 0 or ruptura_atr <= 0:
        raise ValueError("Los multiplicadores deben ser positivos.")
    if not (0 < stop_long < 1 and 0 < stop_short < 1):
        raise ValueError("STOP_LONG y STOP_SHORT deben estar expresados entre 0 y 1.")
    if ruptura_max_sesiones < 1:
        raise ValueError("RUPTURA_MAX_SESIONES debe ser igual o superior a 1.")
    if ventana_senales_dias < 1:
        raise ValueError("VENTANA_SENALES_DIAS debe ser igual o superior a 1.")

    tickers, datos_raw, ultima_fecha = _descargar_datos(activos, fecha_fin, ajustar_dividendos)
    datos = {
        ticker: _preparar_indicadores(
            tabla.loc[tabla.index <= ultima_fecha],
            rsi_periodo,
            bollinger_periodo,
            bollinger_desviaciones,
            periodo_ruptura,
            ema_short_periodo,
            atr_periodo,
        )
        for ticker, tabla in datos_raw.items()
    }
    configuracion = {
        "criterios_long": criterios_long,
        "criterios_salida_long": criterios_salida_long,
        "criterios_short_rsi": criterios_short_rsi,
        "criterios_short_ruptura": criterios_short_ruptura,
        "criterios_salida_short_rsi": criterios_salida_short_rsi,
        "criterios_salida_short_ruptura": criterios_salida_short_ruptura,
        "rsi_periodo": rsi_periodo,
        "umbral_rsi_long": umbral_rsi_long,
        "bollinger_periodo": bollinger_periodo,
        "bollinger_desviaciones": bollinger_desviaciones,
        "umbral_salida_long_rsi": umbral_salida_long_rsi,
        "umbral_rsi_short": umbral_rsi_short,
        "umbral_salida_short_rsi": umbral_salida_short_rsi,
        "umbral_fin_ruptura_rsi": umbral_fin_ruptura_rsi,
        "periodo_ruptura": periodo_ruptura,
        "multiplicador_volumen": multiplicador_volumen,
        "ema_short_periodo": ema_short_periodo,
        "atr_periodo": atr_periodo,
        "stop_long": stop_long,
        "stop_short": stop_short,
        "ruptura_atr": ruptura_atr,
        "ruptura_max_sesiones": ruptura_max_sesiones,
    }
    estados: dict[str, list[dict[str, Any]]] = {}
    historiales: dict[str, list[dict[str, Any]]] = {}
    operaciones_ventana: dict[str, list[dict[str, Any]]] = {}
    for ticker in tickers:
        tabla = datos[ticker]
        estrategias = [
            _simular_estrategia(
                tabla, "LONG", "RSI + BOLLINGER", _entrada_long, _salida_long, configuracion
            )
        ]
        if "RSI" in sistemas_short:
            estrategias.append(
                _simular_estrategia(
                    tabla, "SHORT", "RSI", _entrada_short_rsi, _salida_short_rsi, configuracion
                )
            )
        if "RUPTURA" in sistemas_short:
            estrategias.append(
                _simular_estrategia(
                    tabla, "SHORT", "RUPTURA", _entrada_short_ruptura,
                    _salida_short_ruptura, configuracion, _actualizar_ruptura
                )
            )
        estados[ticker] = estrategias
        inicio_ventana = tabla.index[-1] - pd.Timedelta(days=ventana_senales_dias)
        todos_eventos = [evento for estrategia in estrategias for evento in estrategia["eventos"]]
        historiales[ticker] = [
            evento for evento in todos_eventos if evento["fecha"] >= inicio_ventana
        ]
        todas_operaciones = [
            operacion for estrategia in estrategias for operacion in estrategia["operaciones"]
        ]
        operaciones_ventana[ticker] = [
            operacion
            for operacion in todas_operaciones
            if operacion["fecha_entrada"] >= inicio_ventana
            or (
                operacion["fecha_salida"] is not None
                and operacion["fecha_salida"] >= inicio_ventana
            )
            or operacion["estado"] == "ABIERTA"
        ]

    fechas_resultados = fechas_resultados or {}
    datos_alerta_resultados = datos_alerta_resultados or {}
    salida = Path(carpeta_salida)
    salida.mkdir(parents=True, exist_ok=True)
    tarjetas = []
    if mostrar:
        print(f"PANEL DE SEÑALES V2.5 — cierre {ultima_fecha.date()}")
    for ticker in tickers:
        tabla = datos[ticker]
        fin = tabla.index[-1]
        eventos_grafico = _eventos_para_graficos(ticker, filas_calendario_eventos)
        fig, axes = plt.subplots(1, 3, figsize=(25, 7.5))
        _dibujar_periodo(
            axes[0], tabla, fin - relativedelta(months=1), "Último mes",
            senales=historiales[ticker], eventos_calendario=eventos_grafico
        )
        _dibujar_periodo(
            axes[1], tabla, fin - relativedelta(months=3), "Últimos 3 meses",
            senales=historiales[ticker], eventos_calendario=eventos_grafico
        )
        _dibujar_periodo(
            axes[2], tabla, fin - relativedelta(months=6), "Últimos 6 meses",
            senales=historiales[ticker], eventos_calendario=eventos_grafico
        )
        fig.suptitle(f"{ticker} — precio y volumen hasta {fin:%Y-%m-%d}", fontsize=17, fontweight="bold")
        fig.subplots_adjust(left=0.045, right=0.97, top=0.88, bottom=0.20, wspace=0.28)
        ruta_grafico = salida / f"{ticker}_panel_v2_5.png"
        fig.savefig(ruta_grafico, dpi=170, bbox_inches="tight")
        plt.close(fig)
        if mostrar:
            display(HTML(_grafico_ampliable(ruta_grafico, ticker)))
        tarjeta = _tarjeta_virtual(
            ticker, estados[ticker], operaciones_ventana[ticker], ventana_senales_dias,
            fin, float(tabla["Close"].iloc[-1]), datos_alerta_resultados
        )
        tarjetas.append(tarjeta)
        if mostrar:
            display(HTML(tarjeta))

    resumen = f"""<!doctype html><html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Panel de señales V2.5</title></head><body style='font-family:Arial;max-width:1500px;margin:auto'>
<h2>Panel de señales V2.5</h2><p>Cierre analizado: <b>{ultima_fecha.date()}</b></p>
{''.join(tarjetas)}
<p style='font-size:12px;color:#666'>Cada sistema mantiene su propia operación virtual, independiente
de las demás y de las posiciones reales. El informe conserva las señales relacionadas con los últimos
{ventana_senales_dias} días y no transmite órdenes.</p>
</body></html>"""
    archivo_html = salida / "resumen_senales_v2_5.html"
    archivo_html.write_text(resumen, encoding="utf-8")
    if mostrar:
        print("Resumen V2.5 guardado en:", archivo_html.resolve())
    return {
        "tickers": tickers,
        "datos": datos,
        "senales": estados,
        "historial_senales": historiales,
        "operaciones_virtuales": operaciones_ventana,
        "ultima_fecha_comun": ultima_fecha,
        "archivo_html": archivo_html.resolve(),
    }


__all__ = ["generar_panel_senales"]
