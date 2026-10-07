"""알림 소리: 사운드 파일과 TTS 음성을 요청 순서대로 재생 (영상과 별도의 재생기 프로세스로 동시에 재생)"""
import json
import logging
import os
import queue
import shutil
import tempfile
import threading

import tts
from player import PLAYER_BACKEND, Player

log = logging.getLogger("mqttvlcplayer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 재생 가능한 사운드 파일 폴더, 기본 볼륨(0~200, 100이 원래 크기)
SOUNDS_FOLDER = os.environ.get("SOUNDS_FOLDER", os.path.join(BASE_DIR, "sounds"))
SOUND_VOLUME = int(os.environ.get("SOUND_VOLUME", "100"))
SOUND_EXTENSIONS = (".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac")
if PLAYER_BACKEND == "mpv":
    SOUND_PLAYER_ARGS = ["mpv", "--no-video", "--no-terminal", "--volume-max=200"]
else:
    SOUND_PLAYER_ARGS = ["cvlc", "--play-and-exit", "--no-video"]


def volume_args(volume):
    """볼륨(0~200, 100이 원래 크기)을 재생기 옵션으로 변환"""
    if PLAYER_BACKEND == "mpv":
        return [f"--volume={volume}"]
    return [f"--gain={volume / 100:.2f}"]


def get_sound_files():
    try:
        names = os.listdir(SOUNDS_FOLDER)
    except OSError as e:
        log.warning("Cannot read SOUNDS_FOLDER %r: %s", SOUNDS_FOLDER, e)
        return []
    return sorted(f for f in names if f.lower().endswith(SOUND_EXTENSIONS))


# 알림 기본 볼륨. 웹 UI에서 바꿀 수 있다 (set_default_volume)
default_volume = SOUND_VOLUME


def clamp_volume(volume, fallback):
    """0~200 정수로 변환, 지정하지 않았거나 잘못된 값이면 fallback"""
    try:
        return max(0, min(int(volume), 200))
    except (TypeError, ValueError):
        return fallback


def parse_volume(volume):
    """볼륨을 지정하지 않았거나 잘못된 값이면 알림 기본 볼륨"""
    return clamp_volume(volume, default_volume)


def set_default_volume(volume):
    global default_volume
    default_volume = clamp_volume(volume, default_volume)
    return default_volume


class SoundQueue:
    """알림 소리를 순서대로 재생 (예: 차임 → 음성 안내)"""

    def __init__(self):
        self._queue = queue.Queue()
        self._player = Player(SOUND_PLAYER_ARGS)
        self._generation = 0  # stop() 할 때마다 증가, 이전에 요청된 소리는 재생하지 않음
        threading.Thread(target=self._worker, daemon=True).start()

    def play(self, name, volume=None):
        """사운드 폴더 안의 파일만 재생"""
        if name not in get_sound_files():
            log.warning("Unknown sound file: %r", name)
            return False
        self._queue.put((self._generation, "file", os.path.join(SOUNDS_FOLDER, name), parse_volume(volume)))
        return True

    def say(self, text, volume=None, lang=None, speed=None, voice=None, speaker=None):
        """문장을 음성으로 읽기. voice/speaker는 piper-plus 목소리와 화자 번호"""
        text = text.strip()
        if not text:
            return False
        options = {"speed": speed, "voice": voice, "speaker": speaker}
        self._queue.put((self._generation, "tts", (text, lang or tts.TTS_LANG, options), parse_volume(volume)))
        return True

    def play_temp(self, path, volume=None):
        """이미 만들어 둔 음성 파일을 재생하고 삭제"""
        self._queue.put((self._generation, "temp", path, parse_volume(volume)))

    def stop(self):
        """대기 중인 소리를 버리고 현재 소리 종료"""
        self._generation += 1
        drained = []
        try:
            while True:
                drained.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        self._player.stop()
        for _, kind, target, _ in drained:
            if kind == "temp":
                shutil.rmtree(os.path.dirname(target), ignore_errors=True)

    def _worker(self):
        while True:
            generation, kind, target, volume = self._queue.get()
            try:
                self._play_item(generation, kind, target, volume)
            except Exception:
                log.exception("Sound playback failed")

    def _play_item(self, generation, kind, target, volume):
        extra = volume_args(volume)
        with tempfile.TemporaryDirectory() as tmp:
            if kind == "tts":
                text, lang, options = target
                result = tts.synthesize(text, lang, tmp, **options)
                target = result[0] if result else None
            try:
                # 음성 생성 중에 stop()된 경우 재생하지 않음
                if target and generation == self._generation:
                    self._player.play(target, extra)
                    self._player.wait()
            finally:
                if kind == "temp":
                    shutil.rmtree(os.path.dirname(target), ignore_errors=True)


sounds = SoundQueue()


def parse_sound_payload(payload, key):
    """payload: 그냥 문자열 또는 {"<key>": ..., "volume": 80, "lang": "ja"} JSON"""
    if payload.lstrip().startswith("{"):
        try:
            data = json.loads(payload)
            if isinstance(data, dict):
                return str(data.get(key, "")), data
        except ValueError:
            pass
    return payload, {}
