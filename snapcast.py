"""Snapcast 출력: 영상·알림 소리를 PipeWire/PulseAudio의 "snapcast" 싱크(module-pipe-sink)로 보내서
snapserver가 여러 스피커(snapclient)에 동시에 재생하게 한다.

  mpv ─► 싱크 "snapcast" ─► FIFO(/tmp/snapfifo, s16le 48000Hz 2ch) ─► snapserver ─► snapclient들 (TV, NUGU Candle 등)

영상은 화면에 바로 나오고 소리는 snapserver 버퍼만큼 늦게 나오므로, mpv --audio-delay를 음수로 줘서
소리를 먼저 내보내 립싱크를 맞춘다. 알림 소리는 화면과 상관없으므로 지연 보정을 하지 않는다.
"""
import array
import fcntl
import json
import logging
import os
import shutil
import stat
import subprocess
import termios
import threading
import time
import urllib.request

log = logging.getLogger("mqttvlcplayer")

# 처음 값 (웹 UI·HA에서 바꾸면 config.json 값이 우선)
SNAPCAST = os.environ.get("SNAPCAST", "0").strip().lower() in ("1", "true", "yes", "on")
SNAPCAST_AUDIO_DELAY_MS = int(os.environ.get("SNAPCAST_AUDIO_DELAY_MS", "-450"))
# 출력할 싱크 이름과, 싱크가 없을 때 앱이 만들 때 쓸 FIFO (snapserver.conf의 pipe:// 경로와 같아야 함)
SNAPCAST_SINK = os.environ.get("SNAPCAST_SINK", "snapcast").strip() or "snapcast"
SNAPCAST_FIFO = os.environ.get("SNAPCAST_FIFO", "/tmp/snapfifo").strip() or "/tmp/snapfifo"
# snapserver JSON-RPC (스피커별 음량)
SNAPCAST_RPC_URL = os.environ.get("SNAPCAST_RPC_URL", "http://127.0.0.1:1780/jsonrpc").strip()
# FIFO에 밀려 있어도 되는 소리 양(ms). 넘으면 넘친 만큼 버림 (아래 BacklogGuard)
SNAPCAST_FIFO_MAX_MS = int(os.environ.get("SNAPCAST_FIFO_MAX_MS", "60"))
# snapserver.conf의 sampleformat과 같아야 함 (48000:16:2)
SAMPLE_FORMAT = "s16le"
SAMPLE_RATE = 48000
CHANNELS = 2

AUDIO_DELAY_RANGE = (-3000, 1000)  # ms
# 싱크가 쉬지 않고 늘 돌게 함 (PipeWire): 재생하지 않을 때도 FIFO에 무음이 계속 들어가서
# snapserver 스트림이 idle↔playing을 오가지 않음. 오가면 재생을 시작할 때마다 클라이언트가 동기를 새로 맞추느라 끊김
SINK_PROPERTIES = ("device.description=Snapcast session.suspend-timeout-seconds=0 "
                   "node.always-process=true node.pause-on-idle=false")

_lock = threading.Lock()


def clamp_delay(value, fallback):
    try:
        return max(AUDIO_DELAY_RANGE[0], min(AUDIO_DELAY_RANGE[1], int(round(float(value)))))
    except (TypeError, ValueError):
        return fallback


def device():
    """mpv --audio-device 값 (pipewire-pulse도 pulse 출력으로 같은 싱크 이름을 씀)"""
    return f"pulse/{SNAPCAST_SINK}"


def _pactl(*args):
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10)


def sink_exists():
    try:
        result = _pactl("list", "short", "sinks")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split("\t")[1:2] == [SNAPCAST_SINK] for line in result.stdout.splitlines())


def ensure_sink():
    """싱크가 없으면 만든다. 성공(이미 있음 포함) 여부 반환.
    FIFO는 snapserver가 만들어 둔 것을 쓴다 (없으면 snapserver가 아직 안 떠 있는 것이므로 만들지 않음)"""
    with _lock:
        ok = _ensure_sink()
    if ok:
        _guard.start()
    return ok


def _ensure_sink():
    if not shutil.which("pactl"):
        log.warning("Snapcast: pactl not found (sudo apt install pulseaudio-utils)")
        return False
    if sink_exists():
        return True
    try:
        is_fifo = stat.S_ISFIFO(os.stat(SNAPCAST_FIFO).st_mode)
    except OSError:
        is_fifo = False
    if not is_fifo:
        log.warning("Snapcast: %s is not a FIFO yet. Start snapserver first "
                    "(source = pipe://%s in /etc/snapserver.conf)", SNAPCAST_FIFO, SNAPCAST_FIFO)
        return False
    if not os.access(SNAPCAST_FIFO, os.W_OK):
        log.warning("Snapcast: no write permission on %s (see README: Snapcast)", SNAPCAST_FIFO)
        return False
    try:
        result = _pactl("load-module", "module-pipe-sink", f"file={SNAPCAST_FIFO}",
                        f"sink_name={SNAPCAST_SINK}", f"format={SAMPLE_FORMAT}",
                        f"rate={SAMPLE_RATE}", f"channels={CHANNELS}",
                        f'sink_properties="{SINK_PROPERTIES}"')
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Snapcast: cannot create sink: %s", e)
        return False
    if result.returncode != 0:
        log.warning("Snapcast: cannot create sink %s: %s", SNAPCAST_SINK, result.stderr.strip())
        return False
    log.info("Snapcast: created sink %s -> %s", SNAPCAST_SINK, SNAPCAST_FIFO)
    _warn_if_default()
    return True


