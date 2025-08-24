# app.py (integrated)
# Uses Telethon (Telegram USER) + FastAPI WebSocket server
# Incorporates the user's code extraction idea (regex) where possible.
import os, re, asyncio
from typing import List, Dict, Any, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from datetime import datetime, timedelta
import socket
import uvicorn

PORT = int(os.getenv("PORT", "5000"))
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "tg_session")  # file path or session string
CHANNELS = os.getenv("CHANNELS", "-1002772030545,-1001234567890")  # Multiple channels separated by comma

# Enhanced regex patterns for different code formats
CODE_PATTERNS = [
    r'(?i)Code:\s+([a-zA-Z0-9]{4,25})',           # "Code: stakecomrtlye4" - primary pattern
    r'(?i)Code:([a-zA-Z0-9]{4,25})',              # "Code:stakecomguft19f6" - no space version
    r'(?i)Bonus:\s+([a-zA-Z0-9]{4,25})',         # "Bonus: ABC123"
    r'(?i)Bonus:([a-zA-Z0-9]{4,25})',            # "Bonus:ABC123" 
    r'(?i)Claim:\s+([a-zA-Z0-9]{4,25})',         # "Claim: ABC123"
    r'(?i)Claim:([a-zA-Z0-9]{4,25})',            # "Claim:ABC123"
    r'(?i)Promo:\s+([a-zA-Z0-9]{4,25})',         # "Promo: ABC123"
    r'(?i)Promo:([a-zA-Z0-9]{4,25})',            # "Promo:ABC123"
    r'(?i)Coupon:\s+([a-zA-Z0-9]{4,25})',        # "Coupon: ABC123"
    r'(?i)Coupon:([a-zA-Z0-9]{4,25})',           # "Coupon:ABC123"
    r'(?i)use\s+(?:code\s+)?([a-zA-Z0-9]{4,25})',  # "use code ABC123"
    r'(?i)enter\s+(?:code\s+)?([a-zA-Z0-9]{4,25})', # "enter code ABC123"
]

# Pattern for extracting both code and value from messages like:
# Code: stakecomlop1n84b
# Value: $3
CODE_VALUE_PATTERN = r'(?i)Code:\s+([a-zA-Z0-9]{4,25})(?:.*?\n.*?Value:\s+\$?(\d+(?:\.\d{1,2})?))?'
CLAIM_URL_BASE = os.getenv("CLAIM_URL_BASE", "https://autoclaim.example.com")
RING_SIZE = int(os.getenv("RING_SIZE", "100"))
DEFAULT_USERNAME = "kustdev"  # Default username for WebSocket connections
app = FastAPI()

# Add CORS middleware to allow all origins for WebSocket connections
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files mount removed since index.html is in root directory

