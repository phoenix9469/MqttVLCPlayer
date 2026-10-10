"""영상 코덱 정보: ffprobe로 한 번 읽어 두고 파일에 저장한다 (영상 목록에 코덱, 해상도, 하드웨어 디코딩 가능 여부 표시).

하드웨어 디코딩 가능 여부는 vainfo가 알려 주는 VA-API 디코딩 프로필로 판단한다.
"""
import json
import logging
import os
import re
import subprocess
import threading

log = logging.getLogger("mqttvlcplayer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_INFO_CACHE = os.environ.get("MEDIA_INFO_CACHE", os.path.join(BASE_DIR, "media-info.json"))

_lock = threading.Lock()
_cache = None
_dirty = False
_vaapi_profiles = None

CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
               "mpeg2video": "MPEG-2", "vc1": "VC-1", "mpeg4": "MPEG-4", "wmv3": "WMV9"}


def _load():
    global _cache
    if _cache is None:
        try:
            with open(MEDIA_INFO_CACHE) as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    tmp = MEDIA_INFO_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_cache, f, ensure_ascii=False)
    os.replace(tmp, MEDIA_INFO_CACHE)


def vaapi_profiles():
    """VA-API로 디코딩할 수 있는 프로필 이름 집합 (예: VAProfileH264High). vainfo가 없으면 None"""
    global _vaapi_profiles
    if _vaapi_profiles is None:
        try:
            output = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=10).stdout
            _vaapi_profiles = set(re.findall(r"(VAProfile\w+)\s*:\s*VAEntrypointVLD", output))
        except (OSError, subprocess.TimeoutExpired):
            _vaapi_profiles = set()
        log.info("VA-API decode profiles: %s", ", ".join(sorted(_vaapi_profiles)) or "none")
    return _vaapi_profiles or None


def _needed_profiles(codec, profile, bits):
    """코덱/프로필/비트 수에 맞는 VA-API 프로필 후보. 하드웨어로 풀 수 없는 형식이면 빈 목록"""
    profile = (profile or "").lower()
    if codec == "h264":
        if bits > 8 or "high 10" in profile or "4:2:2" in profile or "4:4:4" in profile:
            return []
        if "baseline" in profile:
            return ["VAProfileH264ConstrainedBaseline", "VAProfileH264Main", "VAProfileH264High"]
        return ["VAProfileH264High"] if "high" in profile else ["VAProfileH264Main", "VAProfileH264High"]
    if codec == "hevc":
        return ["VAProfileHEVCMain10"] if bits > 8 else ["VAProfileHEVCMain"]
    if codec == "vp9":
        return ["VAProfileVP9Profile2"] if bits > 8 else ["VAProfileVP9Profile0"]
    if codec == "av1":
        return ["VAProfileAV1Profile0"]
    if codec == "mpeg2video":
        return ["VAProfileMPEG2Main", "VAProfileMPEG2Simple"]
    if codec in ("vc1", "wmv3"):
        return ["VAProfileVC1Advanced", "VAProfileVC1Main", "VAProfileVC1Simple"]
    if codec == "vp8":
        return ["VAProfileVP8Version0_3"]
    return []


def _probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=codec_name,profile,pix_fmt,width,height",
                             "-of", "json", path], capture_output=True, text=True, timeout=30)
    streams = json.loads(result.stdout or "{}").get("streams") or []
    if not streams:
        return None
    s = streams[0]
    pix_fmt = s.get("pix_fmt") or ""
    match = re.search(r"p(9|10|12|14|16)", pix_fmt)
    return {"codec": s.get("codec_name") or "", "profile": s.get("profile") or "",
            "bits": int(match.group(1)) if match else 8,
            "width": s.get("width") or 0, "height": s.get("height") or 0}


def bluray_stream(folder):
    """블루레이 폴더에서 본편일 가능성이 높은 가장 큰 .m2ts 파일. 없으면 None"""
    try:
        bdmv = next(e.path for e in os.scandir(folder) if e.is_dir() and e.name.lower() == "bdmv")
        stream = next(e.path for e in os.scandir(bdmv) if e.is_dir() and e.name.lower() == "stream")
        files = [e for e in os.scandir(stream) if e.is_file() and e.name.lower().endswith(".m2ts")]
    except (OSError, StopIteration):
        return None
    return max(files, key=lambda e: e.stat().st_size).path if files else None


def info(path):
    """영상 코덱 정보 (캐시, 파일 크기와 수정 시각이 같으면 다시 읽지 않음). 읽지 못하면 None"""
    global _dirty
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = {"size": st.st_size, "mtime": int(st.st_mtime)}
    with _lock:
        entry = _load().get(path)
    if entry and {k: entry.get(k) for k in key} == key:
        data = entry.get("info")
    else:
        try:
            data = _probe(path)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            log.warning("ffprobe failed for %s: %s", path, e)
            data = None
        with _lock:
            _load()[path] = {**key, "info": data}
            _dirty = True
    return describe(data) if data else None


def flush():
    """새로 읽은 정보가 있으면 파일에 저장 (여러 개를 읽은 뒤 한 번만 호출)"""
    global _dirty
    with _lock:
        if not _dirty:
            return
        try:
            _save()
            _dirty = False
        except OSError as e:
            log.warning("Cannot save %s: %s", MEDIA_INFO_CACHE, e)


def describe(data):
    """화면에 보일 정보: {"label": "H.264 10bit · 1080p", "hw": True/False/None}"""
    codec = CODEC_NAMES.get(data["codec"], data["codec"].upper())
    parts = [codec + (f" {data['bits']}bit" if data["bits"] > 8 else "")]
    if data["height"]:
        parts.append(f"{data['height']}p" if data["height"] in (480, 576, 720, 1080, 1440, 2160)
                     else f"{data['width']}×{data['height']}")
    profiles = vaapi_profiles()
    hw = None if profiles is None else any(p in profiles for p in _needed_profiles(
        data["codec"], data["profile"], data["bits"]))
    return {"label": " · ".join(parts), "hw": hw}
