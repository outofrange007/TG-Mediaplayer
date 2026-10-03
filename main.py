import os
import json
import random

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from pyrogram import Client
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
CHAT_ID = int(os.environ["CHAT_ID"])

DATA_PATH = os.getenv("DATA_PATH", "/app/data")
SESSION_NAME = os.getenv("SESSION_NAME", "media_player_session")

os.makedirs(DATA_PATH, exist_ok=True)

# file used to persist the list of media message IDs between restarts
CACHE_FILE = os.path.join(DATA_PATH, "media_cache.json")

tg = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    workdir=DATA_PATH,
)

app = FastAPI()

# simple in-memory cache of found media messages
media_cache: list[Message] = []

# login state (used only while the Telegram session is not yet authorized)
is_authorized = False
login_phone_number: str | None = None
login_phone_code_hash: str | None = None
login_needs_password = False


def save_cache_to_disk():
    """Persists the IDs of the currently known media messages to disk."""
    ids = [msg.id for msg in media_cache]
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f)


async def scan_telegram_media():
    """Performs a full scan of the chat history (slow, rate-limited by Telegram)."""
    global media_cache

    async for _ in tg.get_dialogs():  # ensures peer/access_hash is cached
        pass

    found = []
    async for msg in tg.get_chat_history(CHAT_ID):
        if msg.video or msg.audio:
            found.append(msg)
    found.reverse()

    media_cache = found
    save_cache_to_disk()


async def load_media_cache():
    """
    Fast startup path: if a cached list of message IDs exists on disk, fetch
    only those specific messages instead of scanning the entire chat history.
    Falls back to a full scan if no cache is available yet.
    """
    global media_cache

    async for _ in tg.get_dialogs():  # ensures peer/access_hash is cached
        pass

    if not os.path.exists(CACHE_FILE):
        await scan_telegram_media()
        return

    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cached_ids = json.load(f)

    messages = []
    # get_messages accepts up to 200 IDs per call, so this stays fast
    # and avoids the per-request flood-wait delays of get_chat_history.
    for i in range(0, len(cached_ids), 200):
        chunk = await tg.get_messages(CHAT_ID, cached_ids[i:i + 200])
        chunk = chunk if isinstance(chunk, list) else [chunk]
        messages.extend(m for m in chunk if m and (m.video or m.audio))

    media_cache = messages


@app.on_event("startup")
async def startup():
    global is_authorized

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
# Login endpoints (phone number / code / 2FA password), used when the
# Telegram session is not yet authorized. Mirrors the login flow of
# TG-Uploader / TG-Downloader.
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


@app.post("/api/refresh")
async def refresh_media():
    """Triggers a full re-scan of the Telegram chat and updates the cache on disk."""
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    await scan_telegram_media()
    return JSONResponse({"success": True, "count": len(media_cache)})


@app.get("/api/playlist")
async def playlist(shuffle: bool = False):
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    items = [
        {
            "id": msg.id,
            "name": (msg.video.file_name if msg.video else msg.audio.file_name) or f"File {msg.id}",
            "type": "video" if msg.video else "audio",
            "size": (msg.video.file_size if msg.video else msg.audio.file_size),
        }
        for msg in media_cache
    ]
    if shuffle:
        random.shuffle(items)
    return JSONResponse(items)

CHUNK_SIZE = 1024 * 1024  # Telegram requires offsets aligned to 1 MB


@app.get("/media/{message_id}")
async def stream(message_id: int, request: Request):
    if not is_authorized:
        return JSONResponse({"error": "not authorized"}, status_code=401)

    msg = next((m for m in media_cache if m.id == message_id), None)
    if not msg:
        return JSONResponse({"error": "not found"}, status_code=404)

    file_obj = msg.video or msg.audio
    file_size = file_obj.file_size
    mime_type = file_obj.mime_type or "application/octet-stream"

    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1

    if range_header:
        range_value = range_header.replace("bytes=", "").split("-")
        start = int(range_value[0]) if range_value[0] else 0
        end = int(range_value[1]) if len(range_value) > 1 and range_value[1] else file_size - 1

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

            #player-container:fullscreen #player {
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
                <p class="subtitle">Stream media directly from your Telegram group</p>
            </header>

            <section class="section">
                <h2><i class="fas fa-sliders"></i> Playback Controls</h2>
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

            async function loadPlaylist(shuffle) {
                shuffleEnabled = shuffle;

                document.getElementById("normal-order-btn").classList.toggle("active", !shuffle);
                document.getElementById("shuffle-btn").classList.toggle("active", shuffle);

                const playlistElement = document.getElementById("playlist");
                playlistElement.innerHTML = `
                    <li class="empty-playlist">
                    <i class="fas fa-spinner fa-spin"></i> Loading playlist...
                    </li>
                `;

                try {
                    const response = await fetch(`/api/playlist?shuffle=${shuffle}`);
                    items = await response.json();

                    currentIndex = 0;
                    renderList();

                    if (items.length > 0) {
                    playItem(0);
                    } else {
                    document.getElementById("now-playing").innerHTML =
                    "<strong>Now playing:</strong> No media files found";
                    }
                } catch (error) {
                    playlistElement.innerHTML = `
                    <li class="empty-playlist">
                    <i class="fas fa-triangle-exclamation"></i>
                    Could not load the playlist.
                    </li>
                    `;
                }
            }

            function renderList() {
                const playlistElement = document.getElementById("playlist");
                playlistElement.innerHTML = "";

                if (items.length === 0) {
                    playlistElement.innerHTML = `
                    <li class="empty-playlist">
                    <i class="fas fa-folder-open"></i> No media files found.
                    </li>
                    `;
                    return;
                }

                items.forEach((item, index) => {
                    const listItem = document.createElement("li");
                    const icon = item.type === "audio" ? "fa-music" : "fa-film";

                    listItem.className = `playlist-item ${index === currentIndex ? "active" : ""}`;
                    listItem.innerHTML = `
                    <i class="fas ${icon} playlist-icon"></i>
                    <span class="playlist-name">${escapeHtml(item.name)}</span>
                    <span class="playlist-size">${formatFileSize(item.size)}</span>
                    `;

                    listItem.onclick = () => playItem(index);
                    playlistElement.appendChild(listItem);
                });
            }

            function playItem(index) {
                if (items.length === 0 || index < 0 || index >= items.length) {
                    return;
                }

                currentIndex = index;

                const item = items[index];
                const player = document.getElementById("player");
                const nowPlaying = document.getElementById("now-playing");

                player.src = `/media/${item.id}`;
                player.play().catch(() => {
                    // Browsers may require a user interaction before playback starts.
                });

                nowPlaying.innerHTML = `
                    <strong>Now playing:</strong> ${escapeHtml(item.name)}
                `;

                renderList();
            }

            function playNext() {
                if (items.length === 0) {
                    return;
                }

                const nextIndex = (currentIndex + 1) % items.length;
                playItem(nextIndex);
            }

            function playPrevious() {
                if (items.length === 0) {
                    return;
                }

                const prevIndex = (currentIndex - 1 + items.length) % items.length;
                playItem(prevIndex);
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

            document.addEventListener("DOMContentLoaded", () => {
                const player = document.getElementById("player");

                player.addEventListener("ended", () => {
                    playNext();
                });

                loadPlaylist(false);
            });
        </script>
    </body>
    </html>
"""