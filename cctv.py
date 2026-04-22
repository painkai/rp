#!/usr/bin/env python3
import os
import cv2
import time
import base64
import threading
import requests
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, render_template_string, request, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_file_handler = TimedRotatingFileHandler(
    LOG_DIR / "cctv.log", when="midnight", backupCount=7, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), _file_handler]
)
log = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID")
STREAM_PORT     = int(os.getenv("STREAM_PORT", 5000))
MOTION_THRESHOLD    = int(os.getenv("MOTION_THRESHOLD", 3000))
LIGHTING_THRESHOLD  = float(os.getenv("LIGHTING_THRESHOLD", 0.6))  # 전체 프레임 비율
CONFIRM_THRESHOLD   = int(os.getenv("CONFIRM_THRESHOLD", 1000))
CAPTURE_DELAY           = int(os.getenv("CAPTURE_DELAY", 2))
COOLDOWN_ALERT          = int(os.getenv("COOLDOWN_ALERT", 30))
COOLDOWN_NO_ALERT       = int(os.getenv("COOLDOWN_NO_ALERT", 10))
BG_UPDATE_INTERVAL      = int(os.getenv("BG_UPDATE_INTERVAL", 7200))
CONTINUOUS_ALERT_LIMIT  = int(os.getenv("CONTINUOUS_ALERT_LIMIT", 2))
CONTINUOUS_BG_MINUTES   = int(os.getenv("CONTINUOUS_BG_MINUTES", 3))
BG_CHANGE_THRESHOLD = int(os.getenv("BG_CHANGE_THRESHOLD", 50000))
ANALYZER        = os.getenv("ANALYZER", "ollama")   # "ollama" | "claude"
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "moondream")
STREAM_USER     = os.getenv("STREAM_USER", "")
STREAM_PASS     = os.getenv("STREAM_PASS", "")

IMAGES_DIR      = Path("images")
BACKGROUND_PATH = IMAGES_DIR / "background.jpg"
IMAGES_DIR.mkdir(exist_ok=True)

# ── 공유 상태 ──────────────────────────────────────────────────────────────────
latest_frame = None
frame_lock = threading.Lock()

background_gray = None
background_lock = threading.Lock()

last_event_time = 0.0
last_event_cooldown = COOLDOWN_NO_ALERT
event_time_lock = threading.Lock()

consecutive_alerts = 0
continuous_start = 0.0
last_confirmed_time = 0.0
alert_state_lock = threading.Lock()

next_bg_update_time = 0.0


def _force_bg_update(reason: str) -> None:
    global background_gray, consecutive_alerts, continuous_start, last_confirmed_time, next_bg_update_time
    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None
    if frame is None:
        return
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (21, 21), 0)
    cv2.imwrite(str(BACKGROUND_PATH), frame)
    with background_lock:
        background_gray = gray
    with alert_state_lock:
        consecutive_alerts = 0
        continuous_start = 0.0
        last_confirmed_time = 0.0
    next_bg_update_time = time.time() + BG_UPDATE_INTERVAL
    _send_photo_bytes(frame, f"🔄 배경 갱신 완료 — {reason} ({datetime.now().strftime('%H:%M')})")
    log.info(f"배경 강제 갱신 완료 — {reason}")


def _load_background():
    if not BACKGROUND_PATH.exists():
        return None
    img = cv2.imread(str(BACKGROUND_PATH), cv2.IMREAD_GRAYSCALE)
    return cv2.GaussianBlur(img, (21, 21), 0)


def _frame_diff(a, b) -> int:
    # 전체 밝기 차이(자동노출 변화)를 제거하고 비교
    a_norm = cv2.normalize(a, None, 0, 255, cv2.NORM_MINMAX)
    b_norm = cv2.normalize(b, None, 0, 255, cv2.NORM_MINMAX)
    diff = cv2.absdiff(a_norm, b_norm)
    _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    return cv2.countNonZero(thresh)


# ── 텔레그램 ──────────────────────────────────────────────────────────────────
def send_telegram(image_path: str, analysis: str, ts: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("텔레그램 설정 없음 — 전송 건너뜀")
        return

    caption = f"[동작 감지] {ts}" + (f"\n\n{analysis}" if analysis else "")
    try:
        with open(image_path, "rb") as photo:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT, "caption": caption},
                files={"photo": photo},
                timeout=10,
            )
        resp.raise_for_status()
        log.info("텔레그램 전송 완료")
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")


def send_telegram_text(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("텔레그램 텍스트 전송 완료")
    except Exception as e:
        log.error(f"텔레그램 텍스트 전송 실패: {e}")


def _send_photo_bytes(img, caption: str = "") -> None:
    ret, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ret:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT, "caption": caption},
            files={"photo": ("frame.jpg", buf.tobytes(), "image/jpeg")},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        log.error(f"텔레그램 사진 전송 실패: {e}")


