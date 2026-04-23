#!/usr/bin/env python3
"""
라벨링용 독립 실행 스크립트.
동작 감지 + 텔레그램 전송 + reply 라벨 감지 + PC 전송을 모두 수행합니다.
cctv.py 와 동시에 실행하지 마세요.

사용법 (라즈베리파이):
  python label_bot.py
"""
import os
import sys
import cv2
import json
import re
import time
import threading
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

# ── 설정 ──────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
PC_RECEIVER_URL  = os.getenv("PC_RECEIVER", "")

MOTION_THRESHOLD   = int(os.getenv("MOTION_THRESHOLD", 3000))
LIGHTING_THRESHOLD = float(os.getenv("LIGHTING_THRESHOLD", 0.6))
CONFIRM_THRESHOLD  = int(os.getenv("CONFIRM_THRESHOLD", 1000))
CAPTURE_DELAY      = int(os.getenv("CAPTURE_DELAY", 2))
COOLDOWN_ALERT     = int(os.getenv("COOLDOWN_ALERT", 30))
COOLDOWN_NO_ALERT  = int(os.getenv("COOLDOWN_NO_ALERT", 10))

IMAGES_DIR      = Path("images")
BACKGROUND_PATH = IMAGES_DIR / "background.jpg"
OFFSET_FILE     = Path("dataset") / ".offset"

VALID_LABELS = {"사람", "택배", "비닐봉지", "기타", "오감지", "조명변화"}
_TS_RE = re.compile(r"\[동작 감지\] (\d{8}_\d{6})")

# ── 공유 상태 ──────────────────────────────────────────────────────────────────
latest_frame = None
frame_lock = threading.Lock()

background_gray = None
background_lock = threading.Lock()

last_event_time = 0.0
last_event_cooldown = COOLDOWN_NO_ALERT
event_time_lock = threading.Lock()

# message_id → 이미지 경로 (재시작 시 caption 파싱으로 fallback)
_sent_photo_map = {}
_sent_photo_lock = threading.Lock()


# ── 동작 감지 ──────────────────────────────────────────────────────────────────
def _frame_diff(a, b) -> int:
    norm_a = cv2.normalize(a, None, 0, 255, cv2.NORM_MINMAX)
    norm_b = cv2.normalize(b, None, 0, 255, cv2.NORM_MINMAX)
    diff = cv2.absdiff(norm_a, norm_b)
    _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    return int(cv2.countNonZero(thresh))


# ── 텔레그램 ──────────────────────────────────────────────────────────────────
def _send_photo(image_path: str, ts: str):
    caption = f"[동작 감지] {ts}"
    try:
        with open(image_path, "rb") as photo:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT, "caption": caption},
                files={"photo": photo},
                timeout=10,
            )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")
        return None


def _send_text(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.error(f"텔레그램 텍스트 전송 실패: {e}")


def _send_reply(chat_id: str, reply_to_id: int, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_id},
            timeout=10,
        )
    except Exception as e:
        log.error(f"reply 전송 실패: {e}")


# ── PC 전송 ───────────────────────────────────────────────────────────────────
def _download_from_telegram(photo_sizes: list, dest: Path) -> bool:
    file_id = photo_sizes[-1]["file_id"]
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        img = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=30,
        )
        img.raise_for_status()
        dest.write_bytes(img.content)
        log.info(f"텔레그램에서 다운로드: {dest.name}")
        return True
    except Exception as e:
        log.error(f"텔레그램 다운로드 실패: {e}")
        return False


def _send_to_pc(src: Path, label: str, ts: str) -> bool:
    try:
        with src.open("rb") as f:
            resp = requests.post(
                f"{PC_RECEIVER_URL}/upload",
                data={"label": label, "timestamp": ts},
                files={"photo": (src.name, f, "image/jpeg")},
                timeout=30,
            )
        resp.raise_for_status()
        log.info(f"PC 전송 완료: {src.name} ({label})")
        return True
    except Exception as e:
        log.error(f"PC 전송 실패: {e}")
        return False


# ── 배경 갱신 ─────────────────────────────────────────────────────────────────
def _update_background(reason: str) -> None:
    global background_gray
    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None
    if frame is None:
        _send_text("⚠️ 배경 갱신 실패: 카메라 프레임 없음")
        return
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cv2.imwrite(str(BACKGROUND_PATH), frame)
    with background_lock:
        background_gray = gray
    log.info(f"배경 갱신 완료 ({reason})")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(str(BACKGROUND_PATH), "rb") as photo:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT, "caption": f"🔄 배경 갱신 ({reason})\n{now_str}"},
                files={"photo": photo},
                timeout=10,
            )
    except Exception as e:
        log.error(f"배경 갱신 알림 실패: {e}")


def background_update_loop() -> None:
    while True:
        now = datetime.now()
        seconds_until_next_hour = (60 - now.minute) * 60 - now.second
        log.info(f"다음 배경 갱신: {seconds_until_next_hour // 60}분 후 (정시)")
        time.sleep(seconds_until_next_hour)
        _update_background("정시 갱신")


# ── 이미지 정리 ───────────────────────────────────────────────────────────────
def _cleanup_images(keep: int = 50) -> None:
    files = sorted(IMAGES_DIR.glob("after_*.jpg"), key=lambda f: f.stat().st_mtime)
    for f in files[:-keep]:
        f.unlink(missing_ok=True)


