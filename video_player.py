import io
import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for

import tts
from player import Player
from sound import get_sound_files, parse_sound_payload, sounds, SOUND_VOLUME

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

# 재생 화면 좌측 상단에 표시할 시계 (strftime 형식, 빈 값이면 표시 안 함)
CLOCK_FORMAT = os.environ.get("CLOCK_FORMAT", "%H:%M")
CLOCK_SIZE = int(os.environ.get("CLOCK_SIZE", "0"))  # 글자 크기(px), 0이면 VLC가 화면에 맞춰 자동 결정

# 영상 재생 버퍼(ms). NAS에서 읽을 때 끊기면 늘린다 (VLC 기본값은 1000ms)
VLC_CACHING = int(os.environ.get("VLC_CACHING", "3000"))
# 영상 재생에 추가할 VLC 옵션 (예: "--avcodec-hw=vaapi --vout=xcb_x11")
VLC_EXTRA_ARGS = shlex.split(os.environ.get("VLC_EXTRA_ARGS", ""))


VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi")
CVLC_ARGS = ["cvlc", "--fullscreen", "--play-and-exit", "--no-osd", "--audio-filter", "normvol"]
if CLOCK_FORMAT:
    # marq 필터: position 5 = 위(4) + 왼쪽(1), 1초마다 갱신
    CVLC_ARGS += ["--sub-source=marq", f"--marq-marquee={CLOCK_FORMAT}", "--marq-position=5",
                  "--marq-x=30", "--marq-y=20", "--marq-refresh=1000"]
    if CLOCK_SIZE > 0:
        CVLC_ARGS.append(f"--marq-size={CLOCK_SIZE}")
CVLC_ARGS += [f"--file-caching={VLC_CACHING}", f"--network-caching={VLC_CACHING}"] + VLC_EXTRA_ARGS

