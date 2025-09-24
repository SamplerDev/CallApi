import os
import json
import logging
from fastapi import FastAPI, Request, Response, HTTPException
from dotenv import load_dotenv

# --- Configuración Inicial ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Cargar el token de verificación desde el archivo .env
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

# Inicializar la aplicación FastAPI
app = FastAPI()

# --- Endpoint para la Verificación del Webhook (GET) ---
@app.get("/webhook")
def verify_webhook(request: Request):
    """
    Este endpoint es usado una sola vez por Meta para verificar que la URL es tuya.
    Responde al "challenge" que Meta envía.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("¡WEBHOOK VERIFICADO CON ÉXITO!")
        return Response(content=challenge, status_code=200)
    else:
        logging.error("Fallo en la verificación del Webhook. Tokens no coinciden.")
        raise HTTPException(status_code=403, detail="Verification failed. Invalid token.")

# --- Endpoint para Recibir Notificaciones de Llamadas (POST) ---
@app.post("/webhook")
async def receive_call_notification(request: Request):
    """
    Este es el endpoint principal. Meta enviará aquí todas las notificaciones
    de eventos de llamada a través de una solicitud POST.
    """
    body = await request.json()
    
    # Usamos logging para una mejor visualización en la consola
    logging.info("--- NUEVO WEBHOOK RECIBIDO ---")
    # Imprimimos el cuerpo del webhook de forma legible (pretty-print)
    logging.info(json.dumps(body, indent=2))

    # Aquí es donde extraes la información y actúas en el futuro.
    # Por ahora, solo confirmamos que lo recibimos.
    try:
        # Intentamos extraer el evento de llamada para un log más específico
        call_event = body["entry"][0]["changes"][0]["value"]["calls"][0]["event"]
        call_id = body["entry"][0]["changes"][0]["value"]["calls"][0]["id"]
        logging.info(f"Evento de llamada detectado: '{call_event}' para el Call ID: {call_id}")
    except (KeyError, IndexError):
        logging.warning("El webhook recibido no parece ser un evento de llamada estándar.")

    # Siempre debemos responder con un status 200 OK para que Meta sepa
    # que recibimos la notificación correctamente.
    return Response(status_code=200)