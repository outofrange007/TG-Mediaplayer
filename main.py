import os
import json
import random
import asyncio
from collections import OrderedDict
from urllib.parse import quote

from fastapi import FastAPI, Request, Query
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.types import Message
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PhoneNumberInvalid,
    PasswordHashInvalid,
)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
DEFAULT_CHAT_ID = int(os.environ["CHAT_ID"])  # used until another chat is selected in the UI

DATA_PATH = os.getenv("DATA_PATH", "/app/data")
SESSION_NAME = os.getenv("SESSION_NAME", "media_player_session")

os.makedirs(DATA_PATH, exist_ok=True)

CACHE_VERSION = 2
CHATS_FILE = os.path.join(DATA_PATH, "chats_cache.json")
SETTINGS_FILE = os.path.join(DATA_PATH, "settings.json")

tg = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    workdir=DATA_PATH,
)

app = FastAPI()

# currently selected chat and its media list (plain dicts, no Telegram calls needed)
current_chat_id: int = DEFAULT_CHAT_ID
current_chat_title: str = ""
media_items: list[dict] = []

# list of selectable groups/channels (persisted, refreshed on demand)
chats_list: list[dict] = []

# small LRU cache of Message objects, only used for streaming
message_cache: "OrderedDict[int, Message]" = OrderedDict()
MESSAGE_CACHE_LIMIT = 50

scan_lock = asyncio.Lock()

# login state (used only while the Telegram session is not yet authorized)
is_authorized = False
login_phone_number: str | None = None
login_phone_code_hash: str | None = None
login_needs_password = False

MEDIA_TYPES = ("video", "audio", "image", "file")


# ---------------------------------------------------------------- helpers

def write_json(path: str, data) -> None:
    tmp_file = path + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_file, path)  # atomic write


def read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cache_file_for(chat_id: int) -> str:
    return os.path.join(DATA_PATH, f"media_cache_{chat_id}.json")


def classify(msg: Message):
    """Returns (kind, file_object) for supported messages, otherwise None."""
    if msg.video:
        return "video", msg.video
    if msg.video_note:
        return "video", msg.video_note
    if msg.animation:
        return "video", msg.animation
    if msg.audio:
        return "audio", msg.audio
    if msg.voice:
        return "audio", msg.voice
    if msg.photo:
        return "image", msg.photo
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime.startswith("video/"):
            return "video", msg.document
        if mime.startswith("audio/"):
            return "audio", msg.document
        if mime.startswith("image/"):
            return "image", msg.document
        return "file", msg.document
    return None


def message_to_item(msg: Message) -> dict | None:
    result = classify(msg)
    if not result:
        return None

    kind, obj = result
    default_mime = {
        "image": "image/jpeg",
        "video": "video/mp4",
        "audio": "audio/mpeg",
    }.get(kind, "application/octet-stream")
    default_ext = {"image": ".jpg", "video": ".mp4", "audio": ".mp3"}.get(kind, "")

    return {
        "id": msg.id,
        "name": getattr(obj, "file_name", None) or f"{kind.capitalize()} {msg.id}{default_ext}",
        "type": kind,
        "size": getattr(obj, "file_size", 0) or 0,
        "mime": getattr(obj, "mime_type", None) or default_mime,
    }


def save_cache_to_disk(chat_id: int, title: str, items: list[dict]) -> None:
    write_json(
        cache_file_for(chat_id),
        {"version": CACHE_VERSION, "chat_id": chat_id, "title": title, "items": items},
    )


def load_cache_from_disk(chat_id: int) -> bool:
    """Loads the media list of a chat from disk. Returns False if no valid cache exists."""
    global media_items, current_chat_title

    data = read_json(cache_file_for(chat_id))
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return False
    if not isinstance(data.get("items"), list):
        return False

    media_items = data["items"]
    current_chat_title = data.get("title") or str(chat_id)
    message_cache.clear()
    return True


def save_settings() -> None:
    write_json(SETTINGS_FILE, {"chat_id": current_chat_id})


def load_settings() -> None:
    global current_chat_id
    data = read_json(SETTINGS_FILE)
    if isinstance(data, dict) and isinstance(data.get("chat_id"), int):
        current_chat_id = data["chat_id"]


def load_chats_from_disk() -> None:
    global chats_list
    data = read_json(CHATS_FILE)
    if isinstance(data, list):
        chats_list = data


async def fetch_chats() -> None:
    """Loads all groups/channels of the account (only on first use or on manual reload)."""
    global chats_list

    result = []
    async for dialog in tg.get_dialogs():
        chat = dialog.chat
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
            result.append(
                {
                    "id": chat.id,
                    "title": chat.title or str(chat.id),
                    "type": "channel" if chat.type == ChatType.CHANNEL else "group",
                }
            )
    result.sort(key=lambda c: c["title"].lower())
    chats_list = result
    write_json(CHATS_FILE, chats_list)