# MQTT 토픽
TOPIC_AVAILABILITY = "cvlc_tv/availability"
TOPIC_TV_ON = "cvlc_tv/lgtv/on"
TOPIC_TV_OFF = "cvlc_tv/lgtv/off"
TOPIC_TV_STATUS = "cvlc_tv/lgtv/status"
TOPIC_TV_SWITCH = "cvlc_tv/lgtv/switch"
TOPIC_TV_SWITCH_SET = "cvlc_tv/lgtv/switch/set"
TOPIC_PLAY = "cvlc_tv/cvlc/play"
TOPIC_STOP = "cvlc_tv/cvlc/stop"
TOPIC_SOUND_PLAY = "cvlc_tv/sound/play"
TOPIC_SOUND_SAY = "cvlc_tv/sound/say"
TOPIC_SOUND_STOP = "cvlc_tv/sound/stop"

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
    # notify.send_message의 message가 그대로 payload로 전달됨
    ("homeassistant/notify/cvlc_tv_sound/config", {
        "name": "NOTIFY_CVLC_SOUND",
        "unique_id": "CVLC_SOUND",
        "command_topic": TOPIC_SOUND_PLAY,
    }),
    ("homeassistant/notify/cvlc_tv_say/config", {
        "name": "NOTIFY_CVLC_TTS",
        "unique_id": "CVLC_TTS",
        "command_topic": TOPIC_SOUND_SAY,
    }),
    ("homeassistant/button/cvlc_tv_sound_stop/config", {
        "name": "BTN_CVLC_SOUND_STOP",
        "unique_id": "CVLC_SOUND_STOP",
        "command_topic": TOPIC_SOUND_STOP,
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


player = Player(CVLC_ARGS)


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
    client.subscribe([(t, 0) for t in (TOPIC_TV_ON, TOPIC_TV_OFF, TOPIC_TV_SWITCH_SET, TOPIC_PLAY, TOPIC_STOP,
                                       TOPIC_SOUND_PLAY, TOPIC_SOUND_SAY, TOPIC_SOUND_STOP)])
    publish_ha_discovery()
    client.publish(TOPIC_AVAILABILITY, "online", retain=True)
    if STATUS_INTERVAL > 0:
        threading.Thread(target=update_tv_status, daemon=True).start()


def tts_options(data):
    """MQTT/웹 요청에서 TTS 옵션(lang, speed, voice, speaker)만 골라냄"""
    return {key: data.get(key) for key in ("lang", "speed", "voice", "speaker")}


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
    elif topic == TOPIC_SOUND_PLAY:
        name, opts = parse_sound_payload(payload, "file")
        sounds.play(name.strip(), opts.get("volume"))
    elif topic == TOPIC_SOUND_SAY:
        text, opts = parse_sound_payload(payload, "text")
        sounds.say(text, opts.get("volume"), **tts_options(opts))
    elif topic == TOPIC_SOUND_STOP:
        sounds.stop()


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
    return render_template("index.html", config=config, tv_power=tv_power, playing=player.playing,
                           sound_files=get_sound_files(), sound_volume=SOUND_VOLUME,
                           tts_speed=tts.parse_speed(None), tts_voices=tts.PIPER_VOICES,
                           tts_voice=tts.PIPER_MODEL, tts_speaker=tts.PIPER_SPEAKER)


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


@app.route("/sound/play", methods=["POST"])
def sound_play():
    data = request.get_json(silent=True) or {}
    return result(sounds.play(str(data.get("file", "")), data.get("volume")))


@app.route("/sound/say", methods=["POST"])
def sound_say():
    data = request.get_json(silent=True) or {}
    return result(sounds.say(str(data.get("text", "")), data.get("volume"), **tts_options(data)))


@app.route("/tts/test", methods=["POST"])
def tts_test():
    """TTS 테스트: 음성을 바로 만들어 기기 스피커로 재생하거나(target=device) 브라우저로 돌려줌(target=browser)"""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    lang = data.get("lang") or tts.TTS_LANG
    if not text or lang not in tts.TTS_LANGS:
        return jsonify({"status": "error", "message": "문장과 언어(auto/ja/en)를 확인하세요."}), 400
    tmp = tempfile.mkdtemp(prefix="tts-")
    synthesized = tts.synthesize(text, lang, tmp, data.get("speed"), data.get("voice"), data.get("speaker"))
    if not synthesized:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify({"status": "error", "message": "음성을 만들지 못했습니다. 서버 로그를 확인하세요."}), 500
    path, engine, used_lang = synthesized
    voice = tts.resolve_voice(data.get("voice")) if engine == "piper" else "-"
    if data.get("target") == "browser":
        with open(path, "rb") as f:
            audio = f.read()
        shutil.rmtree(tmp, ignore_errors=True)
        response = send_file(io.BytesIO(audio), mimetype="audio/wav")
        response.headers["X-TTS-Engine"] = engine
        response.headers["X-TTS-Lang"] = used_lang
        response.headers["X-TTS-Voice"] = voice
        return response
    sounds.play_temp(path, data.get("volume"))
    return jsonify({"status": "success", "engine": engine, "lang": used_lang, "voice": voice})


@app.route("/sound/stop", methods=["POST"])
def sound_stop():
    sounds.stop()
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
    tts.preload()
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    if STATUS_INTERVAL > 0:
        threading.Thread(target=status_loop, daemon=True).start()
    exit_code = 0
    try:
        app.run(host=WEB_HOST, port=WEB_PORT)
    except SystemExit as e:
        # 주소/포트 오류 등으로 웹 서버를 시작하지 못한 경우 (값에 숨은 문자가 있는지 repr로 표시)
        log.error("Web server could not start (WEB_HOST=%r, WEB_PORT=%r)", WEB_HOST, WEB_PORT)
        exit_code = e.code if isinstance(e.code, int) else 1
    finally:
        stop_event.set()
        player.stop()
        sounds.stop()
        try:
            client.publish(TOPIC_AVAILABILITY, "offline", retain=True).wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            pass
        client.loop_stop()
        client.disconnect()
    # 정리를 마쳤으니 바로 종료한다. 백그라운드에서 TTS 모델(onnxruntime)을 불러오는 중에
    # 일반 종료 절차를 밟으면 "terminate called without an active exception"으로 비정상 종료될 수 있음
    logging.shutdown()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
