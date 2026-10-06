import json
import logging
import os
import random
import re
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

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

# 재생 화면 좌측 상단에 표시할 시계 (strftime 형식, 빈 값이면 표시 안 함)
CLOCK_FORMAT = os.environ.get("CLOCK_FORMAT", "%H:%M")
CLOCK_SIZE = int(os.environ.get("CLOCK_SIZE", "0"))  # 글자 크기(px), 0이면 VLC가 화면에 맞춰 자동 결정

# 알림 소리: 재생 가능한 사운드 파일 폴더, 기본 볼륨(0~100), TTS 설정
SOUNDS_FOLDER = os.environ.get("SOUNDS_FOLDER", os.path.join(BASE_DIR, "sounds"))
SOUND_VOLUME = int(os.environ.get("SOUND_VOLUME", "100"))
# 사용할 TTS 엔진을 시도할 순서대로 지정. 언어를 지원하지 않거나 실패하면 다음 엔진 사용
#   piper: piper-plus(로컬, 일본어 등), espeak: espeak-ng(로컬, 음질 낮음), gtts: Google(인터넷 필요)
TTS_ENGINES = [e.strip() for e in os.environ.get("TTS_ENGINES", "piper,espeak").split(",") if e.strip()]
TTS_LANG = os.environ.get("TTS_LANG", "auto")  # auto면 문장의 글자로 판별 (한글→ko, 가나·한자→ja, 그 외→en)
PIPER_MODEL = os.environ.get("PIPER_MODEL", "ja_JP-tsukuyomi-chan-medium")  # 모델 이름 또는 .onnx 경로
PIPER_DATA_DIR = os.environ.get("PIPER_DATA_DIR", os.path.join(BASE_DIR, "piper-models"))

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi")
SOUND_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")
CVLC_ARGS = ["cvlc", "--fullscreen", "--play-and-exit", "--no-osd", "--audio-filter", "normvol"]
if CLOCK_FORMAT:
    # marq 필터: position 5 = 위(4) + 왼쪽(1), 1초마다 갱신
    CVLC_ARGS += ["--sub-source=marq", f"--marq-marquee={CLOCK_FORMAT}", "--marq-position=5",
                  "--marq-x=30", "--marq-y=20", "--marq-refresh=1000"]
    if CLOCK_SIZE > 0:
        CVLC_ARGS.append(f"--marq-size={CLOCK_SIZE}")
SOUND_CVLC_ARGS = ["cvlc", "--play-and-exit", "--no-video"]

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


class Player:
    """cvlc 프로세스를 하나만 유지하는 플레이어"""

    def __init__(self, args):
        self._args = args
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
            log.info("Stopped %s", self._process.args[-1])
        self._process = None

    def play(self, target, extra_args=()):
        """기존 재생을 종료하고 target(파일 또는 재생목록) 재생"""
        with self._lock:
            self._stop_locked()
            try:
                self._process = subprocess.Popen(self._args + list(extra_args) + [target])
            except OSError as e:
                log.error("Failed to start cvlc: %s", e)
                return False
            log.info("Playing %s", target)
            return True

    def stop(self):
        with self._lock:
            self._stop_locked()

    def wait(self):
        """재생이 끝나거나 stop()될 때까지 대기"""
        process = self._process
        if process:
            process.wait()

    @property
    def playing(self):
        return self._process is not None and self._process.poll() is None


player = Player(CVLC_ARGS)


def play_random():
    if create_m3u8_playlist() == 0:
        log.warning("No videos to play in %s", config["nas_folder"])
        return False
    return player.play(PLAYLIST_PATH)


# ---------------------------------------------------------------------------
# 알림 소리 / TTS (영상과 별도의 cvlc로 동시에 재생)
# ---------------------------------------------------------------------------

def get_sound_files():
    try:
        names = os.listdir(SOUNDS_FOLDER)
    except OSError:
        return []
    return sorted(f for f in names if f.lower().endswith(SOUND_EXTENSIONS))


def detect_lang(text):
    """문장에 쓰인 글자로 언어 판별"""
    if re.search(r"[\uac00-\ud7a3\u3131-\u318e]", text):
        return "ko"
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text):
        return "ja"
    return "en"


class PiperTTS:
    """piper-plus 음성 모델을 한 번만 로드해서 재사용"""

    def __init__(self):
        self._lock = threading.Lock()
        self._voice = None
        self._failed_at = None

    def _load(self):
        from piper import PiperVoice

        model, config_path = PIPER_MODEL, None
        if not os.path.exists(model):
            # 모델 이름이면 PIPER_DATA_DIR에서 찾고, 없으면 내려받음 (최초 1회 인터넷 필요)
            from piper.download import ensure_voice_exists, find_voice, get_voices
            os.makedirs(PIPER_DATA_DIR, exist_ok=True)
            voices = get_voices(PIPER_DATA_DIR)
            for info in list(voices.values()):
                for alias in info.get("aliases", []):
                    voices[alias] = {"_is_alias": True, **info}
            ensure_voice_exists(model, [PIPER_DATA_DIR], PIPER_DATA_DIR, voices)
            model, config_path = find_voice(model, [PIPER_DATA_DIR])
        log.info("Loading piper-plus model %s", model)
        return PiperVoice.load(model, config_path=config_path)

    def voice(self):
        """로드된 음성 반환, 사용할 수 없으면 None (실패하면 5분 뒤에 다시 시도)"""
        with self._lock:
            retry = self._failed_at is None or time.monotonic() - self._failed_at > 300
            if self._voice is None and retry:
                try:
                    self._voice = self._load()
                except Exception as e:
                    log.error("piper-plus unavailable: %s", e)
                    self._failed_at = time.monotonic()
            return self._voice

    def languages(self, voice):
        return set(voice.config.language_id_map or {}) or {"ja"}

    def synthesize(self, text, lang, path):
        voice = self.voice()
        if voice is None or lang not in self.languages(voice):
            return False
        with wave.open(path, "wb") as wav_file:
            voice.synthesize(text, wav_file, language_id=(voice.config.language_id_map or {}).get(lang))
        return True