class WSManager:
    def __init__(self):
        self.active: Dict[str, Dict] = {}  # client_id -> { 'ws': WebSocket, 'username': str }
        self.username_map: Dict[str, str] = {}  # username -> client_id
        self.heartbeat_task = None
        self.reconnect_buffers: Dict[str, List[Dict[str, Any]]] = {}
        self.code_ownership: Dict[str, str] = {}  # code -> username
        
    async def connect(self, ws: WebSocket, username: str) -> bool:
        print(f"🔍 WebSocket connection attempt for username: {username}")
        
        # Check if username is authenticated
        if username not in authenticated_users:
            print(f"❌ Authentication failed for username: {username}")
            await ws.close(code=1008, reason="Authentication required")
            return False
            
        # Check if username is already connected
        if username in self.username_map:
            print(f"❌ Username already connected: {username}")
            await ws.close(code=1008, reason="mother fucker buy one more")
            return False
            
        await ws.accept()
        client_id = f"{username}:{ws.client.host}:{ws.client.port}:{id(ws)}"
        self.active[client_id] = {'ws': ws, 'username': username}
        self.username_map[username] = client_id
        
        print(f"✅ WebSocket connected: {client_id}")
        
        # Send buffered messages if reconnecting
        if client_id in self.reconnect_buffers:
            for buffered_msg in self.reconnect_buffers[client_id]:
                try:
                    await ws.send_json(buffered_msg)
                except:
                    pass
            del self.reconnect_buffers[client_id]
            
        # Start heartbeat if first connection
        if len(self.active) == 1 and not self.heartbeat_task:
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return True
        
    async def disconnect(self, ws: WebSocket):
        client_id = None
        for cid, data in list(self.active.items()):
            if data['ws'] == ws:
                client_id = cid
                break
        if client_id:
            username = self.active[client_id]['username']
            print(f"🔌 WebSocket disconnected: {client_id}")
            self.active.pop(client_id, None)
            self.username_map.pop(username, None)
            # Keep buffer for potential reconnection
            if client_id not in self.reconnect_buffers:
                self.reconnect_buffers[client_id] = []
                
    async def broadcast(self, message: Dict[str, Any]):
        if not self.active:
            return
        # Add timestamp for instant delivery
        message["server_ts"] = int(asyncio.get_event_loop().time() * 1000)
        # Track code ownership
        if message.get("type") == "code":
            code = message.get("code")
            if code:
                self.code_ownership[code] = message.get("username", "system")
        # Concurrent broadcast for speed (fire and forget)
        for client_id, data in list(self.active.items()):
            asyncio.create_task(self._send_to_client(client_id, data['ws'], message))
            
    async def _send_to_client(self, client_id: str, ws: WebSocket, message: Dict[str, Any]):
        try:
            await ws.send_json(message)
        except Exception as e:
            print(f"❌ Error sending to client {client_id}: {e}")
            # Remove dead connection
            await self.disconnect(ws)
            
    async def _heartbeat_loop(self):
        while self.active:
            try:
                heartbeat = {
                    "type": "heartbeat",
                    "ts": int(asyncio.get_event_loop().time() * 1000),
                    "active_connections": len(self.active)
                }
                # Send heartbeat to all connections
                dead_clients = []
                for client_id, data in list(self.active.items()):
                    try:
                        await asyncio.wait_for(data['ws'].send_json(heartbeat), timeout=0.05)
                    except:
                        dead_clients.append(client_id)
                # Remove dead connections
                for client_id in dead_clients:
                    if client_id in self.active:
                        username = self.active[client_id]['username']
                        print(f"❌ Removing dead connection: {client_id}")
                        self.active.pop(client_id, None)
                        self.username_map.pop(username, None)
                await asyncio.sleep(5)  # Heartbeat every 5 seconds
            except Exception as e:
                print(f"❌ Heartbeat error: {e}")
                await asyncio.sleep(5)
        self.heartbeat_task = None
        
    def validate_code_ownership(self, code: str, username: str) -> bool:
        """Check if the code belongs to the specified username"""
        return self.code_ownership.get(code) == username

ws_manager = WSManager()
ring: List[Dict[str, Any]] = []
seen: Set[str] = set()

# User authentication system
authenticated_users: Dict[str, datetime] = {}  # username -> expiration time
cleanup_task = None

async def cleanup_expired_users():
    """Periodically remove expired user authentications"""
    global cleanup_task
    while True:
        try:
            now = datetime.now()
            expired_users = [user for user, expiry in authenticated_users.items() if expiry < now]
            
            for user in expired_users:
                del authenticated_users[user]
                print(f"🔒 Removed expired user: {user}")
                
            if expired_users:
                print(f"🧹 Cleaned up {len(expired_users)} expired users")
                
            # Schedule next cleanup in 1 hour
            await asyncio.sleep(3600)
        except Exception as e:
            print(f"❌ Cleanup error: {e}")
            await asyncio.sleep(3600)
            
    cleanup_task = None

def normalize_code(s: str) -> str:
    # Remove non-alphanumeric characters but preserve original case
    return re.sub(r"[^A-Za-z0-9]", "", s)

