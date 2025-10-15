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
    raise ValueError("Faltan una o más variables de entorno críticas.")

# --- 2. CONSTANTES Y CONFIGURACIÓN GLOBAL ---
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/calls"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

active_calls = {}
lobby_clients = set() # Usamos un set para todos los frontends conectados

app = FastAPI(title="WhatsApp WebRTC Bridge - Dynamic Lobby")

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

async def broadcast_to_lobby(message: dict):
    """Envía un mensaje a todos los clientes conectados en el lobby."""
    disconnected_clients = set()
    for client in lobby_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected_clients.add(client)
    for client in disconnected_clients:
        lobby_clients.discard(client)

# --- 5. ENDPOINTS DE LA API ---

@app.get("/webhook")
def verify_webhook(request: Request):
    if (request.query_params.get("hub.mode") == "subscribe" and
            request.query_params.get("hub.verify_token") == VERIFY_TOKEN):
        logging.info("WEBHOOK VERIFICADO CON ÉXITO")
        return Response(content=request.query_params.get("hub.challenge"))
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
            if not lobby_clients:
                logging.warning(f"Llamada {call_id} recibida, pero no hay agentes en el lobby. Rechazando.")
                await send_call_action(call_id, "reject")
                return Response(status_code=200)

            # Guardamos la información de la llamada, esperando que un agente la tome
            active_calls[call_id] = {
                "status": "ringing",
                "call_data": call_data,
                "pending_whatsapp_tracks": []
            }
            
            # Notificamos a todos en el lobby sobre la nueva llamada
            caller_name = body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido')
            await broadcast_to_lobby({
                "type": "incoming_call",
                "call_id": call_id,
                "from": caller_name
            })
            logging.info(f"Notificando al lobby sobre la llamada entrante {call_id} de {caller_name}")

        elif event == "terminate":
            if call_id in active_calls:
                session = active_calls.get(call_id, {})
                agent_ws = session.get("agent_websocket")
                if agent_ws:
                    try:
                        await agent_ws.send_json({"type": "call_terminated", "call_id": call_id})
                    except Exception: pass
                
                if session.get("whatsapp_pc"): await session["whatsapp_pc"].close()
                if session.get("browser_pc"): await session["browser_pc"].close()
                
                del active_calls[call_id]
                logging.info(f"Sesión para {call_id} terminada y limpiada.")

    except Exception as e:
        logging.error(f"Error inesperado al procesar el webhook: {e}", exc_info=True)

    return Response(status_code=200)

@app.websocket("/ws") # Endpoint de WebSocket genérico, sin agent_id
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    lobby_clients.add(websocket)
    logging.info(f"Nuevo cliente conectado al lobby. Total: {len(lobby_clients)}")
    try:
        while True:
            try:
                data = await websocket.receive_json()
                event_type = data.get("type")
                call_id = data.get("call_id")

                session = active_calls.get(call_id)
                if not session:
                    logging.warning(f"Recepción de evento '{event_type}' para llamada no activa o ya tomada: {call_id}")
                    continue

                if event_type == "answer_call":
                    logging.info(f"Agente {websocket.client} ha contestado la llamada {call_id}. Iniciando negociación.")
                    
                    session["status"] = "negotiating"
                    session["agent_websocket"] = websocket

                    ice_servers = [RTCIceServer(urls="stun:stun.l.google.com:193_02"), RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL)]
                    config = RTCConfiguration(iceServers=ice_servers)
                    
                    # --- INICIO DE LA CORRECCIÓN DE FLUJO ---
                    # 1. Creamos AMBAS conexiones (para WhatsApp y para el Navegador) al mismo tiempo.
                    whatsapp_pc = RTCPeerConnection(configuration=config)
                    browser_pc = RTCPeerConnection(configuration=config)
                    session["whatsapp_pc"] = whatsapp_pc
                    session["browser_pc"] = browser_pc

                    # 2. Configuramos el "puenteo" de audio entre ellas.
                    @whatsapp_pc.on("track")
                    async def on_whatsapp_track(track):
                        logging.info(f"Recibida pista de WhatsApp para {call_id}, reenviando al navegador.")
                        browser_pc.addTrack(track)

                    @browser_pc.on("track")
                    async def on_browser_track(track):
                        logging.info(f"Recibida pista del navegador para {call_id}, reenviando a WhatsApp.")
                        whatsapp_pc.addTrack(track)

                    # 3. Procesamos la oferta de WhatsApp.
                    whatsapp_sdp_offer = RTCSessionDescription(sdp=session["call_data"]["session"]["sdp"], type="offer")
                    await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)
                    
                    # 4. Creamos una NUEVA oferta para el navegador.
                    # Esto es crucial. El navegador necesita su propia oferta.
                    browser_offer = await browser_pc.createOffer()
                    await browser_pc.setLocalDescription(browser_offer)
                    
                    # 5. Enviamos la oferta al navegador.
                    await websocket.send_json({
                        "type": "offer_from_server",
                        "call_id": call_id,
                        "sdp": browser_pc.localDescription.sdp
                    })
                    # --- FIN DE LA CORRECCIÓN DE FLUJO ---

                elif event_type == "answer_from_browser":
                    logging.info(f"Recibida RESPUESTA SDP del navegador para la llamada {call_id}")
                    
                    whatsapp_pc = session["whatsapp_pc"]
                    browser_pc = session["browser_pc"]

                    # --- INICIO DE LA CORRECCIÓN DE FLUJO (PARTE 2) ---
                    # 1. Establecemos la respuesta del navegador en su conexión.
                    browser_answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
                    await browser_pc.setRemoteDescription(browser_answer)

                    # 2. Ahora que el navegador está listo, generamos la respuesta para WhatsApp.
                    whatsapp_answer = await whatsapp_pc.createAnswer()
                    await whatsapp_pc.setLocalDescription(whatsapp_answer)
                    # --- FIN DE LA CORRECCIÓN DE FLUJO (PARTE 2) ---

                    # --- LÓGICA GANADORA: CONSTRUCCIÓN MANUAL DEL SDP ---
                    # (Esta parte se mantiene igual, ya que es la receta correcta)
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
                        "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext:abs-send-time\r\n"
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
            
            except Exception as e:
                logging.error(f"Error CRÍTICO dentro del bucle del WebSocket: {e}", exc_info=True)
                break

    except WebSocketDisconnect:
        logging.info(f"Cliente desconectado del lobby.")
    finally:
        lobby_clients.discard(websocket)
        logging.info(f"Limpieza de cliente del lobby. Total: {len(lobby_clients)}")

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