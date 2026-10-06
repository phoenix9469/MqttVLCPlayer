#!/bin/bash
# MqttVLCPlayer 실행 스크립트: 환경변수 파일을 읽고 앱을 실행한다.
# 앱이 종료되면 5초 뒤 다시 실행한다. 멈추려면 Ctrl+C.
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-/etc/mqttvlcplayer.env}"

if [ -r "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
elif [ -e "$ENV_FILE" ]; then
    echo "경고: $ENV_FILE 을 읽을 권한이 없습니다. (sudo chown root:$(id -un) $ENV_FILE; sudo chmod 640 $ENV_FILE)"
else
    echo "경고: $ENV_FILE 이 없어 기본 설정으로 실행합니다."
fi

cd "$APP_DIR" || exit 1
PYTHON="$APP_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

while true; do
    echo "[$(date '+%F %T')] MqttVLCPlayer 시작 ($APP_DIR)"
    "$PYTHON" video_player.py
    code=$?
    echo "[$(date '+%F %T')] MqttVLCPlayer 종료 (코드 $code). 5초 후 다시 시작합니다. 멈추려면 Ctrl+C"
    sleep 5
done
