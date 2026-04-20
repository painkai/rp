#!/usr/bin/env python3
"""
현재 카메라 화면을 배경 이미지로 저장합니다.
아무도 없는 상태에서 실행하세요.

사용법:
  python background_update.py          # 즉시 캡처
  python background_update.py --delay 5  # 5초 후 캡처
"""
import cv2
import time
import argparse
from pathlib import Path

IMAGES_DIR = Path("images")
BACKGROUND_PATH = IMAGES_DIR / "background.jpg"


def capture_background(delay: int = 0) -> None:
    IMAGES_DIR.mkdir(exist_ok=True)

    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not cap.isOpened():
        print("카메라를 열 수 없습니다")
        return

    # 카메라 워밍업
    for _ in range(10):
        cap.read()

    if delay > 0:
        print(f"{delay}초 후 캡처합니다. 현관에서 비켜주세요...")
        time.sleep(delay)

    ret, frame = cap.read()
    cap.release()

    if not ret:
        print("프레임 읽기 실패")
        return

    cv2.imwrite(str(BACKGROUND_PATH), frame)
    print(f"배경 이미지 저장 완료: {BACKGROUND_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=int, default=0, help="캡처 전 대기 시간(초)")
    args = parser.parse_args()
    capture_background(args.delay)
