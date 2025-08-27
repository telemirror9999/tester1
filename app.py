# app.py (integrated with MongoDB)
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
from fastapi import HTTPException, status
import base64
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
# MongoDB Configuration
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://frozenbotss:frozenbots@cluster0.s0tak.mongodb.net/?retryWrites=true&w=majority")
MONGODB_DB_NAME = "stake_autoclaimer"
MONGODB_COLLECTION = "premium_users"
PORT = int(os.getenv("PORT", "5001"))
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
DEFAULT_USERNAME = "kustdev"  
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
# MongoDB Client
mongo_client = None
db = None
premium_users_collection = None
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
            
        # If username is already connected, close the old connection
        if username in self.username_map:
            old_client_id = self.username_map[username]
            if old_client_id in self.active:
                old_ws = self.active[old_client_id]['ws']
                # Remove the old connection from active and username_map
                del self.active[old_client_id]
                del self.username_map[username]
                print(f"🔄 Removing old connection for {username} ({old_client_id})")
                try:
                    await old_ws.close(code=1000, reason="New connection from same user")
                    print(f"🔄 Closed old connection for {username} ({old_client_id})")
                except Exception as e:
                    print(f"⚠️ Error closing old connection for {username}: {e}")
            else:
                # If the old_client_id is not in active, just remove from username_map
                del self.username_map[username]
                print(f"🔄 Removed stale mapping for {username} (client_id: {old_client_id})")
            
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
periodic_sync_task = None
async def init_mongodb():
    """Initialize MongoDB connection"""
    global mongo_client, db, premium_users_collection
    try:
        mongo_client = AsyncIOMotorClient(MONGODB_URI)
        db = mongo_client[MONGODB_DB_NAME]
        premium_users_collection = db[MONGODB_COLLECTION]
        print("✅ Connected to MongoDB successfully")
        return True
    except Exception as e:
        print(f"❌ MongoDB connection error: {e}")
        return False
async def load_premium_users():
    """Load premium users from MongoDB into memory"""
    global authenticated_users
    if premium_users_collection is None:
        return
    
    try:
        authenticated_users.clear()
        cursor = premium_users_collection.find({"expires_at": {"$gt": datetime.now()}})
        async for user in cursor:
            authenticated_users[user["username"]] = user["expires_at"]
        print(f"✅ Loaded {len(authenticated_users)} premium users from MongoDB")
    except Exception as e:
        print(f"❌ Error loading premium users: {e}")
async def sync_users_from_mongodb():
    """Sync premium users from MongoDB into memory"""
    global authenticated_users
    if premium_users_collection is None:
        return False
    
    try:
        # Get current users before sync
        before_count = len(authenticated_users)
        
        # Clear and reload from MongoDB
        authenticated_users.clear()
        cursor = premium_users_collection.find({"expires_at": {"$gt": datetime.now()}})
        async for user in cursor:
            authenticated_users[user["username"]] = user["expires_at"]
        
        after_count = len(authenticated_users)
        print(f"✅ Synced {after_count} premium users from MongoDB (was {before_count})")
        return True
    except Exception as e:
        print(f"❌ Error syncing premium users: {e}")
        return False