async def resolve_title(chat_id: int) -> str:
    for chat in chats_list:
        if chat["id"] == chat_id:
            return chat["title"]
    try:
        chat = await tg.get_chat(chat_id)
        return chat.title or chat.first_name or str(chat_id)
    except Exception:
        return str(chat_id)


# ---------------------------------------------------------------- scanning

async def scan_telegram_media(chat_id: int | None = None) -> None:
    """Full scan of a chat history (slow, rate-limited). Only runs on first use or on manual refresh."""
    global media_items, current_chat_title

    chat_id = chat_id if chat_id is not None else current_chat_id

    async with scan_lock:
        async for _ in tg.get_dialogs():  # ensures peer/access_hash is cached
            pass

        title = await resolve_title(chat_id)

        found = []
        async for msg in tg.get_chat_history(chat_id):
            item = message_to_item(msg)
            if item:
                found.append(item)
        found.reverse()

        save_cache_to_disk(chat_id, title, found)

        # only apply if the user did not switch to another chat in the meantime
        if chat_id == current_chat_id:
            media_items = found
            current_chat_title = title
            message_cache.clear()


async def load_media_cache() -> None:
    """Instant if a valid cache exists for the current chat; otherwise one full scan."""
    if load_cache_from_disk(current_chat_id):
        return
    await scan_telegram_media(current_chat_id)


async def get_message(message_id: int) -> Message | None:
    """Fetches a single message on demand (used for streaming)."""
    if message_id in message_cache:
        message_cache.move_to_end(message_id)
        return message_cache[message_id]

    try:
        msg = await tg.get_messages(current_chat_id, message_id)
    except Exception:
        # peer might not be known yet in the session -> load dialogs once and retry
        async for _ in tg.get_dialogs():
            pass
        msg = await tg.get_messages(current_chat_id, message_id)

    if not msg or not classify(msg):
        return None

    message_cache[message_id] = msg
    if len(message_cache) > MESSAGE_CACHE_LIMIT:
        message_cache.popitem(last=False)
    return msg


@app.on_event("startup")
async def startup():
    global is_authorized

    load_settings()
    load_chats_from_disk()

    # connect() returns True if the stored session is already authorized
    is_authorized = await tg.connect()

    if is_authorized:
        await tg.initialize()
        await load_media_cache()


@app.on_event("shutdown")
async def shutdown():
    if tg.is_connected:
        await tg.stop()


# ----
# Login endpoints (phone number / code / 2FA password)
# ----