def extract_codes_with_values(text: str) -> List[Dict[str, Any]]:
    """Extract bonus codes and values using multiple patterns, prioritizing 'Code:' format"""
    if not text:
        return []
    print(f"🔍 Input text: {repr(text)}")  # Debug print to see exact text
    all_codes = []
    
    # First try the CODE_VALUE_PATTERN to extract code and value together
    try:
        pattern = re.compile(CODE_VALUE_PATTERN, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        matches = pattern.findall(text)
        print(f"🔍 Code+Value pattern -> Found: {matches}")  # Debug print
        
        for match in matches:
            if isinstance(match, tuple) and len(match) >= 1:
                code = match[0].strip()
                value = match[1] if len(match) > 1 and match[1] else None
                if code:
                    all_codes.append({"code": code, "value": value})
                    print(f"🎯 Found code with value: {code} = ${value}" if value else f"🎯 Found code: {code}")
    except Exception as e:
        print(f"⚠️ Code+Value pattern error: {e}")
    # Then try all regular patterns (only for codes without values)
    for i, pattern_str in enumerate(CODE_PATTERNS):
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE | re.MULTILINE)
            matches = pattern.findall(text)
            print(f"🔍 Pattern {i+1}: {pattern_str} -> Found: {matches}")  # Debug print
            # Handle both string and tuple results
            for match in matches:
                if isinstance(match, tuple):
                    for group in match:
                        if group:
                            code = group.strip()
                            # Check if this code was already found with a value
                            already_exists = any(existing["code"] == code for existing in all_codes)
                            if not already_exists:
                                all_codes.append({"code": code, "value": None})
                else:
                    code = match.strip()
                    # Check if this code was already found with a value
                    already_exists = any(existing["code"] == code for existing in all_codes)
                    if not already_exists:
                        all_codes.append({"code": code, "value": None})
        except Exception as e:
            print(f"⚠️ Pattern error for {pattern_str}: {e}")
            continue
    
    print(f"🔍 All extracted codes before filtering: {all_codes}")  # Debug print
    
    # Filter and validate codes
    valid_codes = []
    for item in all_codes:
        code = item["code"]
        value = item["value"]
        # Remove any non-alphanumeric characters
        cleaned_code = re.sub(r"[^A-Za-z0-9]", "", code)
        # Valid codes: 4-25 characters, alphanumeric
        if 4 <= len(cleaned_code) <= 25 and cleaned_code.isalnum():
            valid_codes.append({"code": cleaned_code, "value": value})
    # Remove duplicates while preserving order
    unique_codes = []
    for item in valid_codes:
        code = item["code"]
        if not any(existing["code"] == code for existing in unique_codes):
            unique_codes.append(item)
    
    print(f"🔍 Final extracted codes: {unique_codes}")
    return unique_codes

def extract_codes(text: str) -> List[str]:
    """Legacy function for backward compatibility - extracts only codes"""
    codes_with_values = extract_codes_with_values(text)
    return [item["code"] for item in codes_with_values]

def ring_add(entry: Dict[str, Any]):
    ring.append(entry)
    if len(ring) > RING_SIZE:
        ring.pop(0)

def ring_latest() -> Optional[Dict[str, Any]]:
    return ring[-1] if ring else None

tg_client = None

async def ensure_tg():
    global tg_client
    if tg_client:
        return tg_client
    print(f"🔐 Creating Telegram client with session: {TG_SESSION}")
    print(f"🔐 API_ID: {TG_API_ID}")
    print(f"🔐 API_HASH: {'*' * len(TG_API_HASH) if TG_API_HASH else 'Not set'}")
    try:
        client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
        print("🔌 Attempting to connect to Telegram...")
        await client.connect()
        print("✅ Connected to Telegram successfully!")
        print("🔍 Checking authorization status...")
        if not await client.is_user_authorized():
            print("❌ Session not authorized!")
            phone = os.getenv("TG_PHONE")
            login_code = os.getenv("TG_LOGIN_CODE")
            if not phone or not login_code:
                raise RuntimeError("Session not authorized. You need to create a session file locally first. See RENDER_DEPLOYMENT_GUIDE.md")
            print(f"📱 Attempting login with phone: {phone}")
            await client.send_code_request(phone)
            try:
                await client.sign_in(phone=phone, code=login_code)
                print("✅ Signed in successfully!")
            except SessionPasswordNeededError:
                print("🔐 2FA required...")
                pw = os.getenv("TG_2FA_PASSWORD")
                if not pw:
                    raise RuntimeError("2FA required. Set TG_2FA_PASSWORD environment variable.")
                await client.sign_in(password=pw)
                print("✅ 2FA authentication successful!")
        else:
            print("✅ Session already authorized!")
        tg_client = client
        print("🎉 Telegram client setup complete!")
        return client
    except Exception as e:
        print(f"❌ TELEGRAM CLIENT ERROR: {e}")
        print(f"❌ Error type: {type(e).__name__}")
        raise e

