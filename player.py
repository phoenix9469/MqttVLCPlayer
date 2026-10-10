"""재생기(mpv 또는 cvlc) 프로세스 실행/종료 (영상 재생과 알림 소리가 함께 사용)"""
import logging
import json
import os
import socket
import subprocess
import tempfile
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

    def play(self, target, extra_args=(), args=None):
        """기존 재생을 종료하고 target(파일 또는 재생목록) 재생. args를 주면 기본 실행 옵션 대신 사용"""
        with self._lock:
            self._stop_locked()
            try:
                self._process = subprocess.Popen(list(args or self._args) + list(extra_args) + [target])
            except OSError as e:
                log.error("Failed to start %s: %s", (args or self._args)[0], e)
                return False
            log.info("Playing %s", target)
            return True

    def stop(self):
        with self._lock:
            self._stop_locked()

    def mpv_request(self, ipc_path, *command, quiet=False):
        """재생 중인 mpv에 IPC 명령 전송 (--input-ipc-server로 실행한 경우). (성공 여부, 결과 값) 반환"""
        if not self.playing:
            return False, None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(ipc_path)
                sock.sendall((json.dumps({"command": list(command)}) + "\n").encode())
                # 응답 앞에 이벤트 줄이 섞여 올 수 있어서 응답(error 키가 있는 줄)을 찾는다
                reader = sock.makefile(encoding="utf-8", errors="replace")
                reply = {}
                for line in reader:
                    reply = json.loads(line)
                    if "error" in reply:
                        break
        except (OSError, ValueError) as e:
            if not quiet:
                log.warning("mpv IPC %s failed: %s", command, e)
            return False, None
        return reply.get("error") == "success", reply.get("data")

    def mpv_command(self, ipc_path, *command):
        """재생 중인 mpv에 IPC 명령 전송, 성공 여부 반환"""
        return self.mpv_request(ipc_path, *command)[0]

    def mpv_get(self, ipc_path, names):
        """재생 중인 mpv의 속성 여러 개를 한 번에 읽음. {이름: 값}, 읽지 못한 속성은 None. 연결 실패 시 None"""
        if not self.playing:
            return None
        values = {name: None for name in names}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(ipc_path)
                requests = "".join(json.dumps({"command": ["get_property", name], "request_id": i}) + "\n"
                                   for i, name in enumerate(names))
                sock.sendall(requests.encode())
                reader = sock.makefile(encoding="utf-8")
                pending = len(names)
                while pending:
                    line = reader.readline()
                    if not line:
                        break
                    reply = json.loads(line)
                    # 이벤트 줄(event)은 건너뛰고 응답만 request_id로 맞춤
                    if "request_id" not in reply or "event" in reply:
                        continue
                    pending -= 1
                    if reply.get("error") == "success":
                        values[names[reply["request_id"]]] = reply.get("data")
        except (OSError, ValueError, IndexError) as e:
            log.warning("mpv IPC get %s failed: %s", names, e)
            return None
        return values

    def wait(self):
        """재생이 끝나거나 stop()될 때까지 대기"""
        process = self._process
        if process:
            process.wait()

    @property
    def playing(self):
        return self._process is not None and self._process.poll() is None
