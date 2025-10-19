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
import uvicorn
from fastapi.responses import HTMLResponse
from aiortc.contrib.media import MediaRelay

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger("aiortc").setLevel(logging.DEBUG)

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
                
                # Cerrar conexiones
                if session.get("whatsapp_pc"):
                    await session["whatsapp_pc"].close()
                if session.get("browser_pc"):
                    await session["browser_pc"].close()
                
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

            elif event_type == "answer_call":
                if session.get("status") != "ringing":
                    logging.warning(f"[{call_id}] Intento de contestar llamada ya tomada.")
                    await websocket.send_json({"type": "call_already_taken"})
                    continue

                logging.info(f"[{call_id}] Agente ha contestado. Iniciando negociación de puente.")
                
                session["status"] = "negotiating"
                session["agent_websocket"] = websocket
                call_id_handled_by_this_ws = call_id

                config = RTCConfiguration(iceServers=[
                    RTCIceServer(urls="stun:stun.l.google.com:19302"),
                    RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
                    RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
                ])
                
                whatsapp_pc = RTCPeerConnection(configuration=config)
                browser_pc = RTCPeerConnection(configuration=config)
                
                # Crear relay para audio
                relay = MediaRelay()
                
                session["whatsapp_pc"] = whatsapp_pc
                session["browser_pc"] = browser_pc
                session["relay"] = relay
                session["whatsapp_sender"] = None
                session["browser_sender"] = None
                session["media_stats"] = {
                    "whatsapp_packets_received": 0,
                    "browser_packets_received": 0,
                    "last_check_time": time.time()
                }
                session["monitor_task"] = None

                # CORRECCIÓN 1: Transceptores con direcciones correctas
                browser_trx = browser_pc.addTransceiver("audio", direction="recvonly")
                whatsapp_trx = whatsapp_pc.addTransceiver("audio", direction="sendrecv")

                # CORRECCIÓN 2: Guardar transceptores en session para acceso posterior
                session["browser_trx"] = browser_trx
                session["whatsapp_trx"] = whatsapp_trx

                # ===== MANEJADORES DE PISTA CON MEDIARELAY =====
                @browser_pc.on("track")
                async def on_browser_track(track):
                    logging.info(f"[{call_id}] PISTA DEL NAVEGADOR: kind={track.kind}, id={track.id}")
                    
                    if track.kind == "audio":
                        try:
                            relay = session["relay"]
                            relay_track = relay.subscribe(track)
                            logging.info(f"[{call_id}] Track del navegador suscrito al relay")
                            
                            whatsapp_trx = session["whatsapp_trx"]
                            # NO USAR replaceTrack en un transceptor vacío
                            # Usar addTrack directamente
                            sender = whatsapp_pc.addTrack(relay_track)
                            session["whatsapp_sender"] = sender
                            logging.info(f"[{call_id}] Audio del navegador agregado a WhatsApp (sender: {sender})")
                            
                            frame_count = [0]
                            @track.on("frame")
                            async def on_frame(frame):
                                frame_count[0] += 1
                                session["media_stats"]["browser_packets_received"] += 1
                                if frame_count[0] % 50 == 0:
                                    logging.info(f"[{call_id}] Frames navegador: {frame_count[0]}")
                            
                        except Exception as e:
                            logging.error(f"[{call_id}] Error procesando audio del navegador: {e}", exc_info=True)

                @whatsapp_pc.on("track")
                async def on_whatsapp_track(track):
                    logging.info(f"[{call_id}] PISTA DE WHATSAPP: kind={track.kind}, id={track.id}")
                    
                    if track.kind == "audio":
                        try:
                            relay = session["relay"]
                            relay_track = relay.subscribe(track)
                            logging.info(f"[{call_id}] Track de WhatsApp suscrito al relay")
                            
                            # NO USAR replaceTrack en un transceptor vacío
                            # Usar addTrack directamente
                            sender = browser_pc.addTrack(relay_track)
                            session["browser_sender"] = sender
                            logging.info(f"[{call_id}] Audio de WhatsApp agregado al navegador (sender: {sender})")
                            
                            frame_count = [0]
                            @track.on("frame")
                            async def on_frame(frame):
                                frame_count[0] += 1
                                session["media_stats"]["whatsapp_packets_received"] += 1
                                if frame_count[0] % 50 == 0:
                                    logging.info(f"[{call_id}] Frames WhatsApp: {frame_count[0]}")
                            
                        except Exception as e:
                            logging.error(f"[{call_id}] Error procesando audio de WhatsApp: {e}", exc_info=True)

                @whatsapp_pc.on("connectionstatechange")
                async def on_connection_state_change():
                    state = whatsapp_pc.connectionState
                    logging.info(f"[{call_id}] Estado conexión WhatsApp: {state}")
                    if state == "failed":
                        logging.error(f"[{call_id}] CONEXIÓN WHATSAPP FALLIDA")

                @browser_pc.on("connectionstatechange")
                async def on_connection_state_change_browser():
                    state = browser_pc.connectionState
                    logging.info(f"[{call_id}] Estado conexión Navegador: {state}")
                    if state == "failed":
                        logging.error(f"[{call_id}] CONEXIÓN NAVEGADOR FALLIDA")

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

                logging.info(f"[{call_id}] Iniciando negociación con WhatsApp.")
                whatsapp_sdp_offer = RTCSessionDescription(sdp=session["call_data"]["session"]["sdp"], type="offer")
                await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)
                
                whatsapp_answer = await whatsapp_pc.createAnswer()
                await whatsapp_pc.setLocalDescription(whatsapp_answer)
                logging.info(f"[{call_id}] Respuesta para WhatsApp generada.")

                source_sdp = whatsapp_pc.localDescription.sdp
                source_lines = source_sdp.splitlines()

                ice_ufrag = next(line.split(':', 1)[1] for line in source_lines if line.startswith("a=ice-ufrag:"))
                ice_pwd = next(line.split(':', 1)[1] for line in source_lines if line.startswith("a=ice-pwd:"))
                fingerprint_line = next(line for line in source_lines if line.startswith("a=fingerprint:sha-256"))
                fingerprint = fingerprint_line.split(' ', 1)[1]

                ssrc_line = next(line for line in source_lines if line.startswith("a=ssrc:") and "cname:" in line)
                ssrc_parts = ssrc_line.split()
                ssrc = ssrc_parts[0].split(':')[1]
                cname = ssrc_parts[1].split('cname:')[1]

                msid_line = next(line for line in source_lines if line.startswith("a=msid:"))
                msid_parts = msid_line.split()
                msid = msid_parts[0].split(':')[1]
                track_id = msid_parts[1]

                m_line = next(line for line in source_lines if line.startswith("m=audio"))
                payload_types = m_line.split()[3:]

                rtpmap_lines = [line for pt in payload_types for line in source_lines if line.startswith(f"a=rtpmap:{pt}")]
                fmtp_lines = [line for pt in payload_types for line in source_lines if line.startswith(f"a=fmtp:{pt}")]
                ice_candidates = [line for line in source_lines if line.startswith("a=candidate:")]

                session_id_val = int(time.time() * 1000)
                final_sdp_lines = [
                    "v=0", f"o=- {session_id_val} 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
                    "a=group:BUNDLE audio", f"a=msid-semantic: WMS {msid}",
                    f"m=audio 9 UDP/TLS/RTP/SAVPF {' '.join(payload_types)}",
                    "c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0",
                    f"a=ice-ufrag:{ice_ufrag}", f"a=ice-pwd:{ice_pwd}",
                ]
                final_sdp_lines.extend(ice_candidates)
                final_sdp_lines.extend([
                    f"a=fingerprint:sha-256 {fingerprint}", "a=setup:active", "a=mid:audio",
                    "a=sendrecv", "a=rtcp-mux",
                ])
                final_sdp_lines.extend(rtpmap_lines)
                final_sdp_lines.extend(fmtp_lines)
                final_sdp_lines.extend([
                    f"a=ssrc:{ssrc} cname:{cname}",
                    f"a=ssrc:{ssrc} msid:{msid} {track_id}",
                ])
                final_sdp = "\r\n".join(final_sdp_lines) + "\r\n"

                logging.info(f"[{call_id}] SDP final con candidatos ICE:\n{final_sdp}")
                
                pre_accept_response = await send_call_action(call_id, "pre_accept", final_sdp)
                if pre_accept_response:
                    await asyncio.sleep(1)
                    await send_call_action(call_id, "accept", final_sdp)
                    session["status"] = "active"
                    logging.info(f"[{call_id}] Puente WebRTC activo - Iniciando monitoreo de media")
                    
                    # CORRECCIÓN 3: Monitoreo de media inicia cuando status = "active"
                    async def monitor_media_flow():
                        check_interval = 5
                        timeout_threshold = 15
                        
                        while call_id in active_calls and session.get("status") == "active":
                            await asyncio.sleep(check_interval)
                            
                            stats = session["media_stats"]
                            whatsapp_rx = stats["whatsapp_packets_received"]
                            browser_rx = stats["browser_packets_received"]
                            
                            logging.info(f"[{call_id}] CHEQUEO MEDIA: WhatsApp RX={whatsapp_rx}, Navegador RX={browser_rx}")
                            
                            if whatsapp_rx == 0 or browser_rx == 0:
                                time_elapsed = time.time() - stats["last_check_time"]
                                if time_elapsed > timeout_threshold:
                                    logging.error(f"[{call_id}] TIMEOUT MEDIA: WhatsApp RX={whatsapp_rx}, Navegador RX={browser_rx}")
                                    logging.error(f"[{call_id}] No hay media fluyendo después de {timeout_threshold}s")
                                    await send_call_action(call_id, "terminate")
                                    return
                            else:
                                stats["last_check_time"] = time.time()
                                logging.info(f"[{call_id}] Media fluyendo correctamente en ambas direcciones")

                    session["monitor_task"] = asyncio.create_task(monitor_media_flow())
                else:
                    logging.error(f"[{call_id}] Error en pre_accept")

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