async def start_listener():
    global telegram_connected
    try:
        print("🚀 Starting Telegram listener...")
        client = await ensure_tg()
        print(f"🎯 Current CHANNELS environment variable: '{CHANNELS}'")
        print(f"🎯 CHANNELS type: {type(CHANNELS)}")
        # Parse multiple channels from comma-separated string
        channel_list = [ch.strip() for ch in CHANNELS.split(',') if ch.strip()]
        print(f"📋 Parsed {len(channel_list)} channels: {channel_list}")
        # Channel mapping for better tracking
        channel_names = {}
        # First, let's list available dialogs to help find the correct channels
        print("🔍 Listing your available chats/channels:")
        async for dialog in client.iter_dialogs():
            print(f"  📱 {dialog.name} (ID: {dialog.id}, Username: {getattr(dialog.entity, 'username', 'None')})")
        # Validate each channel and build list of valid channels
        valid_channels = []
        for i, channel in enumerate(channel_list):
            try:
                print(f"🔍 [{i+1}/{len(channel_list)}] Attempting to get entity for: {channel}")
                entity = await client.get_entity(int(channel) if channel.lstrip('-').isdigit() else channel)
                channel_name = entity.title if hasattr(entity, 'title') else (entity.first_name or f"Channel_{entity.id}")
                channel_names[str(entity.id)] = channel_name
                print(f"✅ [{i+1}] Successfully connected to: {channel_name} (ID: {entity.id})")
                # Convert to proper format for event listener
                channel_for_events = int(channel) if channel.lstrip('-').isdigit() else channel
                valid_channels.append(channel_for_events)
            except Exception as e:
                print(f"❌ [{i+1}] Could not access channel {channel}: {e}")
                print(f"   Make sure you're a member of this channel/chat")
                print("   Skipping this channel...")
        if not valid_channels:
            print("❌ No valid channels found. Please check your CHANNELS environment variable")
            return
        print(f"🎯 Setting up event listener for {len(valid_channels)} channels: {valid_channels}")
        @client.on(events.NewMessage(chats=valid_channels))
        async def handler(ev):
            channel_name = channel_names.get(str(ev.chat_id), f"Unknown_{ev.chat_id}")
            print(f"📨 NEW MESSAGE from {channel_name} ({ev.chat_id})")
            print(f"📨 Content: {ev.message.message[:100] if ev.message.message else '[No text]'}...")
            # Get message text
            text = (ev.message.message or "")
            # Add caption if it exists (for media messages)
            if hasattr(ev.message, 'caption') and ev.message.caption:
                text += "\n" + ev.message.caption
            print(f"🔍 Full text to process: {text[:300]}...")
            # Extract codes using enhanced patterns (with values)
            codes_with_values = extract_codes_with_values(text)
            print(f"🎯 Extracted {len(codes_with_values)} codes: {codes_with_values}")
            if not codes_with_values:
                print("❌ No valid codes found in message")
                return
            ts = int(ev.message.date.timestamp() * 1000)
            broadcast_count = 0
            for item in codes_with_values:
                code = item["code"]
                value = item["value"]
                if code in seen:
                    print(f"⚠️ Code {code} already seen, skipping")
                    continue
                seen.add(code)
                # Enhanced entry with more metadata
                entry = {
                    "type": "code",
                    "code": code,
                    "ts": ts,
                    "msg_id": ev.message.id,
                    "channel": str(ev.chat_id),
                    "channel_name": channel_name,
                    "claim_base": CLAIM_URL_BASE,
                    "priority": "instant",
                    "telegram_ts": ts,
                    "broadcast_ts": int(asyncio.get_event_loop().time() * 1000),
                    "source": "telegram",
                    "message_preview": text[:100],
                    "username": "system"  # Default to system since no username yet
                }
                
                # Add value to entry if it exists
                if value:
                    entry["value"] = value
                ring_add(entry)
                broadcast_count += 1
                print(f"🚀 BROADCASTING #{broadcast_count} to {len(ws_manager.active)} connections")
                print(f"   Code: {code}")
                if value:
                    print(f"   Value: ${value}")
                print(f"   Source: {channel_name}")
                print(f"   Time: {ev.message.date}")
                # Fire and forget broadcast
                asyncio.create_task(ws_manager.broadcast(entry))
            print(f"⚡ Total codes broadcasted: {broadcast_count}")
        # Update global status
        telegram_connected = True
        print("🎉 Event listener setup complete! Ready to receive messages...")
        print("🚀 Starting Telegram client and listening for messages...")
        await client.start()
        print("✅ Telegram client started successfully!")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"❌ LISTENER ERROR: {e}")
        print(f"❌ Error type: {type(e).__name__}")
        telegram_connected = False

