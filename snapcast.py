"""Snapcast 출력: 영상·알림 소리를 PipeWire/PulseAudio의 "snapcast" 싱크(module-pipe-sink)로 보내서
snapserver가 여러 스피커(snapclient)에 동시에 재생하게 한다.

  mpv ─► 싱크 "snapcast" ─► FIFO(/tmp/snapfifo, s16le 48000Hz 2ch) ─► snapserver ─► snapclient들 (TV, NUGU Candle 등)

영상은 화면에 바로 나오고 소리는 snapserver 버퍼만큼 늦게 나오므로, mpv --audio-delay를 음수로 줘서
소리를 먼저 내보내 립싱크를 맞춘다. 알림 소리는 화면과 상관없으므로 지연 보정을 하지 않는다.
"""
import logging
import os
import shutil
import stat
import subprocess
import threading

log = logging.getLogger("mqttvlcplayer")

# 처음 값 (웹 UI·HA에서 바꾸면 config.json 값이 우선)
SNAPCAST = os.environ.get("SNAPCAST", "0").strip().lower() in ("1", "true", "yes", "on")
SNAPCAST_AUDIO_DELAY_MS = int(os.environ.get("SNAPCAST_AUDIO_DELAY_MS", "-450"))
# 출력할 싱크 이름과, 싱크가 없을 때 앱이 만들 때 쓸 FIFO (snapserver.conf의 pipe:// 경로와 같아야 함)
SNAPCAST_SINK = os.environ.get("SNAPCAST_SINK", "snapcast").strip() or "snapcast"
SNAPCAST_FIFO = os.environ.get("SNAPCAST_FIFO", "/tmp/snapfifo").strip() or "/tmp/snapfifo"
# snapserver.conf의 sampleformat과 같아야 함 (48000:16:2)
SAMPLE_FORMAT = "s16le"
SAMPLE_RATE = 48000
CHANNELS = 2

AUDIO_DELAY_RANGE = (-3000, 1000)  # ms

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
                            "sink_properties=device.description=Snapcast")
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Snapcast: cannot create sink: %s", e)
            return False
        if result.returncode != 0:
            log.warning("Snapcast: cannot create sink %s: %s", SNAPCAST_SINK, result.stderr.strip())
            return False
        log.info("Snapcast: created sink %s -> %s", SNAPCAST_SINK, SNAPCAST_FIFO)
        _warn_if_default()
        return True


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
