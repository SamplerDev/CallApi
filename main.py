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

# --- Configuración Inicial ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Cargar credenciales
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
TURN_USERNAME = os.getenv("TURN_USERNAME")
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL")

if not all([VERIFY_TOKEN, PHONE_NUMBER_ID, ACCESS_TOKEN]):
    raise ValueError("Faltan variables de entorno críticas.")

# --- Constantes y Configuración de la API ---
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/calls"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
# Almacenes de estado en memoria para gestionar llamadas y agentes
active_calls = {}
connected_agents = {}

# Inicialización de la aplicación FastAPI
app = FastAPI(title="WhatsApp WebRTC Bridge (Solución Completa)")

# --- 3. CONFIGURACIÓN DE CORS ---
# Permite que tu frontend se conecte a este backend.
# En producción, reemplaza "*" con la URL específica de tu frontend para mayor seguridad.
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
    """Envía acciones (pre_accept, accept, terminate) a la API de WhatsApp."""
    payload = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
    if sdp:
        payload["session"] = {"sdp_type": "answer", "sdp": sdp}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(WHATSAPP_API_URL, json=payload, headers=HEADERS)
            response.raise_for_status()
            logging.info(f"Acción '{action}' enviada exitosamente para la llamada {call_id}.")
            return response.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"Error al enviar la acción '{action}' para {call_id}: {e.response.text}")
            return None

# --- 5. LÓGICA WEBRTC (EL PUENTE DE AUDIO) ---
async def create_webrtc_bridge(session: dict, browser_sdp_offer: RTCSessionDescription):
    """Crea y gestiona el puente de audio entre WhatsApp y el navegador."""
    whatsapp_sdp_offer = session["whatsapp_sdp_offer"]
    call_id = session["call_id"]
    agent_websocket = session["agent_websocket"]

    # Configuración robusta de servidores ICE (STUN + TURN) para máxima conectividad
    ice_servers = [
        RTCIceServer(urls="stun:stun.l.google.com:19302"),
        RTCIceServer(urls="stun:stun.relay.metered.ca:80"),
        RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
        RTCIceServer(urls="turn:global.relay.metered.ca:443", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
        RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
    ]
    config = RTCConfiguration(iceServers=ice_servers)

    # Crear las dos conexiones PeerConnection con la configuración ICE
    whatsapp_pc = RTCPeerConnection(configuration=config)
    browser_pc = RTCPeerConnection(configuration=config)
    
    session["whatsapp_pc"] = whatsapp_pc
    session["browser_pc"] = browser_pc

    # Definir cómo se transfiere el audio entre las conexiones
    @whatsapp_pc.on("track")
    async def on_whatsapp_track(track):
        logging.info(f"Recibida pista de audio de WhatsApp para {call_id}. Añadiéndola al navegador.")
        browser_pc.addTrack(track)

    @browser_pc.on("track")
    async def on_browser_track(track):
        logging.info(f"Recibida pista de audio del navegador para {call_id}. Añadiéndola a WhatsApp.")
        whatsapp_pc.addTrack(track)

    # Negociación SDP
    await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)
    await browser_pc.setRemoteDescription(browser_sdp_offer)

    whatsapp_answer = await whatsapp_pc.createAnswer()
    browser_answer = await browser_pc.createAnswer()

    await whatsapp_pc.setLocalDescription(whatsapp_answer)
    await browser_pc.setLocalDescription(browser_answer)

    # Enviar respuestas a WhatsApp y al navegador
    await send_call_action(call_id, "pre_accept", whatsapp_pc.localDescription.sdp)
    await asyncio.sleep(1)
    await send_call_action(call_id, "accept", whatsapp_pc.localDescription.sdp)

    await agent_websocket.send_json({
        "type": "answer_from_server",
        "sdp": browser_pc.localDescription.sdp
    })
    session["status"] = "active"

# --- 6. ENDPOINTS DE LA API ---

# Endpoint de verificación para Meta (GET)
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

