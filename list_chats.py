"""
Diagnostic script to list all Telegram chats/groups the authenticated
account has access to, together with their numeric chat IDs.

Run this once interactively (locally or via `docker run -it`) to:
  1. Create the initial Pyrogram session file (login with phone number,
     login code, and optionally a 2FA password).
  2. Find the correct CHAT_ID for the group/channel you want to stream
     media from (use the ID exactly as printed, including the minus sign).

Usage:
    python3 list_chats.py
"""

import os
from pyrogram import Client

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

DATA_PATH = os.getenv("DATA_PATH", "/app/data")
SESSION_NAME = os.getenv("SESSION_NAME", "media_player_session")

os.makedirs(DATA_PATH, exist_ok=True)

app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    workdir=DATA_PATH,
)


async def main():
    await app.start()

    async for dialog in app.get_dialogs():
        print(f"{dialog.chat.id} - {dialog.chat.title or dialog.chat.first_name}")

    await app.stop()


if __name__ == "__main__":
    app.run(main())