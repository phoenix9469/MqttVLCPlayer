import json
import logging
import os
import random
import re
import subprocess
import sys
import threading

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mqttvlcplayer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 실행 환경 설정 (환경변수로 지정, 비밀번호 등은 코드에 넣지 않는다)
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "config.json"))
PLAYLIST_PATH = os.environ.get("PLAYLIST_PATH", os.path.join(BASE_DIR, "playlist.m3u8"))
LGTV_SCRIPT = os.environ.get("LGTV_SCRIPT", os.path.join(BASE_DIR, "libLGTV_serial", "LGTV.py"))

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "CVLC_TV")

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "5000"))
WEB_USERNAME = os.environ.get("WEB_USERNAME")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD")

# TV 전원 상태 조회 주기(초), 0이면 조회하지 않음
STATUS_INTERVAL = int(os.environ.get("STATUS_INTERVAL", "60"))

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi")
CVLC_ARGS = ["cvlc", "--fullscreen", "--play-and-exit", "--no-osd", "--audio-filter", "normvol"]

# MQTT 토픽
TOPIC_AVAILABILITY = "cvlc_tv/availability"
TOPIC_TV_ON = "cvlc_tv/lgtv/on"
TOPIC_TV_OFF = "cvlc_tv/lgtv/off"
TOPIC_TV_STATUS = "cvlc_tv/lgtv/status"
TOPIC_TV_SWITCH = "cvlc_tv/lgtv/switch"
TOPIC_TV_SWITCH_SET = "cvlc_tv/lgtv/switch/set"
TOPIC_PLAY = "cvlc_tv/cvlc/play"
TOPIC_STOP = "cvlc_tv/cvlc/stop"

DEVICE = {
    "name": "Video Control Server",
    "identifiers": "CVLC_TV",
    "manufacturer": "PNXELEC",
    "model": "CVLC",
    "hw_version": "0.00",
    "sw_version": "0.00",
    "configuration_url": f"http://localhost:{WEB_PORT}",
}

# Home Assistant MQTT Discovery: (discovery 토픽, 엔티티 설정)
DISCOVERY = [
    ("homeassistant/button/cvlc_tv_on/config", {
        "name": "BTN_LGTV_232_ON",
        "unique_id": "LGTV_232_ON",
        "command_topic": TOPIC_TV_ON,
    }),
    ("homeassistant/button/cvlc_tv_off/config", {
        "name": "BTN_LGTV_232_OFF",
        "unique_id": "LGTV_232_OFF",
        "command_topic": TOPIC_TV_OFF,
    }),
    ("homeassistant/binary_sensor/cvlc_tv_status/config", {
        "name": "BINARY_SENSOR_LGTV_232_STATUS",
        "unique_id": "LGTV_232_STATUS",
        "state_topic": TOPIC_TV_STATUS,
        "payload_on": "1",
        "payload_off": "0",
    }),
    ("homeassistant/button/cvlc_tv_play/config", {
        "name": "BTN_CVLC_RANDOM_PLAY",
        "unique_id": "CVLC_RANDOM_PLAY",
        "command_topic": TOPIC_PLAY,
    }),
    ("homeassistant/button/cvlc_tv_stop/config", {
        "name": "BTN_CVLC_STOP",
        "unique_id": "CVLC_STOP",
        "command_topic": TOPIC_STOP,
    }),
    ("homeassistant/switch/cvlc_tv_switch/config", {
        "name": "SWITCH_LGTV_PWR",
        "unique_id": "LGTV_232_SWITCH",
        "state_topic": TOPIC_TV_SWITCH,
        "command_topic": TOPIC_TV_SWITCH_SET,
        "payload_on": "1",
        "payload_off": "0",
        "state_on": "1",
        "state_off": "0",
    }),
]

app = Flask(__name__)

# 웹 UI에서 바꿀 수 있는 설정 (config.json에 저장)
config = {"nas_folder": os.environ.get("NAS_FOLDER", "/mv")}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        config.update({k: v for k, v in json.load(f).items() if k in config})


# ---------------------------------------------------------------------------
# 영상 재생
# ---------------------------------------------------------------------------

