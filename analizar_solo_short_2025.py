"""Backtest SHORT desde 2025 con el motor y parámetros del notebook SOLO_RSI.

Escribe un informe HTML y una instantánea reproducible de datos/código.
No modifica el notebook original ni coloca órdenes.
"""
import ast
import hashlib
import html
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
SOURCE = next(ROOT.glob('*Salida_LONG_SOLO_RSI.ipynb'))
OUT = ROOT / 'resultados_solo_short_2025'
OUT.mkdir(exist_ok=True)
nb = json.loads(SOURCE.read_text(encoding='utf-8'))
env = {'pd': pd, 'np': np}
exec(''.join(nb['cells'][1]['source']), env)
for i in [3, 4]:
    tree = ast.parse(''.join(nb['cells'][i]['source']))
    funcs = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)], type_ignores=[])
    exec(compile(funcs, str(SOURCE), 'exec'), env)
today = pd.Timestamp(datetime.now(ZoneInfo('Europe/Madrid')).date())
start = pd.Timestamp('2025-01-01')
raw = {}
for ticker, _ in env['ACTIVOS']:
    # Excluye la sesión de hoy para no mezclar una vela parcial con cierres.
    d = yf.download(ticker, start=today - pd.DateOffset(years=5), end=today,
                    auto_adjust=False, progress=False, threads=False)
    if d.empty:
        raise RuntimeError(f'Sin datos de {ticker}')
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d[['Open', 'High', 'Low', 'Close', 'Volume']].dropna().copy()
    d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
    raw[ticker] = d.loc[~d.index.duplicated(keep='last')].sort_index()
end = max(set.intersection(*(set(d.index) for d in raw.values())))
summary, trades, isolated = [], [], []


def run(d, cash):
    d = d.copy()
    # LONG tampoco puede forzar el cierre de un EXTRA.
    d['LONG_ENTRY'] = False
    d['LONG_EXIT'] = False
    window = d.loc[(d.index >= start) & (d.index <= end)].copy()
    if len(window) < 2:
        raise RuntimeError('Historial insuficiente')
    # Misma convención del notebook: la primera fila inicia la ventana,
    # las primeras órdenes posibles están en la siguiente sesión.
    result = env['ejecutar_backtest'](window, cash)
    assert all(o['Tipo'] == 'SHORT' for o in result['Operaciones'])
    capital = cash
    enriched = []
    for number, op in enumerate(result['Operaciones'], 1):
        i = window.index.get_loc(op['Entrada'])
        prev = window.iloc[i - 1]
        pnl = capital * op['Rentabilidad %'] / 100
        capital += pnl
        enriched.append({'Num. Op.': number, **op,
                         'RSI entrada': prev['RSI'],
                         'BB inferior entrada': prev['BB_LOWER'],
                         'Días naturales': (op['Salida'] - op['Entrada']).days,
                         'Beneficio $': pnl, 'Capital tras salida $': capital})
    assert np.isclose(capital, result['Capital final'])
    # Drawdown incluyendo el capital inicial como primer máximo posible.
    eq = pd.concat([pd.Series([cash]), result['Equity']['Equity'].reset_index(drop=True)], ignore_index=True)
    drawdown = (eq / eq.cummax() - 1).min() * 100
    return result, enriched, drawdown


for ticker, cash in env['ACTIVOS']:
    full = raw[ticker].loc[:end]
    full.to_json(OUT / f'{ticker}_ohlcv.json', orient='table', date_format='iso', indent=2)
    d = env['preparar_senales'](full)
    result, ops, dd = run(d, cash)
    summary.append({'Ticker': ticker, 'Desde': result['Fecha inicio'], 'Hasta': end,
                    'Capital inicial $': cash, 'Capital final $': result['Capital final'],
                    'Rentabilidad %': result['Rentabilidad %'], 'Drawdown %': dd,
                    'Operaciones': len(ops), 'Aciertos %': result['% Aciertos']})
    trades.extend({'Ticker': ticker, **o} for o in ops)
    # Ensayos separados, cada uno desde el mismo capital inicial.
    # EXTRA vuelve a ser elegible cuando los otros sistemas están desactivados.
    for kind in ['RSI > 70', 'RUPTURA', 'EXTRA CONTINUACIÓN']:
        test = d.copy()
        if kind == 'RSI > 70':
            test['SHORT_ENTRY'] = test['SHORT_RSI']
            test['SHORT_RUPTURA'] = False
            test['SHORT_EXTRA'] = False
        elif kind == 'RUPTURA':
            test['SHORT_ENTRY'] = test['SHORT_RUPTURA']
            test['SHORT_EXTRA'] = False
        else:
            test['SHORT_ENTRY'] = False
            test['SHORT_RUPTURA'] = False
            test['SHORT_EXTRA'] = (
                (test['Close'] < test['MINIMO_EXTRA_PREV']) & test['REBOTE_RECIENTE']
                & (test['Close'] < test['SMA50']) & (test['SMA20'] < test['SMA50'])
                & (test['Volume'] > test['VOL_20_PREV']))
        r, separate_ops, drawdown = run(test, cash)
        isolated.append({'Ticker': ticker, 'Sistema': kind,
                         'Rentabilidad %': r['Rentabilidad %'], 'Drawdown %': drawdown,
                         'Operaciones': len(separate_ops), 'Aciertos %': r['% Aciertos']})

