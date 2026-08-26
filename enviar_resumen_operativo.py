"""Envía por SMTP el resumen producido por el notebook operativo.

Las credenciales se reciben exclusivamente mediante variables de entorno.
Está pensado para GitHub Actions y no guarda contraseñas en el repositorio.
"""

import os
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
html = (carpeta / 'resumen_operativo.html').read_text(encoding='utf-8')

mensaje = EmailMessage()
mensaje['Subject'] = 'Panel operativo diario META y SHOP'
mensaje['From'] = usuario
mensaje['To'] = destinatario
mensaje.set_content('Tu cliente de correo no admite HTML. Consulta el resumen adjunto.')
mensaje.add_alternative(html, subtype='html')

for grafico in sorted(carpeta.glob('*_panel.png')):
    mensaje.add_attachment(
        grafico.read_bytes(), maintype='image', subtype='png', filename=grafico.name
    )

with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as servidor:
    servidor.login(usuario, password)
    servidor.send_message(mensaje)

print(f'Correo enviado a {destinatario}.')
