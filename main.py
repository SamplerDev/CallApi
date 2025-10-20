# --- START OF CORRECTED FILE main.py ---

import os
import json
import logging
import asyncio
import time
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
import uvicorn
from aiortc.contrib.media import MediaRelay

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("aiortc.rtcrtpreceiver").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpsender").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.INFO)

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

app = FastAPI(title="WhatsApp WebRTC Bridge - Enhanced Logging")

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
            logging.info(f"[{call_id}] API Request -> Action: '{action}'")
            if sdp:
                logging.debug(f"[{call_id}] Sending SDP for action '{action}':\n{sdp}")
            response = await client.post(WHATSAPP_API_URL, json=payload, headers=HEADERS)
            if response.is_success:
                logging.info(f"[{call_id}] API Response -> Status: {response.status_code}, Body: {response.text}")
            else:
                logging.warning(f"[{call_id}] API Response -> Status: {response.status_code}, Body: {response.text}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"[{call_id}] Error HTTP en la acción '{action}': {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"[{call_id}] Error inesperado en send_call_action: {e}")
            return None

async def broadcast_to_lobby(message: dict):
    logging.debug(f"Broadcasting to {len(lobby_clients)} lobby clients: {message}")
    for client in list(lobby_clients):
        try:
            await client.send_json(message)
        except Exception:
            lobby_clients.discard(client)
            logging.warning("Removed a disconnected client from lobby during broadcast.")

# --- 5. ENDPOINTS DE LA API ---

@app.get("/webhook")
def verify_webhook(request: Request):
    logging.debug(f"GET /webhook received with params: {request.query_params}")
    if (request.query_params.get("hub.mode") == "subscribe" and
            request.query_params.get("hub.verify_token") == VERIFY_TOKEN):
        logging.info("WEBHOOK VERIFICADO CON ÉXITO")
        return Response(content=request.query_params.get("hub.challenge"))
    logging.warning("Webhook verification failed.")
    raise HTTPException(status_code=403, detail="Verification failed.")

@app.post("/webhook")
async def receive_call_notification(request: Request):
    body = await request.json()
    logging.info("--- WEBHOOK RECIBIDO ---")
    logging.debug(json.dumps(body, indent=2))

    try:
        call_data = body["entry"][0]["changes"][0]["value"]["calls"][0]
        call_id = call_data["id"]
        event = call_data.get("event")
        logging.info(f"[{call_id}] Processing webhook event: '{event}'")

        if event == "connect":
            if call_id in active_calls:
                logging.warning(f"[{call_id}] Duplicated 'connect' event received. Ignoring.")
                return Response(status_code=200)
            if not lobby_clients:
                logging.warning(f"[{call_id}] Call received, but no agents in lobby. Rejecting call.")
                await send_call_action(call_id, "reject")
                return Response(status_code=200)

            active_calls[call_id] = { "status": "ringing", "call_data": call_data }
            
            caller_name = body["entry"][0]["changes"][0]["value"]["contacts"][0].get('profile', {}).get('name', 'Desconocido')
            await broadcast_to_lobby({ "type": "incoming_call", "call_id": call_id, "from": caller_name })
            logging.info(f"[{call_id}] Notified lobby about incoming call from '{caller_name}'.")

        elif event == "terminate":
            if call_id in active_calls:
                logging.info(f"[{call_id}] Terminate event received. Cleaning up session.")
                session = active_calls.pop(call_id, {})
                agent_ws = session.get("agent_websocket")
                if agent_ws:
                    try:
                        logging.debug(f"[{call_id}] Sending 'call_terminated' to agent's WebSocket.")
                        await agent_ws.send_json({"type": "call_terminated", "call_id": call_id})
                    except Exception as e:
                        logging.warning(f"[{call_id}] Could not send termination message to agent: {e}")
                
                if session.get("monitor_task"):
                    logging.debug(f"[{call_id}] Cancelling media monitor task.")
                    session["monitor_task"].cancel()
                
                if session.get("whatsapp_pc"):
                    logging.debug(f"[{call_id}] Closing WhatsApp PeerConnection.")
                    await session["whatsapp_pc"].close()
                if session.get("browser_pc"):
                    logging.debug(f"[{call_id}] Closing Browser PeerConnection.")
                    await session["browser_pc"].close()
                
                logging.info(f"[{call_id}] Session terminated and cleaned up successfully.")
            else:
                logging.warning(f"[{call_id}] Received 'terminate' for an unknown or already cleaned-up call.")

    except (KeyError, IndexError) as e:
        logging.error(f"Error parsing webhook. Unexpected structure: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"Unexpected error processing webhook: {e}", exc_info=True)

    return Response(status_code=200)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    lobby_clients.add(websocket)
    logging.info(f"New client connected to lobby. Total: {len(lobby_clients)}")
    
    call_id_handled_by_this_ws = None

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")
            call_id = data.get("call_id")
            logging.info(f"[{call_id or 'N/A'}] WebSocket received event: '{event_type}'")
            logging.debug(f"[{call_id or 'N/A'}] WebSocket data: {data}")

            session = active_calls.get(call_id)
            if not session:
                logging.warning(f"[{call_id or 'N/A'}] Event '{event_type}' for non-active call. Ignoring.")
                continue

            if event_type == "answer_call":
                if session.get("status") != "ringing":
                    logging.warning(f"[{call_id}] Agent tried to answer a call that is not 'ringing' (current status: {session.get('status')}).")
                    await websocket.send_json({"type": "call_already_taken"})
                    continue

                logging.info(f"[{call_id}] Agent answered. Initializing WebRTC bridge.")
                call_id_handled_by_this_ws = call_id
                
                session.update({
                    "status": "negotiating",
                    "agent_websocket": websocket,
                    "monitor_task": None,
                    "whatsapp_track": None,
                    "browser_track": None,
                    "browser_connected": asyncio.Event(),
                    "whatsapp_connected": asyncio.Event(),
                    "media_stats": {
                        "whatsapp_packets_received": 0,
                        "browser_packets_received": 0,
                        "start_time": time.time()
                    }
                })

                config = RTCConfiguration(iceServers=[
                RTCIceServer(urls="stun:stun.l.google.com:19302"),
                RTCIceServer(urls="turn:global.relay.metered.ca:80", username=TURN_USERNAME, credential=TURN_CREDENTIAL),
                RTCIceServer(urls="turns:global.relay.metered.ca:443?transport=tcp", username=TURN_USERNAME, credential=TURN_CREDENTIAL)
            ])
            
                whatsapp_pc = RTCPeerConnection(configuration=config)
                browser_pc = RTCPeerConnection(configuration=config)

            # --- AÑADIR ESTA LÍNEA ---
            # Forzamos a la conexión del navegador a usar únicamente el servidor TURN,
            # ya que la conexión directa está fallando.
                browser_pc.iceTransportPolicy = "relay"

                
                session["whatsapp_pc"] = whatsapp_pc
                session["browser_pc"] = browser_pc

                browser_pc.addTransceiver("audio", direction="sendrecv")
                whatsapp_pc.addTransceiver("audio", direction="sendrecv")

                @browser_pc.on("track")
                async def on_browser_track(track):
                    logging.info(f"[{call_id}] BROWSER TRACK RECEIVED: kind={track.kind}, id={track.id}")
                    if track.kind == "audio":
                        session["browser_track"] = track
                        @track.on("frame")
                        async def on_browser_frame_rx(frame):
                            session["media_stats"]["browser_packets_received"] += 1
                            if session["media_stats"]["browser_packets_received"] % 100 == 1:
                                logging.debug(f"[{call_id}] Received packet from Browser. Total: {session['media_stats']['browser_packets_received']}")

                @whatsapp_pc.on("track")
                async def on_whatsapp_track(track):
                    logging.info(f"[{call_id}] WHATSAPP TRACK RECEIVED: kind={track.kind}, id={track.id}")
                    if track.kind == "audio":
                        session["whatsapp_track"] = track
                        @track.on("frame")
                        async def on_whatsapp_frame_rx(frame):
                            session["media_stats"]["whatsapp_packets_received"] += 1
                            if session["media_stats"]["whatsapp_packets_received"] % 100 == 1:
                                logging.debug(f"[{call_id}] Received packet from WhatsApp. Total: {session['media_stats']['whatsapp_packets_received']}")

                @whatsapp_pc.on("connectionstatechange")
                async def on_connection_state_change():
                    logging.info(f"[{call_id}] WhatsApp PeerConnection State: {whatsapp_pc.connectionState}")
                    if whatsapp_pc.connectionState == "connected":
                        logging.info(f"[{call_id}] --> WhatsApp connection is CONNECTED. Setting event.")
                        session["whatsapp_connected"].set()

                @browser_pc.on("connectionstatechange")
                async def on_connection_state_change_browser():
                    logging.info(f"[{call_id}] Browser PeerConnection State: {browser_pc.connectionState}")
                    if browser_pc.connectionState == "connected":
                        logging.info(f"[{call_id}] --> Browser connection is CONNECTED. Setting event.")
                        session["browser_connected"].set()

                logging.info(f"[{call_id}] Creating SDP offer for the browser.")
                browser_offer = await browser_pc.createOffer()
                await browser_pc.setLocalDescription(browser_offer)
                logging.debug(f"[{call_id}] Browser offer created and set as local description.")
                
                await websocket.send_json({
                    "type": "offer_from_server",
                    "call_id": call_id,
                    "sdp": browser_pc.localDescription.sdp
                })
                logging.info(f"[{call_id}] Sent SDP offer to browser via WebSocket.")

            elif event_type == "answer_from_browser":
                # ... (el resto del código es idéntico y correcto)
                logging.info(f"[{call_id}] Received SDP answer from browser.")
                
                whatsapp_pc = session["whatsapp_pc"]
                browser_pc = session["browser_pc"]
                
                browser_answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
                await browser_pc.setRemoteDescription(browser_answer)
                logging.info(f"[{call_id}] Browser remote description set. Browser negotiation complete.")

                logging.info(f"[{call_id}] Starting negotiation with WhatsApp.")
                whatsapp_sdp_offer = RTCSessionDescription(sdp=session["call_data"]["session"]["sdp"], type="offer")
                await whatsapp_pc.setRemoteDescription(whatsapp_sdp_offer)
                logging.debug(f"[{call_id}] WhatsApp remote description (offer) set.")
                
                whatsapp_answer = await whatsapp_pc.createAnswer()
                await whatsapp_pc.setLocalDescription(whatsapp_answer)
                logging.info(f"[{call_id}] Internal aiortc answer for WhatsApp generated and set.")

                source_sdp = whatsapp_pc.localDescription.sdp
                source_lines = source_sdp.splitlines()
                logging.debug(f"[{call_id}] Reconstructing SDP from aiortc answer:\n{source_sdp}")

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
                
                logging.debug(f"[{call_id}] SDP components extracted: ufrag={ice_ufrag}, pwd=***, fingerprint=***, candidates={len(ice_candidates)}")

                session_id_val = int(time.time() * 1000)
                final_sdp_lines = [
                    "v=0", f"o=- {session_id_val} 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
                    "a=group:BUNDLE audio", f"m=audio 9 UDP/TLS/RTP/SAVPF {selected_payload_type}",
                    "c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0",
                    f"a=ice-ufrag:{ice_ufrag}", f"a=ice-pwd:{ice_pwd}",
                ]
                final_sdp_lines.extend(ice_candidates)
                final_sdp_lines.extend([
                    f"a=fingerprint:sha-256 {fingerprint}", "a=setup:active", "a=mid:audio",
                    "a=sendrecv", "a=rtcp-mux", rtpmap_line,
                ])
                if fmtp_line: final_sdp_lines.append(fmtp_line)
                final_sdp = "\r\n".join(final_sdp_lines) + "\r\n"

                logging.info(f"[{call_id}] Final SDP reconstructed for WhatsApp.")
                
                pre_accept_response = await send_call_action(call_id, "pre_accept", final_sdp)
                if pre_accept_response:
                    await asyncio.sleep(1)
                    await send_call_action(call_id, "accept", final_sdp)
                    session["status"] = "active"
                    logging.info(f"[{call_id}] Call is now active. Starting media bridge & monitor task.")
                    
                    async def bridge_and_monitor_media():
                        try:
                            logging.info(f"[{call_id}] Waiting for both PeerConnections to be 'connected'...")
                            await asyncio.gather(
                                session["browser_connected"].wait(),
                                session["whatsapp_connected"].wait()
                            )
                            
                            logging.info(f"[{call_id}] SUCCESS: Both connections are 'connected'. Building media relay.")
                            
                            relay = MediaRelay()
                            browser_track = session.get("browser_track")
                            whatsapp_track = session.get("whatsapp_track")

                            if not browser_track or not whatsapp_track:
                                logging.error(f"[{call_id}] CRITICAL: Missing audio tracks after connections established. Terminating.")
                                await send_call_action(call_id, "terminate")
                                return

                            whatsapp_pc.addTrack(relay.subscribe(browser_track))
                            browser_pc.addTrack(relay.subscribe(whatsapp_track))
                            logging.info(f"[{call_id}] Media relay bridge constructed. Audio should be flowing.")

                            start_time = time.time()
                            while call_id in active_calls:
                                await asyncio.sleep(5)
                                stats = session["media_stats"]
                                logging.info(f"[{call_id}] MEDIA CHECK: WhatsApp RX={stats['whatsapp_packets_received']}, Browser RX={stats['browser_packets_received']}")
                                
                                if time.time() - start_time > 15:
                                    if stats["whatsapp_packets_received"] == 0 or stats["browser_packets_received"] == 0:
                                        logging.error(f"[{call_id}] MEDIA TIMEOUT: No bidirectional media flow after 15s. Terminating.")
                                        await send_call_action(call_id, "terminate")
                                        return
                                    else:
                                        logging.info(f"[{call_id}] Bidirectional media flow confirmed. Monitor task exiting.")
                                        return

                        except asyncio.CancelledError:
                            logging.info(f"[{call_id}] Media bridge task was cancelled.")
                        except Exception as e:
                            logging.error(f"[{call_id}] Unhandled error in bridge_and_monitor_media: {e}", exc_info=True)
                            await send_call_action(call_id, "terminate")

                    session["monitor_task"] = asyncio.create_task(bridge_and_monitor_media())
                else:
                    logging.error(f"[{call_id}] 'pre_accept' action failed. Call will not be accepted.")

            elif event_type == "hangup_from_browser":
                if call_id in active_calls:
                    logging.info(f"[{call_id}] Agent hung up from browser. Terminating call.")
                    await send_call_action(call_id, "terminate")

    except WebSocketDisconnect as e:
        logging.warning(f"WebSocket client disconnected with code: {e.code}")
    finally:
        lobby_clients.discard(websocket)
        if call_id_handled_by_this_ws and call_id_handled_by_this_ws in active_calls:
            logging.warning(f"Agent for call {call_id_handled_by_this_ws} disconnected abruptly. Terminating call.")
            await send_call_action(call_id_handled_by_this_ws, "terminate")
        logging.info(f"Cleaned up WebSocket client. Lobby clients remaining: {len(lobby_clients)}")

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    frontend_path = Path(__file__).parent / "frontend" / "main.html"
    if frontend_path.is_file():
        with open(frontend_path) as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Frontend not found.</h1>", status_code=404)

# --- 6. EJECUCIÓN DEL SERVIDOR ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

# --- END OF CORRECTED FILE main.py ---