# Endpoint principal para recibir eventos de llamada (POST)
@app.post("/webhook")
async def receive_call_notification(request: Request):
    """
    Endpoint principal que recibe todas las notificaciones de eventos de llamada.
    Implementa el flujo de señalización corregido.
    """
    body = await request.json()
    logging.info("--- WEBHOOK RECIBIDO ---")
    logging.info(json.dumps(body, indent=2))

    try:
        call_data = body["entry"][0]["changes"][0]["value"]["calls"][0]
        call_id = call_data["id"]
        event = call_data["event"]

        if event == "connect":
            # Lógica simple: notificar siempre al agente "agent_001"
            agent_id_to_notify = "agent_001"
            agent_websocket = connected_agents.get(agent_id_to_notify)

            if agent_websocket:
                logging.info(f"Iniciando negociación para {call_id} con el agente {agent_id_to_notify}")
                
                # Configuración de ICE (STUN + TURN)
                ice_servers = [
                    RTCIceServer(urls="stun:stun.l.google.com:19302"),
                    RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
                    RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
                ]
                config = RTCConfiguration(iceServers=ice_servers)

                # 1. Crear la conexión PeerConnection para la "pata" de WhatsApp
                whatsapp_pc = RTCPeerConnection(configuration=config)
                
                # Guardar la sesión inmediatamente
                active_calls[call_id] = {
                    "status": "negotiating",
                    "call_id": call_id,
                    "agent_websocket": agent_websocket,
                    "whatsapp_pc": whatsapp_pc
                }

                # Definir cómo se manejará el audio que llega de WhatsApp
                @whatsapp_pc.on("track")
                async def on_whatsapp_track(track):
                    # Esta lógica es para el puente de audio, se conectará más tarde
                    logging.info(f"Recibida pista de audio de WhatsApp para {call_id}.")
                    session = active_calls.get(call_id)
                    if session and session.get("browser_pc"):
                        logging.info("Añadiendo pista de WhatsApp al navegador.")
                        session["browser_pc"].addTrack(track)

                # 2. Establecer la oferta original de WhatsApp como la descripción remota
                whatsapp_sdp_offer = RTCSessionDescription(sdp=call_data["session"]["sdp"], type="offer")
                await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)

                # 3. Crear una RESPUESTA para WhatsApp. Esta respuesta se convertirá en la OFERTA para el navegador.
                answer_for_whatsapp = await whatsapp_pc.createAnswer()
                await whatsapp_pc.setLocalDescription(answer_for_whatsapp)

                # 4. Enviar esta descripción local (que es una respuesta a WhatsApp) como una OFERTA al navegador
                logging.info(f"Enviando oferta al navegador para la llamada {call_id}")
                await agent_websocket.send_json({
                    "type": "offer_from_server",
                    "call_id": call_id,
                    "from": body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido'),
                    "sdp": whatsapp_pc.localDescription.sdp
                })
            else:
                # Si no hay agentes conectados, rechazar la llamada
                logging.warning(f"Llamada {call_id} recibida, pero el agente {agent_id_to_notify} no está conectado. Rechazando.")
                await send_call_action(call_id, "reject")

        elif event == "terminate":
            # Limpiar la sesión cuando la llamada termina
            if call_id in active_calls:
                session = active_calls[call_id]
                # Notificar al frontend si todavía está conectado
                if session.get("agent_websocket") and session["agent_websocket"].client_state.name == 'CONNECTED':
                    await session["agent_websocket"].send_json({"type": "call_terminated", "call_id": call_id})
                
                # Cerrar las conexiones WebRTC de forma segura
                if session.get("whatsapp_pc"): await session["whatsapp_pc"].close()
                if session.get("browser_pc"): await session["browser_pc"].close()
                
                del active_calls[call_id]
                logging.info(f"Sesión para {call_id} terminada y limpiada.")

    except (KeyError, IndexError) as e:
        logging.warning(f"El webhook recibido no parece ser un evento de llamada estándar. Error: {e}")
    except Exception as e:
        logging.error(f"Error inesperado al procesar el webhook: {e}", exc_info=True)

    return Response(status_code=200)

# Endpoint WebSocket para la conexión con el frontend
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

            # CAMBIO: Ahora esperamos una "answer_from_browser"
            if event_type == "answer_from_browser":
                logging.info(f"Recibida RESPUESTA SDP del navegador para la llamada {call_id}")
                session = active_calls.get(call_id)
                if session:
                    # La respuesta del navegador es la pieza final que necesitamos
                    browser_sdp_answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
                    await create_webrtc_bridge(session, browser_sdp_answer)
            elif event_type == "hangup_from_browser":
                logging.info(f"Agente colgó la llamada {call_id} desde el navegador.")
                if call_id in active_calls:
                    await send_call_action(call_id, "terminate")
    except WebSocketDisconnect:
        logging.info(f"Agente '{agent_id}' desconectado.")
        del connected_agents[agent_id]

# Endpoint de verificación de salud para Render
@app.get("/")
def health_check():
    return {"status": "ok", "active_calls": len(active_calls), "connected_agents": len(connected_agents)}

# --- 7. EJECUCIÓN DEL SERVIDOR ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)