detail = pd.DataFrame(trades)
assert not detail.empty
detail = detail.drop(columns=['Es original'])
group_rows = []
for (ticker, system), g in detail.groupby(['Ticker', 'Sistema entrada']):
    returns = g['Rentabilidad %']
    gains = g.loc[g['Beneficio $'] > 0, 'Beneficio $'].sum()
    losses = -g.loc[g['Beneficio $'] < 0, 'Beneficio $'].sum()
    group_rows.append({'Ticker': ticker, 'Sistema': system, 'Operaciones': len(g),
                       'Aciertos %': (returns > 0).mean() * 100,
                       'Media por operación %': returns.mean(),
                       'Mediana %': returns.median(),
                       'Mejor %': returns.max(), 'Peor %': returns.min(),
                       'Beneficio $': g['Beneficio $'].sum(),
                       'Factor beneficio': gains / losses if losses else np.nan,
                       'Cierres fin periodo': (g['Motivo'] == 'FIN DEL PERIODO').sum()})
groups = pd.DataFrame(group_rows)
best = detail.sort_values('Rentabilidad %', ascending=False).head(10)
worst = detail.sort_values('Rentabilidad %').head(10)
tables = {'Resumen': pd.DataFrame(summary), 'Tipos en estrategia combinada': groups,
          'Sistemas ejecutados por separado': pd.DataFrame(isolated),
          'Mejores operaciones': best, 'Peores operaciones': worst,
          'Todas las operaciones': detail}
meta = {'notebook': SOURCE.name, 'sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'fecha_ejecucion': str(today.date()), 'inicio_solicitado': str(start.date()),
        'ultima_sesion_comun': str(end.date()),
        'parametros': {k: v for k, v in env.items() if k.isupper()},
        'tablas': {name: json.loads(df.to_json(orient='records', date_format='iso')) for name, df in tables.items()}}
(OUT / 'resultados.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
(OUT / 'codigo_fuente_notebook.json').write_text(json.dumps(
    {str(i): ''.join(nb['cells'][i]['source']) for i in [1, 3, 4]}, ensure_ascii=False, indent=2), encoding='utf-8')
notes = f'''<h1>Solo SHORT · desde enero de 2025</h1>
<p>Datos Yahoo Finance sin ajustar. Última sesión común: <b>{end.date()}</b>.
10.000 dólares iniciales por activo. Sin entradas LONG ni cierres EXTRA provocados por señales LONG.
Primera sesión de la ventana: {summary[0]['Desde'].date()}; primera orden posible en la sesión siguiente,
como en el notebook original. No se arrastran posiciones anteriores.</p>
<p>SHORT RSI y ruptura: stop 7 %, salida RSI &lt; 50. EXTRA: stop 5 %, trailing 2 ATR,
salida RSI &lt; 35, dos cierres sobre EMA10, 15 sesiones o prioridad SHORT base.
Señal al cierre y ejecución en apertura siguiente; deslizamiento adverso de 0,05 % por ejecución y stops con gaps.
Sin comisiones, financiación, préstamo de acciones, dividendos adeudados por los cortos ni spread real.</p>
<p>Los tipos de entrada de la estrategia combinada compiten por una única posición por activo.
El beneficio en dólares de cada tipo suma sus aportaciones reales al capital; no equivale a ejecutarlo solo.
Si coinciden RSI y ruptura, el motor etiqueta RUPTURA. Los ensayos separados recalculan la elegibilidad de EXTRA.
Los cierres FIN DEL PERIODO son valoraciones forzadas, no salidas por señal.
El drawdown incluye el capital inicial. Las mejores operaciones se ordenan por porcentaje ganado,
no por dólares. Esta clasificación describe esta muestra histórica.</p>'''
sections = []
for name, df in tables.items():
    display = df.copy()
    for col in display.columns:
        if pd.api.types.is_datetime64_any_dtype(display[col]):
            display[col] = display[col].dt.strftime('%Y-%m-%d')
    sections.append('<h2>' + html.escape(name) + '</h2><div class="tabla">' +
                    display.to_html(index=False, border=0, float_format=lambda x: f'{x:,.2f}', na_rep='—') + '</div>')
page = '''<!doctype html><html lang="es"><meta charset="utf-8"><title>Solo SHORT desde 2025</title>
<style>body{font:15px system-ui;margin:30px;color:#172333;background:#f5f7fa}h1{font-size:28px}p{max-width:1200px;line-height:1.6}table{border-collapse:collapse;background:white;white-space:nowrap}th,td{padding:10px 13px;border-bottom:1px solid #dce2e8;text-align:right}th{background:#172e48;color:white}tr:nth-child(even){background:#edf2f6}.tabla{overflow:auto;margin-bottom:30px}h2{margin-top:35px}</style>'''
(OUT / 'informe_solo_short.html').write_text(page + notes + ''.join(sections) + '</html>', encoding='utf-8')
for name in ['Resumen', 'Tipos en estrategia combinada', 'Sistemas ejecutados por separado', 'Mejores operaciones']:
    print('\n' + name)
    print(tables[name].to_string(index=False, float_format=lambda x: f'{x:.2f}'))
print('\nINFORME', OUT / 'informe_solo_short.html')
