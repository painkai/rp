#!/usr/bin/env python3
"""
라즈베리파이 label_bot 에서 전송된 이미지+라벨을 받아 저장하는 PC 수신 서버.

사용법 (PC 에서 실행):
  pip install flask
  python receiver.py

저장 위치: dataset/{timestamp}_{label}.jpg
           dataset/labels.jsonl
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from flask import Flask, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATASET_DIR = Path("dataset")
LABELS_FILE = DATASET_DIR / "labels.jsonl"
PORT = 8765

app = Flask(__name__)


@app.route("/upload", methods=["POST"])
def upload():
    label = request.form.get("label", "").strip()
    ts = request.form.get("timestamp", "").strip()
    photo = request.files.get("photo")

    if not label or not ts or not photo:
        return "label, timestamp, photo 필드가 필요합니다", 400

    DATASET_DIR.mkdir(exist_ok=True)

    dest_name = f"{ts}_{label}.jpg"
    dest = DATASET_DIR / dest_name
    photo.save(dest)

    entry = {
        "file": dest_name,
        "label": label,
        "timestamp": ts,
        "saved_at": datetime.now().isoformat(),
    }
    with LABELS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    log.info(f"저장: {dest_name} ({label})")
    return "ok", 200


if __name__ == "__main__":
    log.info(f"수신 서버 시작 — 포트 {PORT}")
    app.run(host="0.0.0.0", port=PORT)