async def periodic_sync():
    """Periodically sync users from MongoDB every 5 minutes"""
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            print("🔄 Starting periodic sync from MongoDB...")
            await sync_users_from_mongodb()
        except Exception as e:
            print(f"❌ Periodic sync error: {e}")
            await asyncio.sleep(60)  # Wait a minute before retrying
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
    # Check for Basic Authentication
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"}
        )
    
    try:
        # Decode the credentials
        encoded_credentials = auth_header.split(" ")[1]
        decoded_credentials = base64.b64decode(encoded_credentials).decode("utf-8")
        username, password = decoded_credentials.split(":", 1)
    except:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"}
        )
    
    # Verify password (username is ignored)
    if password != "4y3q9y4q5r8qy49rq4nr3qy3y46y36437eq0hdu9e8wy8":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"}
        )
    
    # Original code continues here
    host = request.headers.get("host", "unknown")
    scheme = request.url.scheme
    base_url = f"{scheme}://{host}"
    ws_url = f"{'wss' if scheme == 'https' else 'ws'}://{host}/ws?user={DEFAULT_USERNAME}"
    
    print(f"🌐 Serving index.html to client")
    print(f"🌐 Base URL: {base_url}")
    print(f"🌐 WebSocket URL: {ws_url}")
    
    # Get current user connections
    user_connections = []
    now = datetime.now()
    for username, expiry in authenticated_users.items():
        if expiry > now:
            delta = expiry - now
            days = delta.days
            hours, remainder = divmod(delta.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            time_left = f"{days}d {hours}h {minutes}m"
        else:
            time_left = "Expired"
        
        user_connections.append({
            "username": username,
            "time_left": time_left,
            "expires": expiry.isoformat()
        })
    
    # Render HTML with user connections
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Stake Auto Claimer - User Management</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root {{
                --primary: #6a11cb;
                --secondary: #2575fc;
                --success: #2ecc71;
                --danger: #e74c3c;
                --warning: #f39c12;
                --info: #3498db;
                --dark: #1a1a2e;
                --darker: #0f0f1e;
                --light: #f8f9fa;
                --glass: rgba(255, 255, 255, 0.1);
                --glass-border: rgba(255, 255, 255, 0.2);
            }}
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            }}
            body {{
                background: linear-gradient(135deg, var(--darker) 0%, var(--dark) 100%);
                color: var(--light);
                min-height: 100vh;
                overflow-x: hidden;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                text-align: center;
                margin-bottom: 30px;
                padding: 20px;
            }}
            .header h1 {{
                font-size: 2.5rem;
                background: linear-gradient(45deg, var(--primary), var(--secondary));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin-bottom: 10px;
            }}
            .header p {{
                color: #aaa;
            }}
            .panel {{
                background: var(--glass);
                backdrop-filter: blur(10px);
                border: 1px solid var(--glass-border);
                border-radius: 15px;
                padding: 20px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
                margin-bottom: 20px;
            }}
            .panel-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
                padding-bottom: 10px;
                border-bottom: 1px solid var(--glass-border);
            }}
            .panel-title {{
                font-size: 1.5rem;
                font-weight: 600;
            }}
            .user-table {{
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 20px;
            }}
            .user-table th, .user-table td {{
                padding: 12px;
                text-align: left;
                border-bottom: 1px solid var(--glass-border);
            }}
            .user-table th {{
                background: rgba(255, 255, 255, 0.1);
            }}
            .user-table tr:hover {{
                background: rgba(255, 255, 255, 0.05);
            }}
            .btn {{
                display: inline-block;
                padding: 10px 15px;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s ease;
                text-align: center;
                text-decoration: none;
            }}
            .btn-danger {{
                background: var(--danger);
                color: white;
            }}
            .btn-danger:hover {{
                background: #c0392b;
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(231, 76, 60, 0.4);
            }}
            .btn-success {{
                background: var(--success);
                color: white;
            }}
            .btn-success:hover {{
                background: #27ae60;
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(46, 204, 113, 0.4);
            }}
            .btn-primary {{
                background: linear-gradient(45deg, var(--primary), var(--secondary));
                color: white;
            }}
            .btn-primary:hover {{
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(106, 17, 203, 0.4);
            }}
            .btn-info {{
                background: var(--info);
                color: white;
            }}
            .btn-info:hover {{
                background: #2980b9;
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(52, 152, 219, 0.4);
            }}
            .add-user-form {{
                display: flex;
                flex-wrap: wrap;
                gap: 15px;
                align-items: flex-end;
            }}
            .form-group {{
                flex: 1;
                min-width: 200px;
            }}
            .form-group label {{
                display: block;
                margin-bottom: 5px;
                font-weight: 600;
            }}
            .form-group input, .form-group select {{
                width: 100%;
                padding: 10px;
                border: none;
                border-radius: 8px;
                background: rgba(255, 255, 255, 0.1);
                color: var(--light);
            }}
            .form-group select {{
                cursor: pointer;
            }}
            .stats {{
                display: flex;
                justify-content: space-around;
                margin-bottom: 20px;
            }}
            .stat {{
                text-align: center;
            }}
            .stat-value {{
                font-size: 2rem;
                font-weight: 700;
                color: var(--primary);
            }}
            .stat-label {{
                color: #aaa;
            }}
            .notification {{
                position: fixed;
                top: 20px;
                right: 20px;
                padding: 15px 20px;
                border-radius: 10px;
                background: var(--glass);
                backdrop-filter: blur(10px);
                border: 1px solid var(--glass-border);
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
                z-index: 1000;
                max-width: 300px;
                display: none;
            }}
            .notification.success {{
                border-left: 4px solid var(--success);
            }}
            .notification.error {{
                border-left: 4px solid var(--danger);
            }}
            .notification-title {{
                font-weight: 600;
                margin-bottom: 5px;
            }}
            .notification-message {{
                font-size: 0.9rem;
            }}
            .notification-close {{
                position: absolute;
                top: 10px;
                right: 10px;
                background: none;
                border: none;
                color: var(--light);
                cursor: pointer;
                font-size: 1rem;
            }}
            .sync-status {{
                display: inline-block;
                margin-left: 10px;
                font-size: 0.9rem;
                color: var(--success);
            }}
            @media (max-width: 768px) {{
                .add-user-form {{
                    flex-direction: column;
                }}
                .form-group {{
                    width: 100%;
                }}
                .stats {{
                    flex-direction: column;
                    gap: 15px;
                }}
                .panel-header {{
                    flex-direction: column;
                    align-items: flex-start;
                }}
                .panel-header .btn {{
                    margin-top: 10px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1><i class="fas fa-rocket"></i> Stake Auto Claimer</h1>
                <p>User Management Dashboard</p>
            </div>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{len(user_connections)}</div>
                    <div class="stat-label">Active Users</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{len(ws_manager.active)}</div>
                    <div class="stat-label">Connected Clients</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{len(ring)}</div>
                    <div class="stat-label">Codes Claimed</div>
                </div>
            </div>
            
            <div class="panel">
                <div class="panel-header">
                    <h2 class="panel-title"><i class="fas fa-users"></i> User Management</h2>
                    <div>
                        <button class="btn btn-info" onclick="syncUsers()">
                            <i class="fas fa-sync"></i> Sync Users
                        </button>
                        <button class="btn btn-primary" onclick="window.location.href='/dashboard'">
                            <i class="fas fa-tachometer-alt"></i> Go to Dashboard
                        </button>
                    </div>
                </div>
                
                <table class="user-table">
                    <thead>
                        <tr>
                            <th>Username</th>
                            <th>Time Left</th>
                            <th>Expires</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
    """
    
    for user in user_connections:
        html_content += f"""
                        <tr>
                            <td>{user['username']}</td>
                            <td>{user['time_left']}</td>
                            <td>{user['expires'][:10]}</td>
                            <td>
                                <button class="btn btn-danger" onclick="deleteUser('{user['username']}')">
                                    <i class="fas fa-trash"></i> Delete
                                </button>
                            </td>
                        </tr>
        """
    
    if not user_connections:
        html_content += """
                        <tr>
                            <td colspan="4" style="text-align: center; color: #aaa;">No users found</td>
                        </tr>
        """
    
    html_content += """
                    </tbody>
                </table>
                
                <div class="add-user-form">
                    <div class="form-group">
                        <label for="username">Username</label>
                        <input type="text" id="username" placeholder="Enter username">
                    </div>
                    <div class="form-group">
                        <label for="plan">Plan</label>
                        <select id="plan">
                            <option value="1day">1 Day</option>
                            <option value="7days">7 Days</option>
                            <option value="lifetime">Lifetime</option>
                        </select>
                    </div>
                    <button class="btn btn-success" onclick="addUser()">
                        <i class="fas fa-user-plus"></i> Add User
                    </button>
                </div>
            </div>
        </div>
        
        <div class="notification" id="notification">
            <button class="notification-close" onclick="closeNotification()">&times;</button>
            <div class="notification-title" id="notificationTitle"></div>
            <div class="notification-message" id="notificationMessage"></div>
        </div>
        
        <script>
            function showNotification(title, message, type) {
                const notification = document.getElementById('notification');
                const titleEl = document.getElementById('notificationTitle');
                const messageEl = document.getElementById('notificationMessage');
                
                titleEl.textContent = title;
                messageEl.textContent = message;
                
                notification.className = 'notification ' + type;
                notification.style.display = 'block';
                
                setTimeout(() => {
                    notification.style.display = 'none';
                }, 5000);
            }
            
            function closeNotification() {
                document.getElementById('notification').style.display = 'none';
            }
            
            async function addUser() {
                const username = document.getElementById('username').value.trim();
                const plan = document.getElementById('plan').value;
                
                if (!username) {
                    showNotification('Error', 'Please enter a username', 'error');
                    return;
                }
                
                try {
                    const response = await fetch('/admin/add_user', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ username, plan })
                    });
                    
                    const data = await response.json();
                    
                    if (data.status === 'success') {
                        showNotification('Success', data.message, 'success');
                        document.getElementById('username').value = '';
                        setTimeout(() => window.location.reload(), 1000);
                    } else {
                        showNotification('Error', data.message, 'error');
                    }
                } catch (error) {
                    showNotification('Error', 'Failed to add user', 'error');
                    console.error('Error:', error);
                }
            }
            
            async function deleteUser(username) {
                if (!confirm(`Are you sure you want to delete user "${username}"?`)) {
                    return;
                }
                
                try {
                    const response = await fetch('/admin/delete_user', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ username })
                    });
                    
                    const data = await response.json();
                    
                    if (data.status === 'success') {
                        showNotification('Success', data.message, 'success');
                        setTimeout(() => window.location.reload(), 1000);
                    } else {
                        showNotification('Error', data.message, 'error');
                    }
                } catch (error) {
                    showNotification('Error', 'Failed to delete user', 'error');
                    console.error('Error:', error);
                }
            }
            
            async function syncUsers() {
                const syncButton = document.querySelector('.btn-info');
                const originalText = syncButton.innerHTML;
                syncButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Syncing...';
                syncButton.disabled = true;
                
                try {
                    const response = await fetch('/admin/sync_users', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        }
                    });
                    
                    const data = await response.json();
                    
                    if (data.status === 'success') {
                        showNotification('Success', data.message, 'success');
                        setTimeout(() => window.location.reload(), 1000);
                    } else {
                        showNotification('Error', data.message, 'error');
                    }
                } catch (error) {
                    showNotification('Error', 'Failed to sync users', 'error');
                    console.error('Error:', error);
                } finally {
                    syncButton.innerHTML = originalText;
                    syncButton.disabled = false;
                }
            }
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)
@app.get("/dashboard")
async def dashboard(request: Request):
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
    print(f"🗄️ MongoDB URI configured: {'✅' if MONGODB_URI else '❌'}")
    
    # Get server IP for display
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        print(f"🌐 Server hostname: {hostname}")
        print(f"🌐 Server local IP: {local_ip}")
    except:
        print("🌐 Could not determine server hostname/IP")
    
    # Initialize MongoDB
    if not await init_mongodb():
        print("❌ Failed to connect to MongoDB. Continuing without database.")
    
    # Load premium users from MongoDB
    if premium_users_collection is not None:
        await load_premium_users()
    
    # Start user cleanup task
    global cleanup_task, periodic_sync_task
    cleanup_task = asyncio.create_task(cleanup_expired_users())
    print("🔒 Started user authentication cleanup task")
    
    # Start periodic sync task
    periodic_sync_task = asyncio.create_task(periodic_sync())
    print("🔄 Started periodic MongoDB sync task (every 5 minutes)")
    
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
@app.get("/server_status")  # Renamed from /status to avoid conflict
async def server_status():
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
    
    mongodb_status = False
    mongodb_error = None
    try:
        if mongo_client:
            # Ping MongoDB to check connection
            await mongo_client.admin.command('ping')
            mongodb_status = True
    except Exception as e:
        mongodb_error = str(e)
    
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
        "periodic_sync_running": periodic_sync_task is not None,
        "default_user": DEFAULT_USERNAME,
        "mongodb_status": mongodb_status,
        "mongodb_error": mongodb_error,
        "mongodb_uri_set": bool(MONGODB_URI)
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
    
    # Add or update user authentication in memory
    authenticated_users[username] = expiration
    
    # Add user to MongoDB if available
    if premium_users_collection is not None:
        try:
            user_data = {
                "username": username,
                "plan": plan,
                "created_at": datetime.now(),
                "expires_at": expiration
            }
            await premium_users_collection.replace_one(
                {"username": username},
                user_data,
                upsert=True
            )
            print(f"✅ User {username} added to MongoDB with plan {plan}")
        except Exception as e:
            print(f"❌ Error adding user to MongoDB: {e}")
    
    # Log the action
    print(f"🔒 Added user: {username} with plan: {plan} (expires: {expiration})")
    
    return JSONResponse({
        "status": "success",
        "username": username,
        "plan": plan,
        "expires": expiration.isoformat(),
        "message": f"User {username} authenticated for {hours} hours"
    }, 200)