# ── 이벤트 처리 ───────────────────────────────────────────────────────────────
def handle_event(ts: str) -> None:
    log.info(f"이벤트: {ts} — {CAPTURE_DELAY}초 후 캡처")
    time.sleep(CAPTURE_DELAY)

    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None
    if frame is None:
        return

    with background_lock:
        bg = background_gray.copy() if background_gray is not None else None
    if bg is None:
        return

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    diff = _frame_diff(bg, gray)

    if diff <= CONFIRM_THRESHOLD:
        log.info(f"재확인 변화 없음 ({diff}px) — 건너뜀")
        with event_time_lock:
            global last_event_cooldown
            last_event_cooldown = COOLDOWN_NO_ALERT
        return

    after_path = str(IMAGES_DIR / f"after_{ts}.jpg")
    cv2.imwrite(after_path, frame)
    log.info(f"저장: {after_path}")
    _cleanup_images()

    msg_id = _send_photo(after_path, ts)
    if msg_id is not None:
        with _sent_photo_lock:
            _sent_photo_map[msg_id] = after_path

    with event_time_lock:
        last_event_cooldown = COOLDOWN_ALERT


# ── 카메라 루프 ───────────────────────────────────────────────────────────────
def camera_loop() -> None:
    global latest_frame, background_gray, last_event_time, last_event_cooldown

    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not cap.isOpened():
        log.error("카메라를 열 수 없습니다")
        return

    for _ in range(10):
        cap.read()
    log.info("카메라 준비 완료")

    with background_lock:
        background_gray = cv2.imread(str(BACKGROUND_PATH), cv2.IMREAD_GRAYSCALE)
    if background_gray is None:
        log.error("배경 이미지 없음 — background_update.py 를 먼저 실행하세요")
        cap.release()
        return

    while True:
        ret, frame = cap.read()
        if not ret:
            log.warning("프레임 읽기 실패")
            time.sleep(1)
            continue

        with frame_lock:
            latest_frame = frame.copy()

        now = time.time()
        with event_time_lock:
            elapsed = now - last_event_time
            cooldown = last_event_cooldown

        if elapsed < cooldown:
            time.sleep(0.05)
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        with background_lock:
            bg = background_gray.copy() if background_gray is not None else None
        if bg is None:
            time.sleep(0.05)
            continue

        changed = _frame_diff(bg, gray)
        total = gray.size

        if changed / total > LIGHTING_THRESHOLD:
            time.sleep(0.05)
            continue

        if changed < MOTION_THRESHOLD:
            time.sleep(0.05)
            continue

        with event_time_lock:
            last_event_time = now
            last_event_cooldown = COOLDOWN_NO_ALERT

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pct = changed / total * 100
        log.info(f"동작 감지: {changed:,}px ({pct:.1f}%) — {ts}")
        threading.Thread(target=handle_event, args=(ts,), daemon=True).start()

        time.sleep(0.05)


# ── 텔레그램 봇 루프 ──────────────────────────────────────────────────────────
def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def _get_updates(offset: int):
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", []) if data.get("ok") else None
    except Exception as e:
        log.error(f"getUpdates 실패: {str(e).replace(TELEGRAM_TOKEN, '***')}")
        return None


def _process_update(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    chat_id = str(msg["chat"]["id"])
    if chat_id != TELEGRAM_CHAT:
        return

    msg_id = msg["message_id"]
    reply_to = msg.get("reply_to_message")

    if text == "갱신":
        _send_reply(chat_id, msg_id, "🔄 배경 갱신 중...")
        threading.Thread(target=_update_background, args=("수동 갱신",), daemon=True).start()
        return

    ref_id = reply_to.get("message_id")

    # message_id 로 이미지 경로 조회, 없으면 caption 에서 파싱 (재시작 대비)
    with _sent_photo_lock:
        src_path = _sent_photo_map.get(ref_id)

    if src_path is None:
        caption = reply_to.get("caption") or ""
        m = _TS_RE.search(caption)
        if not m:
            return
        src_path = str(IMAGES_DIR / f"after_{m.group(1)}.jpg")

    src = Path(src_path)
    ts = src.stem.replace("after_", "")

    if text not in VALID_LABELS:
        _send_reply(chat_id, msg_id,
                    f"알 수 없는 라벨입니다.\n사용 가능: {', '.join(sorted(VALID_LABELS))}")
        return

    tmp = None
    if not src.exists():
        photo_sizes = reply_to.get("photo")
        if not photo_sizes:
            _send_reply(chat_id, msg_id, f"이미지를 찾을 수 없습니다: {src.name}")
            return
        tmp = IMAGES_DIR / f"tmp_{ts}.jpg"
        if not _download_from_telegram(photo_sizes, tmp):
            _send_reply(chat_id, msg_id, "텔레그램에서 이미지를 가져오지 못했습니다.")
            return
        src = tmp

    if _send_to_pc(src, text, ts):
        src.unlink(missing_ok=True)
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)
        _send_reply(chat_id, msg_id, f"✅ 라벨 저장: {text}")
    else:
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)
        _send_reply(chat_id, msg_id, "PC 전송 실패 — 로그를 확인하세요.")


def telegram_bot_loop() -> None:
    offset = _load_offset()
    log.info(f"텔레그램 봇 시작 (offset={offset})")
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
                _process_update(update)
            except Exception as e:
                log.error(f"업데이트 처리 실패: {e}")
            offset = max(offset, update["update_id"] + 1)

        if updates:
            _save_offset(offset)

        time.sleep(2)


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.error("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 가 설정되지 않았습니다")
        sys.exit(1)
    if not PC_RECEIVER_URL:
        log.error("PC_RECEIVER 가 설정되지 않았습니다 (예: http://192.168.0.10:8765)")
        sys.exit(1)

    IMAGES_DIR.mkdir(exist_ok=True)
    OFFSET_FILE.parent.mkdir(exist_ok=True)

    threading.Thread(target=telegram_bot_loop, daemon=True).start()
    threading.Thread(target=background_update_loop, daemon=True).start()
    camera_loop()


if __name__ == "__main__":
    main()
