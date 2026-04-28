# 현관 CCTV `v1.3`

라즈베리파이 + USB 웹캠으로 구성하는 현관 모션 감지 CCTV.  
동작 감지 시 AI가 상황을 분석해 텔레그램으로 알림을 보냅니다.

## 기능

### 핵심
- **실시간 스트리밍** — Flask MJPEG 스트림 (기본 포트 5000)
- **동작 감지** — MOG2 적응형 배경 모델로 전경 픽셀 수 판단 (조명 변화 자동 적응)
- **AI 분석** — Ollama(moondream) 또는 Claude Vision으로 상황 분석
  - 분류: 사람 인식 / 택배 인식 / 동물 인식 / 차량 인식 / 비닐봉지 인식 / 기타 물체 인식 / 오감지
  - 두 이미지가 동일하거나 차이를 명확히 식별할 수 없으면 오감지로 출력
- **텔레그램 알림** — 동작 감지 시 사진 + 분석 결과 전송

### 오감지 방지
- **링버퍼 비교** — 감지 시점의 30초 전 프레임과 현재 프레임 비교, 변화 없으면 건너뜀
  - 지나가는 사람(30초 이내 퇴장) → 오감지 없음
  - 멈춰 있는 택배·사람 → 정상 감지
- **MOG2 조명 적응** — 아침/저녁 자동노출 변화를 배경 모델이 흡수해 오감지 방지
- **급격한 조명 변화 무시** — 전체 프레임의 60% 이상 변화 시 조명 이벤트로 판단해 무시
- **밝기 정규화** — 프레임 비교 시 자동노출 변화 영향 제거

### 연속 감지 처리
- 연속 감지 시 텔레그램 최대 2회만 발송
- 3분간 연속 감지 시 배경 강제 갱신 후 알림 재개

### 배경 자동 갱신
- 2시간마다 자동 갱신
- 변화가 클 경우 1분 후 재촬영해 안정적일 때만 갱신 (사람이 있을 때 갱신 방지)
- 갱신 시 텔레그램 알림

### 접근 보안
- Basic 인증 (STREAM_USER / STREAM_PASS)
- URL 토큰 인증 (`?token=비밀번호`) — 모바일 홈 화면 바로가기용

### 텔레그램 봇 커맨드
봇에게 메시지를 보내면 즉시 응답합니다.

| 명령어 | 동작 |
|--------|------|
| `화면` 또는 `지금 화면 보여줘` | 현재 카메라 화면 전송 |
| `배경` 또는 `지금 배경 보여줘` | 저장된 배경 이미지 전송 |
| `언제` | 다음 배경 갱신 예정 시간 안내 |
| `갱신` | 배경 즉시 갱신 |

### 운영
- **로그** — 일별 rotation, 7일 보관 (`logs/cctv.log`)
- **이미지 정리** — 최근 50장만 유지

## 요구사항

- 라즈베리파이 2 이상
- USB 웹캠
- Python 3.9+
- Ollama (다른 PC에서 실행 가능) 또는 Anthropic API 키
- 텔레그램 봇 토큰 + Chat ID

## 설치

```bash
# 시스템 패키지
sudo apt update
sudo apt install -y git python3 python3-pip python3-venv python3-opencv

# 소스 받기
git clone <repo-url> ~/rp
cd ~/rp

# 가상환경
python3 -m venv .venv --system-site-packages
source .venv/bin/activate

# 패키지 설치
pip install flask python-dotenv requests anthropic

# 환경 설정
cp .env.example .env
nano .env
```

## 실행

```bash
# 배경 촬영 (아무도 없는 상태에서)
python background_update.py --delay 5

# 실행
python cctv.py
```

브라우저에서 `http://<파이_IP>:5000` 접속

## 부팅 시 자동 시작

```bash
sudo nano /etc/systemd/system/cctv.service
```

```ini
[Unit]
Description=CCTV
After=network.target

[Service]
User=<사용자명>
WorkingDirectory=/home/<사용자명>/rp
ExecStart=/home/<사용자명>/rp/.venv/bin/python cctv.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable cctv
sudo systemctl start cctv
```

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | — | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | — | 텔레그램 Chat ID |
| `ANALYZER` | `ollama` | 분석기 선택: `ollama` / `claude` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama 서버 주소 |
| `OLLAMA_MODEL` | `moondream` | Ollama 모델명 |
| `ANTHROPIC_API_KEY` | — | Claude API 키 |
| `STREAM_PORT` | `5000` | 웹 스트리밍 포트 |
| `STREAM_USER` | — | 스트림 접근 아이디 |
| `STREAM_PASS` | — | 스트림 접근 비밀번호 |
| `MOTION_THRESHOLD` | `3000` | MOG2 전경 픽셀 수 트리거 |
| `MOG2_VAR_THRESHOLD` | `50` | MOG2 민감도 (높을수록 둔감) |
| `MOG2_HISTORY` | `1000` | MOG2 배경 모델 학습 프레임 수 (~50초 @ 20fps) |
| `RING_BUFFER_SECONDS` | `30` | 링버퍼 비교 기준 과거 시점 (초) |
| `RING_SAMPLE_INTERVAL` | `1.0` | 링버퍼 프레임 저장 간격 (초) |
| `CAPTURE_DELAY` | `2` | 감지 후 캡처까지 대기 시간 (초) |
| `CONFIRM_THRESHOLD` | `1000` | 링버퍼 비교 확인 임계값 (픽셀) |
| `LIGHTING_THRESHOLD` | `0.6` | 급격한 조명 변화 판단 비율 (0.0~1.0) |
| `COOLDOWN_ALERT` | `30` | 알림 발송 후 재감지 대기 (초) |
| `COOLDOWN_NO_ALERT` | `10` | 알림 미발송 후 재감지 대기 (초) |
| `CONTINUOUS_ALERT_LIMIT` | `2` | 연속 감지 시 최대 알림 횟수 |
| `CONTINUOUS_BG_MINUTES` | `3` | 연속 감지 후 배경 갱신 시간 (분) |
| `BG_UPDATE_INTERVAL` | `7200` | 배경 정기 갱신 주기 (초) |
| `BG_CHANGE_THRESHOLD` | `50000` | 배경 갱신 전 변화 감지 임계값 (픽셀) |

## 변경 이력

### v1.3
- 동작 감지 방식을 정적 배경 diff → **MOG2 적응형 배경 모델**로 교체
  - 아침/저녁 조명 변화에 의한 오감지 해소
- 확인 비교를 "감지→2초 후" 방식 → **30초 전 링버퍼 프레임 비교**로 교체
  - 문 앞에 멈춰 있는 택배·사람도 안정적으로 감지
  - 빠르게 지나가는 사람(30초 이내)은 알림 없음

### v1.2
- MOG2 적응형 배경 + 링버퍼 비교 실험 (→ v1.3에서 정식 적용)

### v1.1
- 감지→캡처 / 캡처→배경 두 단계 비교 도입
- 시작 시 배경 즉시 갱신
