import os
import json
import logging
import asyncio
import time
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.mediastreams import MediaStreamTrack # [NUEVO] Importación necesaria
import uvicorn
from fastapi.responses import HTMLResponse
from aiortc.contrib.media import MediaRelay

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# [MODIFICADO] Bajamos el nivel de log de aiortc para no saturar la consola con sus mensajes DEBUG.
# Nos enfocaremos en nuestros propios logs de audio.
logging.getLogger("aiortc").setLevel(logging.INFO) 

# --- [NUEVO] Clase para loguear paquetes de audio ---
class AudioLogger(MediaStreamTrack):
    """
    Una pista de audio "proxy" que intercepta cada paquete, imprime su información,
    y luego lo pasa al siguiente eslabón de la cadena (el MediaRelay).
    """
    kind = "audio"

    def __init__(self, track, source_name):
        super().__init__()
        self.track = track
        self.source_name = source_name
        self.packet_count = 0
        self.start_time = time.time()
        self.total_bytes = 0

    async def recv(self):
        # Espera el siguiente paquete de la pista original
        packet = await self.track.recv()
        
        self.packet_count += 1
        self.total_bytes += len(packet.payload)
        
        # Imprimir un log cada 50 paquetes (aprox. cada segundo) para no inundar la consola
        if self.packet_count % 50 == 0:
            elapsed = time.time() - self.start_time
            avg_size = self.total_bytes / self.packet_count
            logging.info(
                f"[AUDIO LOG] Fuente: '{self.source_name}' | "
                f"Paquetes: {self.packet_count} | "
                f"Tamaño Promedio: {avg_size:.1f} bytes | "
                f"Packets/sec: {self.packet_count / elapsed:.1f}"
            )
        
        # Devuelve el paquete original para que el audio siga fluyendo
        return packet

# --- FIN DE LA NUEVA CLASE ---


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
lobby_clients = set()

