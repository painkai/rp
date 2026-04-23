#!/usr/bin/env python3
"""
텔레그램 reply 기반 동작 감지 이미지 라벨링 봇.

사용법:
  python label_bot.py

동작:
  cctv.py 가 전송한 동작 감지 사진에 텔레그램으로 reply 하면
  해당 이미지와 라벨을 dataset/ 에 저장합니다.

주의:
  cctv.py 와 같은 봇 토큰을 사용하므로, 동시에 실행하면
  getUpdates 가 경쟁하여 일부 메시지를 놓칠 수 있습니다.
  cctv.py 중지 후 단독 실행을 권장합니다.
"""
import os
import sys
import json
import re
import shutil
import time
import logging
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

IMAGES_DIR  = Path("images")
DATASET_DIR = Path("dataset")
LABELS_FILE = DATASET_DIR / "labels.jsonl"
OFFSET_FILE = DATASET_DIR / ".offset"

VALID_LABELS = {"사람", "택배", "동물", "차량", "비닐봉지", "기타", "오감지"}
_TS_RE = re.compile(r"\[동작 감지\] (\d{8}_\d{6})")


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def _get_updates(offset: int) -> list:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        log.error(f"getUpdates 실패: {str(e).replace(TELEGRAM_TOKEN, '***')}")
        return []


def _send_reply(chat_id: str, reply_to_id: int, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_id,
            },
            timeout=10,
        )
    except Exception as e:
        log.error(f"sendMessage 실패: {e}")


def _save_label(src: Path, label: str, ts: str) -> None:
    dest_name = f"{ts}_{label}.jpg"
    dest = DATASET_DIR / dest_name
    shutil.copy2(src, dest)
    entry = {
        "file": dest_name,
        "label": label,
        "timestamp": ts,
        "saved_at": datetime.now().isoformat(),
    }
    with LABELS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info(f"라벨 저장: {dest_name} ({label})")


def _process(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    reply_to = msg.get("reply_to_message")
    if not reply_to:
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    chat_id = str(msg["chat"]["id"])
    if chat_id != TELEGRAM_CHAT:
        return

    caption = reply_to.get("caption") or ""
    m = _TS_RE.search(caption)
    if not m:
        return

    ts = m.group(1)
    msg_id = msg["message_id"]

    if text not in VALID_LABELS:
        _send_reply(chat_id, msg_id,
                    f"알 수 없는 라벨입니다.\n사용 가능: {', '.join(sorted(VALID_LABELS))}")
        return

    src = IMAGES_DIR / f"after_{ts}.jpg"
    if not src.exists():
        _send_reply(chat_id, msg_id, f"이미지를 찾을 수 없습니다: after_{ts}.jpg")
        return

    _save_label(src, text, ts)
    _send_reply(chat_id, msg_id, f"✅ 라벨 저장: {text}")


def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.error("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 가 설정되지 않았습니다")
        sys.exit(1)

    DATASET_DIR.mkdir(exist_ok=True)

    offset = _load_offset()
    log.info(f"라벨 봇 시작 (offset={offset})")
    log.warning("cctv.py 와 동시 실행 시 getUpdates 충돌 가능 — 단독 실행 권장")

    backoff = 2
    while True:
        updates = _get_updates(offset)
        if updates is None:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        backoff = 2
        for update in updates:
            try:
                _process(update)
            except Exception as e:
                log.error(f"처리 실패: {e}")
            offset = max(offset, update["update_id"] + 1)

        if updates:
            _save_offset(offset)

        time.sleep(2)


if __name__ == "__main__":
    main()
