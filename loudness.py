"""영상별 음량 맞추기: 영상마다 평균 음량(LUFS)을 한 번 측정해 두고, 재생할 때 영상 전체에 같은 보정값을 적용한다.

dynaudnorm처럼 재생 중에 음량을 계속 바꾸지 않으므로 음악의 강약은 그대로 유지되고,
영상끼리의 음량 차이만 줄어든다. 측정은 백그라운드에서 ffmpeg로 하고 결과는 파일에 저장한다.
"""
import json
import logging
import math
import os
import re
import subprocess
import threading

log = logging.getLogger("mqttvlcplayer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 맞출 목표 음량(LUFS), 0이면 기능 끔. 보정은 -MAX_CUT ~ +MAX_BOOST dB로 제한
LOUDNESS_TARGET = float(os.environ.get("LOUDNESS_TARGET", "-18"))
LOUDNESS_MAX_BOOST = float(os.environ.get("LOUDNESS_MAX_BOOST", "10"))
LOUDNESS_MAX_CUT = float(os.environ.get("LOUDNESS_MAX_CUT", "20"))
# 측정 결과 저장 파일, mpv 스크립트가 읽는 보정값 파일
LOUDNESS_CACHE = os.environ.get("LOUDNESS_CACHE", os.path.join(BASE_DIR, "loudness.json"))
LOUDNESS_GAINS = os.environ.get("LOUDNESS_GAINS", os.path.join(BASE_DIR, "loudness-gains.json"))

# 긴 영상은 전체를 읽지 않고 여러 구간만 측정 (NAS에서 큰 파일을 통째로 읽지 않도록)
SAMPLE_COUNT = 5
SAMPLE_SECONDS = 20

enabled = LOUDNESS_TARGET < 0

_lock = threading.Lock()
_cache = {}
_pending = []
_wakeup = threading.Event()
_worker = None


def _load():
    global _cache
    try:
        with open(LOUDNESS_CACHE) as f:
            _cache = json.load(f)
    except (OSError, ValueError):
        _cache = {}


def _file_key(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def _is_measured(path):
    entry = _cache.get(path)
    try:
        return entry is not None and {k: entry.get(k) for k in ("size", "mtime")} == _file_key(path)
    except OSError:
        return True  # 파일이 없으면 측정하지 않음


def gain_db(lufs):
    return max(-LOUDNESS_MAX_CUT, min(LOUDNESS_TARGET - lufs, LOUDNESS_MAX_BOOST))


def _save():
    """측정 결과와 mpv용 보정값(dB) 파일 저장"""
    gains = {path: round(gain_db(e["lufs"]), 2) for path, e in _cache.items() if e.get("lufs") is not None}
    for target, data in ((LOUDNESS_CACHE, _cache), (LOUDNESS_GAINS, gains)):
        tmp = target + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, target)


def _duration(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=60)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _measure_segment(path, start=None, length=None):
    """구간의 평균 음량(LUFS), 소리가 없거나 실패하면 None"""
    cmd = ["nice", "-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-nostats"]
    if start is not None:
        cmd += ["-ss", f"{start:.1f}", "-t", str(length)]
    cmd += ["-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", "ebur128=framelog=quiet", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    matches = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", result.stderr)
    if not matches or matches[-1] == "-inf" or float(matches[-1]) <= -70:
        return None
    return float(matches[-1])


def measure(path):
    """영상의 평균 음량(LUFS). 긴 영상은 고르게 나눈 여러 구간의 음량을 에너지 평균"""
    duration = _duration(path)
    if duration <= SAMPLE_COUNT * SAMPLE_SECONDS * 2:
        return _measure_segment(path)
    values = []
    for i in range(SAMPLE_COUNT):
        start = duration * (i + 0.5) / SAMPLE_COUNT - SAMPLE_SECONDS / 2
        value = _measure_segment(path, start, SAMPLE_SECONDS)
        if value is not None:
            values.append(value)
    if not values:
        return None
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


def _run():
    while True:
        _wakeup.wait()
        while True:
            with _lock:
                if not _pending:
                    _wakeup.clear()
                    break
                path = _pending.pop(0)
            if _is_measured(path):
                continue
            try:
                key = _file_key(path)
                lufs = measure(path)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("Loudness measurement failed for %s: %s", path, e)
                continue
            log.info("Loudness %s: %s LUFS", os.path.basename(path),
                     f"{lufs:.1f}" if lufs is not None else "no audio")
            with _lock:
                _cache[path] = dict(key, lufs=lufs)
                try:
                    _save()
                except OSError as e:
                    log.warning("Cannot save loudness data: %s", e)


def scan(paths):
    """아직 측정하지 않은 영상을 백그라운드에서 측정"""
    global _worker
    if not enabled:
        return
    with _lock:
        if _worker is None:
            _load()
            try:
                _save()  # 보정값 파일을 바로 만들어 mpv가 읽을 수 있게
            except OSError as e:
                log.warning("Cannot save loudness data: %s", e)
            _worker = threading.Thread(target=_run, daemon=True)
            _worker.start()
        new = [p for p in paths if p not in _pending and not _is_measured(p)]
        _pending.extend(new)
    if new:
        log.info("Measuring loudness of %d videos in the background", len(new))
        _wakeup.set()
