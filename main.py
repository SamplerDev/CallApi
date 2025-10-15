import os
import json
import logging
import asyncio
import random
import string
import time
import uuid
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
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
    raise ValueError("Faltan una o más variables de entorno críticas. Revisa tus variables.")

# --- 2. CONSTANTES Y CONFIGURACIÓN GLOBAL ---
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/calls"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

active_calls = {}
connected_agents = {}

app = FastAPI(title="WhatsApp WebRTC Bridge - Versión Final Funcional")

# --- 3. CONFIGURACIÓN DE CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
            logging.info(f"API Request -> Action: '{action}', Call ID: {call_id}")
            logging.info(f"API Response -> Status: {response.status_code}, Body: {response.text}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"Error HTTP en la acción '{action}' para {call_id}: {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"Error inesperado en send_call_action para {call_id}: {e}")
            return None

# --- 5. ENDPOINTS DE LA API ---

@app.get("/webhook")
def verify_webhook(request: Request):
    if (request.query_params.get("hub.mode") == "subscribe" and
            request.query_params.get("hub.verify_token") == VERIFY_TOKEN):
        logging.info("WEBHOOK VERIFICADO CON ÉXITO")
        return Response(content=request.query_params.get("hub.challenge"))
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
        event = call_data.get("event")

        if event == "connect":
            agent_id_to_notify = "agent_001"
            agent_websocket = connected_agents.get(agent_id_to_notify)

            if not agent_websocket:
                logging.warning(f"Llamada {call_id} recibida, pero el agente {agent_id_to_notify} no está conectado. Rechazando.")
                await send_call_action(call_id, "reject")
                return Response(status_code=200)

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
                "agent_websocket": agent_websocket, "whatsapp_pc": whatsapp_pc,
                "pending_whatsapp_tracks": []
            }

            @whatsapp_pc.on("track")
            async def on_whatsapp_track(track):
                logging.info(f"Recibida pista de WhatsApp [ID: {track.id}] para {call_id}.")
                session = active_calls.get(call_id)
                if not session: return

                browser_pc = session.get("browser_pc")
                if browser_pc and browser_pc.connectionState != "closed":
                    logging.info(f"Añadiendo pista de WhatsApp {track.id} al navegador.")
                    browser_pc.addTrack(track)
                else:
                    logging.warning(f"Navegador no listo. Poniendo pista {track.id} en cola.")
                    session["pending_whatsapp_tracks"].append(track)

            # Usamos "sendrecv" para que la negociación inicial sea completa
            whatsapp_pc.addTransceiver("audio", direction="sendrecv")
            
            whatsapp_sdp_offer = RTCSessionDescription(sdp=call_data["session"]["sdp"], type="offer")
            await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)

            answer_for_whatsapp = await whatsapp_pc.createAnswer()
            await whatsapp_pc.setLocalDescription(answer_for_whatsapp)

            logging.info(f"Enviando oferta al navegador para la llamada {call_id}")
            await agent_websocket.send_json({
                "type": "offer_from_server", "call_id": call_id,
                "from": body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido'),
                "sdp": whatsapp_pc.localDescription.sdp
            })

        elif event == "terminate":
            if call_id in active_calls:
                session = active_calls[call_id]
                if session.get("agent_websocket") and session["agent_websocket"].client_state.name == 'CONNECTED':
                    await session["agent_websocket"].send_json({"type": "call_terminated", "call_id": call_id})
                
                if session.get("whatsapp_pc"): await session["whatsapp_pc"].close()
                if session.get("browser_pc"): await session["browser_pc"].close()
                
                del active_calls[call_id]
                logging.info(f"Sesión para {call_id} terminada y limpiada.")

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
                if not session: continue

                whatsapp_pc = session["whatsapp_pc"]
                config = whatsapp_pc.configuration
                browser_pc = RTCPeerConnection(configuration=config)
                session["browser_pc"] = browser_pc

                @browser_pc.on("track")
                async def on_browser_track(track):
                    logging.info(f"Recibida pista de audio del navegador para {call_id}.")
                    if whatsapp_pc and whatsapp_pc.connectionState != "closed":
                        whatsapp_pc.addTrack(track)

                await browser_pc.setRemoteDescription(whatsapp_pc.localDescription)
                browser_answer_sdp = RTCSessionDescription(sdp=data["sdp"], type="answer")
                await browser_pc.setLocalDescription(browser_answer_sdp)
                
                if session.get("pending_whatsapp_tracks"):
                    for track in session["pending_whatsapp_tracks"]:
                        browser_pc.addTrack(track)
                    session["pending_whatsapp_tracks"] = []

                # --- INICIO DE LA LÓGICA GANADORA: CONSTRUCCIÓN MANUAL DEL SDP ---
                local_sdp_lines = whatsapp_pc.localDescription.sdp.splitlines()
                ice_ufrag = next((line.split(':', 1)[1] for line in local_sdp_lines if line.startswith("a=ice-ufrag:")), None)
                ice_pwd = next((line.split(':', 1)[1] for line in local_sdp_lines if line.startswith("a=ice-pwd:")), None)
                fingerprint = next((line.split(':', 1)[1] for line in local_sdp_lines if line.startswith("a=fingerprint:")), None)
                ssrc_line = next((line for line in local_sdp_lines if line.startswith("a=ssrc:") and "cname:" in line), None)
                ssrc = ssrc_line.split(' ')[0].split(':')[1] if ssrc_line else None
                cname = ssrc_line.split('cname:')[1] if ssrc_line else None

                if not all([ice_ufrag, ice_pwd, fingerprint, ssrc, cname]):
                    logging.error("Fallo al extraer piezas para el SDP final. Abortando.")
                    continue

                session_id = int(time.time() * 1000)
                session_uuid = str(uuid.uuid4())
                track_id = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

                final_sdp = (
                    "v=0\r\n"
                    f"o=- {session_id} 2 IN IP4 127.0.0.1\r\n"
                    "s=-\r\n"
                    "t=0 0\r\n"
                    "a=group:BUNDLE audio\r\n"
                    f"a=msid-semantic: WMS {session_uuid}\r\n"
                    "m=audio 9 UDP/TLS/RTP/SAVPF 111 126\r\n"
                    "c=IN IP4 0.0.0.0\r\n"
                    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
                    f"a=ice-ufrag:{ice_ufrag}\r\n"
                    f"a=ice-pwd:{ice_pwd}\r\n"
                    f"a=fingerprint:{fingerprint}\r\n"
                    "a=setup:active\r\n"
                    "a=mid:audio\r\n"
                    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
                    "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
                    "a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01\r\n"
                    "a=sendrecv\r\n"
                    "a=rtcp-mux\r\n"
                    "a=rtpmap:111 opus/48000/2\r\n"
                    "a=rtcp-fb:111 transport-cc\r\n"
                    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
                    "a=rtpmap:126 telephone-event/8000\r\n"
                    f"a=ssrc:{ssrc} cname:{cname}\r\n"
                    f"a=ssrc:{ssrc} msid:{session_uuid} {track_id}\r\n"
                )
                # --- FIN DE LA LÓGICA GANADORA ---

                pre_accept_response = await send_call_action(call_id, "pre_accept", final_sdp)
                if pre_accept_response:
                    await asyncio.sleep(1)
                    await send_call_action(call_id, "accept", final_sdp)
                    session["status"] = "active"
                    logging.info(f"Puente WebRTC para la llamada {call_id} completado y activo.")
                else:
                    logging.error(f"Falló el pre_accept para {call_id}. No se pudo conectar la llamada.")

            elif event_type == "hangup_from_browser":
                if call_id in active_calls:
                    await send_call_action(call_id, "terminate")
                    
    except WebSocketDisconnect:
        logging.info(f"Agente '{agent_id}' desconectado.")
        if agent_id in connected_agents:
            del connected_agents[agent_id]

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "frontend", "main.html")
    try:
        with open(frontend_path) as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Frontend no encontrado.</h1>", status_code=404)

# --- 6. EJECUCIÓN DEL SERVIDOR ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)