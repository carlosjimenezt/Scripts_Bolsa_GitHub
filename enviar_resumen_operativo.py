"""Envía por SMTP el resumen producido por el notebook operativo.

Las credenciales se reciben exclusivamente mediante variables de entorno.
Está pensado para GitHub Actions y no guarda contraseñas en el repositorio.
"""

import json
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path


def obligatorio(nombre):
    valor = os.environ.get(nombre, '').strip()
    if not valor:
        raise RuntimeError(f'Falta el secreto/variable {nombre}.')
    return valor


smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com').strip()
smtp_port = int(os.environ.get('SMTP_PORT', '465'))
usuario = obligatorio('SMTP_USERNAME')
password = obligatorio('SMTP_PASSWORD')
destinatario = obligatorio('EMAIL_TO')

carpeta = Path('salida_operativa')
ruta_resumen = carpeta / 'resumen_operativo.html'
ruta_calendario = carpeta / 'calendario_resultados' / 'informe_resultados.html'
ruta_estado = carpeta / 'calendario_resultados' / 'estado_fechas_v2_4.json'

if not ruta_resumen.exists() or not ruta_calendario.exists() or not ruta_estado.exists():
    faltan = [
        str(ruta) for ruta in (ruta_resumen, ruta_calendario, ruta_estado)
        if not ruta.exists()
    ]
    raise RuntimeError('No se generaron los informes esperados: ' + ', '.join(faltan))


def contenido_body(documento):
    coincidencia = re.search(r'<body[^>]*>([\s\S]*?)</body>', documento, re.IGNORECASE)
    return coincidencia.group(1) if coincidencia else documento


def estilos(documento):
    return '\n'.join(re.findall(r'<style[^>]*>[\s\S]*?</style>', documento, re.IGNORECASE))


resumen = ruta_resumen.read_text(encoding='utf-8')
calendario = ruta_calendario.read_text(encoding='utf-8')
estado = json.loads(ruta_estado.read_text(encoding='utf-8'))
valores_seleccionados = list(estado)
html = f'''<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {estilos(calendario)}
</head>
<body style="font-family:Arial,sans-serif">
  {contenido_body(resumen)}
  <hr style="border:0;border-top:1px solid #dadce0;margin:28px 0">
  {contenido_body(calendario)}
</body>
</html>'''

graficos = [carpeta / f'{ticker}_panel.png' for ticker in valores_seleccionados]
graficos_faltantes = [str(grafico) for grafico in graficos if not grafico.exists()]
if graficos_faltantes:
    raise RuntimeError('No se generaron los gráficos esperados: ' + ', '.join(graficos_faltantes))
valores = ', '.join(valores_seleccionados)

mensaje = EmailMessage()
mensaje['Subject'] = f'Panel operativo diario{": " + valores if valores else ""}'
mensaje['From'] = usuario
mensaje['To'] = destinatario
mensaje.set_content(
    'Tu cliente de correo no admite HTML. Consulta el calendario y los gráficos adjuntos.'
)
mensaje.add_alternative(html, subtype='html')

for grafico in graficos:
    mensaje.add_attachment(
        grafico.read_bytes(), maintype='image', subtype='png', filename=grafico.name
    )

mensaje.add_attachment(
    ruta_calendario.read_bytes(),
    maintype='text', subtype='html', filename='calendario_resultados.html'
)

with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as servidor:
    servidor.login(usuario, password)
    servidor.send_message(mensaje)

print(f'Correo enviado a {destinatario}.')