def get_video_files():
    """NAS 폴더에서 영상 파일 리스트 가져오기 (폴더가 없으면 빈 리스트)"""
    folder = config["nas_folder"]
    try:
        names = os.listdir(folder)
    except OSError as e:
        log.warning("Cannot read video folder %s: %s", folder, e)
        return []
    files = [os.path.join(folder, f) for f in names if f.lower().endswith(VIDEO_EXTENSIONS)]
    return sorted(files, key=lambda x: os.path.basename(x).lower())


def create_m3u8_playlist():
    """영상 목록을 섞어서 m3u8 재생목록 생성, 생성된 영상 수 반환"""
    video_files = get_video_files()
    random.shuffle(video_files)
    with open(PLAYLIST_PATH, "w") as playlist:
        playlist.write("#EXTM3U\n")
        for file in video_files:
            playlist.write(f"#EXTINF:-1,{os.path.basename(file)}\n")
            playlist.write(f"{file}\n")
    log.info("m3u8 playlist created at %s (%d videos)", PLAYLIST_PATH, len(video_files))
    return len(video_files)


class Player:
    """cvlc 프로세스를 하나만 유지하는 플레이어"""

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None

    def _stop_locked(self):
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            log.info("Video stopped.")
        self._process = None

    def play(self, target):
        """기존 재생을 종료하고 target(파일 또는 재생목록) 재생"""
        with self._lock:
            self._stop_locked()
            try:
                self._process = subprocess.Popen(CVLC_ARGS + [target])
            except OSError as e:
                log.error("Failed to start cvlc: %s", e)
                return False
            log.info("Playing %s", target)
            return True

    def stop(self):
        with self._lock:
            self._stop_locked()

    @property
    def playing(self):
        return self._process is not None and self._process.poll() is None


player = Player()


def play_random():
    if create_m3u8_playlist() == 0:
        log.warning("No videos to play in %s", config["nas_folder"])
        return False
    return player.play(PLAYLIST_PATH)


# ---------------------------------------------------------------------------
# LG TV 제어 (RS-232, libLGTV_serial)
# ---------------------------------------------------------------------------

# 시리얼 포트는 동시에 하나만 사용할 수 있으므로 명령을 직렬화한다
tv_lock = threading.Lock()
tv_power = None  # True/False, 알 수 없으면 None


