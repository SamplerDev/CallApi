import os
import json
import logging
import asyncio
from fastapi import FastAPI, Request, Response, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
import uvicorn
from fastapi.responses import HTMLResponse

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Cargar credenciales
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
TURN_USERNAME = os.getenv("TURN_USERNAME")
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL")

if not all([VERIFY_TOKEN, PHONE_NUMBER_ID, ACCESS_TOKEN, TURN_USERNAME, TURN_CREDENTIAL]):
    raise ValueError("Faltan una o más variables de entorno críticas. Revisa tus variables en Render.")

# --- 2. CONSTANTES Y CONFIGURACIÓN GLOBAL ---
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/calls"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

active_calls = {}
connected_agents = {}

app = FastAPI(title="WhatsApp WebRTC Bridge (Final y Corregido)")

# --- 3. CONFIGURACIÓN DE CORS ---
origins = ["*"] 
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. FUNCIONES AUXILIARES ---
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

# --- 5. ENDPOINTS DE LA API ---

@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("WEBHOOK VERIFICADO CON ÉXITO")
        return Response(content=challenge, status_code=200)
    logging.error("Fallo en la verificación del Webhook.")
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
            agent_id_to_notify = "agent_001"
            agent_websocket = connected_agents.get(agent_id_to_notify)

            if agent_websocket:
                logging.info(f"Iniciando negociación para {call_id} con el agente {agent_id_to_notify}")
                
                ice_servers = [
                    RTCIceServer(urls="stun:stun.l.google.com:19302"),
                    RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
                    RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
                ]
                config = RTCConfiguration(iceServers=ice_servers)

                whatsapp_pc = RTCPeerConnection(configuration=config)
                
                active_calls[call_id] = {
                    "status": "negotiating", "call_id": call_id,
                    "agent_websocket": agent_websocket, "whatsapp_pc": whatsapp_pc
                }

                @whatsapp_pc.on("track")
                async def on_whatsapp_track(track):
                    logging.info(f"Recibida pista de audio de WhatsApp para {call_id}.")
                    session = active_calls.get(call_id)
                    if session and session.get("browser_pc"):
                        logging.info("Añadiendo pista de WhatsApp al navegador.")
                        session["browser_pc"].addTrack(track)

                whatsapp_sdp_offer = RTCSessionDescription(sdp=call_data["session"]["sdp"], type="offer")
                await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)

                answer_for_whatsapp = await whatsapp_pc.createAnswer()
                await whatsapp_pc.setLocalDescription(answer_for_whatsapp)

                logging.info(f"Enviando oferta al navegador para la llamada {call_id}")
                await agent_websocket.send_json({
                    "type": "offer_from_server",
                    "call_id": call_id,
                    "from": body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido'),
                    "sdp": whatsapp_pc.localDescription.sdp
                })
            else:
                logging.warning(f"Llamada {call_id} recibida, pero el agente {agent_id_to_notify} no está conectado. Rechazando.")
                await send_call_action(call_id, "reject")

        elif event == "terminate":
            if call_id in active_calls:
                session = active_calls[call_id]
                if session.get("agent_websocket") and session["agent_websocket"].client_state.name == 'CONNECTED':
                    await session["agent_websocket"].send_json({"type": "call_terminated", "call_id": call_id})
                
                if session.get("whatsapp_pc"): await session["whatsapp_pc"].close()
                if session.get("browser_pc"): await session["browser_pc"].close()
                
                del active_calls[call_id]
                logging.info(f"Sesión para {call_id} terminada y limpiada.")

    except (KeyError, IndexError) as e:
        logging.warning(f"El webhook recibido no parece ser un evento de llamada estándar. Error: {e}")
    except Exception as e:
        logging.error(f"Error inesperado al procesar el webhook: {e}", exc_info=True)

    return Response(status_code=200)

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

            if event_type == "answer_from_browser":
                logging.info(f"Recibida RESPUESTA SDP del navegador para la llamada {call_id}")
                session = active_calls.get(call_id)
                if session:
                    whatsapp_pc = session["whatsapp_pc"]
                    
                    # Crear la conexión para el navegador
                    ice_servers = whatsapp_pc.getConfiguration().iceServers
                    config = RTCConfiguration(iceServers=ice_servers)
                    browser_pc = RTCPeerConnection(configuration=config)
                    session["browser_pc"] = browser_pc

                    @browser_pc.on("track")
                    async def on_browser_track(track):
                        logging.info(f"Recibida pista de audio del navegador para {call_id}.")
                        whatsapp_pc.addTrack(track)

                    # Establecer la respuesta del navegador como la descripción remota del PC del navegador
                    browser_answer_sdp = RTCSessionDescription(sdp=data["sdp"], type="answer")
                    await browser_pc.setRemoteDescription(browser_answer_sdp)
                    
                    # Crear una oferta para el navegador (necesario para completar la negociación)
                    browser_offer = await browser_pc.createOffer()
                    await browser_pc.setLocalDescription(browser_offer)
                    
                    # Ahora que ambas conexiones están listas, enviamos la respuesta a WhatsApp
                    await send_call_action(call_id, "pre_accept", whatsapp_pc.localDescription.sdp)
                    await asyncio.sleep(1)
                    await send_call_action(call_id, "accept", whatsapp_pc.localDescription.sdp)
                    
                    session["status"] = "active"
                    logging.info(f"Puente WebRTC para la llamada {call_id} completado y activo.")

            elif event_type == "hangup_from_browser":
                logging.info(f"Agente colgó la llamada {call_id} desde el navegador.")
                if call_id in active_calls:
                    await send_call_action(call_id, "terminate")
    except WebSocketDisconnect:
        logging.info(f"Agente '{agent_id}' desconectado.")
        if agent_id in connected_agents:
            del connected_agents[agent_id]

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("main.html") as f: 
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Frontend no encontrado. Asegúrate de que el archivo 'main.html' está en la raíz del proyecto.</h1>", status_code=404)

# --- 6. EJECUCIÓN DEL SERVIDOR ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)