@app.post("/auth/send_code")
async def auth_send_code(request: Request):
    global login_phone_number, login_phone_code_hash, login_needs_password

    data = await request.json()
    phone_number = (data.get("phone_number") or "").strip()

    if not phone_number:
        return JSONResponse({"error": "Phone number is required."}, status_code=400)

    try:
        sent_code = await tg.send_code(phone_number)
    except PhoneNumberInvalid:
        return JSONResponse({"error": "Invalid phone number."}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    login_phone_number = phone_number
    login_phone_code_hash = sent_code.phone_code_hash
    login_needs_password = False

    return JSONResponse({"success": True})


@app.post("/auth/sign_in")
async def auth_sign_in(request: Request):
    global is_authorized, login_needs_password

    data = await request.json()
    code = (data.get("code") or "").strip()

    if not login_phone_number or not login_phone_code_hash:
        return JSONResponse({"error": "Please request a login code first."}, status_code=400)

    try:
        await tg.sign_in(login_phone_number, login_phone_code_hash, code)
    except SessionPasswordNeeded:
        login_needs_password = True
        return JSONResponse({"need_password": True})
    except (PhoneCodeInvalid, PhoneCodeExpired) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    await tg.initialize()
    await load_media_cache()
    is_authorized = True

    return JSONResponse({"success": True})


@app.post("/auth/password")
async def auth_password(request: Request):
    global is_authorized

    data = await request.json()
    password = data.get("password") or ""

    try:
        await tg.check_password(password)
    except PasswordHashInvalid:
        return JSONResponse({"error": "Incorrect password."}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    await tg.initialize()
    await load_media_cache()
    is_authorized = True

    return JSONResponse({"success": True})


@app.get("/auth/status")
async def auth_status():
    return JSONResponse({"authorized": is_authorized})


# ----
# Chat selection / state
# ----

@app.get("/api/chats")
async def api_chats(refresh: bool = False):
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    if refresh or not chats_list:
        try:
            await fetch_chats()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(
        {"current": current_chat_id, "current_title": current_chat_title, "chats": chats_list}
    )


@app.post("/api/chat")
async def api_select_chat(request: Request):
    """Switches to another group/channel. Uses its cache or performs a first scan."""
    global current_chat_id, current_chat_title, media_items

    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    data = await request.json()
    try:
        chat_id = int(data.get("chat_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid chat id."}, status_code=400)

    previous = (current_chat_id, current_chat_title, media_items)

    current_chat_id = chat_id
    try:
        await load_media_cache()
    except Exception as exc:
        current_chat_id, current_chat_title, media_items = previous
        message_cache.clear()
        return JSONResponse({"error": f"Could not open this chat: {exc}"}, status_code=400)

    save_settings()
    return JSONResponse({"success": True, "chat_id": current_chat_id, "title": current_chat_title})


@app.get("/api/state")
async def api_state():
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    counts = {t: 0 for t in MEDIA_TYPES}
    for item in media_items:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    counts["all"] = len(media_items)

    return JSONResponse(
        {"chat_id": current_chat_id, "title": current_chat_title, "counts": counts}
    )


@app.post("/api/refresh")
async def refresh_media():
    """Triggers a full re-scan of the selected chat and updates its cache on disk."""
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    try:
        await scan_telegram_media(current_chat_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"success": True, "count": len(media_items)})


@app.get("/api/playlist")
async def playlist(shuffle: bool = False, media_type: str = Query("all", alias="type")):
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    items = [
        {"id": i["id"], "name": i["name"], "type": i["type"], "size": i["size"]}
        for i in media_items
        if media_type == "all" or i["type"] == media_type
    ]
    if shuffle:
        random.shuffle(items)
    return JSONResponse(items)


CHUNK_SIZE = 1024 * 1024  # Telegram requires offsets aligned to 1 MB


@app.get("/media/{message_id}")
async def stream(message_id: int, request: Request, download: bool = False):
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    item = next((i for i in media_items if i["id"] == message_id), None)
    if not item:
        return JSONResponse({"error": "not found"}, status_code=404)

    file_size = item["size"]
    if file_size <= 0:
        return JSONResponse({"error": "unknown file size"}, status_code=404)

    msg = await get_message(message_id)
    if not msg:
        return JSONResponse({"error": "message no longer available"}, status_code=404)

    mime_type = item["mime"]

    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1

    if range_header:
        range_value = range_header.replace("bytes=", "").split("-")
        start = int(range_value[0]) if range_value[0] else 0
        end = int(range_value[1]) if len(range_value) > 1 and range_value[1] else file_size - 1
        end = min(end, file_size - 1)

    # align offset down to the nearest chunk boundary (required by Telegram)
    aligned_offset = (start // CHUNK_SIZE) * CHUNK_SIZE
    skip_bytes = start - aligned_offset  # bytes to drop from the first chunk
    bytes_to_send = end - start + 1

    async def chunk_generator():
        bytes_sent = 0
        first_chunk = True
        async for chunk in tg.stream_media(msg, offset=aligned_offset // CHUNK_SIZE):
            if first_chunk:
                chunk = chunk[skip_bytes:]
                first_chunk = False

            if bytes_sent + len(chunk) > bytes_to_send:
                chunk = chunk[: bytes_to_send - bytes_sent]

            if chunk:
                yield chunk
                bytes_sent += len(chunk)

            if bytes_sent >= bytes_to_send:
                break

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(bytes_to_send),
    }
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(item['name'])}"

    status_code = 206 if range_header else 200

    return StreamingResponse(
        chunk_generator(),
        status_code=status_code,
        headers=headers,
        media_type=mime_type,
    )

@app.get("/", response_class=HTMLResponse)
async def index():
    if not is_authorized:
        return LOGIN_PAGE
    return PLAYER_PAGE


LOGIN_PAGE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Telegram Media Player - Login</title>
        <link rel="stylesheet"
              href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">

        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #1a1a1a;
                min-height: 100vh;
                color: #e0e0e0;
                display: flex;
                align-items: center;
                justify-content: center;
            }

            .login-container {
                width: 100%;
                max-width: 420px;
                padding: 30px;
            }

            header {
                text-align: center;
                margin-bottom: 25px;
                color: #fff;
            }

            h1 {
                font-size: 1.8rem;
                margin-bottom: 8px;
            }

            .subtitle {
                color: #8899a6;
                font-size: 0.9rem;
            }

            .section {
                background: rgba(255, 255, 255, 0.03);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 10px;
                padding: 25px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            }

            .section h2 {
                color: #6ca6fd;
                margin-bottom: 18px;
                font-size: 1.1rem;
                display: flex;
                align-items: center;
                gap: 10px;
            }

            label {
                display: block;
                margin-bottom: 8px;
                color: #8899a6;
                font-size: 0.9rem;
            }

            input[type="text"],
            input[type="password"] {
                width: 100%;
                padding: 11px 14px;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                background: rgba(0, 0, 0, 0.25);
                color: #e0e0e0;
                font-size: 1rem;
                margin-bottom: 18px;
            }

            input[type="text"]:focus,
            input[type="password"]:focus {
                outline: none;
                border-color: rgba(108, 166, 253, 0.6);
            }

            .btn {
                width: 100%;
                background: rgba(108, 166, 253, 0.2);
                color: #6ca6fd;
                border: 1px solid rgba(108, 166, 253, 0.3);
                padding: 12px 18px;
                border-radius: 6px;
                cursor: pointer;
                font-weight: 500;
                font-size: 1rem;
                transition: all 0.2s ease;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
            }

            .btn:hover {
                background: rgba(108, 166, 253, 0.3);
                transform: translateY(-1px);
            }

            .btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
                transform: none;
            }

            .step {
                display: none;
            }

            .step.active {
                display: block;
            }

            .message {
                margin-top: 14px;
                padding: 10px 12px;
                border-radius: 6px;
                font-size: 0.88rem;
                display: none;
            }

            .message.error {
                display: block;
                background: rgba(255, 90, 90, 0.12);
                border: 1px solid rgba(255, 90, 90, 0.3);
                color: #ff8a8a;
            }

            .message.success {
                display: block;
                background: rgba(108, 253, 150, 0.12);
                border: 1px solid rgba(108, 253, 150, 0.3);
                color: #8afcae;
            }
        </style>
    </head>

    <body>
        <main class="login-container">
            <header>
                <h1>
                    <i class="fas fa-circle-play" style="color: #6ca6fd;"></i>
                    Telegram Media Player
                </h1>
                <p class="subtitle">Sign in with your Telegram account to continue</p>
            </header>

            <section class="section">
                <div id="step-phone" class="step active">
                    <h2><i class="fas fa-phone"></i> Phone Number</h2>
                    <label for="phone-input">Phone number (with country code)</label>
                    <input type="text" id="phone-input" placeholder="+49 151 23456789" autocomplete="tel">
                    <button class="btn" id="send-code-btn" onclick="sendCode()">
                    <i class="fas fa-paper-plane"></i> Send Code
                    </button>
                </div>

                <div id="step-code" class="step">
                    <h2><i class="fas fa-key"></i> Login Code</h2>
                    <label for="code-input">Enter the code sent to your Telegram app</label>
                    <input type="text" id="code-input" placeholder="12345" autocomplete="one-time-code">
                    <button class="btn" id="sign-in-btn" onclick="signIn()">
                    <i class="fas fa-right-to-bracket"></i> Confirm Code
                    </button>
                </div>

                <div id="step-password" class="step">
                    <h2><i class="fas fa-lock"></i> Two-Factor Password</h2>
                    <label for="password-input">Enter your Telegram cloud password</label>
                    <input type="password" id="password-input" placeholder="Password" autocomplete="current-password">
                    <button class="btn" id="password-btn" onclick="submitPassword()">
                    <i class="fas fa-unlock"></i> Confirm Password
                    </button>
                </div>

                <div id="message" class="message"></div>
            </section>
        </main>

        <script>
            function showMessage(text, type) {
                const el = document.getElementById("message");
                el.textContent = text;
                el.className = `message ${type}`;
            }

            function clearMessage() {
                const el = document.getElementById("message");
                el.textContent = "";
                el.className = "message";
            }

            function showStep(stepId) {
                document.querySelectorAll(".step").forEach((el) => el.classList.remove("active"));
                document.getElementById(stepId).classList.add("active");
            }

            async function sendCode() {
                clearMessage();
                const phoneNumber = document.getElementById("phone-input").value.trim();

                if (!phoneNumber) {
                    showMessage("Please enter a phone number.", "error");
                    return;
                }

                const btn = document.getElementById("send-code-btn");
                btn.disabled = true;

                try {
                    const response = await fetch("/auth/send_code", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ phone_number: phoneNumber }),
                    });
                    const result = await response.json();

                    if (!response.ok || result.error) {
                    showMessage(result.error || "Could not send the login code.", "error");
                    return;
                    }

                    showStep("step-code");
                } catch (error) {
                    showMessage("Network error while sending the code.", "error");
                } finally {
                    btn.disabled = false;
                }
            }

            async function signIn() {
                clearMessage();
                const code = document.getElementById("code-input").value.trim();

                if (!code) {
                    showMessage("Please enter the login code.", "error");
                    return;
                }

                const btn = document.getElementById("sign-in-btn");
                btn.disabled = true;

                try {
                    const response = await fetch("/auth/sign_in", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ code: code }),
                    });
                    const result = await response.json();

                    if (!response.ok || result.error) {
                    showMessage(result.error || "Invalid code.", "error");
                    return;
                    }

                    if (result.need_password) {
                    showStep("step-password");
                    return;
                    }

                    showMessage("Login successful! Loading player...", "success");
                    setTimeout(() => window.location.reload(), 1000);
                } catch (error) {
                    showMessage("Network error while confirming the code.", "error");
                } finally {
                    btn.disabled = false;
                }
            }

            async function submitPassword() {
                clearMessage();
                const password = document.getElementById("password-input").value;

                if (!password) {
                    showMessage("Please enter your password.", "error");
                    return;
                }

                const btn = document.getElementById("password-btn");
                btn.disabled = true;

                try {
                    const response = await fetch("/auth/password", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password: password }),
                    });
                    const result = await response.json();

                    if (!response.ok || result.error) {
                    showMessage(result.error || "Incorrect password.", "error");
                    return;
                    }

                    showMessage("Login successful! Loading player...", "success");
                    setTimeout(() => window.location.reload(), 1000);
                } catch (error) {
                    showMessage("Network error while confirming the password.", "error");
                } finally {
                    btn.disabled = false;
                }
            }
        </script>
    </body>
    </html>
