"""유튜브 등 링크 재생과 라이브 녹화(아카이브).

재생은 mpv가 내장 ytdl_hook으로 yt-dlp를 불러 직접 하고, 여기서는
- 링크 정보 미리 확인 (라이브 여부, 제목, 채널, 시청자 수)
- mpv에 넘길 화질·yt-dlp 옵션
- 라이브 녹화: yt-dlp를 별도 프로세스로 실행해서 방송이 끝나거나 멈출 때까지 저장
을 맡는다.
"""
import itertools
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import downloader

try:
    import yt_dlp
except ImportError:  # yt-dlp가 없으면 링크 재생/녹화 불가
    yt_dlp = None

log = logging.getLogger("mqttvlcplayer")

# 녹화 파일을 둘 폴더 (비우면 수동 재생 폴더 안의 "라이브 아카이브")
ARCHIVE_FOLDER = os.environ.get("ARCHIVE_FOLDER", "")
MAX_FINISHED_ARCHIVES = 20


def ytdl_path():
    """mpv의 ytdl_hook이 쓸 yt-dlp 실행 파일: 앱의 가상환경(웹 UI에서 업데이트하는 것)을 우선"""
    local = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    return local if os.access(local, os.X_OK) else (shutil.which("yt-dlp") or "yt-dlp")


def best_format(settings):
    """항상 최고 화질. 다운로드 설정의 H.264 우선/AV1 제외(이 기기의 하드웨어 디코딩 범위)는 따름"""
    return downloader.format_selector("best", settings.get("prefer_h264"), settings.get("avoid_av1"))


def probe(url, settings):
    """링크 정보. 실패하면 None (그래도 재생은 mpv가 시도)"""
    if yt_dlp is None:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if settings.get("cookies"):
        opts["cookiefile"] = settings["cookies"]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception as e:  # yt-dlp는 다양한 예외를 던진다
        log.warning("Cannot read stream info for %s: %s", url, e)
        return None
    if not isinstance(info, dict):
        return None
    return {
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "is_live": bool(info.get("is_live")),
        "viewers": info.get("concurrent_view_count"),
        "url": info.get("webpage_url") or url,
        "id": info.get("id") or "",
    }


def mpv_args(settings):
    """링크 재생용 mpv 옵션"""
    args = [f"--ytdl-format={best_format(settings)}",
            f"--script-opts-append=ytdl_hook-ytdl_path={ytdl_path()}"]
    raw = []
    if settings.get("cookies"):
        raw.append(f"cookies={settings['cookies']}")
    if raw:
        args.append("--ytdl-raw-options=" + ",".join(raw))
    return args


class Archiver:
    """라이브 녹화. 녹화마다 yt-dlp 프로세스 하나 (방송이 끝나면 저절로 종료)"""

    def __init__(self, get_settings, get_folder):
        self._get_settings = get_settings
        self._get_folder = get_folder
        self._jobs = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def folder(self):
        return ARCHIVE_FOLDER or os.path.join(self._get_folder(), "라이브 아카이브")

    def recording(self, url):
        """이 링크를 녹화 중이면 그 작업 id"""
        with self._lock:
            for job in self._jobs.values():
                if job["url"] == url and job["status"] == "녹화 중":
                    return job["id"]
        return None

    def start(self, url, title=""):
        if self.recording(url):
            raise ValueError("이미 녹화 중입니다")
        folder = self.folder()
        os.makedirs(folder, exist_ok=True)
        settings = self._get_settings()
        # 받는 중인 파일도 재생할 수 있게 MPEG-TS로 저장 (중간에 멈춰도 그때까지 분량이 남음)
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--no-colors",
               "-f", best_format(settings), "--hls-use-mpegts", "--no-part",
               # HLS를 MPEG-TS로 받으므로 확장자는 .ts로 고정
               "-o", os.path.join(folder, "%(channel,uploader)s", "%(upload_date)s %(title).80B [%(id)s].ts"),
               "--print", "before_dl:%(_filename)s", "--print", "after_move:filepath", url]
        if settings.get("cookies"):
            cmd[-1:-1] = ["--cookies", settings["cookies"]]
        job = {"id": next(self._ids), "url": url, "title": title, "status": "녹화 중", "file": "",
               "started": time.time(), "ended": None, "error": ""}
        try:
            # 새 프로세스 그룹: 멈출 때 yt-dlp와 그 아래 ffmpeg에 함께 신호를 보내려고
            job["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, start_new_session=True)
        except OSError as e:
            raise ValueError(f"녹화를 시작하지 못했습니다: {e}") from None
        with self._lock:
            self._jobs[job["id"]] = job
            self._trim()
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        log.info("Archive started: %s", url)
        return job["id"]

    def stop(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "녹화 중":
                return False
            job["status"] = "멈추는 중"
        try:
            # Ctrl+C와 같은 신호: yt-dlp가 ffmpeg를 정리하고 파일을 마무리한다
            os.killpg(job["proc"].pid, signal.SIGINT)
        except OSError:
            pass
        return True

    def stop_all(self):
        for job in self.jobs():
            if job["status"] == "녹화 중":
                self.stop(job["id"])

    def jobs(self):
        with self._lock:
            jobs = []
            for job in sorted(self._jobs.values(), key=lambda j: -j["id"]):
                item = {k: v for k, v in job.items() if k != "proc"}
                item["size"] = _size(job["file"])
                item["seconds"] = int((job["ended"] or time.time()) - job["started"])
                jobs.append(item)
            return jobs

    def _watch(self, job):
        proc = job["proc"]
        tail = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith(os.sep):
                # --print로 받은 저장 경로
                with self._lock:
                    job["file"] = line
            else:
                tail = (tail + [line])[-5:]
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            code = proc.wait()
        with self._lock:
            job["ended"] = time.time()
            stopped = job["status"] == "멈추는 중"
            if stopped or code == 0:
                job["status"] = "완료"
            else:
                job["status"] = "실패"
                job["error"] = next((l for l in reversed(tail) if "ERROR" in l), tail[-1] if tail else f"코드 {code}")
        log.info("Archive %s: %s (%s)", job["status"], job["url"], job["file"])

    def _trim(self):
        finished = [j for j in self._jobs.values() if j["status"] in ("완료", "실패")]
        for job in sorted(finished, key=lambda j: j["id"])[:-MAX_FINISHED_ARCHIVES or None]:
            del self._jobs[job["id"]]


def _size(path):
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0
