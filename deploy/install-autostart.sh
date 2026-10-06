#!/bin/bash
# 데스크톱 로그인 후 터미널 창을 열어 MqttVLCPlayer를 실행하도록 자동 시작에 등록한다.
# 사용법: 실행할 사용자로(sudo 없이) deploy/install-autostart.sh
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$HOME/.local/bin/mqttvlcplayer-launch"
DESKTOP="$HOME/.config/autostart/mqttvlcplayer.desktop"

# 실행기는 로컬 디스크에 둔다 (프로젝트가 NAS에 있으면 부팅 직후에는 아직 안 보일 수 있음)
mkdir -p "$(dirname "$LAUNCHER")" "$(dirname "$DESKTOP")"
cat > "$LAUNCHER" <<LAUNCH
#!/bin/bash
APP_DIR="$APP_DIR"
until [ -x "\$APP_DIR/deploy/start.sh" ]; do
    echo "\$APP_DIR 를 기다리는 중... (NAS 연결 대기)"
    sleep 5
done
# Ctrl+C로 앱을 멈춰도 이 창은 닫히지 않게 한다
trap 'true' INT
"\$APP_DIR/deploy/start.sh"
echo
echo "MqttVLCPlayer가 멈췄습니다. 다시 실행: \$APP_DIR/deploy/start.sh"
exec bash
LAUNCH
chmod +x "$LAUNCHER" "$APP_DIR/deploy/start.sh"

# 설치된 터미널 프로그램에 맞는 실행 명령
if command -v gnome-terminal >/dev/null; then
    TERM_CMD="gnome-terminal --title=MqttVLCPlayer -- $LAUNCHER"
elif command -v xfce4-terminal >/dev/null; then
    TERM_CMD="xfce4-terminal --title=MqttVLCPlayer -x $LAUNCHER"
elif command -v konsole >/dev/null; then
    TERM_CMD="konsole -e $LAUNCHER"
elif command -v lxterminal >/dev/null; then
    TERM_CMD="lxterminal -t MqttVLCPlayer -e $LAUNCHER"
elif command -v x-terminal-emulator >/dev/null; then
    TERM_CMD="x-terminal-emulator -e $LAUNCHER"
else
    echo "터미널 프로그램을 찾지 못했습니다." >&2
    exit 1
fi

cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=MqttVLCPlayer
Comment=로그인 후 터미널에서 MqttVLCPlayer 실행
Exec=$TERM_CMD
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=10
DESK

echo "자동 시작 등록 완료"
echo "  실행기: $LAUNCHER"
echo "  자동 시작 항목: $DESKTOP"
echo "  터미널 명령: $TERM_CMD"
echo "해제하려면: rm $DESKTOP"
