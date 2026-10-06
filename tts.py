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
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.0"))  # 말하는 속도 배율 (1.2 = 20% 빠르게, 0.8 = 20% 느리게)
ESPEAK_WPM = 175  # espeak-ng 기본 속도(분당 단어 수)
PIPER_MODEL = os.environ.get("PIPER_MODEL", "ja_JP-tsukuyomi-chan-medium")  # 기본 목소리: 모델 이름 또는 .onnx 경로
PIPER_DATA_DIR = os.environ.get("PIPER_DATA_DIR", os.path.join(BASE_DIR, "piper-models"))
# 요청에서 고를 수 있는 목소리(모델) 목록. 기본 목소리는 항상 포함
PIPER_VOICES = list(dict.fromkeys([PIPER_MODEL] + [
    v.strip() for v in os.environ.get("PIPER_VOICES", "ja_JP-css10-6lang-medium").split(",") if v.strip()]))
PIPER_SPEAKER = int(os.environ.get("PIPER_SPEAKER", "0"))  # 화자가 여러 명인 모델에서 쓸 기본 화자 번호


def parse_speed(speed):
    """0.5~2.0 배율로 변환, 지정하지 않았거나 잘못된 값이면 기본 속도"""
    try:
        return max(0.5, min(float(speed), 2.0))
    except (TypeError, ValueError):
        return max(0.5, min(TTS_SPEED, 2.0))


def resolve_voice(voice):
    """목록에 있는 목소리면 그대로, 아니면 기본 목소리"""
    if voice and voice not in PIPER_VOICES:
        log.warning("Unknown voice %r, using %s", voice, PIPER_MODEL)
    return voice if voice in PIPER_VOICES else PIPER_MODEL


def parse_speaker(speaker):
    """0 이상의 정수로 변환, 지정하지 않았거나 잘못된 값이면 기본 화자"""
    try:
        return max(0, int(speaker))
    except (TypeError, ValueError):
        return PIPER_SPEAKER


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
    """piper-plus 음성 모델을 목소리별로 한 번만 로드해서 재사용"""

    RETRY_SECONDS = 300

    def __init__(self):
        self._lock = threading.Lock()
        self._voices = {}
        self._failed_at = {}

    def _find_model(self, download, name):
        """(onnx 경로, config 경로) 반환. 없을 때만 내려받음 (최초 1회 인터넷 필요)"""
        if os.path.exists(name):
            return name, None
        # 모델마다 config.json 이름이 같아서 목소리별 폴더에 받는다.
        # 예전처럼 PIPER_DATA_DIR에 바로 받아 둔 모델도 찾는다
        voice_dir = os.path.join(PIPER_DATA_DIR, name)
        # ensure_voice_exists는 파일 크기가 목록과 다르면 매번 다시 받으므로 먼저 찾아본다
        try:
            return download.find_voice(name, [voice_dir, PIPER_DATA_DIR])
        except ValueError:
            pass
        os.makedirs(voice_dir, exist_ok=True)
        voices = download.get_voices(voice_dir)
        for info in list(voices.values()):
            for alias in info.get("aliases", []):
                voices[alias] = {"_is_alias": True, **info}
        download.ensure_voice_exists(name, [voice_dir], voice_dir, voices)
        return download.find_voice(name, [voice_dir])

    def _load(self, name):
        piper, download = import_piper()
        model, config_path = self._find_model(download, name)
        log.info("Loading piper-plus model %s", model)
        voice = piper.PiperVoice.load(model, config_path=config_path)
        if "en" in self.languages(voice):
            ensure_nltk_data()
        return voice

    def voice(self, name=None):
        """로드된 음성 반환, 사용할 수 없으면 None (실패하면 5분 뒤에 다시 시도)"""
        name = resolve_voice(name)
        with self._lock:
            failed_at = self._failed_at.get(name)
            retry = failed_at is None or time.monotonic() - failed_at > self.RETRY_SECONDS
            if name not in self._voices and retry:
                try:
                    self._voices[name] = self._load(name)
                    self._failed_at.pop(name, None)
                except Exception as e:
                    log.error("piper-plus voice %s unavailable: %s", name, e)
                    self._failed_at[name] = time.monotonic()
            return self._voices.get(name)

    def languages(self, voice):
        return set(voice.config.language_id_map or {}) or {"ja"}

    def synthesize(self, text, lang, path, speed, voice=None, speaker=None):
        model = self.voice(voice)
        if model is None or lang not in self.languages(model):
            return False
        speaker_id = parse_speaker(speaker)
        if speaker_id >= getattr(model.config, "num_speakers", 1):
            speaker_id = 0
        with wave.open(path, "wb") as wav_file:
            # length_scale은 음소 길이 배율이라 클수록 느려짐
            model.synthesize(text, wav_file, speaker_id=speaker_id, length_scale=1.0 / speed,
                             language_id=(model.config.language_id_map or {}).get(lang))
        return True


piper_tts = PiperTTS()


def synthesize_espeak(text, lang, path, speed, voice=None, speaker=None):
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not espeak:
        return False
    result = subprocess.run([espeak, "-v", lang, "-s", str(round(ESPEAK_WPM * speed)), "-w", path, text],
                            capture_output=True, timeout=60)
    return result.returncode == 0


# 엔진 이름: (합성 함수, 출력 파일 확장자)
TTS_BACKENDS = {
    "piper": (piper_tts.synthesize, "wav"),
    "espeak": (synthesize_espeak, "wav"),
}


def synthesize(text, lang, folder, speed=None, voice=None, speaker=None):
    """TTS_ENGINES 순서대로 시도해서 (음성 파일 경로, 엔진, 언어) 반환 (모두 실패하면 None).
    voice/speaker는 piper-plus에만 적용"""
    if lang == "auto":
        lang = detect_lang(text)
    speed = parse_speed(speed)
    for engine in TTS_ENGINES:
        if engine not in TTS_BACKENDS:
            log.warning("Unknown TTS engine: %s", engine)
            continue
        func, ext = TTS_BACKENDS[engine]
        path = os.path.join(folder, f"tts-{engine}.{ext}")
        try:
            if func(text, lang, path, speed, voice, speaker):
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