# Admin endpoints for user management
@app.post("/admin/add_user")
async def add_user_api(request: dict):
    """Add a user with a selected plan"""
    username = request.get("username")
    plan = request.get("plan")
    
    if not username or not plan:
        return JSONResponse({
            "status": "error",
            "message": "Missing username or plan"
        }, 400)
    
    # Calculate expiration based on plan
    now = datetime.now()
    if plan == "1day":
        expiration = now + timedelta(days=1)
    elif plan == "7days":
        expiration = now + timedelta(days=7)
    elif plan == "lifetime":
        expiration = now + timedelta(days=365*100)  # 100 years
    else:
        return JSONResponse({
            "status": "error",
            "message": "Invalid plan"
        }, 400)
    
    # Add or update user authentication in memory
    authenticated_users[username] = expiration
    
    # Add user to MongoDB if available
    if premium_users_collection is not None:
        try:
            user_data = {
                "username": username,
                "plan": plan,
                "created_at": now,
                "expires_at": expiration
            }
            await premium_users_collection.replace_one(
                {"username": username},
                user_data,
                upsert=True
            )
            print(f"✅ User {username} added to MongoDB with plan {plan}")
        except Exception as e:
            print(f"❌ Error adding user to MongoDB: {e}")
            return JSONResponse({
                "status": "error",
                "message": f"Failed to add user to database: {str(e)}"
            }, 500)
    
    # Log the action
    print(f"🔒 Added user: {username} with plan: {plan} (expires: {expiration})")
    
    return JSONResponse({
        "status": "success",
        "message": f"User {username} added with {plan} plan"
    })
