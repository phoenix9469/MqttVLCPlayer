"""유튜브 등 링크 재생과 라이브 녹화(아카이브).

재생은 mpv가 내장 ytdl_hook으로 yt-dlp를 불러 직접 하고, 여기서는
- 링크 정보 미리 확인 (라이브 여부, 제목, 채널, 시청자 수)
- mpv에 넘길 화질·yt-dlp 옵션
- 라이브 녹화: yt-dlp를 별도 프로세스로 실행해서 방송이 끝나거나 멈출 때까지 저장
을 맡는다.
"""
import glob
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

MAX_FINISHED_ARCHIVES = 20


def ytdl_path():
    """mpv의 ytdl_hook이 쓸 yt-dlp 실행 파일: 앱의 가상환경(웹 UI에서 업데이트하는 것)을 우선"""
    local = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    return local if os.access(local, os.X_OK) else (shutil.which("yt-dlp") or "yt-dlp")


def best_format(settings, prefer_dash=False):
    """항상 최고 화질. 다운로드 설정의 H.264 우선/AV1 제외(이 기기의 하드웨어 디코딩 범위)는 따름.
    prefer_dash: 같은 조건이면 HLS(m3u8)가 아닌 형식을 먼저 고름. 유튜브 일반 영상의 HLS는 조각을 하나씩
    받느라 시작이 느리고 연결 경고가 많다. 라이브처럼 HLS만 있으면 뒤의 원래 선택으로 넘어감"""
    selector = downloader.format_selector("best", settings.get("prefer_h264"), settings.get("avoid_av1"))
    if not prefer_dash:
        return selector
    no_hls = "/".join("+".join(part + "[protocol!*=m3u8]" for part in choice.split("+"))
                      for choice in selector.split("/"))
    return no_hls + "/" + selector


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
    # force-window: yt-dlp가 정보를 받는 동안에도 창을 바로 띄움 (불러오는 중 표시용)
    # cache-pause-wait: 시작 직후 잠깐 데이터가 늦어도 NAS용 대기(MPV_CACHE_PAUSE_WAIT)만큼 멈추지 않게
    args = ["--force-window=immediate", "--cache-pause-wait=1",
            f"--ytdl-format={best_format(settings, prefer_dash=True)}",
            f"--script-opts-append=ytdl_hook-ytdl_path={ytdl_path()}"]
    # 자막은 쓰지 않으므로 받지 않음 (mpv는 따로 정하지 않으면 라이브 채팅까지 모든 자막을 요청함)
    raw = ["sub-langs=-live_chat"]
    if settings.get("cookies"):
        raw.append(f"cookies={settings['cookies']}")
    if raw:
        args.append("--ytdl-raw-options=" + ",".join(raw))
    return args


