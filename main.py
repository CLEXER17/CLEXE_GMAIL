"""
Runs on RAILWAY. Two jobs in one process:
  1) Telegram bot: listens for /1 /2 /3 /4 (add more as needed)
  2) Tiny HTTP job queue: your Windows PC (running bridge.py, next to creator.exe)
     polls this to pick up work and post results back.

Why: creator.exe is a Windows binary and can't run on Railway's Linux containers.
So Railway only handles Telegram + queues the job; your own PC does the actual
exe execution and reports back.

ENV VARS to set in Railway dashboard:
  TELEGRAM_BOT_TOKEN   - from @BotFather
  BRIDGE_SECRET        - any long random string you make up (shared with bridge.py)

Install (add to requirements.txt):
  python-telegram-bot>=20.0
  flask>=3.0.0
"""

import os
import time
import uuid
import threading
import asyncio
import requests
from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SECRET = os.environ["BRIDGE_SECRET"]
PORT = int(os.environ.get("PORT", 5000))

# ---- map your bot commands to whatever creator.exe expects ----
# The bridge just receives this "command" string as-is and decides what
# arguments to pass to creator.exe. Edit COMMAND_MAP if you want to pass
# extra args instead of relying on bridge.py's own mapping.
COMMAND_MAP = {
    "1": "option_1",
    "2": "option_2",
    "3": "option_3",
    "4": "option_4",
}
# -----------------------------------------------------------------

app_flask = Flask(__name__)

# in-memory job queue: {job_id: {"command": str, "chat_id": int, "status": "pending"/"done", "output": str}}
jobs = {}
jobs_lock = threading.Lock()


def check_secret(req):
    return req.args.get("secret") == SECRET or (req.json or {}).get("secret") == SECRET


@app_flask.route("/jobs/next", methods=["GET"])
def jobs_next():
    if not check_secret(request):
        return jsonify({"error": "unauthorized"}), 401
    with jobs_lock:
        for job_id, job in jobs.items():
            if job["status"] == "pending":
                job["status"] = "sent"
                return jsonify({"job_id": job_id, "command": job["command"]})
    return jsonify({}), 204


@app_flask.route("/jobs/result", methods=["POST"])
def jobs_result():
    if not check_secret(request):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    output = data.get("output", "")
    ok = data.get("ok", True)

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job_id"}), 404
        job["status"] = "done"
        job["output"] = output

    # relay result straight to the Telegram chat that requested it
    prefix = "" if ok else "Error: "
    text = f"{prefix}{output}"[:4000] or "(no output)"
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": job["chat_id"], "text": text},
        timeout=10,
    )

    with jobs_lock:
        jobs.pop(job_id, None)  # cleanup

    return jsonify({"ok": True})


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot is up. Use /1 /2 /3 /4 to run a job on creator.exe (via bridge)."
    )


def queue_job(command: str, chat_id: int) -> str:
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"command": command, "chat_id": chat_id, "status": "pending", "output": ""}
    return job_id


async def handle_numbered_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.lstrip("/").split("@")[0]  # "1", "2", ...
    mapped = COMMAND_MAP.get(cmd, cmd)
    queue_job(mapped, update.effective_chat.id)
    await update.message.reply_text(f"Queued job '{mapped}'. Waiting for your PC to pick it up...")


def run_bot():
    """Runs python-telegram-bot's polling loop in its own thread/event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    for cmd in COMMAND_MAP:
        application.add_handler(CommandHandler(cmd, handle_numbered_command))

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app_flask.run(host="0.0.0.0", port=PORT)
