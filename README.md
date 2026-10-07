# MqttVLCPlayer

NAS 폴더의 영상을 mpv(하드웨어 디코딩)로 무작위 전체화면 재생하고, LG TV 전원을 RS-232 시리얼로 제어하는 서버입니다.
Home Assistant(MQTT Discovery)와 웹 UI에서 제어할 수 있습니다.

## 기능

- **랜덤 재생 / 정지**: NAS 폴더의 `.mp4`, `.mkv`, `.avi` 파일을 섞어서 재생목록을 만들고 재생합니다. 재생은 항상 하나만 유지됩니다.
- **시계 표시**: 재생 중 화면 좌측 상단에 기기의 현재 시각(`HH:MM`)을 표시합니다.
- **알림 소리 / 음성 안내**: Home Assistant 자동화에서 기기 스피커로 사운드 파일이나 TTS 음성을 재생합니다. 영상 재생 중에도 함께 재생됩니다.
- **개별 영상 재생**: 웹 UI의 영상 목록에서 선택해서 재생합니다.
- **음량 조절**: 웹 UI에서 영상 음량과 알림 음량을 따로 조절합니다. 영상 음량은 재생 중에도 바로 바뀝니다(mpv).
- **LG TV 전원 제어**: [libLGTV_serial](https://github.com/ehjortberg/libLGTV_serial)로 전원 켜기/끄기, 상태 조회를 합니다.
- **Home Assistant 연동**: 버튼, 스위치, 바이너리 센서가 자동으로 등록됩니다(retain). 서버가 꺼지면 엔티티가 "사용 불가"로 표시됩니다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `video_player.py` | 실행 진입점. 영상 재생, LG TV 제어, MQTT(Home Assistant), 웹 UI |
| `sound.py` | 알림 소리 대기열. 사운드 파일과 TTS 음성을 요청 순서대로 재생 |
| `tts.py` | TTS 엔진(piper-plus, espeak-ng). 문장을 WAV 파일로 생성 |
| `player.py` | 재생기(mpv 또는 cvlc) 프로세스 실행/종료 (영상과 알림 소리가 함께 사용) |
| `mpv/clock.lua` | mpv 재생 화면 좌측 상단 시계 표시 스크립트 |
| `templates/` | 웹 UI 화면 |
| `libLGTV_serial/` | LG TV RS-232 제어 라이브러리 (서브모듈) |

## 설치

```bash
git clone --recurse-submodules https://github.com/phoenix9469/MqttVLCPlayer.git
cd MqttVLCPlayer
sudo apt install mpv espeak-ng   # espeak-ng: piper-plus를 쓸 수 없을 때 대체 TTS
# VIDEO_PLAYER=vlc로 VLC를 쓸 때만: sudo apt install vlc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

일본어 음성 모델(약 40MB)을 미리 받아 둡니다. 받지 않으면 첫 음성 안내 때 자동으로 내려받습니다(인터넷 필요).

```bash
.venv/bin/piper-plus --download-model ja_JP-tsukuyomi-chan-medium --download-dir piper-models
```

piper-plus 1.x를 쓰는 경우 명령어 이름은 `piper`입니다(2.0부터 `piper-plus`로 바뀜). 앱은 두 버전 모두 지원합니다.

이미 clone했다면 `git submodule update --init`으로 `libLGTV_serial`을 받습니다.

`libLGTV_serial/LGTV.py`에서 `model`과 `serial_port`(예: `/dev/ttyUSB0`)를 사용하는 TV와 포트에 맞게 수정하세요.
실행 계정이 시리얼 포트에 접근할 수 있어야 합니다(`sudo usermod -aG dialout <user>`).

## 설정

설정은 환경변수로 지정합니다. 예시는 `deploy/mqttvlcplayer.env.example`에 있습니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `1883` | MQTT 브로커 |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | 없음 | 브로커 계정 |
| `NAS_FOLDER` | `/mv` | 영상 폴더 기본값 (웹 UI에서 바꾸면 `config.json`에 저장) |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `5000` | 웹 UI 주소 |
| `WEB_USERNAME` / `WEB_PASSWORD` | 없음 | 설정하면 웹 UI에 HTTP Basic 인증 적용 |
| `CLOCK_FORMAT` | `%H:%M` | 재생 화면 좌측 상단에 표시할 현재 시각 형식(strftime). 비우면 표시 안 함 |
| `CLOCK_SIZE` | `0` | 시계 글자 크기(px). `0`이면 화면 높이에 맞춰 자동으로 결정 |
| `CLOCK_FONT` | 없음 | 시계 글꼴 이름(`fc-list : family`로 확인). 비우면 mpv 기본 글꼴 |
| `VIDEO_PLAYER` | `mpv` | 영상과 알림 소리를 재생할 프로그램. `mpv` 또는 `vlc`(`cvlc`) |
| `MPV_HWDEC` | `auto-safe` | mpv 하드웨어 디코딩 방식. `auto-safe`는 VA-API 등을 자동 선택, `vaapi-copy`는 화면 연결이 안 될 때, `no`는 CPU 디코딩 |
| `LIBVA_DRIVER_NAME` | 없음 | VA-API 드라이버 강제 지정. Ivy Bridge(HD 4000) 등 구형 Intel GPU는 `i965`. 재생 중 터미널에 `Using hardware decoding (vaapi)`가 나오면 하드웨어 디코딩 중 |
| `MPV_CACHE_SECS` | `10` | mpv가 미리 읽어 둘 영상 길이(초). NAS에서 읽다가 끊기면 늘림 |
| `MPV_AUDIO_FILTER` | `dynaudnorm=f=150:g=5` | 음량 평준화 필터. 값을 키우면(예: `dynaudnorm`) 더 부드럽지만 시작 시 소리를 모으느라 첫 화면이 수 초 멈춤. 비우면 필터 없음 |
| `MPV_EXTRA_ARGS` | 없음 | 영상 재생에 추가할 mpv 옵션. 예: `--vo=gpu` |
| `VLC_CACHING` | `3000` | (`VIDEO_PLAYER=vlc`) 영상 재생 버퍼(ms) |
| `VLC_EXTRA_ARGS` | 없음 | (`VIDEO_PLAYER=vlc`) 영상 재생에 추가할 VLC 옵션. 비교는 `bash deploy/vlc-benchmark.sh <영상 파일>` |
| `SOUNDS_FOLDER` | `./sounds` | 알림용 사운드 파일 폴더(`.mp3`, `.wav`, `.ogg`, `.oga`, `.opus`, `.flac`, `.m4a`, `.aac`). 웹 UI 알림 소리 섹션에 실제로 읽는 폴더가 표시됨 |
| `VIDEO_VOLUME` | `100` | 영상 음량 처음 값(0~200, 100이 원래 크기). 웹 UI에서 바꾸면 `config.json`에 저장되어 그 값이 우선 |
| `SOUND_VOLUME` | `100` | 알림 소리 기본 볼륨 처음 값(0~200). 웹 UI에서 바꾸면 `config.json`에 저장되어 그 값이 우선 |
| `TTS_ENGINES` | `piper,espeak` | 시도할 TTS 엔진 순서. 실패하면 다음 엔진 사용. `piper`(piper-plus), `espeak`(espeak-ng, 음질 낮음) |
| `TTS_LANG` | `auto` | TTS 언어 `ja` 또는 `en`. `auto`면 가나·한자가 있을 때 `ja`, 아니면 `en` |
| `TTS_SPEED` | `1.0` | 말하는 속도 배율(0.5~2.0). 모델의 기본 속도 기준으로 `1.2`는 20% 빠르게, `0.8`은 20% 느리게 |
| `PIPER_MODEL` | `ja_JP-tsukuyomi-chan-medium` | 기본 목소리. piper-plus 모델 이름 또는 `.onnx` 파일 경로 |
| `PIPER_VOICES` | `ja_JP-css10-6lang-medium` | 기본 목소리 외에 고를 수 있는 목소리 목록(쉼표로 구분) |
| `PIPER_SPEAKER` | `0` | 화자가 여러 명인 모델에서 쓸 기본 화자 번호 |
| `PIPER_DATA_DIR` | `./piper-models` | piper-plus 모델 저장 폴더 |
| `STATUS_INTERVAL` | `60` | TV 전원 상태 조회 주기(초), `0`이면 조회 안 함 |
| `CONFIG_FILE` | `./config.json` | 웹 UI 설정 저장 위치 |
| `PLAYLIST_PATH` | `./playlist.m3u8` | 생성되는 재생목록 위치 |
| `LGTV_SCRIPT` | `./libLGTV_serial/LGTV.py` | TV 제어 스크립트 |

## 실행

```bash
set -a; . /etc/mqttvlcplayer.env; set +a
.venv/bin/python video_player.py
```

### 부팅 시 터미널 창에서 자동 실행 (데스크톱)

데스크톱에 로그인하면 터미널 창이 열리고 그 안에서 앱이 실행됩니다. 앱이 종료되면 5초 뒤 다시 시작합니다.

```bash
deploy/install-autostart.sh      # sudo 없이, 앱을 실행할 사용자로 한 번만 실행
```

- 환경변수는 `/etc/mqttvlcplayer.env`에서 읽습니다(`ENV_FILE`로 위치 변경 가능). 실행 사용자가 읽을 수 있어야 합니다:
  `sudo chown root:<user> /etc/mqttvlcplayer.env && sudo chmod 640 /etc/mqttvlcplayer.env`
- 프로젝트가 NAS 폴더에 있어도, 폴더가 보일 때까지 기다렸다가 실행합니다.
- 전원을 켜면 바로 실행되도록 **자동 로그인**을 켜세요(GNOME: 설정 → 사용자 → 자동 로그인).
- 영상이 계속 보이도록 **화면 꺼짐과 잠금**을 끄세요:
  `gsettings set org.gnome.desktop.session idle-delay 0 && gsettings set org.gnome.desktop.screensaver lock-enabled false`
- 환경변수 파일은 앱이 시작될 때마다 다시 읽습니다. 파일을 고친 뒤 터미널에서 **Ctrl+C를 한 번** 누르면 5초 뒤 새 설정으로 다시 시작합니다.
- 완전히 멈추려면 재시작 대기(5초) 중에 Ctrl+C를 한 번 더 누르세요. 직접 다시 실행하려면 `bash deploy/start.sh`.
- 자동 실행 해제: `rm ~/.config/autostart/mqttvlcplayer.desktop`
- 아래 systemd 서비스와 **동시에 쓰지 마세요**(앱이 두 개 실행됨).

### systemd 서비스로 실행

```bash
sudo cp deploy/mqttvlcplayer.env.example /etc/mqttvlcplayer.env
sudo chmod 600 /etc/mqttvlcplayer.env      # 값 채우기
sudo cp deploy/mqttvlcplayer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mqttvlcplayer
```

서비스 파일의 `User`, 경로, `DISPLAY`는 환경에 맞게 수정하세요.

## MQTT 토픽

| 토픽 | 방향 | 내용 |
|---|---|---|
| `cvlc_tv/lgtv/on`, `cvlc_tv/lgtv/off` | 구독 | TV 켜기 / 끄기 |
| `cvlc_tv/lgtv/switch/set` | 구독 | `1` 켜기, `0` 끄기 |
| `cvlc_tv/cvlc/play`, `cvlc_tv/cvlc/stop` | 구독 | 랜덤 재생 / 정지 |
| `cvlc_tv/sound/play` | 구독 | 사운드 파일 재생. payload: `doorbell.mp3` 또는 `{"file": "doorbell.mp3", "volume": 80}` |
| `cvlc_tv/sound/say` | 구독 | 문장 읽기(TTS). payload: `玄関のドアが開きました。` 또는 `{"text": "...", "volume": 80, "lang": "ja", "speed": 1.2, "voice": "ja_JP-css10-6lang-medium", "speaker": 0}` |
| `cvlc_tv/sound/stop` | 구독 | 재생 중인 알림 소리와 대기 중인 알림 모두 취소 |
| `cvlc_tv/lgtv/status`, `cvlc_tv/lgtv/switch` | 발행(retain) | TV 전원 상태 `1` / `0` |
| `cvlc_tv/availability` | 발행(retain) | `online` / `offline` |

알림 소리는 요청된 순서대로 하나씩 재생됩니다. 그래서 `sound/play`로 차임을 보낸 뒤 `sound/say`로 안내 문장을 보내면 차례로 들립니다.
`sound/play`는 `SOUNDS_FOLDER` 안의 파일만 재생합니다.

## Home Assistant에서 알림 소리 사용

`NOTIFY_CVLC_SOUND`(사운드 파일), `NOTIFY_CVLC_TTS`(음성 안내) notify 엔티티가 자동으로 등록됩니다(HA 2024.5 이상).
엔티티 ID는 보통 `notify.video_control_server_notify_cvlc_sound`처럼 기기 이름이 앞에 붙습니다. 실제 ID는 HA의 엔티티 목록에서 확인하세요.

```yaml
automation:
  - alias: "현관문 열림 알림"
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door
        to: "on"
    actions:
      - action: notify.send_message
        target:
          entity_id: notify.video_control_server_notify_cvlc_sound
        data:
          message: doorbell.mp3
      - action: notify.send_message
        target:
          entity_id: notify.video_control_server_notify_cvlc_tts
        data:
          message: 玄関のドアが開きました。

  - alias: "매일 아침 7시 안내"
    triggers:
      - trigger: time
        at: "07:00:00"
    actions:
      - action: mqtt.publish
        data:
          topic: cvlc_tv/sound/say
          payload: '{"text": "おはようございます。今日は{{ now().month }}月{{ now().day }}日です。", "volume": 70}'
```

### TTS 엔진

기본 엔진은 [piper-plus](https://github.com/ayutaz/piper-plus)입니다. 기기 안에서만 동작하는 신경망 TTS입니다.
기본 모델 `tsukuyomi-chan`으로 일본어와 영어를 읽습니다.
영어 발음 변환에 필요한 NLTK 데이터(`cmudict`, `averaged_perceptron_tagger_eng`)는 모델을 불러올 때 없으면 자동으로 내려받습니다(최초 1회 인터넷 필요).

웹 UI의 **TTS 테스트**에서 문장을 입력해 기기 스피커로 재생하거나, 브라우저에서 바로 들어볼 수 있습니다. 어떤 엔진과 언어로 읽었는지도 표시됩니다.

#### 목소리 바꾸기

- 기본 목소리는 `PIPER_MODEL`, 고를 수 있는 목소리 목록은 `PIPER_VOICES`로 정합니다. 목록에 없는 목소리를 요청하면 기본 목소리로 읽습니다.
- 요청마다 `voice`(목소리)와 `speaker`(화자 번호)를 지정할 수 있습니다. 화자가 한 명뿐인 모델은 `speaker`를 무시합니다.
- 웹 UI의 **TTS 테스트**에서 목소리와 화자를 바꿔 가며 들어볼 수 있습니다.
- 모델 파일은 목소리마다 `piper-models/<목소리 이름>/` 폴더에 받습니다(모델마다 `config.json` 이름이 같아서 한 폴더에 두면 덮어씀). 처음 쓰는 목소리는 자동으로 내려받고, 미리 받으려면:

  ```bash
  .venv/bin/piper-plus --download-model ja_JP-css10-6lang-medium --download-dir piper-models/ja_JP-css10-6lang-medium
  ```

모델은 서버를 시작할 때 미리 불러옵니다. 불러오지 못하면 5분 뒤 다시 시도하고, 그동안은 다음 엔진을 사용합니다.

영상과 알림 소리가 동시에 나려면 기기에서 PulseAudio 또는 PipeWire를 사용해야 합니다.
ALSA 장치를 직접 사용하는 환경에서는 두 번째 소리가 재생되지 않을 수 있습니다.

예약 재생이 필요하면 Home Assistant 자동화에서 위 토픽(또는 등록된 버튼)을 호출하세요.