@app.post("/admin/delete_user")
async def delete_user_api(request: dict):
    """Delete a user"""
    username = request.get("username")
    
    if not username:
        return JSONResponse({
            "status": "error",
            "message": "Missing username"
        }, 400)
    
    # Remove user from memory
    if username in authenticated_users:
        del authenticated_users[username]
        print(f"🗑️ Deleted user from memory: {username}")
    
    # Remove user from MongoDB if available
    if premium_users_collection is not None:
        try:
            result = await premium_users_collection.delete_one({"username": username})
            if result.deleted_count > 0:
                print(f"🗑️ Deleted user from MongoDB: {username}")
            else:
                print(f"⚠️ User not found in MongoDB: {username}")
        except Exception as e:
            print(f"❌ Error deleting user from MongoDB: {e}")
            return JSONResponse({
                "status": "error",
                "message": f"Failed to delete user from database: {str(e)}"
            }, 500)
    
    return JSONResponse({
        "status": "success",
        "message": f"User {username} deleted"
    })
@app.post("/admin/sync_users")
async def sync_users_api():
    """Manually sync users from MongoDB"""
    success = await sync_users_from_mongodb()
    if success:
        return JSONResponse({
            "status": "success",
            "message": "Users synced from MongoDB successfully"
        })
    else:
        return JSONResponse({
            "status": "error",
            "message": "Failed to sync users from MongoDB"
        }, 500)
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
    
    # Cancel periodic sync task
    if periodic_sync_task:
        periodic_sync_task.cancel()
        try:
            await periodic_sync_task
        except asyncio.CancelledError:
            pass
        print("🛑 Periodic sync task stopped")
    
    # Close MongoDB connection
    if mongo_client:
        mongo_client.close()
        print("🛑 MongoDB connection closed")
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
                                "message": "Multiple connections not allowed. Please purchase additional licenses."
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