# ── 텔레그램 봇 커맨드 수신 ───────────────────────────────────────────────────
_COMMANDS = {
    "화면": "현재 화면",
    "지금 화면 보여줘": "현재 화면",
    "배경": "현재 배경",
    "지금 배경 보여줘": "현재 배경",
    "언제": "배경 갱신 시간",
    "갱신": "배경 즉시 갱신",
}

def telegram_bot_loop() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return

    offset = 0
    log.info("텔레그램 봇 커맨드 수신 시작")

    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            updates = resp.json().get("result", [])
        except Exception as e:
            log.error(f"텔레그램 getUpdates 실패: {e}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            if chat_id != TELEGRAM_CHAT or not text:
                continue

            label = _COMMANDS.get(text)
            if label is None:
                continue

            log.info(f"봇 커맨드 수신: {text}")

            if "화면" in label:
                with frame_lock:
                    frame = latest_frame.copy() if latest_frame is not None else None
                if frame is not None:
                    _send_photo_bytes(frame, f"[{label}] {datetime.now().strftime('%H:%M:%S')}")
                else:
                    send_telegram_text("카메라 프레임을 가져올 수 없습니다.")

            elif "즉시 갱신" in label:
                send_telegram_text("🔄 배경 갱신 중...")
                threading.Thread(target=_force_bg_update, args=("수동 갱신",), daemon=True).start()

            elif "갱신 시간" in label:
                if next_bg_update_time > 0:
                    remaining = int(next_bg_update_time - time.time())
                    next_str = datetime.fromtimestamp(next_bg_update_time).strftime("%H:%M")
                    if remaining > 0:
                        m, s = divmod(remaining, 60)
                        send_telegram_text(f"🕐 다음 배경 갱신: {next_str} (약 {m}분 후)")
                    else:
                        send_telegram_text("🔄 배경 갱신 중...")
                else:
                    send_telegram_text("배경 갱신 시간이 아직 설정되지 않았습니다.")

            elif "배경" in label:
                if BACKGROUND_PATH.exists():
                    bg = cv2.imread(str(BACKGROUND_PATH))
                    if bg is not None:
                        _send_photo_bytes(bg, f"[{label}] {datetime.now().strftime('%H:%M:%S')}")
                else:
                    send_telegram_text("배경 이미지가 없습니다.")


# ── 분석 프롬프트 (공통) ──────────────────────────────────────────────────────
_PROMPT = (
    "Image 1 is the empty background of a front door entrance.\n"
    "Image 2 was captured after motion was detected.\n"
    "Compare the two images and identify what is new or different in Image 2.\n"
    "If the two images look identical or you cannot clearly identify any difference, reply with 오감지.\n"
    "Reply in Korean using ONLY one of these labels, then add a brief detail in parentheses:\n"
    "사람 인식 / 택배 인식 / 동물 인식 / 차량 인식 / 비닐봉지 인식 / 기타 물체 인식 / 오감지\n"
    "Example: 택배 인식 (현관 앞 중간 크기 박스)"
)


def _encode(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def _check_background() -> str | None:
    if not BACKGROUND_PATH.exists():
        return "배경 이미지 없음 — background_update.py 를 먼저 실행하세요"
    return None


# ── Claude Vision 분석 ────────────────────────────────────────────────────────
def analyze_with_claude(after_path: str) -> str:
    from anthropic import Anthropic

    err = _check_background()
    if err:
        return err

    try:
        response = Anthropic().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _encode(str(BACKGROUND_PATH))}},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _encode(after_path)}},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
        result = response.content[0].text.strip()
        log.info(f"Claude 분석 결과: {result}")
        return result
    except Exception as e:
        log.error(f"Claude 분석 실패: {e}")
        return f"분석 실패: {e}"


# ── Ollama Vision 분석 ────────────────────────────────────────────────────────
def analyze_with_ollama(after_path: str) -> str:
    err = _check_background()
    if err:
        return err

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": _PROMPT,
                "images": [_encode(str(BACKGROUND_PATH)), _encode(after_path)],
                "stream": False,
            },
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()["response"].strip()
        log.info(f"Ollama 분석 결과: {result}")
        return result
    except Exception as e:
        log.error(f"Ollama 분석 실패: {e}")
        return f"분석 실패: {e}"


# ── 분석기 선택 (Ollama 우선, 실패 시 Claude fallback) ────────────────────────
def analyze(after_path: str) -> str:
    if ANALYZER == "claude":
        return analyze_with_claude(after_path)

    try:
        result = analyze_with_ollama(after_path)
        if result and "분석 실패" not in result:
            return result
        raise RuntimeError("Ollama 응답 없음")
    except Exception as e:
        log.warning(f"Ollama 실패 → Claude fallback: {e}")
        return analyze_with_claude(after_path)