class Archiver:
    """라이브 녹화. 녹화마다 yt-dlp 프로세스 하나 (방송이 끝나면 저절로 종료).
    from_start면 방송 처음부터 받는다(--live-from-start). 처음부터 받을 수 없는 방송이면 지금부터 받는다."""

    def __init__(self, get_settings, get_folder):
        self._get_settings = get_settings
        self._get_folder = get_folder  # 저장 폴더 (웹 설정)
        self._jobs = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def folder(self):
        return self._get_folder()

    def recording(self, url):
        """이 링크를 녹화 중이면 그 작업 id"""
        with self._lock:
            for job in self._jobs.values():
                if job["url"] == url and job["status"] == "녹화 중":
                    return job["id"]
        return None

    def start(self, url, title="", from_start=True):
        if self.recording(url):
            raise ValueError("이미 녹화 중입니다")
        folder = self.folder()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            raise ValueError(f"녹화 폴더를 만들 수 없습니다: {folder} ({e})") from None
        job = {"id": next(self._ids), "url": url, "title": title, "status": "녹화 중", "file": "",
               "folder": folder, "from_start": from_start, "note": "" if from_start else "지금부터 녹화", "started": time.time(), "ended": None,
               "error": ""}
        self._launch(job)
        with self._lock:
            self._jobs[job["id"]] = job
            self._trim()
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        log.info("Archive started: %s", url)
        return job["id"]

    def _launch(self, job):
        settings = self._get_settings()
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--no-colors",
               "-f", best_format(settings), "--merge-output-format", "mkv", "--hls-use-mpegts",
               "-o", os.path.join(job["folder"], "%(channel,uploader)s",
                                  "%(upload_date)s %(title).80B [%(id)s].%(ext)s"),
               "--print", "before_dl:%(_filename)s", "--print", "after_move:filepath"]
        cmd.append("--live-from-start" if job["from_start"] else "--no-live-from-start")
        if settings.get("cookies"):
            cmd += ["--cookies", settings["cookies"]]
        cmd.append(job["url"])
        try:
            # 새 프로세스 그룹: 멈출 때 yt-dlp와 그 아래 ffmpeg에 함께 신호를 보내려고
            job["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, start_new_session=True)
        except OSError as e:
            raise ValueError(f"녹화를 시작하지 못했습니다: {e}") from None

    def stop(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "녹화 중":
                return False
            job["status"] = "멈추는 중"
        try:
            # Ctrl+C와 같은 신호. 받은 데이터는 남기고 _finish에서 파일로 합친다
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
                item["size"] = sum(_size(p) for p in _related_files(job["file"])) if job["file"] else 0
                item["seconds"] = int((job["ended"] or time.time()) - job["started"])
                jobs.append(item)
            return jobs

    def _watch(self, job):
        while True:
            proc = job["proc"]
            tail = []
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if line.startswith(os.sep):
                    # --print로 받은 저장 경로 (받기 전 예정 경로, 다 받은 뒤 최종 경로)
                    with self._lock:
                        job["file"] = line
                else:
                    tail = (tail + [line])[-8:]
            try:
                code = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait()
            # 처음부터 받을 수 없는 방송(다시 보기 꺼짐 등)이면 지금부터 다시 시작
            if (code != 0 and job["from_start"] and job["status"] == "녹화 중"
                    and any("from the start" in l for l in tail)):
                log.info("Cannot record %s from the start, recording from now", job["url"])
                job["from_start"] = False
                job["note"] = "처음부터 받을 수 없는 방송이라 지금부터 녹화"
                try:
                    self._launch(job)
                    continue
                except ValueError as e:
                    tail.append(str(e))
            break
        stopped = job["status"] == "멈추는 중"
        final = self._finish(job["file"]) if job["file"] else ""
        with self._lock:
            job["ended"] = time.time()
            if final:
                job["file"] = final
            if (stopped or code == 0) and final:
                job["status"] = "완료"
            else:
                job["status"] = "실패"
                job["error"] = next((l for l in reversed(tail) if "ERROR" in l), tail[-1] if tail else f"코드 {code}")
        log.info("Archive %s: %s (%s)", job["status"], job["url"], job["file"])

    def _finish(self, path):
        """녹화 파일 마무리. 정상 종료면 이미 합쳐진 파일, 중간에 멈췄으면 받은 조각(영상/음성)을 합친다.
        최종 파일 경로, 아무것도 없으면 ''"""
        # yt-dlp가 이어받기용으로 남긴 파일은 지움
        for leftover in _related_files(path):
            if leftover.endswith((".ytdl", ".temp")):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        if os.path.isfile(path):
            return path
        parts = [p for p in _related_files(path) if not p.endswith(".json")]
        if not parts:
            return ""
        # 이름 끝의 .part를 떼어 둔다 (합치기에 실패해도 받은 조각은 재생할 수 있게)
        named = []
        for part in sorted(parts):
            target = part[:-5] if part.endswith(".part") else part
            try:
                os.replace(part, target)
            except OSError:
                target = part
            named.append(target)
        if len(named) == 1:
            return named[0]
        output = os.path.splitext(path)[0] + ".mkv"
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for part in named:
            cmd += ["-i", part]
        for i in range(len(named)):
            cmd += ["-map", str(i)]
        cmd += ["-c", "copy", output]
        try:
            merged = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Cannot merge archive parts %s: %s", named, e)
            return named[0]
        if merged.returncode != 0 or not os.path.isfile(output):
            log.warning("Cannot merge archive parts %s: %s", named, merged.stderr.strip()[-300:])
            return named[0]
        for part in named:
            try:
                os.remove(part)
            except OSError:
                pass
        return output

    def _trim(self):
        finished = [j for j in self._jobs.values() if j["status"] in ("완료", "실패")]
        for job in sorted(finished, key=lambda j: j["id"])[:-MAX_FINISHED_ARCHIVES or None]:
            del self._jobs[job["id"]]


def _related_files(path):
    """녹화 파일과 받는 중인 조각들 (같은 이름에 .f137.mp4.part 같은 꼬리가 붙은 파일)"""
    base = os.path.splitext(path)[0]
    return [p for p in glob.glob(glob.escape(base) + ".*") if os.path.isfile(p)]


def _size(path):
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0
