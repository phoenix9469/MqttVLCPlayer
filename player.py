"""재생기(mpv 또는 cvlc) 프로세스 실행/종료 (영상 재생과 알림 소리가 함께 사용)"""
import logging
import os
import subprocess
import threading

log = logging.getLogger("mqttvlcplayer")

# 사용할 재생기: mpv(기본, 하드웨어 디코딩이 잘 됨) 또는 vlc
PLAYER_BACKEND = os.environ.get("VIDEO_PLAYER", "mpv").strip().lower()
if PLAYER_BACKEND not in ("mpv", "vlc"):
    log.warning("Unknown VIDEO_PLAYER %r, using mpv", PLAYER_BACKEND)
    PLAYER_BACKEND = "mpv"


class Player:
    """재생기 프로세스를 하나만 유지하는 플레이어"""

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
                log.error("Failed to start %s: %s", self._args[0], e)
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