piper_tts = PiperTTS()


def synthesize_gtts(text, lang, path):
    from gtts import gTTS
    gTTS(text, lang=lang, timeout=10).save(path)
    return True


def synthesize_espeak(text, lang, path):
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not espeak:
        return False
    result = subprocess.run([espeak, "-v", lang, "-w", path, text], capture_output=True, timeout=60)
    return result.returncode == 0


# 엔진 이름: (합성 함수, 출력 파일 확장자)
TTS_BACKENDS = {
    "piper": (piper_tts.synthesize, "wav"),
    "gtts": (synthesize_gtts, "mp3"),
    "espeak": (synthesize_espeak, "wav"),
}


def synthesize(text, lang, folder):
    """TTS_ENGINES 순서대로 시도해서 음성 파일 경로 반환 (모두 실패하면 None)"""
    if lang == "auto":
        lang = detect_lang(text)
    for engine in TTS_ENGINES:
        if engine not in TTS_BACKENDS:
            log.warning("Unknown TTS engine: %s", engine)
            continue
        func, ext = TTS_BACKENDS[engine]
        path = os.path.join(folder, f"tts-{engine}.{ext}")
        try:
            if func(text, lang, path):
                log.info("TTS by %s (%s): %s", engine, lang, text)
                return path
        except Exception as e:
            log.warning("TTS engine %s failed: %s", engine, e)
    log.error("No TTS engine could speak %r (lang=%s)", text, lang)
    return None


def parse_volume(volume):
    """0~200 정수로 변환, 지정하지 않았거나 잘못된 값이면 기본 볼륨"""
    try:
        return max(0, min(int(volume), 200))
    except (TypeError, ValueError):
        return SOUND_VOLUME


class SoundQueue:
    """알림 소리를 순서대로 재생 (예: 차임 → 음성 안내)"""

    def __init__(self):
        self._queue = queue.Queue()
        self._player = Player(SOUND_CVLC_ARGS)
        self._generation = 0  # stop() 할 때마다 증가, 이전에 요청된 소리는 재생하지 않음
        threading.Thread(target=self._worker, daemon=True).start()

    def play(self, name, volume=None):
        """사운드 폴더 안의 파일만 재생"""
        if name not in get_sound_files():
            log.warning("Unknown sound file: %r", name)
            return False
        self._queue.put((self._generation, "file", os.path.join(SOUNDS_FOLDER, name), parse_volume(volume)))
        return True

    def say(self, text, lang=None, volume=None):
        text = text.strip()
        if not text:
            return False
        self._queue.put((self._generation, "tts", (text, lang or TTS_LANG), parse_volume(volume)))
        return True

    def stop(self):
        """대기 중인 소리를 버리고 현재 소리 종료"""
        self._generation += 1
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        self._player.stop()

    def _worker(self):
        while True:
            generation, kind, target, volume = self._queue.get()
            try:
                self._play_item(generation, kind, target, volume)
            except Exception:
                log.exception("Sound playback failed")

    def _play_item(self, generation, kind, target, volume):
        extra = [f"--gain={volume / 100:.2f}"]
        with tempfile.TemporaryDirectory() as tmp:
            if kind == "tts":
                text, lang = target
                target = synthesize(text, lang, tmp)
            # 음성 생성 중에 stop()된 경우 재생하지 않음
            if target and generation == self._generation:
                self._player.play(target, extra)
                self._player.wait()


sounds = SoundQueue()


def parse_sound_payload(payload, key):
    """payload: 그냥 문자열 또는 {"<key>": ..., "volume": 80, "lang": "ko"} JSON"""
    if payload.lstrip().startswith("{"):
        try:
            data = json.loads(payload)
            if isinstance(data, dict):
                return str(data.get(key, "")), data
        except ValueError:
            pass
    return payload, {}


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
        sounds.say(text, opts.get("lang"), opts.get("volume"))
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
                           sound_files=get_sound_files())


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
    return result(sounds.say(str(data.get("text", "")), data.get("lang"), data.get("volume")))


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
    if "piper" in TTS_ENGINES:
        # 첫 음성 안내가 늦지 않도록 모델을 미리 로드
        threading.Thread(target=piper_tts.voice, daemon=True).start()
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    if STATUS_INTERVAL > 0:
        threading.Thread(target=status_loop, daemon=True).start()
    try:
        app.run(host=WEB_HOST, port=WEB_PORT)
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


if __name__ == "__main__":
    main()
