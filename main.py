import random
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from pyrogram import Client
from pyrogram.types import Message
import os

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
CHAT_ID = int(os.environ["CHAT_ID"])

DATA_PATH = os.getenv("DATA_PATH", "/app/data")
SESSION_NAME = os.getenv("SESSION_NAME", "media_player_session")

os.makedirs(DATA_PATH, exist_ok=True)

tg = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    workdir=DATA_PATH,
)

app = FastAPI()

# simple in-memory cache of found media messages
media_cache: list[Message] = []

@app.on_event("startup")
async def startup():
    await tg.start()
    async for _ in tg.get_dialogs():  # ensures peer/access_hash is cached
        pass

    global media_cache
    media_cache = []
    async for msg in tg.get_chat_history(CHAT_ID):
        if msg.video or msg.audio:
            media_cache.append(msg)
    media_cache.reverse()

@app.on_event("shutdown")
async def shutdown():
    await tg.stop()

@app.get("/api/playlist")
async def playlist(shuffle: bool = False):
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

    async def chunk_generator():
        # stream only the requested byte range from Telegram, chunk by chunk
        async for chunk in tg.stream_media(msg, offset=start, limit=chunk_size):
            yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
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
    return """
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
                color: #ffffff;
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

            #next-overlay-btn,
            #fullscreen-btn {
                position: absolute;
                bottom: 80px;
                padding: 9px 13px;
                background: rgba(0, 0, 0, 0.68);
                color: #ffffff;
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
                </div>
            </section>

            <section class="section">
                <h2><i class="fas fa-film"></i> Player</h2>

                <div id="player-container">
                    <video id="player" controls controlsList="nofullscreen"></video>

                    <button id="fullscreen-btn" title="Toggle fullscreen" onclick="toggleFullscreen()">
                        <i class="fas fa-expand"></i>
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