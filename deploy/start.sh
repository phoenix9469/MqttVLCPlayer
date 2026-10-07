#!/bin/bash
# MqttVLCPlayer 실행 스크립트: 환경변수 파일을 읽고 앱을 실행한다.
# 앱이 종료되면 5초 뒤 환경변수 파일을 다시 읽어 다시 실행한다.
# 설정을 바꾼 뒤 Ctrl+C를 한 번 누르면 새 설정으로 재시작, 대기 중에 한 번 더 누르면 완전히 멈춘다.
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-/etc/mqttvlcplayer.env}"

load_env() {
    if [ -r "$ENV_FILE" ]; then
        # Windows에서 편집해 줄 끝에 \r이 붙은 파일도 읽을 수 있게 지우고 불러옴
        set -a
        . <(tr -d '\r' < "$ENV_FILE")
        set +a
    elif [ -e "$ENV_FILE" ]; then
        echo "경고: $ENV_FILE 을 읽을 권한이 없습니다. (sudo chown root:$(id -un) $ENV_FILE; sudo chmod 640 $ENV_FILE)"
    else
        echo "경고: $ENV_FILE 이 없어 기본 설정으로 실행합니다."
    fi
}

cd "$APP_DIR" || exit 1
PYTHON="$APP_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

while true; do
    echo "[$(date '+%F %T')] MqttVLCPlayer 시작 ($APP_DIR, 설정: $ENV_FILE)"
    # 매번 새 하위 셸에서 설정을 읽어서, 파일에서 지운 변수가 남지 않고 바뀐 값이 반영됨
    ( load_env; export MQTTVLCPLAYER_SUPERVISED=1; exec "$PYTHON" video_player.py )
    code=$?
    if [ "$code" -eq 75 ]; then
        # 웹 UI의 재시작 버튼
        echo "[$(date '+%F %T')] 재시작 요청. 설정을 다시 읽고 시작합니다."
        sleep 1
        continue
    fi
    echo "[$(date '+%F %T')] MqttVLCPlayer 종료 (코드 $code). 5초 후 설정을 다시 읽고 시작합니다. 완전히 멈추려면 지금 Ctrl+C"
    sleep 5
done
