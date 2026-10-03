# Telegram Media Player

A FastAPI-based web application that streams video and audio files from a Telegram chat to a browser. The application builds a playlist from the chat history and streams media on demand using HTTP byte-range requests.

## Features

- Browser-based media player with a dark interface
- Video and audio playback from Telegram
- Playlist with normal and shuffled order
- Next-item and fullscreen controls in the player
- Automatic playback of the next item when the current one ends
- HTTP byte-range streaming for seeking
- Docker and Docker Compose deployment
- Persistent Pyrogram session storage

## Requirements

- Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- A Telegram account session that can access the target chat
- The target chat ID (`CHAT_ID`)
- Docker and Docker Compose, or Python 3.11+ to run without Docker

## Configuration

Copy `.env.example` to `.env` and set the values for your Telegram account and target chat:

```env
API_ID=your_api_id
API_HASH=your_api_hash
CHAT_ID=your_chat_id

SESSION_NAME=media_player_session
DATA_PATH=/app/data

HOST_PORT=8000
```

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `CHAT_ID` | ID of the Telegram chat to read media from |
| `SESSION_NAME` | Pyrogram session name |
| `DATA_PATH` | Path used to store the Pyrogram session |
| `HOST_PORT` | Host port mapped to the web interface |

Keep `.env` and Pyrogram session files private. Do not commit them to a public repository.

## Docker Compose Deployment

1. Create the environment file:

       cp .env.example .env

2. Edit `.env` and enter your Telegram credentials and chat ID.

3. Start the application:

       docker compose up -d --build

The web interface is available at `http://localhost:8000` by default. To access it from another device on your home network, use the server's local IP address, for example `http://192.168.1.10:8000`.

View the application logs:

    docker compose logs -f

Stop the application:

    docker compose down

The Compose file mounts `./data` into `/app/data` in the container so the Pyrogram session can persist across container restarts.

## Telegram Session

Pyrogram requires an authorized session to access Telegram. The session file is stored under `DATA_PATH` using the configured `SESSION_NAME`.

For a first-time login, create and authorize the session interactively before running the application in the background. The session file grants access to the Telegram account, so keep it private and do not commit it to Git.

## Web API

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Web media player interface |
| `/api/playlist` | `GET` | Returns the media playlist; supports `?shuffle=true` |
| `/media/{message_id}` | `GET` | Streams a media item from Telegram |

## Streaming

The application uses Pyrogram to stream media from Telegram. It supports HTTP byte-range requests so the browser can request portions of a media file, including when seeking during playback.

## Security

- The web interface is intended for a trusted local network.
- Do not expose it directly to the public internet without appropriate authentication and HTTPS.
- Do not commit `.env` files, Telegram credentials, or Pyrogram session files.

## Technology Stack

Python, FastAPI, Uvicorn, Pyrogram, TgCrypto, HTML, CSS, JavaScript, Font Awesome, Docker, and Docker Compose.