class BacklogGuard:
    """FIFO에 밀린 소리를 일정 양 이하로 유지.

    snapserver는 FIFO를 실시간 속도로만 읽고 밀린 것을 따라잡지 않는다. 그래서 snapserver나 PipeWire가
    잠깐 멈칫한 사이 FIFO에 쌓인 소리만큼 그 뒤로 계속 늦게 나오고(립싱크가 재생마다 달라짐),
    가득 차면 PipeWire가 소리를 버려서 끊긴다. 밀린 양이 SNAPCAST_FIFO_MAX_MS를 넘으면 넘친 만큼 읽어서 버린다
    (한 번 짧게 건너뛰고 지연은 다시 일정해짐)."""

    INTERVAL = 0.5
    FRAME = 4  # s16le 2ch 한 프레임 바이트

    def __init__(self):
        self._thread = None
        self._fd = None

    def start(self):
        if SNAPCAST_FIFO_MAX_MS <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _open(self):
        if self._fd is None:
            # 읽기 쪽으로 하나 더 열어 둠 (막히지 않게 O_NONBLOCK, 평소에는 읽지 않음)
            self._fd = os.open(SNAPCAST_FIFO, os.O_RDONLY | os.O_NONBLOCK)
        return self._fd

    def _close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _run(self):
        bytes_per_ms = SAMPLE_RATE * self.FRAME // 1000
        limit = SNAPCAST_FIFO_MAX_MS * bytes_per_ms
        keep = limit // 2 // self.FRAME * self.FRAME  # 버릴 때는 절반까지 줄임
        while True:
            time.sleep(self.INTERVAL)
            try:
                fd = self._open()
                if os.fstat(fd).st_ino != os.stat(SNAPCAST_FIFO).st_ino:
                    raise OSError("FIFO was recreated")
                pending = array.array("i", [0])
                fcntl.ioctl(fd, termios.FIONREAD, pending)
                excess = (pending[0] - keep) // self.FRAME * self.FRAME
                if pending[0] > limit and excess > 0:
                    dropped = len(os.read(fd, excess))
                    log.info("Snapcast: FIFO backlog %dms, dropped %dms to keep latency steady",
                             pending[0] // bytes_per_ms, dropped // bytes_per_ms)
            except OSError as e:
                # snapserver가 FIFO를 다시 만들었거나 아직 없음: 다음에 다시 열기
                if self._fd is not None:
                    log.debug("Snapcast: FIFO guard reopening: %s", e)
                self._close()


_guard = BacklogGuard()


def _warn_if_default():
    """snapcast 싱크가 기본 출력이 되면, 기본 출력으로 내는 로컬 snapclient 소리가 다시 snapcast로 돌아가 되먹임됨"""
    try:
        default = _pactl("get-default-sink").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return
    if default == SNAPCAST_SINK:
        log.warning("Snapcast: sink %s became the default output. Set the TV output as default "
                    "(pactl set-default-sink <name>) or the local snapclient will feed back into Snapcast",
                    SNAPCAST_SINK)


def mpv_args(enabled, delay_ms, video=True):
    """Snapcast로 낼 때 mpv 인자. video: 영상이면 립싱크용 --audio-delay도"""
    if not enabled:
        return []
    args = [f"--audio-device={device()}"]
    if video and delay_ms:
        args.append(f"--audio-delay={delay_ms / 1000:.3f}")
    return args


class RpcError(Exception):
    pass


def rpc(method, params=None):
    """snapserver JSON-RPC 호출, result 반환"""
    body = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params or {}}).encode()
    request = urllib.request.Request(SNAPCAST_RPC_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            reply = json.load(response)
    except (OSError, ValueError) as e:
        raise RpcError(f"snapserver에 연결할 수 없습니다 ({SNAPCAST_RPC_URL}): {e}") from e
    if "error" in reply:
        raise RpcError(reply["error"].get("message", str(reply["error"])))
    return reply.get("result")


def clients():
    """snapserver에 등록된 스피커(클라이언트) 목록. 연결된 것 먼저"""
    server = rpc("Server.GetStatus")["server"]
    result = []
    for group in server.get("groups", []):
        for client in group.get("clients", []):
            config = client.get("config", {})
            volume = config.get("volume", {})
            result.append({
                "id": client["id"],
                # 이름을 따로 안 정했으면 hostID (tv, candle 등)
                "name": config.get("name") or client["id"],
                "host": client.get("host", {}).get("name", ""),
                "connected": bool(client.get("connected")),
                "percent": volume.get("percent", 100),
                "muted": bool(volume.get("muted")),
                "latency": config.get("latency", 0),
                "version": client.get("snapclient", {}).get("version", ""),
            })
    result.sort(key=lambda c: (not c["connected"], c["name"]))
    return result


def set_client_volume(client_id, percent=None, muted=None):
    """스피커 음량(0~100)과 음소거. 지정한 것만 바꿈"""
    current = next((c for c in clients() if c["id"] == client_id), None)
    if current is None:
        raise RpcError(f"스피커를 찾을 수 없습니다: {client_id}")
    if percent is not None:
        percent = max(0, min(100, int(percent)))
    volume = {"percent": current["percent"] if percent is None else percent,
              "muted": current["muted"] if muted is None else bool(muted)}
    return rpc("Client.SetVolume", {"id": client_id, "volume": volume})