@app.get("/")
async def root(request: Request):
    # Add debug info to help with WebSocket connection
    host = request.headers.get("host", "unknown")
    scheme = request.url.scheme
    base_url = f"{scheme}://{host}"
    ws_url = f"{'wss' if scheme == 'https' else 'ws'}://{host}/ws?user={DEFAULT_USERNAME}"
    
    print(f"🌐 Serving index.html to client")
    print(f"🌐 Base URL: {base_url}")
    print(f"🌐 WebSocket URL: {ws_url}")
    
    return FileResponse('index.html')

@app.get("/api")
@app.head("/api")
async def api_root():
    return JSONResponse({"status": "running", "endpoints": ["/health", "/latest", "/version", "/ws", "/add"]})

@app.on_event("startup")
async def startup_event():
    print("🚀 STAKE ULTRA CLAIMER - VPS DEPLOYMENT")
    print("=" * 50)
    print(f"📡 Server starting on port {PORT}")
    print(f"🔑 TG_API_ID configured: {'✅' if TG_API_ID else '❌'}")
    print(f"🔑 TG_API_HASH configured: {'✅' if TG_API_HASH else '❌'}")
    print(f"📺 CHANNELS configured: {'✅' if CHANNELS else '❌'}")
    print(f"👤 Default username: {DEFAULT_USERNAME}")
    
    # Get server IP for display
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        print(f"🌐 Server hostname: {hostname}")
        print(f"🌐 Server local IP: {local_ip}")
    except:
        print("🌐 Could not determine server hostname/IP")
    
    # Start user cleanup task
    global cleanup_task
    cleanup_task = asyncio.create_task(cleanup_expired_users())
    print("🔒 Started user authentication cleanup task")
    
    # Auto-authenticate default user
    global authenticated_users
    default_expiration = datetime.now() + timedelta(days=365)  # 1 year expiration
    authenticated_users[DEFAULT_USERNAME] = default_expiration
    print(f"🔓 Auto-authenticated default user: {DEFAULT_USERNAME} (expires: {default_expiration})")
    
    # Add some test codes for immediate testing
    test_codes = ["STAKE123", "BONUS456", "PROMO789"]
    for i, code in enumerate(test_codes):
        if code not in seen:
            seen.add(code)
            entry = {
                "type": "code",
                "code": code,
                "ts": int(asyncio.get_event_loop().time() * 1000) + i,
                "msg_id": 9999 + i,
                "channel": "startup_test",
                "channel_name": "Test Channel",
                "claim_base": CLAIM_URL_BASE,
                "source": "startup_test",
                "username": "system"
            }
            ring_add(entry)
            print(f"🧪 Added test code: {code}")
            
    if TG_API_ID and TG_API_HASH and CHANNELS:
        channel_count = len([ch.strip() for ch in CHANNELS.split(',') if ch.strip()])
        print(f"🚀 Starting Telegram listener for {channel_count} channels: {CHANNELS}")
        print("⚡ Ultra-fast code extraction and broadcasting enabled")
        asyncio.create_task(start_listener())
    else:
        print("❌ STARTUP ERROR: Missing Telegram configuration")
        print("   Required environment variables:")
        print("   - TG_API_ID (Telegram API ID)")
        print("   - TG_API_HASH (Telegram API Hash)")
        print("   - CHANNELS (Channel IDs separated by comma)")
        print("   Example: CHANNELS=-1002772030545,-1001234567890")

