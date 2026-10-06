# MqttVLCPlayer

NAS 폴더의 영상을 VLC(`cvlc`)로 무작위 전체화면 재생하고, LG TV 전원을 RS-232 시리얼로 제어하는 서버입니다.
Home Assistant(MQTT Discovery)와 웹 UI에서 제어할 수 있습니다.

## 기능

- **랜덤 재생 / 정지**: NAS 폴더의 `.mp4`, `.mkv`, `.avi` 파일을 섞어서 재생목록을 만들고 재생합니다. 재생은 항상 하나만 유지됩니다.
- **개별 영상 재생**: 웹 UI의 영상 목록에서 선택해서 재생합니다.
- **LG TV 전원 제어**: [libLGTV_serial](https://github.com/ehjortberg/libLGTV_serial)로 전원 켜기/끄기, 상태 조회를 합니다.
- **Home Assistant 연동**: 버튼, 스위치, 바이너리 센서가 자동으로 등록됩니다(retain). 서버가 꺼지면 엔티티가 "사용 불가"로 표시됩니다.

## 설치

```bash
git clone --recurse-submodules https://github.com/phoenix9469/MqttVLCPlayer.git
cd MqttVLCPlayer
sudo apt install vlc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

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
| `STATUS_INTERVAL` | `60` | TV 전원 상태 조회 주기(초), `0`이면 조회 안 함 |
| `CONFIG_FILE` | `./config.json` | 웹 UI 설정 저장 위치 |
| `PLAYLIST_PATH` | `./playlist.m3u8` | 생성되는 재생목록 위치 |
| `LGTV_SCRIPT` | `./libLGTV_serial/LGTV.py` | TV 제어 스크립트 |

## 실행

```bash
set -a; . /etc/mqttvlcplayer.env; set +a
.venv/bin/python video_player.py
```

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
| `cvlc_tv/lgtv/status`, `cvlc_tv/lgtv/switch` | 발행(retain) | TV 전원 상태 `1` / `0` |
| `cvlc_tv/availability` | 발행(retain) | `online` / `offline` |

예약 재생이 필요하면 Home Assistant 자동화에서 위 토픽(또는 등록된 버튼)을 호출하세요.