"""


PLAYER_PAGE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Telegram Media Player</title>
        <link rel="stylesheet"
              href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">

        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #1a1a1a;
                min-height: 100vh;
                color: #e0e0e0;
            }

            .container {
                max-width: 1000px;
                margin: 0 auto;
                padding: 30px;
            }

            header {
                text-align: center;
                margin-bottom: 30px;
                color: #fff;
            }

            h1 {
                font-size: 2.2rem;
                margin-bottom: 10px;
            }

            .subtitle {
                color: #8899a6;
                font-size: 0.95rem;
            }

            .subtitle strong {
                color: #6ca6fd;
                font-weight: 500;
            }

            .section {
                background: rgba(255, 255, 255, 0.03);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 10px;
                padding: 25px;
                margin-bottom: 20px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            }

            .section h2 {
                color: #6ca6fd;
                margin-bottom: 18px;
                font-size: 1.3rem;
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .field-label {
                display: block;
                margin-bottom: 8px;
                color: #8899a6;
                font-size: 0.9rem;
            }

            .chat-row {
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
            }

            select {
                flex: 1;
                min-width: 0;
                padding: 11px 14px;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                background: #242424;
                color: #e0e0e0;
                font-size: 1rem;
            }

            select:focus {
                outline: none;
                border-color: rgba(108, 166, 253, 0.6);
            }

            .button-row {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
            }

            .btn {
                background: rgba(108, 166, 253, 0.2);
                color: #6ca6fd;
                border: 1px solid rgba(108, 166, 253, 0.3);
                padding: 10px 18px;
                border-radius: 6px;
                cursor: pointer;
                font-weight: 500;
                font-size: 1rem;
                transition: all 0.2s ease;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                text-decoration: none;
            }

            .btn:hover {
                background: rgba(108, 166, 253, 0.3);
                transform: translateY(-1px);
            }

            .btn.active {
                background: rgba(108, 166, 253, 0.35);
                border-color: rgba(108, 166, 253, 0.7);
                color: #ffff;
            }

            .btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
                transform: none;
            }

            .btn.spinning i {
                animation: spin 1s linear infinite;
            }

            .count {
                font-size: 0.8rem;
                opacity: 0.75;
            }

            @keyframes spin {
                from {
                    transform: rotate(0deg);
                }
                to {
                    transform: rotate(360deg);
                }
            }

            #player-container {
                position: relative;
                width: 100%;
                background: #000;
                overflow: hidden;
                border-radius: 8px;
                border: 1px solid rgba(255, 255, 255, 0.08);
            }

            #player-container:fullscreen {
                width: 100%;
                height: 100%;
                border: none;
                border-radius: 0;
            }

            #player {
                width: 100%;
                max-height: 70vh;
                display: block;
                background: #000;
            }

            #image-view {
                display: none;
                width: 100%;
                max-height: 70vh;
                object-fit: contain;
                background: #000;
            }

            #file-panel {
                display: none;
                padding: 70px 20px;
                text-align: center;
                color: #8899a6;
            }

            #file-panel i.big {
                font-size: 3rem;
                color: #6ca6fd;
                margin-bottom: 15px;
            }

            #file-panel .file-name {
                color: #e0e0e0;
                margin-bottom: 18px;
                word-break: break-all;
            }

            #player-container:fullscreen #player,
            #player-container:fullscreen #image-view {
                width: 100%;
                height: 100%;
                max-height: none;
                object-fit: contain;
            }

            #prev-overlay-btn,
            #next-overlay-btn,
            #fullscreen-btn {
                position: absolute;
                bottom: 80px;
                padding: 9px 13px;
                background: rgba(0, 0, 0, 0.68);
                color: #ffff;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 6px;
                cursor: pointer;
                font-size: 15px;
                z-index: 10;
                opacity: 0.8;
                transition: all 0.2s ease;
            }

            #next-overlay-btn {
                right: 16px;
            }

            #fullscreen-btn {
                right: 70px;
            }

            #prev-overlay-btn {
                right: 124px;
            }

            #prev-overlay-btn:hover,
            #next-overlay-btn:hover,
            #fullscreen-btn:hover {
                opacity: 1;
                background: rgba(108, 166, 253, 0.75);
                border-color: rgba(108, 166, 253, 0.9);
            }

            .now-playing {
                margin-top: 15px;
                padding: 12px 14px;
                border-radius: 6px;
                background: rgba(0, 0, 0, 0.2);
                border: 1px solid rgba(255, 255, 255, 0.05);
                color: #8899a6;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }

            .now-playing strong {
                color: #6ca6fd;
            }

            #playlist {
                list-style: none;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }

            .playlist-item {
                display: flex;
                align-items: center;
                gap: 12px;
                width: 100%;
                padding: 13px 15px;
                background: rgba(0, 0, 0, 0.2);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 6px;
                color: #e0e0e0;
                cursor: pointer;
                text-align: left;
                transition: all 0.2s ease;
            }

            .playlist-item:hover {
                background: rgba(108, 166, 253, 0.12);
                border-color: rgba(108, 166, 253, 0.3);
            }

            .playlist-item.active {
                background: rgba(108, 166, 253, 0.2);
                border-color: rgba(108, 166, 253, 0.55);
            }

            .playlist-icon {
                color: #6ca6fd;
                width: 18px;
                text-align: center;
            }

            .playlist-name {
                flex: 1;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }

            .playlist-size {
                color: #8899a6;
                font-size: 0.85rem;
                white-space: nowrap;
            }

            .empty-playlist {
                color: #8899a6;
                text-align: center;
                padding: 25px 0;
            }

            @media (max-width: 600px) {
                .container {
                    padding: 18px;
                }

                h1 {
                    font-size: 1.7rem;
                }

                .section {
                    padding: 18px;
                }

                .btn {
                    width: 100%;
                    justify-content: center;
                }

                .chat-row .btn {
                    width: auto;
                }

                #prev-overlay-btn,
                #next-overlay-btn,
                #fullscreen-btn {
                    bottom: 65px;
                }
            }
        </style>
    </head>

    <body>
        <main class="container">
            <header>
                <h1>
                    <i class="fas fa-circle-play" style="color: #6ca6fd;"></i>
                    Telegram Media Player
                </h1>
                <p class="subtitle">Streaming from: <strong id="chat-title">...</strong></p>
            </header>

            <section class="section">
                <h2><i class="fas fa-sliders"></i> Source &amp; Controls</h2>

                <label class="field-label" for="chat-select">Group / Channel</label>
                <div class="chat-row">
                    <select id="chat-select" onchange="changeChat()">
                    <option>Loading chats...</option>
                    </select>
                    <button id="reload-chats-btn" class="btn" title="Reload chat list from Telegram"
                            onclick="loadChats(true)">
                    <i class="fas fa-arrows-rotate"></i>
                    </button>
                </div>

                <label class="field-label">Media type</label>
                <div class="button-row" style="margin-bottom: 20px;">
                    <button class="btn type-btn active" data-type="all" onclick="setType('all')">
                    <i class="fas fa-layer-group"></i> All <span class="count" id="count-all"></span>
                    </button>
                    <button class="btn type-btn" data-type="video" onclick="setType('video')">
                    <i class="fas fa-film"></i> Videos <span class="count" id="count-video"></span>
                    </button>
                    <button class="btn type-btn" data-type="audio" onclick="setType('audio')">
                    <i class="fas fa-music"></i> Audio <span class="count" id="count-audio"></span>
                    </button>
                    <button class="btn type-btn" data-type="image" onclick="setType('image')">
                    <i class="fas fa-image"></i> Images <span class="count" id="count-image"></span>
                    </button>
                    <button class="btn type-btn" data-type="file" onclick="setType('file')">
                    <i class="fas fa-file"></i> Files <span class="count" id="count-file"></span>
                    </button>
                </div>

                <label class="field-label">Playback</label>
                <div class="button-row">
                    <button id="normal-order-btn" class="btn active" onclick="loadPlaylist(false)">
                    <i class="fas fa-list"></i> Normal Order
                    </button>

                    <button id="shuffle-btn" class="btn" onclick="loadPlaylist(true)">
                    <i class="fas fa-shuffle"></i> Shuffle Mode
                    </button>

                    <button id="refresh-btn" class="btn" onclick="refreshMedia()">
                    <i class="fas fa-rotate"></i> Refresh Media List
                    </button>
                </div>
            </section>

            <section class="section">
                <h2><i class="fas fa-film"></i> Player</h2>

                <div id="player-container">
                    <video id="player" controls controlsList="nofullscreen"></video>
                    <img id="image-view" alt="">

                    <div id="file-panel">
                        <i class="fas fa-file-arrow-down big"></i>
                        <div class="file-name" id="file-name"></div>
                        <a id="file-download" class="btn" href="#">
                        <i class="fas fa-download"></i> Download
                        </a>
                    </div>

                    <button id="fullscreen-btn" title="Toggle fullscreen" onclick="toggleFullscreen()">
                    <i class="fas fa-expand"></i>
                    </button>

                    <button id="prev-overlay-btn" title="Play previous item" onclick="playPrevious()">
                    <i class="fas fa-backward-step"></i>
                    </button>

                    <button id="next-overlay-btn" title="Play next item" onclick="playNext()">
                    <i class="fas fa-forward-step"></i>
                    </button>
                </div>

                <div id="now-playing" class="now-playing">
                    <strong>Now playing:</strong> Nothing selected
                </div>
            </section>

            <section class="section">
                <h2><i class="fas fa-list-ul"></i> Playlist</h2>
                <ul id="playlist">
                    <li class="empty-playlist">
                    <i class="fas fa-spinner fa-spin"></i> Loading playlist...
                    </li>
                </ul>
            </section>
        </main>

        <script>
            let currentIndex = 0;
            let items = [];
            let shuffleEnabled = false;
            let mediaType = "all";

            const ICONS = {
                video: "fa-film",
                audio: "fa-music",
                image: "fa-image",
                file: "fa-file",
            };

            function setPlaylistMessage(html) {
                document.getElementById("playlist").innerHTML =
                    `<li class="empty-playlist">${html}</li>`;
            }

            async function loadChats(refresh) {
                const select = document.getElementById("chat-select");
                const reloadBtn = document.getElementById("reload-chats-btn");
                reloadBtn.disabled = true;
                reloadBtn.classList.add("spinning");

                try {
                    const response = await fetch(`/api/chats?refresh=${refresh}`);
                    const result = await response.json();

                    if (!response.ok || result.error) {
                    select.innerHTML = "<option>Could not load chats</option>";
                    return;
                    }

                    select.innerHTML = "";
                    let found = false;

                    result.chats.forEach((chat) => {
                    const option = document.createElement("option");
                    option.value = chat.id;
                    option.textContent = (chat.type === "channel" ? "[Channel] " : "[Group] ") + chat.title;
                    if (chat.id === result.current) {
                    option.selected = true;
                    found = true;
                    }
                    select.appendChild(option);
                    });

                    // current chat is not part of the dialog list (e.g. set via CHAT_ID)
                    if (!found) {
                    const option = document.createElement("option");
                    option.value = result.current;
                    option.textContent = result.current_title || String(result.current);
                    option.selected = true;
                    select.insertBefore(option, select.firstChild);
                    }
                } catch (error) {
                    select.innerHTML = "<option>Could not load chats</option>";
                } finally {
                    reloadBtn.disabled = false;
                    reloadBtn.classList.remove("spinning");
                }
            }

            async function loadState() {
                try {
                    const response = await fetch("/api/state");
                    const state = await response.json();
                    if (!response.ok || state.error) {
                    return;
                    }

                    document.getElementById("chat-title").textContent = state.title || state.chat_id;
                    ["all", "video", "audio", "image", "file"].forEach((key) => {
                    document.getElementById(`count-${key}`).textContent = `(${state.counts[key] || 0})`;
                    });
                } catch (error) {
                    // keep old values
                }
            }

            function setControlsDisabled(disabled) {
                document.querySelectorAll(".btn, select").forEach((el) => {
                    el.disabled = disabled;
                });
            }

            async function changeChat() {
                const select = document.getElementById("chat-select");
                const chatId = parseInt(select.value, 10);
                if (isNaN(chatId)) {
                    return;
                }

                stopPlayback();
                setControlsDisabled(true);
                setPlaylistMessage(
                    '<i class="fas fa-spinner fa-spin"></i> Loading chat... ' +
                    "The first time this can take a few minutes (Telegram rate limit)."
                );

                try {
                    const response = await fetch("/api/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ chat_id: chatId }),
                    });
                    const result = await response.json();

                    if (!response.ok || result.error) {
                    alert(result.error || "Could not switch the chat.");
                    }
                } catch (error) {
                    alert("Network error while switching the chat.");
                }

                await loadChats(false);
                await loadState();
                setControlsDisabled(false);
                await loadPlaylist(shuffleEnabled);
            }

            function setType(type) {
                mediaType = type;
                document.querySelectorAll(".type-btn").forEach((btn) => {
                    btn.classList.toggle("active", btn.dataset.type === type);
                });
                loadPlaylist(shuffleEnabled);
            }

            async function loadPlaylist(shuffle) {
                shuffleEnabled = shuffle;

                document.getElementById("normal-order-btn").classList.toggle("active", !shuffle);
                document.getElementById("shuffle-btn").classList.toggle("active", shuffle);

                setPlaylistMessage('<i class="fas fa-spinner fa-spin"></i> Loading playlist...');

                try {
                    const response = await fetch(`/api/playlist?shuffle=${shuffle}&type=${mediaType}`);
                    items = await response.json();

                    currentIndex = 0;
                    renderList();

                    if (items.length > 0) {
                    playItem(0, true);
                    } else {
                    stopPlayback();
                    document.getElementById("now-playing").innerHTML =
                    "<strong>Now playing:</strong> No media files found";
                    }
                } catch (error) {
                    setPlaylistMessage(
                    '<i class="fas fa-triangle-exclamation"></i> Could not load the playlist.'
                    );
                }
            }

            function renderList() {
                const playlistElement = document.getElementById("playlist");
                playlistElement.innerHTML = "";

                if (items.length === 0) {
                    setPlaylistMessage('<i class="fas fa-folder-open"></i> No media files found.');
                    return;
                }

                items.forEach((item, index) => {
                    const listItem = document.createElement("li");
                    const icon = ICONS[item.type] || "fa-file";

                    listItem.className = `playlist-item ${index === currentIndex ? "active" : ""}`;
                    listItem.innerHTML = `
                    <i class="fas ${icon} playlist-icon"></i>
                    <span class="playlist-name">${escapeHtml(item.name)}</span>
                    <span class="playlist-size">${formatFileSize(item.size)}</span>
                    `;

                    listItem.onclick = () => playItem(index, false);
                    playlistElement.appendChild(listItem);
                });
            }

            function stopPlayback() {
                const player = document.getElementById("player");
                player.pause();
                player.removeAttribute("src");
                player.load();

                document.getElementById("image-view").removeAttribute("src");
                document.getElementById("player").style.display = "block";
                document.getElementById("image-view").style.display = "none";
                document.getElementById("file-panel").style.display = "none";
            }

            function playItem(index, auto) {
                if (items.length === 0 || index < 0 || index >= items.length) {
                    return;
                }

                currentIndex = index;

                const item = items[index];
                const player = document.getElementById("player");
                const imageView = document.getElementById("image-view");
                const filePanel = document.getElementById("file-panel");
                const nowPlaying = document.getElementById("now-playing");

                // stop whatever was shown before
                player.pause();
                player.removeAttribute("src");
                player.load();
                imageView.removeAttribute("src");
                player.style.display = "none";
                imageView.style.display = "none";
                filePanel.style.display = "none";

                if (item.type === "video" || item.type === "audio") {
                    player.style.display = "block";
                    player.src = `/media/${item.id}`;
                    player.play().catch(() => {
                    // Browsers may require a user interaction before playback starts.
                    });
                } else if (item.type === "image") {
                    imageView.style.display = "block";
                    imageView.src = `/media/${item.id}`;
                    imageView.alt = item.name;
                } else {
                    filePanel.style.display = "block";
                    document.getElementById("file-name").textContent =
                    `${item.name} (${formatFileSize(item.size)})`;
                    document.getElementById("file-download").href = `/media/${item.id}?download=true`;
                }

                const label = item.type === "file" ? "Selected" : "Now playing";
                nowPlaying.innerHTML = `<strong>${label}:</strong> ${escapeHtml(item.name)}`;

                renderList();
            }

            function playNext() {
                if (items.length === 0) {
                    return;
                }

                const nextIndex = (currentIndex + 1) % items.length;
                playItem(nextIndex, false);
            }

            function playPrevious() {
                if (items.length === 0) {
                    return;
                }

                const prevIndex = (currentIndex - 1 + items.length) % items.length;
                playItem(prevIndex, false);
            }

            async function refreshMedia() {
                const btn = document.getElementById("refresh-btn");
                btn.disabled = true;
                btn.classList.add("spinning");

                try {
                    const response = await fetch("/api/refresh", { method: "POST" });
                    const result = await response.json();

                    if (!response.ok || result.error) {
                    alert(result.error || "Could not refresh the media list.");
                    return;
                    }

                    await loadState();
                    await loadPlaylist(shuffleEnabled);
                } catch (error) {
                    alert("Network error while refreshing the media list.");
                } finally {
                    btn.disabled = false;
                    btn.classList.remove("spinning");
                }
            }

            function toggleFullscreen() {
                const container = document.getElementById("player-container");

                if (!document.fullscreenElement) {
                    container.requestFullscreen().catch(() => {
                    // Fullscreen may be blocked by browser settings.
                    });
                } else {
                    document.exitFullscreen();
                }
            }

            function formatFileSize(bytes) {
                if (!bytes) {
                    return "Unknown size";
                }

                const units = ["B", "KB", "MB", "GB", "TB"];
                const index = Math.floor(Math.log(bytes) / Math.log(1024));
                const value = bytes / Math.pow(1024, index);

                return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
            }

            function escapeHtml(value) {
                const element = document.createElement("div");
                element.textContent = value || "Unnamed file";
                return element.innerHTML;
            }

            document.addEventListener("DOMContentLoaded", async () => {
                const player = document.getElementById("player");

                player.addEventListener("ended", () => {
                    playNext();
                });

                await loadChats(false);
                await loadState();
                await loadPlaylist(false);
            });
        </script>
    </body>
    </html>
"""