# ── 이벤트 처리 (동작 감지 후 5초 대기 → 분석 → 알림) ────────────────────────
def handle_event(ts: str) -> None:
    global last_event_cooldown, consecutive_alerts, continuous_start, last_confirmed_time
    log.info(f"이벤트 시작: {ts} — {CAPTURE_DELAY}초 후 캡처")
    time.sleep(CAPTURE_DELAY)

    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None

    if frame is None:
        log.warning("after 프레임 없음 — 이벤트 취소")
        return

    with background_lock:
        bg = background_gray

    if bg is not None:
        after_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        after_gray = cv2.GaussianBlur(after_gray, (21, 21), 0)
        diff = _frame_diff(bg, after_gray)
        log.info(f"3초 후 diff: {diff} (임계값: {CONFIRM_THRESHOLD})")
        if diff <= CONFIRM_THRESHOLD:
            log.info(f"3초 후 변화 없음 — 알림 건너뜀 ({ts})")
            with alert_state_lock:
                consecutive_alerts = 0
                continuous_start = 0.0
                last_confirmed_time = 0.0
            with event_time_lock:
                last_event_cooldown = COOLDOWN_NO_ALERT
            return

    with event_time_lock:
        last_event_cooldown = COOLDOWN_ALERT

    now = time.time()
    with alert_state_lock:
        # 마지막 감지로부터 쿨다운의 3배 이상 지났으면 연속이 끊긴 것으로 판단
        gap = now - last_confirmed_time if last_confirmed_time > 0 else 0
        if gap > COOLDOWN_ALERT * 3:
            continuous_start = now
            consecutive_alerts = 0
        if continuous_start == 0.0:
            continuous_start = now
        last_confirmed_time = now
        consecutive_alerts += 1
        count = consecutive_alerts
        elapsed = now - continuous_start

    # 3분 연속 감지 → 배경 강제 갱신
    if elapsed >= CONTINUOUS_BG_MINUTES * 60:
        log.info(f"연속 감지 {CONTINUOUS_BG_MINUTES}분 경과 — 배경 강제 갱신")
        _force_bg_update(f"연속 감지 {CONTINUOUS_BG_MINUTES}분")
        return

    # 연속 알림 횟수 초과 시 텔레그램 건너뜀
    if count > CONTINUOUS_ALERT_LIMIT:
        log.info(f"연속 알림 {count}회 — 텔레그램 건너뜀 (최대 {CONTINUOUS_ALERT_LIMIT}회)")
        return

    after_path = str(IMAGES_DIR / f"after_{ts}.jpg")
    cv2.imwrite(after_path, frame)
    log.info(f"after 저장: {after_path}")

    # analysis = analyze(after_path)
    # send_telegram(after_path, analysis, ts)
    send_telegram(after_path, "", ts)

    cleanup_images()


def cleanup_images(keep: int = 50) -> None:
    files = sorted(IMAGES_DIR.glob("after_*.jpg"), key=lambda f: f.stat().st_mtime)
    for f in files[:-keep]:
        f.unlink(missing_ok=True)


