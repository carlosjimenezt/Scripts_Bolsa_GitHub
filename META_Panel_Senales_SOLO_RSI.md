# Panel diario para TradingView

Archivo: `META_Panel_Senales_SOLO_RSI.pine` (Pine Script v6).

## Instalación y alerta

1. Abrir el gráfico del activo, por ejemplo NASDAQ:META, con velas normales de 1 día y sesión regular. Usar los mismos ajustes de precios que los datos sin ajustar del notebook si se quiere comparar.
2. Abrir el Editor de Pine, crear un indicador y pegar el contenido completo del archivo `.pine`.
3. Guardar y añadir al gráfico. El panel aparece en un espacio propio; ampliar ese espacio para leer las columnas.
4. Crear una alerta y elegir este indicador y **Any alert() function call / Cualquier llamada a la función alert()**.
5. Activar la notificación deseada. El código fija la frecuencia al cierre diario.

No se han creado alertas en tu cuenta ni conectado órdenes de IBKR. El archivo requiere compilarse en el Editor de Pine; no se ha validado con el compilador de TradingView en este entorno.

Las alertas se repiten cada cierre que cumpla condiciones. Un único mensaje recoge todas las coincidencias: no se pierde SALIDA LONG cuando también se cumple ENTRADA SHORT. No comprueban posiciones ni envían órdenes. Las salidas EXTRA son condiciones independientes, no afirmaciones de que exista un EXTRA abierto. En el motor del notebook, la prioridad entre salidas puede hacer que solo se ejecute una.

Cada entrada SI incluye INSTRUCCIONES: ejecución en apertura siguiente, lado de la orden stop, nivel orientativo con el cierre de la señal y fórmula para recalcularlo con el precio real de entrada. LONG: stop de venta a entrada × 0,93 y salida RSI >= 65. SHORT RSI/ruptura: stop de compra a entrada × 1,07 y salida RSI < 50. SHORT EXTRA: stop de compra a entrada × 1,05, gestión manual del trailing y salidas propias del módulo. El nivel orientativo se redondea al tick del símbolo; no conoce el precio de la próxima apertura. La salida por RSI no garantiza ganancias. Los avisos CERCA no incluyen instrucciones de entrada, porque la señal aún no se ha cumplido.

Se recuerda cancelar el stop pendiente si se cierra por otra vía y no repetir el cierre si el stop ya se ejecutó. Sin estado, el indicador no calcula el trailing de una operación ni su vencimiento por sesiones; en EXTRA, el contador del motor llega a 15 en la decimoquinta sesión posterior a la entrada.

Después de cambiar código o ajustes, recrear la alerta: TradingView conserva una copia de la configuración con la que se creó.

## Columnas y colores

El panel separa las columnas en tres franjas horizontales: indicadores, entradas y salidas. Muestra el último cierre confirmado; durante la sesión no presenta señales provisionales.

- **SI, verde:** condición cumplida exactamente.
- **CERCA, naranja:** no se cumple, pero todos sus requisitos numéricos cumplen o están dentro de los márgenes. Las restricciones lógicas (rebote previo y exclusión de SHORT base para EXTRA) deben cumplirse.
- **NO, negro:** no cumple ni está cerca.
- **N/D, gris:** falta historial o la condición requiere datos de una operación.

Márgenes configurables: 1 punto RSI, 1 % de precio respecto al nivel de referencia y 5 % de déficit de volumen respecto al volumen exigido. Los avisos CERCA se activan por separado; por defecto solo se notifican SI.

Ejemplos: salida LONG con RSI 64 = CERCA; 65 y 69 = SI. Entrada SHORT con RSI 69 o 70 = CERCA; requiere superar 70 para SI. Entrada LONG con RSI 30 = CERCA si Bollinger también cumple o está cerca; exige RSI estrictamente menor de 30 para SI.

## Reglas reproducidas

| Entrada | Condición |
|---|---|
| LONG | RSI < 30 y cierre <= banda inferior |
| SHORT RSI | RSI > 70 |
| SHORT ruptura | Cierre < mínimo de las 20 sesiones previas y volumen > media de volumen de esas sesiones × multiplicador; interruptor activo |
| SHORT EXTRA | Cierre < mínimo de las 10 sesiones previas, algún cierre > EMA10 en las 5 sesiones previas, cierre < SMA50, SMA20 < SMA50, volumen > media previa de 20 y ninguna entrada SHORT base |

| Salida | Condición |
|---|---|
| LONG RSI | RSI >= 65 |
| SHORT base RSI | RSI < 50 |
| EXTRA RSI | RSI < 35 |
| EXTRA EMA10 | Dos cierres consecutivos > su respectiva EMA10 |
| EXTRA prioridad | Señal LONG o SHORT base |
| Stops LONG / SHORT | 7 % desde entrada: N/D sin operación |
| EXTRA stop / trailing | Stop 5 % y trailing de mínimo de cierres desde entrada + 2 ATR, sin elevar el trailing: N/D sin operación |
| EXTRA tiempo | 15 sesiones contabilizadas por el motor: N/D sin operación |
| Fin del backtest | Cierre contable del periodo: N/D, no genera alerta de mercado |

RSI: medias simples de subidas y bajadas de 14 cambios. Bollinger: SMA20 menos 2 desviaciones muestrales. EMA10 y ATR14 utilizan actualización recursiva inicializada en el primer dato, como `ewm(adjust=False)` del notebook; un inicio distinto del historial puede provocar diferencias de calentamiento. Los datos de Yahoo y TradingView también pueden diferir.

Los indicadores se evalúan al cierre. El notebook ejecuta las señales en la apertura siguiente. Los stops reales se gestionan intradía y no se sustituyen por estas alertas diarias.

Referencias: https://www.tradingview.com/pine-script-docs/concepts/alerts/ y https://www.tradingview.com/pine-script-docs/visuals/tables/