@app.get("/health")
@app.head("/health")
async def health():
    return PlainTextResponse("OK", 200)

@app.get("/status")
async def status():
    """Detailed status endpoint for monitoring"""
    telegram_status = False
    telegram_error = None
    session_exists = False
    try:
        import os
        session_exists = os.path.exists(f"{TG_SESSION}.session")
        if tg_client:
            telegram_status = tg_client.is_connected() and await tg_client.is_user_authorized()
    except Exception as e:
        telegram_error = str(e)
    return JSONResponse({
        "telegram_client": telegram_status,
        "telegram_error": telegram_error,
        "session_file_exists": session_exists,
        "session_path": f"{TG_SESSION}.session",
        "websocket_connections": len(ws_manager.active),
        "codes_in_history": len(ring),
        "server_time": int(asyncio.get_event_loop().time() * 1000),
        "channels_configured": CHANNELS,
        "api_id_set": bool(TG_API_ID),
        "api_hash_set": bool(TG_API_HASH and len(TG_API_HASH) > 5),
        "authenticated_users": len(authenticated_users),
        "auth_cleanup_running": cleanup_task is not None,
        "default_user": DEFAULT_USERNAME
    }, 200)

@app.get("/latest")
async def latest():
    return JSONResponse(ring_latest() or {}, 200)

@app.get("/api/codes")
async def api_codes():
    """API endpoint for userscript to fetch available codes - returns empty array (only WebSocket delivers new codes)"""
    current_time = int(asyncio.get_event_loop().time() * 1000)
    response = {
        "codes": [],
        "latest_updated": current_time,
        "total_codes": 0,
        "method": "websocket_only",
        "message": "Use WebSocket connection for real-time codes"
    }
    print(f"📡 API request for codes: directing to use WebSocket for real-time codes")
    return JSONResponse(response, 200)

@app.get("/version")
async def version():
    return JSONResponse({"v":"1.0.0"}, 200)

@app.post("/test-code")
async def test_code(request: dict):
    """Test endpoint to simulate receiving a Telegram code"""
    test_code = request.get("code", "TEST123")
    if test_code in seen:
        return JSONResponse({"status": "already_seen", "code": test_code}, 200)
    seen.add(test_code)
    entry = {
        "type": "code",
        "code": test_code,
        "ts": int(asyncio.get_event_loop().time() * 1000),
        "msg_id": 999,
        "channel": "test",
        "claim_base": CLAIM_URL_BASE,
        "username": "system"
    }
    ring_add(entry)
    print(f"🧪 Broadcasting test code to {len(ws_manager.active)} WebSocket connections: {test_code}")
    await ws_manager.broadcast(entry)
    print(f"✅ Test code broadcasted successfully: {test_code}")
    return JSONResponse({"status": "sent", "code": test_code, "active_connections": len(ws_manager.active)}, 200)

@app.get("/send-test-code/{code}")
async def send_test_code_get(code: str):
    """Quick test endpoint to send a code via GET request"""
    if code in seen:
        return JSONResponse({"status": "already_seen", "code": code}, 200)
    seen.add(code)
    entry = {
        "type": "code",
        "code": code,
        "ts": int(asyncio.get_event_loop().time() * 1000),
        "msg_id": 999,
        "channel": "test",
        "claim_base": CLAIM_URL_BASE,
        "username": "system"
    }
    ring_add(entry)
    print(f"🧪 Broadcasting test code to {len(ws_manager.active)} WebSocket connections: {code}")
    await ws_manager.broadcast(entry)
    print(f"✅ Test code broadcasted successfully: {code}")
    return JSONResponse({"status": "sent", "code": code, "active_connections": len(ws_manager.active)}, 200)