# ── 배경 자동 갱신 루프 ───────────────────────────────────────────────────────
def background_update_loop() -> None:
    global background_gray, consecutive_alerts, continuous_start, next_bg_update_time

    # 첫 실행은 interval 후 시작
    next_bg_update_time = time.time() + BG_UPDATE_INTERVAL
    for _ in range(BG_UPDATE_INTERVAL // 60):
        time.sleep(60)

    while True:
        with frame_lock:
            candidate = latest_frame.copy() if latest_frame is not None else None

        if candidate is None:
            time.sleep(60)
            continue

        candidate_gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
        candidate_gray = cv2.GaussianBlur(candidate_gray, (21, 21), 0)

        with background_lock:
            current_bg = background_gray

        if current_bg is not None and _frame_diff(current_bg, candidate_gray) > BG_CHANGE_THRESHOLD:
            # 차이가 크면 1분 후 재촬영해서 안정적인지 확인
            log.info("배경 변화 큼 — 1분 후 재확인")
            time.sleep(60)

            with frame_lock:
                candidate2 = latest_frame.copy() if latest_frame is not None else None

            if candidate2 is None:
                time.sleep(BG_UPDATE_INTERVAL - 60)
                continue

            candidate2_gray = cv2.cvtColor(candidate2, cv2.COLOR_BGR2GRAY)
            candidate2_gray = cv2.GaussianBlur(candidate2_gray, (21, 21), 0)

            if _frame_diff(candidate_gray, candidate2_gray) > BG_CHANGE_THRESHOLD:
                log.info("배경 여전히 불안정 — 갱신 건너뜀")
                time.sleep(BG_UPDATE_INTERVAL - 60)
                continue

            # 1분 사이 안정화됨 → 두 번째 프레임으로 갱신
            save_gray = candidate2_gray
            save_frame = candidate2
        else:
            save_gray = candidate_gray
            save_frame = candidate

        cv2.imwrite(str(BACKGROUND_PATH), save_frame)
        with background_lock:
            background_gray = save_gray
        with alert_state_lock:
            consecutive_alerts = 0
            continuous_start = 0.0
        next_bg_update_time = time.time() + BG_UPDATE_INTERVAL
        _send_photo_bytes(save_frame, f"🔄 배경 이미지 정기 갱신 완료 ({datetime.now().strftime('%H:%M')})")
        log.info("배경 이미지 자동 갱신 완료")

        for _ in range(BG_UPDATE_INTERVAL // 60):
            time.sleep(60)


# ── 카메라 캡처 + 동작 감지 루프 ──────────────────────────────────────────────
def camera_loop() -> None:
    global latest_frame, background_gray, last_event_time, last_event_cooldown, consecutive_alerts, continuous_start

    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not cap.isOpened():
        log.error("카메라를 열 수 없습니다")
        return

    log.info("카메라 시작")

    with background_lock:
        background_gray = _load_background()

    if background_gray is not None:
        log.info("배경 이미지 로드 완료")
    else:
        log.warning("배경 이미지 없음 — 동작 감지 비활성. background_update.py 를 실행하세요")

    while True:
        ret, frame = cap.read()
        if not ret:
            log.warning("프레임 읽기 실패 — 재시도")
            time.sleep(0.5)
            continue

        with frame_lock:
            latest_frame = frame

        with background_lock:
            bg = background_gray

        if bg is None:
            time.sleep(0.05)
            continue

        now = time.time()
        with event_time_lock:
            elapsed = now - last_event_time
            cooldown = last_event_cooldown

        if elapsed < cooldown:
            time.sleep(0.05)
            continue

        # 동작 감지
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        changed = _frame_diff(bg, gray)
        total_pixels = frame.shape[0] * frame.shape[1]

        if changed / total_pixels > LIGHTING_THRESHOLD:
            time.sleep(0.05)
            continue  # 전체 프레임 변화 → 조명 변화로 판단, 무시

        if changed > MOTION_THRESHOLD:
            with event_time_lock:
                last_event_time = now
                last_event_cooldown = COOLDOWN_NO_ALERT  # handle_event에서 확정
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log.info(f"동작 감지! 변화 픽셀: {changed} — {ts}")

            threading.Thread(target=handle_event, args=(ts,), daemon=True).start()

        time.sleep(0.05)


# ── Flask 웹 스트리밍 ──────────────────────────────────────────────────────────
app = Flask(__name__)


def _check_auth():
    if not STREAM_USER or not STREAM_PASS:
        return True
    # URL 토큰 인증 (?token=비밀번호)
    if request.args.get("token") == STREAM_PASS:
        return True
    # Basic 인증
    auth = request.authorization
    return auth and auth.username == STREAM_USER and auth.password == STREAM_PASS


def _require_auth():
    return Response(
        "인증이 필요합니다.", 401,
        {"WWW-Authenticate": 'Basic realm="CCTV"'}
    )

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>현관 CCTV</title>
  <style>
    body { margin: 0; background: #111; display: flex; flex-direction: column;
           align-items: center; justify-content: center; height: 100vh; }
    h1   { color: #eee; font-family: sans-serif; font-size: 1rem; margin-bottom: 12px; }
    img  { width: 100%; max-width: 640px; border: 2px solid #333; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>현관 CCTV — 실시간</h1>
  <img src="/stream{{ token_param }}" alt="live">
</body>
</html>
"""


def mjpeg_generator():
    while True:
        with frame_lock:
            frame = latest_frame.copy() if latest_frame is not None else None

        if frame is None:
            time.sleep(0.1)
            continue

        ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ret:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + buf.tobytes()
                + b"\r\n"
            )
        time.sleep(0.05)  # ~20 fps


@app.route("/")
def index():
    if not _check_auth():
        return _require_auth()
    token = request.args.get("token", "")
    token_param = f"?token={token}" if token else ""
    return render_template_string(INDEX_HTML, token_param=token_param)


@app.route("/stream")
def stream():
    if not _check_auth():
        return _require_auth()
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ── 진입점 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cam_thread = threading.Thread(target=camera_loop, daemon=True)
    cam_thread.start()

    bg_thread = threading.Thread(target=background_update_loop, daemon=True)
    bg_thread.start()

    bot_thread = threading.Thread(target=telegram_bot_loop, daemon=True)
    bot_thread.start()

    log.info(f"웹 스트리밍 시작: http://0.0.0.0:{STREAM_PORT}")
    app.run(host="0.0.0.0", port=STREAM_PORT, threaded=True)