app = FastAPI(title="WhatsApp WebRTC Bridge - Final Version")

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
            if response.is_success:
                logging.info(f"API Response -> Status: {response.status_code}, Body: {response.text}")
            else:
                logging.warning(f"API Response -> Status: {response.status_code}, Body: {response.text}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"Error HTTP en la acción '{action}' para {call_id}: {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"Error inesperado en send_call_action para {call_id}: {e}")
            return None

async def broadcast_to_lobby(message: dict):
    for client in list(lobby_clients):
        try:
            await client.send_json(message)
        except Exception:
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

            active_calls[call_id] = { "status": "ringing", "call_data": call_data }
            
            caller_name = body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido')
            await broadcast_to_lobby({ "type": "incoming_call", "call_id": call_id, "from": caller_name })
            logging.info(f"Notificando al lobby sobre la llamada entrante {call_id} de {caller_name}")

        elif event == "terminate":
            if call_id in active_calls:
                session = active_calls.pop(call_id, {})
                agent_ws = session.get("agent_websocket")
                if agent_ws:
                    try:
                        await agent_ws.send_json({"type": "call_terminated", "call_id": call_id})
                    except Exception: pass
                
                if session.get("whatsapp_pc"): await session["whatsapp_pc"].close()
                if session.get("browser_pc"): await session["browser_pc"].close()
                
                logging.info(f"Sesión para {call_id} terminada y limpiada.")

    except (KeyError, IndexError) as e:
        logging.error(f"Error al parsear el webhook. Estructura inesperada: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"Error inesperado al procesar el webhook: {e}", exc_info=True)

    return Response(status_code=200)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    lobby_clients.add(websocket)
    logging.info(f"Nuevo cliente conectado al lobby. Total: {len(lobby_clients)}")
    
    call_id_handled_by_this_ws = None

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")
            call_id = data.get("call_id")

            session = active_calls.get(call_id)
            if not session:
                logging.warning(f"[{call_id or 'N/A'}] Evento '{event_type}' para llamada no activa.")
                continue

            if event_type == "answer_call":
                if session.get("status") != "ringing":
                    logging.warning(f"[{call_id}] Intento de contestar llamada ya tomada.")
                    await websocket.send_json({"type": "call_already_taken"})
                    continue

                logging.info(f"[{call_id}] Agente ha contestado. Iniciando negociación de puente.")
                
                session["status"] = "negotiating"
                session["agent_websocket"] = websocket
                call_id_handled_by_this_ws = call_id
                session["browser_track_ready"] = asyncio.Event()

                config = RTCConfiguration(iceServers=[
                    RTCIceServer(urls="stun:stun.l.google.com:19302"),
                    RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
                    RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
                ])
                
                whatsapp_pc = RTCPeerConnection(configuration=config)
                browser_pc = RTCPeerConnection(configuration=config)
                
                session["whatsapp_pc"] = whatsapp_pc
                session["browser_pc"] = browser_pc

                relay = MediaRelay()
                
                # --- [MODIFICADO] Añadir logs de audio ---
                @browser_pc.on("track")
                async def on_browser_track(track):
                    logging.info(f"[{call_id}] Pista del navegador recibida. Envolviendo en logger y conectando a WhatsApp.")
                    logged_track = AudioLogger(track, "Navegador")
                    whatsapp_pc.addTrack(relay.subscribe(logged_track))
                    session["browser_track_ready"].set()

                @whatsapp_pc.on("track")
                async def on_whatsapp_track(track):
                    logging.info(f"[{call_id}] Pista de WhatsApp recibida. Envolviendo en logger y conectando al Navegador.")
                    logged_track = AudioLogger(track, "WhatsApp")
                    browser_pc.addTrack(relay.subscribe(logged_track))
                # --- FIN DE LA MODIFICACIÓN ---

                browser_pc.addTransceiver("audio", direction="sendrecv")
                
                logging.info(f"[{call_id}] Creando oferta para el navegador.")
                browser_offer = await browser_pc.createOffer()
                await browser_pc.setLocalDescription(browser_offer)
                
                logging.info(f"[{call_id}] Enviando oferta al navegador.")
                await websocket.send_json({
                    "type": "offer_from_server",
                    "call_id": call_id,
                    "sdp": browser_pc.localDescription.sdp
                })

            elif event_type == "answer_from_browser":
                logging.info(f"[{call_id}] Recibida RESPUESTA SDP del navegador.")
                
                whatsapp_pc = session["whatsapp_pc"]
                browser_pc = session["browser_pc"]

                browser_answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
                await browser_pc.setRemoteDescription(browser_answer)
                logging.info(f"[{call_id}] Negociación con navegador completada.")

                try:
                    logging.info(f"[{call_id}] Esperando la pista de audio del navegador...")
                    await asyncio.wait_for(session["browser_track_ready"].wait(), timeout=10.0)
                    logging.info(f"[{call_id}] Pista del navegador recibida y lista.")
                except asyncio.TimeoutError:
                    logging.error(f"[{call_id}] Timeout: No se recibió la pista de audio del navegador en 10 segundos. Abortando.")
                    await send_call_action(call_id, "terminate")
                    continue

                logging.info(f"[{call_id}] Iniciando negociación con WhatsApp.")
                whatsapp_sdp_offer = RTCSessionDescription(sdp=session["call_data"]["session"]["sdp"], type="offer")
                await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)
                
                whatsapp_answer = await whatsapp_pc.createAnswer()
                await whatsapp_pc.setLocalDescription(whatsapp_answer)
                logging.info(f"[{call_id}] Respuesta para WhatsApp generada (con pista de envío).")

                local_sdp_lines = whatsapp_pc.localDescription.sdp.splitlines()
                ice_ufrag = next((line.split(':', 1)[1] for line in local_sdp_lines if line.startswith("a=ice-ufrag:")), None)
                ice_pwd = next((line.split(':', 1)[1] for line in local_sdp_lines if line.startswith("a=ice-pwd:")), None)
                fingerprint = next((line.split(':', 1)[1] for line in local_sdp_lines if line.startswith("a=fingerprint:")), None)
                ssrc, cname, msid, track_id = None, None, None, None
                cname_line = next((line for line in local_sdp_lines if line.startswith("a=ssrc:") and "cname:" in line), None)
                if cname_line:
                    parts = cname_line.split(); ssrc = parts[0].split(':')[1]; cname = parts[1].split('cname:')[1]
                msid_line = next((line for line in local_sdp_lines if line.startswith("a=msid:")), None)
                if msid_line:
                    parts = msid_line.split(); msid = parts[0].split(':')[1]; track_id = parts[1]

                if not all([ice_ufrag, ice_pwd, fingerprint, ssrc, cname, msid, track_id]):
                    logging.error(f"[{call_id}] Fallo crítico en la extracción de SDP. Abortando.")
                    await send_call_action(call_id, "terminate")
                    continue

                session_id_val = int(time.time() * 1000)
                final_sdp = (
                    f"v=0\r\no=- {session_id_val} 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE audio\r\n"
                    f"a=msid-semantic: WMS {msid}\r\n"
                    "m=audio 9 UDP/TLS/RTP/SAVPF 111 126\r\nc=IN IP4 0.0.0.0\r\na=rtcp:9 IN IP4 0.0.0.0\r\n"
                    f"a=ice-ufrag:{ice_ufrag}\r\na=ice-pwd:{ice_pwd}\r\na=fingerprint:{fingerprint}\r\n"
                    "a=setup:active\r\na=mid:audio\r\n"
                    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
                    "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext:abs-send-time\r\n"
                    "a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01\r\n"
                    "a=sendrecv\r\na=rtcp-mux\r\n"
                    "a=rtpmap:111 opus/48000/2\r\na=rtcp-fb:111 transport-cc\r\na=fmtp:111 minptime=10;useinbandfec=1\r\n"
                    "a=rtpmap:126 telephone-event/8000\r\n"
                    f"a=ssrc:{ssrc} cname:{cname}\r\n"
                    f"a=ssrc:{ssrc} msid:{msid} {track_id}\r\n"
                )
                
                pre_accept_response = await send_call_action(call_id, "pre_accept", final_sdp)
                if pre_accept_response:
                    await asyncio.sleep(1)
                    await send_call_action(call_id, "accept", final_sdp)
                    session["status"] = "active"
                    logging.info(f"[{call_id}] Puente WebRTC completado y activo.")
                else:
                    logging.error(f"[{call_id}] Falló el pre_accept. No se pudo conectar la llamada.")

            elif event_type == "hangup_from_browser":
                if call_id in active_calls:
                    await send_call_action(call_id, "terminate")
            
    except WebSocketDisconnect as e:
        logging.info(f"Cliente desconectado con código: {e.code}")
    finally:
        lobby_clients.discard(websocket)
        if call_id_handled_by_this_ws and call_id_handled_by_this_ws in active_calls:
            logging.warning(f"El agente de la llamada {call_id_handled_by_this_ws} se desconectó. Terminando la llamada.")
            await send_call_action(call_id_handled_by_this_ws, "terminate")
        logging.info(f"Limpieza de cliente del lobby. Total restantes: {len(lobby_clients)}")

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
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)