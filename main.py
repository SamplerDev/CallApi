# --- START OF FINAL CORRECTED FILE main.py ---

import os
import json
import logging
import asyncio
import time
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from dotenv import load_dotenv
import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
import uvicorn
from aiortc.contrib.media import MediaRelay, MediaRecorder

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("aiortc.rtcrtpreceiver").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpsender").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.INFO)

RECORDINGS_DIR = Path("./recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)

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
            logging.info(f"API Request -> Action: '{action}', Call ID: {call_id}")
            response = await client.post(WHATSAPP_API_URL, json=payload, headers=HEADERS)
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
            if call_id in active_calls:
                logging.warning(f"Llamada duplicada {call_id} recibida. Ignorando.")
                return Response(status_code=200)
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
                
                if session.get("monitor_task"):
                    session["monitor_task"].cancel()
                
                # CORRECCIÓN: Usar .file en lugar de ._file
                if session.get("whatsapp_recorder"):
                    await session["whatsapp_recorder"].stop()
                    logging.info(f"Grabación de WhatsApp guardada en {session['whatsapp_recorder'].file}")
                if session.get("browser_recorder"):
                    await session["browser_recorder"].stop()
                    logging.info(f"Grabación del navegador guardada en {session['browser_recorder'].file}")

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

@app.get("/recordings/{call_id}/{source}.wav", response_class=FileResponse)
async def get_recording(call_id: str, source: str):
    if source not in ["whatsapp", "browser"]:
        raise HTTPException(status_code=400, detail="La fuente debe ser 'whatsapp' o 'browser'.")
    
    file_path = RECORDINGS_DIR / f"{call_id}_{source}.wav"
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Grabación no encontrada.")
    return FileResponse(file_path, media_type="audio/wav", filename=f"{call_id}_{source}.wav")

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
                
                wa_recorder_file = str(RECORDINGS_DIR / f"{call_id}_whatsapp.wav")
                br_recorder_file = str(RECORDINGS_DIR / f"{call_id}_browser.wav")

                session.update({
                    "status": "negotiating",
                    "agent_websocket": websocket,
                    "monitor_task": None,
                    "whatsapp_recorder": MediaRecorder(wa_recorder_file),
                    "browser_recorder": MediaRecorder(br_recorder_file),
                    "whatsapp_track": None,
                    "browser_track": None,
                    # NUEVO: Eventos para sincronización
                    "browser_connected": asyncio.Event(),
                    "whatsapp_connected": asyncio.Event(),
                    "media_stats": {
                        "whatsapp_packets_received": 0,
                        "browser_packets_received": 0,
                        "start_time": time.time()
                    }
                })
                call_id_handled_by_this_ws = call_id

                config = RTCConfiguration(iceServers=[
                    RTCIceServer(urls="stun:stun.l.google.com:19302"),
                    RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
                    RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
                ])
                
                whatsapp_pc = RTCPeerConnection(configuration=config)
                browser_pc = RTCPeerConnection(configuration=config)
                
                session["whatsapp_pc"] = whatsapp_pc
                session["browser_pc"] = browser_pc

                browser_pc.addTransceiver("audio", direction="sendrecv")
                whatsapp_pc.addTransceiver("audio", direction="sendrecv")

                # MODIFICADO: Los manejadores solo guardan el track
                @browser_pc.on("track")
                async def on_browser_track(track):
                    logging.info(f"[{call_id}] PISTA DEL NAVEGADOR RECIBIDA: kind={track.kind}")
                    if track.kind == "audio":
                        session["browser_track"] = track

                @whatsapp_pc.on("track")
                async def on_whatsapp_track(track):
                    logging.info(f"[{call_id}] PISTA DE WHATSAPP RECIBIDA: kind={track.kind}")
                    if track.kind == "audio":
                        session["whatsapp_track"] = track

                # MODIFICADO: Los manejadores de estado activan los eventos de sincronización
                @whatsapp_pc.on("connectionstatechange")
                async def on_connection_state_change():
                    logging.info(f"[{call_id}] Estado conexión WhatsApp: {whatsapp_pc.connectionState}")
                    if whatsapp_pc.connectionState == "connected":
                        session["whatsapp_connected"].set()

                @browser_pc.on("connectionstatechange")
                async def on_connection_state_change_browser():
                    logging.info(f"[{call_id}] Estado conexión Navegador: {browser_pc.connectionState}")
                    if browser_pc.connectionState == "connected":
                        session["browser_connected"].set()

                logging.info(f"[{call_id}] Creando oferta para el navegador.")
                browser_offer = await browser_pc.createOffer()
                await browser_pc.setLocalDescription(browser_offer)
                
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
                logging.info(f"[{call_id}] Respuesta interna de aiortc generada.")

                source_sdp = whatsapp_pc.localDescription.sdp
                source_lines = source_sdp.splitlines()

                def find_line(prefix, lines, default=None):
                    for line in lines:
                        if line.startswith(prefix):
                            return line
                    return default

                ice_ufrag = find_line("a=ice-ufrag:", source_lines).split(':', 1)[1]
                ice_pwd = find_line("a=ice-pwd:", source_lines).split(':', 1)[1]
                fingerprint_line = find_line("a=fingerprint:sha-256", source_lines)
                fingerprint = fingerprint_line.split(' ', 1)[1]
                
                m_line = find_line("m=audio", source_lines)
                selected_payload_type = m_line.split()[3]

                rtpmap_line = find_line(f"a=rtpmap:{selected_payload_type}", source_lines)
                fmtp_line = find_line(f"a=fmtp:{selected_payload_type}", source_lines)

                ice_candidates = [line for line in source_lines if line.startswith("a=candidate:")]

                session_id_val = int(time.time() * 1000)
                final_sdp_lines = [
                    "v=0", f"o=- {session_id_val} 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
                    "a=group:BUNDLE audio",
                    f"m=audio 9 UDP/TLS/RTP/SAVPF {selected_payload_type}",
                    "c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0",
                    f"a=ice-ufrag:{ice_ufrag}", f"a=ice-pwd:{ice_pwd}",
                ]
                final_sdp_lines.extend(ice_candidates)
                final_sdp_lines.extend([
                    f"a=fingerprint:sha-256 {fingerprint}", "a=setup:active", "a=mid:audio",
                    "a=sendrecv", "a=rtcp-mux", rtpmap_line,
                ])
                if fmtp_line:
                    final_sdp_lines.append(fmtp_line)

                final_sdp = "\r\n".join(final_sdp_lines) + "\r\n"

                logging.info(f"[{call_id}] SDP final reconstruido para WhatsApp:\n{final_sdp}")
                
                pre_accept_response = await send_call_action(call_id, "pre_accept", final_sdp)
                if pre_accept_response:
                    await asyncio.sleep(1)
                    await send_call_action(call_id, "accept", final_sdp)
                    session["status"] = "active"
                    logging.info(f"[{call_id}] Puente WebRTC activo - Esperando conexiones para iniciar media...")
                    
                    # MODIFICADO: Tarea que espera y luego construye el puente
                    async def bridge_and_monitor_media():
                        try:
                            # Esperar a que AMBAS conexiones estén listas
                            await asyncio.gather(
                                session["browser_connected"].wait(),
                                session["whatsapp_connected"].wait()
                            )
                            
                            logging.info(f"[{call_id}] Ambas conexiones 'connected'. Construyendo puente de media y grabación.")
                            
                            relay = MediaRelay()
                            
                            # Flujo: Navegador -> WhatsApp y Grabadora
                            browser_track = session["browser_track"]
                            whatsapp_pc.addTrack(relay.subscribe(browser_track))
                            
                            track_for_browser_recorder = relay.subscribe(browser_track)
                            session["browser_recorder"].addTrack(track_for_browser_recorder)
                            await session["browser_recorder"].start()
                            
                            @track_for_browser_recorder.on("frame")
                            async def on_browser_frame_rx(frame):
                                session["media_stats"]["browser_packets_received"] += 1

                            # Flujo: WhatsApp -> Navegador y Grabadora
                            whatsapp_track = session["whatsapp_track"]
                            browser_pc.addTrack(relay.subscribe(whatsapp_track))

                            track_for_whatsapp_recorder = relay.subscribe(whatsapp_track)
                            session["whatsapp_recorder"].addTrack(track_for_whatsapp_recorder)
                            await session["whatsapp_recorder"].start()

                            @track_for_whatsapp_recorder.on("frame")
                            async def on_whatsapp_frame_rx(frame):
                                session["media_stats"]["whatsapp_packets_received"] += 1

                            logging.info(f"[{call_id}] Puente y grabación iniciados.")

                            # Iniciar monitoreo de media
                            start_time = time.time()
                            while call_id in active_calls:
                                await asyncio.sleep(5)
                                stats = session["media_stats"]
                                logging.info(f"[{call_id}] CHEQUEO MEDIA: WhatsApp RX={stats['whatsapp_packets_received']}, Navegador RX={stats['browser_packets_received']}")
                                
                                if time.time() - start_time > 15:
                                    if stats["whatsapp_packets_received"] == 0 or stats["browser_packets_received"] == 0:
                                        logging.error(f"[{call_id}] TIMEOUT MEDIA: Flujo de media no es bidireccional.")
                                        await send_call_action(call_id, "terminate")
                                        return
                                    else:
                                        logging.info(f"[{call_id}] Flujo bidireccional confirmado.")
                                        return

                        except Exception as e:
                            logging.error(f"[{call_id}] Error en bridge_and_monitor_media: {e}", exc_info=True)
                            await send_call_action(call_id, "terminate")

                    session["monitor_task"] = asyncio.create_task(bridge_and_monitor_media())
                else:
                    logging.error(f"[{call_id}] Error en pre_accept, no se aceptará la llamada.")

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

# --- END OF FINAL CORRECTED FILE main.py ---