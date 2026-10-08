"""유튜브 등 영상 다운로드 (yt-dlp): 웹 UI에서 링크를 받아 순서대로 받고 진행 상황을 보여 준다"""
import itertools
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("mqttvlcplayer")

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # yt-dlp가 없어도 앱의 다른 기능은 동작하도록
    yt_dlp = None
    DownloadCancelled = Exception

# 웹 UI에서 바꿀 수 있는 다운로드 설정 (config.json의 "ytdlp"에 저장)
DEFAULT_SETTINGS = {
    "folder": "",             # 비우면 영상(NAS) 폴더
    "quality": "1080",        # best, 2160, 1440, 1080, 720, 480, audio
    "prefer_h264": True,      # 구형 GPU가 하드웨어 디코딩할 수 있는 H.264를 우선
    "container": "mp4",       # mp4, mkv
    "filename": "%(title)s [%(id)s].%(ext)s",
    "playlist": False,        # 재생목록 링크면 전체를 받을지
    "rate_limit": "",         # 예: 5M (초당 5MB), 비우면 제한 없음
    "embed_metadata": True,   # 제목·설명 등을 파일에 기록
    "embed_thumbnail": False,
    "cookies": "",            # 로그인/연령 제한 영상용 cookies.txt 경로
    "extra": "",              # yt-dlp 파이썬 옵션(JSON), 예: {"concurrent_fragment_downloads": 4}
}
QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "audio")
CONTAINERS = ("mp4", "mkv")
MAX_FINISHED_JOBS = 30


def normalize_settings(data, current=None):
    """웹에서 받은 값을 검사해서 설정 dict로 만든다. 잘못된 값이면 ValueError"""
    settings = dict(DEFAULT_SETTINGS)
    settings.update(current or {})
    for key, default in DEFAULT_SETTINGS.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(default, bool):
            settings[key] = bool(value)
        else:
            settings[key] = str(value).strip()
    if settings["quality"] not in QUALITIES:
        raise ValueError(f"알 수 없는 화질: {settings['quality']}")
    if settings["container"] not in CONTAINERS:
        raise ValueError(f"알 수 없는 형식: {settings['container']}")
    if not settings["filename"]:
        settings["filename"] = DEFAULT_SETTINGS["filename"]
    if settings["folder"] and not os.path.isdir(settings["folder"]):
        raise ValueError(f"폴더를 찾을 수 없습니다: {settings['folder']}")
    if settings["cookies"] and not os.path.isfile(settings["cookies"]):
        raise ValueError(f"쿠키 파일을 찾을 수 없습니다: {settings['cookies']}")
    if settings["extra"]:
        try:
            extra = json.loads(settings["extra"])
        except ValueError as e:
            raise ValueError(f"추가 옵션 JSON 오류: {e}") from None
        if not isinstance(extra, dict):
            raise ValueError("추가 옵션은 JSON 객체({...})여야 합니다")
    return settings


def parse_rate(text):
    """'5M', '500K', '1.5M' → 초당 바이트, 비었으면 None"""
    text = text.strip().upper().rstrip("B")
    if not text:
        return None
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    factor = units.get(text[-1], 1)
    number = text[:-1] if text[-1] in units else text
    try:
        return int(float(number) * factor)
    except ValueError:
        raise ValueError(f"속도 제한 형식 오류: {text} (예: 5M, 500K)") from None


def format_selector(quality, prefer_h264):
    if quality == "audio":
        return "bestaudio/best"
    limit = "" if quality == "best" else f"[height<={quality}]"
    choices = []
    if prefer_h264:
        # avc1 = H.264. 없으면 아래의 일반 선택으로 넘어감
        choices += [f"bv*{limit}[vcodec^=avc1]+ba[ext=m4a]", f"bv*{limit}[vcodec^=avc1]+ba", f"b{limit}[vcodec^=avc1]"]
    choices += [f"bv*{limit}+ba", f"b{limit}", "bv*+ba/b"]
    return "/".join(choices)


def build_options(settings, folder, hook):
    opts = {
        # 받는 중인 조각 파일은 숨김 폴더에 두었다가 끝나면 옮긴다 (랜덤 재생에 반쯤 받은 파일이 섞이지 않게)
        "paths": {"home": folder, "temp": os.path.join(folder, ".ytdlp-tmp")},
        "outtmpl": settings["filename"],
        "format": format_selector(settings["quality"], settings["prefer_h264"]),
        "noplaylist": not settings["playlist"],
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "restrictfilenames": False,
        "windowsfilenames": True,  # NAS(SMB/NFS)에서 문제 되는 문자 피하기
        "postprocessors": [],
    }
    if settings["quality"] == "audio":
        opts["postprocessors"].append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})
    else:
        opts["merge_output_format"] = settings["container"]
    if settings["embed_metadata"]:
        opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
    if settings["embed_thumbnail"]:
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail"})
    rate = parse_rate(settings["rate_limit"])
    if rate:
        opts["ratelimit"] = rate
    if settings["cookies"]:
        opts["cookiefile"] = settings["cookies"]
    if settings["extra"]:
        opts.update(json.loads(settings["extra"]))
    return opts