@app.get("/add")
async def add_user(username: str = Query(...), plan: str = Query(...)):
    """Add a user with time-based authentication"""
    global authenticated_users
    
    # Validate plan
    if plan not in ["24hours", "168hours"]:
        return JSONResponse({
            "status": "error",
            "message": "Invalid plan. Must be '24hours' or '168hours'"
        }, 400)
    
    # Calculate expiration time
    hours = 24 if plan == "24hours" else 168
    expiration = datetime.now() + timedelta(hours=hours)
    
    # Add or update user authentication
    authenticated_users[username] = expiration
    
    # Log the action
    print(f"🔒 Added user: {username} with plan: {plan} (expires: {expiration})")
    
    return JSONResponse({
        "status": "success",
        "username": username,
        "plan": plan,
        "expires": expiration.isoformat(),
        "message": f"User {username} authenticated for {hours} hours"
    }, 200)

@app.on_event("shutdown")
async def shutdown_event():
    """Clean shutdown of services"""
    # Cancel cleanup task
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        print("🛑 User cleanup task stopped")

@app.websocket("/ws")
async def ws(ws: WebSocket, user: str = Query(..., alias="user")):
    # Get username from query parameter
    username = user
    if not username:
        print("❌ WebSocket connection rejected: No username provided")
        await ws.close(code=1008, reason="Username required")
        return
        
    client_info = f"{ws.client.host}:{ws.client.port}"
    print(f"🔌 New WebSocket connection from {client_info} with username: {username}")
    
    try:
        # Connect with username validation
        connected = await ws_manager.connect(ws, username)
        if not connected:
            print(f"❌ Rejected connection for username: {username} (authentication failed)")
            return
            
        print(f"✅ WebSocket connected instantly. Active: {len(ws_manager.active)}")
        
        # Send immediate connection confirmation with latest code if available
        welcome_msg = {
            "type": "connected",
            "server_port": PORT,
            "status": "ready",
            "username": username,
            "server_ts": int(asyncio.get_event_loop().time() * 1000)
        }
        
        # Include latest code if available
        latest = ring_latest()
        if latest:
            welcome_msg["latest_code"] = latest
            
        await ws.send_json(welcome_msg)
        
        # Keep connection alive with minimal overhead
        while True:
            try:
                # Set very short timeout to detect disconnects quickly
                message = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
                # Handle client ping/pong for connection verification
                if message == "ping":
                    await ws.send_json({
                        "type": "pong",
                        "ts": int(asyncio.get_event_loop().time() * 1000)
                    })
                # Handle code validation requests
                elif message.startswith("validate:"):
                    parts = message.split(":", 2)
                    if len(parts) == 3:
                        _, code, user = parts
                        if ws_manager.validate_code_ownership(code, user):
                            await ws.send_json({
                                "type": "validation_result",
                                "code": code,
                                "valid": True
                            })
                        else:
                            await ws.send_json({
                                "type": "validation_result",
                                "code": code,
                                "valid": False,
                                "message": "mother fucker buy one more"
                            })
            except asyncio.TimeoutError:
                # Timeout is normal, just continue the loop
                continue
            except WebSocketDisconnect:
                print(f"🔌 Client {client_info} disconnected cleanly")
                break
            except Exception as e:
                print(f"❌ WebSocket error for {client_info}: {e}")
                break
    except Exception as e:
        print(f"❌ WebSocket setup error for {client_info}: {e}")
    finally:
        await ws_manager.disconnect(ws)
        print(f"🔌 WebSocket {client_info} cleaned up. Active: {len(ws_manager.active)}")

# Server startup
if __name__ == "__main__":
    # Get the local IP address for display
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except:
        local_ip = "0.0.0.0"
    
    print(f"🚀 Starting server on http://{local_ip}:{PORT}")
    print(f"🚀 WebSocket endpoint: ws://{local_ip}:{PORT}/ws?user={DEFAULT_USERNAME}")
    print(f"🚀 For external access, use: http://<your-server-ip>:{PORT}")
    print(f"🚀 WebSocket external: ws://<your-server-ip>:{PORT}/ws?user={DEFAULT_USERNAME}")
    
    # Check if port is available
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('0.0.0.0', PORT))
        print(f"✅ Port {PORT} is available")
    except OSError as e:
        print(f"❌ Port {PORT} is already in use: {e}")
        exit(1)
    
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
