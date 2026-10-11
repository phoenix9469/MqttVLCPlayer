"""영상 옆 자막 파일 찾기와 언어 판별.

자막 파일은 영상과 같은 폴더에 같은 이름으로 확장자만 다르다 (예: 01화.mkv → 01화.srt, 01화.ass).
이름에 언어가 없으므로 내용의 글자로 한국어(한글)·일본어(가나)·영어(로마자)를 구분해서
설정한 언어 순서대로 mpv에 넘긴다 (mpv는 맨 앞 자막을 표시).
"""
import json
import logging
import os
import re
import subprocess

log = logging.getLogger("mqttvlcplayer")

# mpv(ffmpeg)가 읽을 수 있는 자막 형식. idx는 같은 이름의 .sub(VobSub 그림 자막)와 함께 쓰임
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".smi", ".sami", ".sup", ".idx", ".sub", ".lrc")
TEXT_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".smi", ".sami", ".sub", ".lrc")
LANG_NAMES = {"ko": "한국어", "ja": "일본어", "en": "영어"}
# mpv --slang에 넘길 이름 (영상 안에 든 자막의 언어 표기는 제각각이라 여러 가지로)
SLANG_ALIASES = {"ko": ["ko", "kor", "kr"], "ja": ["ja", "jpn", "jp"], "en": ["en", "eng"]}

HANGUL = re.compile(r"[가-힣]")
KANA = re.compile(r"[぀-ヿ]")
LATIN = re.compile(r"[A-Za-z]")
# 글자 수를 셀 때 빼는 부분: ASS 효과 태그 {\...}, HTML 태그 <...>, 시간·번호 줄
MARKUP = re.compile(r"\{[^}]*\}|<[^>]*>|\d{1,2}:\d{2}:\d{2}[,.]\d+|-->")


def _read_text(path, limit=200_000):
    """자막 글자 읽기. UTF-8/UTF-16이 아니면 한국어(cp949)와 일본어(shift_jis) 중 더 그럴듯한 쪽으로
    (둘 다 오류 없이 읽히는 경우가 있어서, 각자 자기 글자(한글/가나)가 더 많이 나오는 쪽을 고른다)"""
    with open(path, "rb") as f:
        raw = f.read(limit)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    candidates = {}
    for encoding in ("cp949", "shift_jis"):
        try:
            candidates[encoding] = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    if len(candidates) == 2:
        korean = len(HANGUL.findall(candidates["cp949"]))
        japanese = len(KANA.findall(candidates["shift_jis"]))
        return candidates["shift_jis"] if japanese > korean else candidates["cp949"]
    if candidates:
        return next(iter(candidates.values()))
    return raw.decode("latin-1")


def detect_language(path):
    """자막 내용으로 언어 판별: 'ko', 'ja', 'en', 알 수 없으면 ''. 그림 자막(sup, idx)은 ''"""
    if not path.lower().endswith(TEXT_EXTENSIONS):
        return ""
    try:
        text = _read_text(path)
    except OSError as e:
        log.warning("Cannot read subtitle %s: %s", path, e)
        return ""
    if path.lower().endswith((".ass", ".ssa")):
        # 스타일 정의 등은 빼고 대사(Dialogue 줄)만
        text = "\n".join(line.split(",", 9)[-1] for line in text.splitlines() if line.startswith("Dialogue:"))
    text = MARKUP.sub(" ", text)
    hangul, kana, latin = len(HANGUL.findall(text)), len(KANA.findall(text)), len(LATIN.findall(text))
    if hangul >= 20 and hangul >= kana:
        return "ko"
    if kana >= 20:
        return "ja"
    if latin >= 100 and hangul < 5 and kana < 5:
        return "en"
    return ""


def find(video_path):
    """영상과 같은 이름의 자막 파일 목록 [{"path", "ext", "lang"}]"""
    folder, name = os.path.split(video_path)
    stem = os.path.splitext(name)[0]
    try:
        entries = os.listdir(folder or ".")
    except OSError:
        return []
    subs = []
    for entry in sorted(entries):
        base, ext = os.path.splitext(entry)
        ext = ext.lower()
        if base != stem or ext not in SUBTITLE_EXTENSIONS:
            continue
        # .sub는 .idx와 짝이면 .idx로 함께 읽히므로 따로 넣지 않음
        if ext == ".sub" and any(e.lower() == stem.lower() + ".idx" for e in entries):
            continue
        path = os.path.join(folder, entry)
        subs.append({"path": path, "ext": ext[1:], "lang": detect_language(path)})
    return subs


def code(lang):
    """자막 언어 표기(kor, jpn, en 등) → 'ko', 'ja', 'en', 모르면 ''"""
    lang = (lang or "").lower()
    return next((code for code, aliases in SLANG_ALIASES.items() if lang in aliases), "")


def embedded_languages(video_path):
    """영상 파일 안에 든 자막들의 언어 ['en', '', ...] (mpv 자막 번호 1, 2, ... 순서). 읽지 못하면 []"""
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s",
                                 "-show_entries", "stream_tags=language", "-of", "json", video_path],
                                capture_output=True, text=True, timeout=15)
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("ffprobe failed for %s: %s", video_path, e)
        return []
    return [code((s.get("tags") or {}).get("language")) for s in streams]


def choose_embedded(subs, embedded, order):
    """mpv는 언어와 상관없이 외부 자막 파일을 먼저 고르므로, 영상 안의 자막이 설정 순서상 더 앞 언어면
    그 자막 번호(1부터)를 돌려준다. 외부 자막 파일을 쓰면 되면 None"""
    def rank(lang):
        return order.index(lang) if lang in order else len(order)
    best_external = min((rank(sub["lang"]) for sub in subs), default=len(order) + 1)
    ranks = [rank(lang) for lang in embedded]
    if ranks and min(ranks) < best_external:
        return ranks.index(min(ranks)) + 1
    return None


def parse_order(text):
    """'ko,ja,en' → ['ko', 'ja', 'en'] (알 수 없는 값은 버림)"""
    order = [lang.strip().lower() for lang in str(text).split(",")]
    order = [lang for lang in order if lang in LANG_NAMES]
    return list(dict.fromkeys(order)) or ["ko", "ja", "en"]


def ordered(subs, order):
    """설정한 언어 순서대로 정렬. 같은 언어면 글자 자막(ass > srt > 기타)을 먼저, 언어를 모르는 자막은 맨 뒤"""
    ext_rank = {"ass": 0, "ssa": 1, "srt": 2, "vtt": 3, "smi": 4, "sami": 4}

    def key(sub):
        lang_rank = order.index(sub["lang"]) if sub["lang"] in order else len(order)
        return lang_rank, ext_rank.get(sub["ext"], 9)
    return sorted(subs, key=key)


def slang(order):
    """mpv --slang 값 (영상 파일 안에 든 자막을 고를 때)"""
    return ",".join(alias for lang in order for alias in SLANG_ALIASES[lang])


def label(sub):
    return f"{LANG_NAMES.get(sub['lang'], '언어 모름')} ({sub['ext']})"
