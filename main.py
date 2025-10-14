import os
import json
import logging
import asyncio
from fastapi import FastAPI, Request, Response, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
import uvicorn

# --- Configuración Inicial ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Cargar credenciales
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
if not all([VERIFY_TOKEN, PHONE_NUMBER_ID, ACCESS_TOKEN]):
    raise ValueError("Faltan variables de entorno críticas.")

# --- Constantes y Configuración de la API ---
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/calls"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

# --- Almacenes de Estado en Memoria ---
active_calls = {}
connected_agents = {}

# --- Inicialización de la App FastAPI ---
app = FastAPI(title="WhatsApp WebRTC Bridge")

# --- Configuración de CORS ---
origins = [
    "http://localhost",
    "http://localhost:8000", # Para pruebas locales
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Funciones Auxiliares para la API de WhatsApp ---
async def send_call_action(call_id: str, action: str, sdp: str = None):
    payload = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
    if sdp:
        payload["session"] = {"sdp_type": "answer", "sdp": sdp}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(WHATSAPP_API_URL, json=payload, headers=HEADERS)
            response.raise_for_status()
            logging.info(f"Acción '{action}' enviada para la llamada {call_id}.")
            return response.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"Error en la acción '{action}' para {call_id}: {e.response.text}")
            return None

# --- Endpoint WebSocket para el Frontend ---
@app.websocket("/ws/{agent_id}")
async def websocket_endpoint(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    connected_agents[agent_id] = websocket
    logging.info(f"Agente '{agent_id}' conectado vía WebSocket.")
    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")
            call_id = data.get("call_id")

            if event_type == "offer_from_browser":
                logging.info(f"Recibida oferta SDP del navegador para la llamada {call_id}")
                session = active_calls.get(call_id)
                if session:
                    browser_sdp_offer = RTCSessionDescription(sdp=data["sdp"], type="offer")
                    await create_webrtc_bridge(session, browser_sdp_offer)
            
            elif event_type == "hangup_from_browser":
                logging.info(f"Agente colgó la llamada {call_id} desde el navegador.")
                if call_id in active_calls:
                    await send_call_action(call_id, "terminate")
                    # El webhook de terminación se encargará de la limpieza final

    except WebSocketDisconnect:
        logging.info(f"Agente '{agent_id}' desconectado.")
        del connected_agents[agent_id]

# --- Lógica WebRTC para el Puente de Audio ---
async def create_webrtc_bridge(session: dict, browser_sdp_offer: RTCSessionDescription):
    whatsapp_sdp_offer = session["whatsapp_sdp_offer"]
    call_id = session["call_id"]
    agent_websocket = session["agent_websocket"]

    whatsapp_pc = RTCPeerConnection()
    browser_pc = RTCPeerConnection()
    session["whatsapp_pc"] = whatsapp_pc
    session["browser_pc"] = browser_pc

    @whatsapp_pc.on("track")
    async def on_whatsapp_track(track):
        logging.info(f"Recibida pista de audio de WhatsApp para {call_id}. Añadiéndola al navegador.")
        browser_pc.addTrack(track)

    @browser_pc.on("track")
    async def on_browser_track(track):
        logging.info(f"Recibida pista de audio del navegador para {call_id}. Añadiéndola a WhatsApp.")
        whatsapp_pc.addTrack(track)

    await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)
    await browser_pc.setRemoteDescription(browser_sdp_offer)

    whatsapp_answer = await whatsapp_pc.createAnswer()
    browser_answer = await browser_pc.createAnswer()

    await whatsapp_pc.setLocalDescription(whatsapp_answer)
    await browser_pc.setLocalDescription(browser_answer)

    logging.info(f"Enviando respuesta SDP a la API de WhatsApp para {call_id}")
    await send_call_action(call_id, "pre_accept", whatsapp_pc.localDescription.sdp)
    await asyncio.sleep(1)
    await send_call_action(call_id, "accept", whatsapp_pc.localDescription.sdp)

    logging.info(f"Enviando respuesta SDP al navegador para {call_id}")
    await agent_websocket.send_json({
        "type": "answer_from_server",
        "sdp": browser_pc.localDescription.sdp
    })
    session["status"] = "active"

# --- Endpoint del Webhook de WhatsApp ---
@app.get("/webhook")
def verify_webhook(request: Request):
    # (Igual que antes)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed.")

@app.post("/webhook")
async def receive_call_notification(request: Request):
    body = await request.json()
    logging.info("--- WEBHOOK RECIBIDO ---")
    logging.info(json.dumps(body, indent=2))

    try:
        call_data = body["entry"][0]["changes"][0]["value"]["calls"][0]
        call_id = call_data["id"]
        event = call_data["event"]

        if event == "connect":
            agent_id_to_notify = "agent_001" # Lógica simple: notificar siempre al mismo agente
            agent_websocket = connected_agents.get(agent_id_to_notify)

            if agent_websocket:
                logging.info(f"Creando sesión para {call_id} y notificando al agente {agent_id_to_notify}")
                active_calls[call_id] = {
                    "status": "ringing",
                    "call_id": call_id,
                    "whatsapp_sdp_offer": RTCSessionDescription(sdp=call_data["session"]["sdp"], type="offer"),
                    "agent_websocket": agent_websocket
                }
                await agent_websocket.send_json({
                    "type": "incoming_call",
                    "call_id": call_id,
                    "from": body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido')
                })
            else:
                logging.warning(f"Llamada {call_id} recibida, pero el agente {agent_id_to_notify} no está conectado. Rechazando.")
                await send_call_action(call_id, "reject")

        elif event == "terminate":
            if call_id in active_calls:
                session = active_calls[call_id]
                if session.get("agent_websocket"):
                    await session["agent_websocket"].send_json({"type": "call_terminated", "call_id": call_id})
                if session.get("whatsapp_pc"): await session["whatsapp_pc"].close()
                if session.get("browser_pc"): await session["browser_pc"].close()
                del active_calls[call_id]
                logging.info(f"Sesión para {call_id} terminada y limpiada.")

    except (KeyError, IndexError):
        logging.warning("Webhook no parece ser un evento de llamada.")
    return Response(status_code=200)

# --- Ejecución del Servidor ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8000)) # Puerto común para servidores web
    uvicorn.run(app, host="0.0.0.0", port=port)