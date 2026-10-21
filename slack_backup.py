#!/usr/bin/env python3
"""Daily incremental backup of all public Slack channels to GCS.

Requires env vars SLACK_BOT_TOKEN and GCS_BUCKET (see .env.example).
Run via run_backup.sh on a daily cron schedule.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.cloud import storage
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
GCS_PREFIX = os.environ.get("GCS_PREFIX", "slack-backup").strip("/")
STATE_DIR = Path(os.environ.get("STATE_DIR", "./state"))
LOCK_FILE = STATE_DIR / "backup.lock"
STATE_FILE = STATE_DIR / "last_ts.json"
STATE_BLOB = f"{GCS_PREFIX}/state/last_ts.json"
LOCK_STALE_SECONDS = 3 * 60 * 60

client = WebClient(token=SLACK_TOKEN)
bucket = storage.Client().bucket(GCS_BUCKET)
user_name_cache: dict[str, str] = {}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def slack_call(method, **kwargs):
    while True:
        try:
            return method(**kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429:
                delay = int(e.response.headers.get("Retry-After", "5"))
                log(f"rate limited, sleeping {delay}s")
                time.sleep(delay)
                continue
            raise


def acquire_lock() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            log("another backup run appears to be in progress, exiting")
            sys.exit(1)
        log("found stale lock, removing")
        LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    blob = bucket.blob(STATE_BLOB)
    if blob.exists():
        log("no local state, restoring from GCS")
        return json.loads(blob.download_as_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    bucket.blob(STATE_BLOB).upload_from_string(
        json.dumps(state, indent=2), content_type="application/json"
    )


def list_public_channels() -> list[dict]:
    channels = []
    cursor = None
    while True:
        resp = slack_call(
            client.conversations_list,
            types="public_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        channels.extend(resp["channels"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def ensure_joined(channel_id: str) -> None:
    try:
        slack_call(client.conversations_join, channel=channel_id)
    except SlackApiError as e:
        if e.response.get("error") not in ("already_in_channel", "method_not_supported_for_channel_type"):
            log(f"could not join {channel_id}: {e.response.get('error')}")


def resolve_user_name(user_id: str) -> str:
    if not user_id:
        return "unknown"
    if user_id not in user_name_cache:
        try:
            resp = slack_call(client.users_info, user=user_id)
            profile = resp["user"]
            user_name_cache[user_id] = profile.get("real_name") or profile["name"]
        except SlackApiError:
            user_name_cache[user_id] = user_id
    return user_name_cache[user_id]


def fetch_history(channel_id: str, oldest_ts: str | None) -> list[dict]:
    messages = []
    cursor = None
    while True:
        kwargs = dict(channel=channel_id, limit=200, cursor=cursor)
        if oldest_ts:
            kwargs["oldest"] = oldest_ts
        resp = slack_call(client.conversations_history, **kwargs)
        messages.extend(resp["messages"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    # Slack returns newest-first; oldest-first reads like a transcript.
    messages.reverse()
    # The oldest filter is inclusive, so drop the message we already recorded last time.
    if oldest_ts:
        messages = [m for m in messages if m["ts"] != oldest_ts]
    return messages


def fetch_thread_replies(channel_id: str, thread_ts: str) -> list[dict]:
    replies = []
    cursor = None
    while True:
        resp = slack_call(
            client.conversations_replies,
            channel=channel_id,
            ts=thread_ts,
            limit=200,
            cursor=cursor,
        )
        replies.extend(resp["messages"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    # First message is the parent itself, already handled by the caller.
    return replies[1:]


def upload_file_attachment(channel_name: str, file_info: dict) -> str | None:
    url = file_info.get("url_private")
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"failed to download file {file_info.get('id')}: {e}")
        return None
    blob_path = f"{GCS_PREFIX}/files/{channel_name}/{file_info['id']}_{file_info.get('name', 'file')}"
    blob = bucket.blob(blob_path)
    blob.upload_from_string(resp.content, content_type=file_info.get("mimetype"))
    return f"gs://{GCS_BUCKET}/{blob_path}"


def format_message(channel_name: str, msg: dict, indent: str = "") -> str:
    ts = float(msg["ts"])
    when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
    author = resolve_user_name(msg.get("user") or msg.get("bot_id", ""))
    text = msg.get("text", "").replace("\n", f"\n{indent}    ")
    lines = [f"{indent}[{when}] {author}: {text}"]
    for f in msg.get("files", []):
        gcs_uri = upload_file_attachment(channel_name, f)
        label = f.get("name", f.get("id"))
        if gcs_uri:
            lines.append(f"{indent}    [attachment: {label} -> {gcs_uri}]")
        else:
            lines.append(f"{indent}    [attachment: {label} -> upload failed]")
    return "\n".join(lines)


def append_transcript(channel_name: str, date_str: str, text: str) -> None:
    blob_path = f"{GCS_PREFIX}/transcripts/{channel_name}/{date_str}.txt"
    blob = bucket.blob(blob_path)
    existing = blob.download_as_text() if blob.exists() else ""
    blob.upload_from_string(existing + text + "\n", content_type="text/plain; charset=utf-8")


def backup_channel(channel: dict, state: dict) -> None:
    channel_id, channel_name = channel["id"], channel["name"]
    oldest_ts = state.get(channel_id)
    ensure_joined(channel_id)
    messages = fetch_history(channel_id, oldest_ts)
    if not messages:
        return

    log(f"#{channel_name}: {len(messages)} new message(s)")
    by_date: dict[str, list[str]] = {}
    for msg in messages:
        date_str = datetime.fromtimestamp(float(msg["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(format_message(channel_name, msg))

        if msg.get("reply_count") and msg.get("thread_ts") == msg.get("ts"):
            for reply in fetch_thread_replies(channel_id, msg["ts"]):
                reply_date = datetime.fromtimestamp(float(reply["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
                by_date.setdefault(reply_date, []).append(format_message(channel_name, reply, indent="  "))

    for date_str, lines in by_date.items():
        append_transcript(channel_name, date_str, "\n".join(lines))

    state[channel_id] = messages[-1]["ts"]


def main() -> None:
    acquire_lock()
    try:
        state = load_state()
        channels = list_public_channels()
        log(f"found {len(channels)} public channel(s)")
        for channel in channels:
            try:
                backup_channel(channel, state)
            except SlackApiError as e:
                log(f"error backing up #{channel['name']}: {e.response.get('error')}")
        save_state(state)
        log("backup complete")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