def run_lgtv(command):
    """LGTV.py 실행 후 출력값 반환 (실패 시 None)"""
    with tv_lock:
        try:
            result = subprocess.run([sys.executable, LGTV_SCRIPT, f"--{command}"],
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("LGTV %s failed: %s", command, e)
            return None
    output = result.stdout.strip()
    if result.returncode != 0:
        log.error("LGTV %s failed (rc=%d): %s", command, result.returncode, result.stderr.strip())
        return None
    return output


def publish_tv_power(on):
    global tv_power
    tv_power = on
    state = "1" if on else "0"
    client.publish(TOPIC_TV_STATUS, state, retain=True)
    client.publish(TOPIC_TV_SWITCH, state, retain=True)


def set_tv_power(on):
    """TV 전원 켜기/끄기, 성공 여부 반환"""
    if run_lgtv("poweron" if on else "poweroff") == "True":
        publish_tv_power(on)
        return True
    return False


def update_tv_status():
    """TV 전원 상태 조회 후 발행. 출력은 b'01' 또는 01 형태"""
    output = run_lgtv("powerstatus")
    match = re.fullmatch(r"(?:b')?(\d{2})'?", output or "")
    if not match:
        log.warning("Unknown TV power status: %r", output)
        return
    publish_tv_power(match.group(1) != "00")


stop_event = threading.Event()


def status_loop():
    while not stop_event.wait(STATUS_INTERVAL):
        update_tv_status()


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def publish_ha_discovery():
    availability = {"availability_topic": TOPIC_AVAILABILITY}
    for topic, payload in DISCOVERY:
        client.publish(topic, json.dumps({**payload, **availability, "device": DEVICE}), retain=True)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.error("MQTT connect failed: %s", reason_code)
        return
    log.info("MQTT connected to %s:%d", MQTT_HOST, MQTT_PORT)
    # 재접속 시에도 구독/discovery가 유지되도록 연결될 때마다 수행
    client.subscribe([(t, 0) for t in (TOPIC_TV_ON, TOPIC_TV_OFF, TOPIC_TV_SWITCH_SET, TOPIC_PLAY, TOPIC_STOP)])
    publish_ha_discovery()
    client.publish(TOPIC_AVAILABILITY, "online", retain=True)
    if STATUS_INTERVAL > 0:
        threading.Thread(target=update_tv_status, daemon=True).start()


def handle_message(topic, payload):
    if topic == TOPIC_TV_ON:
        set_tv_power(True)
    elif topic == TOPIC_TV_OFF:
        set_tv_power(False)
    elif topic == TOPIC_TV_SWITCH_SET:
        if payload in ("0", "1"):
            set_tv_power(payload == "1")
    elif topic == TOPIC_PLAY:
        play_random()
    elif topic == TOPIC_STOP:
        player.stop()


def on_message(client, userdata, message):
    payload = message.payload.decode("utf-8", errors="replace")
    log.info("MQTT message %s: %s", message.topic, payload)
    # TV 제어는 시간이 걸리므로 MQTT 네트워크 루프를 막지 않도록 별도 스레드에서 처리
    threading.Thread(target=handle_message, args=(message.topic, payload), daemon=True).start()


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
if MQTT_USERNAME:
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
client.will_set(TOPIC_AVAILABILITY, "offline", retain=True)
client.on_connect = on_connect
client.on_message = on_message


# ---------------------------------------------------------------------------
# 웹 UI
# ---------------------------------------------------------------------------

@app.before_request
def require_auth():
    """WEB_USERNAME/WEB_PASSWORD가 설정된 경우 HTTP Basic 인증 요구"""
    if not WEB_USERNAME:
        return None
    auth = request.authorization
    if auth and auth.username == WEB_USERNAME and auth.password == WEB_PASSWORD:
        return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="MqttVLCPlayer"'})


def result(ok):
    return jsonify({"status": "success" if ok else "error"}), 200 if ok else 500


@app.template_filter("basename")
def basename_filter(path):
    return os.path.basename(path)


@app.route("/")
def index():
    """웹 UI 메인 페이지"""
    return render_template("index.html", config=config, tv_power=tv_power, playing=player.playing)


@app.route("/update", methods=["POST"])
def update_config():
    """설정 업데이트"""
    data = request.get_json(silent=True) or {}
    nas_folder = data.get("nas_folder", config["nas_folder"])
    if not os.path.isdir(nas_folder):
        return jsonify({"status": "error", "message": f"폴더를 찾을 수 없습니다: {nas_folder}"}), 400
    config["nas_folder"] = nas_folder
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)
    return jsonify({"status": "success", "config": config})


@app.route("/on_tv", methods=["POST"])
def on_tv():
    return result(set_tv_power(True))


@app.route("/off_tv", methods=["POST"])
def off_tv():
    return result(set_tv_power(False))


@app.route("/play_now", methods=["POST"])
def play_now():
    """랜덤 재생 시작"""
    return result(play_random())


@app.route("/stop_video", methods=["POST"])
def stop_video():
    """영상 종료"""
    player.stop()
    return result(True)


@app.route("/file_list")
def file_list():
    """영상 파일 목록을 보여주는 페이지"""
    return render_template("file_list.html", video_files=get_video_files())


@app.route("/play_video", methods=["POST"])
def play_video():
    """선택한 영상 재생 (NAS 폴더 안의 영상 파일만 허용)"""
    name = request.form.get("video", "")
    videos = {os.path.basename(v): v for v in get_video_files()}
    if name not in videos:
        return "Unknown video", 400
    player.play(videos[name])
    return redirect(url_for("file_list"))


def main():
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    if STATUS_INTERVAL > 0:
        threading.Thread(target=status_loop, daemon=True).start()
    try:
        app.run(host=WEB_HOST, port=WEB_PORT)
    finally:
        stop_event.set()
        player.stop()
        try:
            client.publish(TOPIC_AVAILABILITY, "offline", retain=True).wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            pass
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