class Downloader:
    """다운로드 요청을 하나씩 순서대로 처리"""

    def __init__(self, get_settings, get_default_folder, on_finished=None):
        self._get_settings = get_settings
        self._get_default_folder = get_default_folder
        self._on_finished = on_finished
        self._queue = queue.Queue()
        self._jobs = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    @property
    def available(self):
        return yt_dlp is not None

    def add(self, url, quality=None):
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("http:// 또는 https:// 로 시작하는 링크를 입력하세요")
        if quality and quality not in QUALITIES:
            raise ValueError(f"알 수 없는 화질: {quality}")
        job = {"id": next(self._ids), "url": url, "quality": quality, "status": "대기", "title": "",
               "progress": 0.0, "speed": "", "eta": "", "file": "", "error": "", "cancel": False,
               "added": time.time()}
        with self._lock:
            self._jobs[job["id"]] = job
            self._trim()
        self._queue.put(job["id"])
        return job["id"]

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] in ("완료", "실패", "취소"):
                return False
            job["cancel"] = True
            if job["status"] == "대기":
                job["status"] = "취소"
        return True

    def jobs(self):
        with self._lock:
            return [{k: v for k, v in job.items() if k != "cancel"}
                    for job in sorted(self._jobs.values(), key=lambda j: -j["id"])]

    def _trim(self):
        finished = [j for j in self._jobs.values() if j["status"] in ("완료", "실패", "취소")]
        for job in sorted(finished, key=lambda j: j["id"])[:-MAX_FINISHED_JOBS or None]:
            del self._jobs[job["id"]]

    def _worker(self):
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
                if not job or job["cancel"]:
                    continue
                job["status"] = "준비 중"
            try:
                self._download(job)
            except Exception:
                log.exception("Download failed: %s", job["url"])
                job["status"], job["error"] = "실패", "알 수 없는 오류 (앱 로그 확인)"

    def _download(self, job):
        if yt_dlp is None:
            job["status"], job["error"] = "실패", "yt-dlp가 설치되어 있지 않습니다 (pip install yt-dlp)"
            return
        settings = dict(self._get_settings())
        if job["quality"]:
            settings["quality"] = job["quality"]
        folder = settings["folder"] or self._get_default_folder()

        def hook(d):
            if job["cancel"]:
                raise DownloadCancelled()
            info = d.get("info_dict") or {}
            job["title"] = info.get("title") or job["title"]
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job["progress"] = round(d.get("downloaded_bytes", 0) * 100 / total, 1)
                job["status"] = "다운로드 중"
                job["speed"] = (d.get("_speed_str") or "").strip()
                job["eta"] = (d.get("_eta_str") or "").strip()
            elif d["status"] == "finished":
                job["status"] = "변환 중"  # 영상+음성 합치기, 메타데이터 기록
                job["progress"] = 100.0

        log.info("Download started: %s -> %s", job["url"], folder)
        options = build_options(settings, folder, hook)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(job["url"], download=True)
        except DownloadCancelled:
            job["status"] = "취소"
            log.info("Download cancelled: %s", job["url"])
            return
        except yt_dlp.utils.DownloadError as e:
            job["status"], job["error"] = "실패", str(e).replace("ERROR: ", "")[:300]
            log.warning("Download failed: %s: %s", job["url"], job["error"])
            return
        finally:
            # 취소·실패하면 조각 파일이 남으므로 임시 폴더를 지운다 (다운로드는 한 번에 하나라 안전)
            shutil.rmtree(options["paths"]["temp"], ignore_errors=True)
        entries = info.get("entries") if info else None
        files = []
        for entry in (entries or [info]):
            if entry:
                for item in entry.get("requested_downloads") or []:
                    files.append(os.path.basename(item.get("filepath") or ""))
        job["title"] = (info or {}).get("title") or job["title"]
        job["file"] = ", ".join(f for f in files if f)
        job["status"] = "완료"
        log.info("Download finished: %s", job["file"] or job["url"])
        if self._on_finished:
            self._on_finished()


def update_ytdlp():
    """yt-dlp 최신 버전으로 업데이트 (사이트가 바뀌어 다운로드가 실패할 때). 결과 메시지 반환"""
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                            capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[-500:]
    return True, "업데이트했습니다. 적용하려면 앱을 재시작하세요."


def version():
    return yt_dlp.version.__version__ if yt_dlp else None
