"""TTS: 문장을 음성 파일(WAV)로 만든다. piper-plus를 쓰고, 쓸 수 없으면 espeak-ng로 대체"""
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import wave

log = logging.getLogger("mqttvlcplayer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 사용할 TTS 엔진을 시도할 순서대로 지정. 언어를 지원하지 않거나 실패하면 다음 엔진 사용
#   piper: piper-plus(일본어·영어), espeak: espeak-ng(음질 낮음, piper를 쓸 수 없을 때 대체용)
TTS_ENGINES = [e.strip() for e in os.environ.get("TTS_ENGINES", "piper,espeak").split(",") if e.strip()]
TTS_LANG = os.environ.get("TTS_LANG", "auto")  # ja, en 또는 auto(가나·한자가 있으면 ja, 아니면 en)
TTS_LANGS = ("auto", "ja", "en")
PIPER_MODEL = os.environ.get("PIPER_MODEL", "ja_JP-tsukuyomi-chan-medium")  # 모델 이름 또는 .onnx 경로
PIPER_DATA_DIR = os.environ.get("PIPER_DATA_DIR", os.path.join(BASE_DIR, "piper-models"))


def detect_lang(text):
    """가나·한자가 있으면 일본어, 아니면 영어"""
    return "ja" if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text) else "en"


def ensure_nltk_data():
    """영어 발음 변환(g2p_en)에 필요한 NLTK 데이터가 없으면 내려받음 (최초 1회 인터넷 필요)"""
    try:
        import nltk
    except ImportError:
        return
    # g2p_en은 예전 이름의 태거만 받지만, 최신 NLTK는 averaged_perceptron_tagger_eng를 찾음
    for path, name in (("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng"),
                       ("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger"),
                       ("corpora/cmudict", "cmudict")):
        try:
            nltk.data.find(path)
        except LookupError:
            if not nltk.download(name, quiet=True):
                log.warning("Failed to download NLTK data %s (English TTS may not work)", name)


def import_piper():
    """piper-plus 모듈 반환 (2.x는 piper_plus, 1.x는 piper)"""
    import importlib
    try:
        return importlib.import_module("piper_plus"), importlib.import_module("piper_plus.download")
    except ImportError:
        return importlib.import_module("piper"), importlib.import_module("piper.download")


class PiperTTS:
    """piper-plus 음성 모델을 한 번만 로드해서 재사용"""

    def __init__(self):
        self._lock = threading.Lock()
        self._voice = None
        self._failed_at = None

    def _load(self):
        piper, download = import_piper()

        model, config_path = PIPER_MODEL, None
        if not os.path.exists(model):
            # 모델 이름이면 PIPER_DATA_DIR에서 찾고, 없으면 내려받음 (최초 1회 인터넷 필요)
            os.makedirs(PIPER_DATA_DIR, exist_ok=True)
            voices = download.get_voices(PIPER_DATA_DIR)
            for info in list(voices.values()):
                for alias in info.get("aliases", []):
                    voices[alias] = {"_is_alias": True, **info}
            download.ensure_voice_exists(model, [PIPER_DATA_DIR], PIPER_DATA_DIR, voices)
            model, config_path = download.find_voice(model, [PIPER_DATA_DIR])
        log.info("Loading piper-plus model %s", model)
        voice = piper.PiperVoice.load(model, config_path=config_path)
        if "en" in self.languages(voice):
            ensure_nltk_data()
        return voice

    def voice(self):
        """로드된 음성 반환, 사용할 수 없으면 None (실패하면 5분 뒤에 다시 시도)"""
        with self._lock:
            retry = self._failed_at is None or time.monotonic() - self._failed_at > 300
            if self._voice is None and retry:
                try:
                    self._voice = self._load()
                except Exception as e:
                    log.error("piper-plus unavailable: %s", e)
                    self._failed_at = time.monotonic()
            return self._voice

    def languages(self, voice):
        return set(voice.config.language_id_map or {}) or {"ja"}

    def synthesize(self, text, lang, path):
        voice = self.voice()
        if voice is None or lang not in self.languages(voice):
            return False
        with wave.open(path, "wb") as wav_file:
            voice.synthesize(text, wav_file, language_id=(voice.config.language_id_map or {}).get(lang))
        return True


piper_tts = PiperTTS()


def synthesize_espeak(text, lang, path):
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not espeak:
        return False
    result = subprocess.run([espeak, "-v", lang, "-w", path, text], capture_output=True, timeout=60)
    return result.returncode == 0


# 엔진 이름: (합성 함수, 출력 파일 확장자)
TTS_BACKENDS = {
    "piper": (piper_tts.synthesize, "wav"),
    "espeak": (synthesize_espeak, "wav"),
}


def synthesize(text, lang, folder):
    """TTS_ENGINES 순서대로 시도해서 (음성 파일 경로, 엔진, 언어) 반환 (모두 실패하면 None)"""
    if lang == "auto":
        lang = detect_lang(text)
    for engine in TTS_ENGINES:
        if engine not in TTS_BACKENDS:
            log.warning("Unknown TTS engine: %s", engine)
            continue
        func, ext = TTS_BACKENDS[engine]
        path = os.path.join(folder, f"tts-{engine}.{ext}")
        try:
            if func(text, lang, path):
                log.info("TTS by %s (%s): %s", engine, lang, text)
                return path, engine, lang
        except Exception as e:
            log.warning("TTS engine %s failed: %s", engine, e)
    log.error("No TTS engine could speak %r (lang=%s)", text, lang)
    return None


def preload():
    """첫 음성 안내가 늦지 않도록 piper-plus 모델을 백그라운드에서 미리 로드"""
    if "piper" in TTS_ENGINES:
        threading.Thread(target=piper_tts.voice, daemon=True).start()
