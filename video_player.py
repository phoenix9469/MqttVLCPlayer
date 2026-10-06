import os
import random
import subprocess
import json
import threading
import urllib.parse
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import time
import paho.mqtt.client as mqtt
# import serial

# ser = serial.Serial('/dev/ttyUSB0', 9600)

app = Flask(__name__)

# 기본 설정 파일 경로
CONFIG_FILE = '/home/user/config.json'
broker_address = "localhost"

discovery_topic_1 = "homeassistant/button/cvlc_tv_on/config"
discovery_topic_2 = "homeassistant/button/cvlc_tv_off/config"
discovery_topic_3 = "homeassistant/binary_sensor/cvlc_tv_status/config"
discovery_topic_4 = "homeassistant/button/cvlc_tv_play/config"
discovery_topic_5 = "homeassistant/button/cvlc_tv_stop/config"
discovery_topic_6 = "homeassistant/switch/cvlc_tv_swtich/config"

payload_1 = {
            "name":"BTN_LGTV_232_ON",
            "unique_id":"LGTV_232_ON",
            "command_topic": "cvlc_tv/lgtv/on",
            "device": {
                "name":"Video Control Server",
                "identifiers":"CVLC_TV",
                "manufacturer":"PNXELEC",
                "model":"CVLC",
                "hw_version":"0.00",
                "sw_version":"0.00",
                "configuration_url":"http://localhost:5000"
            }
            }

payload_2 = {
            "name":"BTN_LGTV_232_OFF",
            "unique_id":"LGTV_232_OFF",
            "command_topic": "cvlc_tv/lgtv/off",
            "device": {
                "name":"Video Control Server",
                "identifiers":"CVLC_TV",
                "manufacturer":"PNXELEC",
                "model":"CVLC",
                "hw_version":"0.00",
                "sw_version":"0.00",
                "configuration_url":"http://localhost:5000"
            }
            }

payload_3 = {
            "name":"BINARY_SENSOR_LGTV_232_STATUS",
            "unique_id":"LGTV_232_STATUS",
            "state_topic": "cvlc_tv/lgtv/status",
            "payload_on": "1",
            "payload_off": "0",
            "device": {
                "name":"Video Control Server",
                "identifiers":"CVLC_TV",
                "manufacturer":"PNXELEC",
                "model":"CVLC",
                "hw_version":"0.00",
                "sw_version":"0.00",
                "configuration_url":"http://localhost:5000"
            }
            }

payload_4 = {
            "name":"BTN_CVLC_RANDOM_PLAY",
            "unique_id":"CVLC_RANDOM_PLAY",
            "command_topic": "cvlc_tv/cvlc/play",
            "device": {
                "name":"Video Control Server",
                "identifiers":"CVLC_TV",
                "manufacturer":"PNXELEC",
                "model":"CVLC",
                "hw_version":"0.00",
                "sw_version":"0.00",
                "configuration_url":"http://localhost:5000"
            }
            }

payload_5 = {
            "name":"BTN_CVLC_STOP",
            "unique_id":"CVLC_STOP",
            "command_topic": "cvlc_tv/cvlc/stop",
            "device": {
                "name":"Video Control Server",
                "identifiers":"CVLC_TV",
                "manufacturer":"PNXELEC",
                "model":"CVLC",
                "hw_version":"0.00",
                "sw_version":"0.00",
                "configuration_url":"http://localhost:5000"
            }
            }

payload_6 = {
            "name":"SWITCH_LGTV_PWR",
            "unique_id":"LGTV_232_SWITCH",
            "state_topic": "cvlc_tv/lgtv/switch",
            "payload_on": "1",
            "payload_off": "0",
            "command_topic": "cvlc_tv/lgtv/switch/set",
            "state_on": "1",
            "state_off": "0",
            "device": {
                "name":"Video Control Server",
                "identifiers":"CVLC_TV",
                "manufacturer":"PNXELEC",
                "model":"CVLC",
                "hw_version":"0.00",
                "sw_version":"0.00",
                "configuration_url":"http://localhost:5000"
            }
            }

def on_message(client, userdata, message):
    print("message received ", str(message.payload.decode("utf-8")))
    print("message topic=", message.topic)
    print("message qos=", message.qos)
    print("message retain flag=", message.retain)
    # if(message.topic == "cvlc_tv/lgtv/status"):
    #     status_tv_power()
    if(message.topic == "cvlc_tv/lgtv/on"):
        on_tv_power()
    if(message.topic == "cvlc_tv/lgtv/off"):
        off_tv_power()
    if(message.topic == "cvlc_tv/cvlc/play"):
        create_m3u8_playlist()
        play_random_video()
    if(message.topic == "cvlc_tv/cvlc/stop"):
        stop_current_video()
    if(message.topic == "cvlc_tv/lgtv/switch/set"):
        if(str(message.payload.decode("utf-8")) == "1"):
            on_tv_power()
        if(str(message.payload.decode("utf-8")) == "0"):
            off_tv_power()


def publish_message(client, topic, message):
    client.publish(topic, message)
    print(f"Published message: {message} to topic: {topic}")

#     {
#   "name":"BTN_LGTV_232_ON",
#   "unique_id":"LGTV_232_ON",
#   "command_topic": "cvlc_tv/lgtv/on",
#   "device": {
#      "name":"Video Control Server",
#      "identifiers":"CVLC_TV",
#      "manufacturer":"PNXELEC",
#      "model":"CVLC",
#      "hw_version":"0.00",
#      "sw_version":"0.00",
#      "configuration_url":"http://localhost:5000"
#   }
# }

client = mqtt.Client(
    client_id="CVLC_TV",
    callback_api_version=mqtt.CallbackAPIVersion.VERSION1
)
client.username_pw_set("phoenix9469", "Phoenix1995!")
client.on_message = on_message

client.connect(broker_address,1883)
client.loop_start() 

client.subscribe("cvlc_tv/lgtv/status")
client.subscribe("cvlc_tv/lgtv/on")
client.subscribe("cvlc_tv/lgtv/off")
client.subscribe("cvlc_tv/cvlc/play")
client.subscribe("cvlc_tv/cvlc/stop")

client.subscribe("cvlc_tv/lgtv/switch/set")

def publish_ha_discovery():
    publish_message(client, discovery_topic_1, json.dumps(payload_1))
    publish_message(client, discovery_topic_2, json.dumps(payload_2))
    publish_message(client, discovery_topic_3, json.dumps(payload_3))
    publish_message(client, discovery_topic_4, json.dumps(payload_4))
    publish_message(client, discovery_topic_5, json.dumps(payload_5))
    publish_message(client, discovery_topic_6, json.dumps(payload_6))

publish_ha_discovery()

# 기본 설정
config = {
    "nas_folder": "/mv",
    "time": "07:00",
    "days": ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
}

# 스케줄러 설정
scheduler = BackgroundScheduler(daemon=True, timezone='Asia/Seoul')
scheduler.start()

# 설정 파일 로드
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)

# 영상 목록을 저장하는 변수
video_list = []

play_video = 0

# 현재 재생 중인 영상 프로세스
current_process = None





def get_video_files():
    """NAS 폴더에서 영상 파일 리스트 가져오기"""
    files = [os.path.join(config["nas_folder"], f) for f in os.listdir(config["nas_folder"])
             if f.lower().endswith(('.mp4', '.mkv', '.avi'))]
    return sorted(files, key=lambda x: os.path.basename(x).lower())  # 파일 이름을 알파벳 순으로 정렬

def create_m3u8_playlist():
    video_files = get_video_files()
    random.shuffle(video_files)
    output_m3u8_path = "/home/user/playlist.m3u8"
    with open(output_m3u8_path, 'w') as playlist:
        playlist.write("#EXTM3U\n")  # m3u8 파일의 헤더
        for file in video_files:
            playlist.write(f"#EXTINF:-1,{os.path.basename(file)}\n")
            playlist.write(f"{file}\n")
    print(f"m3u8 playlist created at {output_m3u8_path}")
    return

def play_random_video():
    global video_list, current_process, play_video
    # 영상 종료 후 다시 재생되도록 설정
    current_process = subprocess.Popen(["cvlc", "--fullscreen", "--play-and-exit","--no-osd","--audio-filter", "normvol" , "/home/user/playlist.m3u8"])
    #current_process.communicate()

def play_random_video_scheduled():
    create_m3u8_playlist()
    toggle_tv_power()
    global video_list, current_process, play_video
    # 영상 종료 후 다시 재생되도록 설정
    time.sleep(12) 
    current_process = subprocess.Popen(["cvlc", "--fullscreen", "--play-and-exit","--no-osd","--audio-filter", "normvol" , "/home/user/playlist.m3u8"])
    #current_process.communicate()

def play_single_video(video_path):
    """단일 영상 파일을 즉시 재생"""
    global current_process
    current_process = subprocess.Popen(["cvlc", "--fullscreen", "--play-and-exit","--no-osd","--audio-filter", "normvol" , video_path])
    #current_process.communicate()

def stop_current_video():
    """현재 재생 중인 영상 종료"""
    global current_process
    if current_process:
        current_process.terminate()
        current_process = None
        print("Video stopped.")

def status_tv_power():
    # command = "a"
    # ser.write(command.encode())
    #os.system('python3 LGTV.py --poweron')
    result = subprocess.run(['python3', 'libLGTV_serial/LGTV.py', '--powerstatus'],capture_output=True)
    if(result.stdout.decode().strip() == "b'00'"):
        publish_message(client, "cvlc_tv/lgtv/status","0")
        publish_message(client, "cvlc_tv/lgtv/switch","0")
    if(result.stdout.decode().strip() == "b'01'"):
        publish_message(client, "cvlc_tv/lgtv/status","1")
        publish_message(client, "cvlc_tv/lgtv/switch","0")
    """TV 전원 토글"""

def on_tv_power():
    # command = "a"
    # ser.write(command.encode())
    #os.system('python3 LGTV.py --poweron')
    result = subprocess.run(['python3', 'libLGTV_serial/LGTV.py', '--poweron'],capture_output=True)
    if(result.stdout.decode().strip() == "True"):
        publish_message(client, "cvlc_tv/lgtv/status","1")
        publish_message(client, "cvlc_tv/lgtv/switch","1")
        
    """TV 전원 토글"""

def off_tv_power():
    # command = "a"
    # ser.write(command.encode())
    #os.system('python3 LGTV.py --poweron')
    result = subprocess.run(['python3', 'libLGTV_serial/LGTV.py', '--poweroff'],capture_output=True)
    if(result.stdout.decode().strip() == "True"):
        publish_message(client, "cvlc_tv/lgtv/status","0")
        publish_message(client, "cvlc_tv/lgtv/switch","0")
    """TV 전원 토글"""
    
@app.template_filter('basename')
def basename_filter(path):
    return os.path.basename(path)

@app.route("/")
def index():
    """웹 UI 메인 페이지"""
    video_files = get_video_files()
    publish_ha_discovery()
    return render_template("index.html", config=config)

@app.route("/update", methods=["POST"])
def update_config():
    """설정 업데이트"""
    data = request.get_json()  # JSON 데이터 받기
    global config

    config["nas_folder"] = data.get("nas_folder", config["nas_folder"])
    config["time"] = data.get("time", config["time"])
    config["days"] = data.get("days", config["days"])
    config["second"] = data.get("second", config.get("second", 0))
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)
    schedule_task()  # 스케줄러 업데이트
    return jsonify({"status": "success", "config": config})

def schedule_task():
    """스케줄러 업데이트"""
    scheduler.remove_all_jobs()
    hour, minute = map(int, config["time"].split(":"))
    second = config.get("second", 0)
    if not config.get("days"):
        scheduler.remove_all_jobs()
        print("No Schedule")
        return
    target_time = datetime.strptime(f"{hour}:{minute}:{second}", "%H:%M:%S") - timedelta(seconds=12)
    scheduler.add_job(play_random_video_scheduled, 'cron', hour=target_time.hour, minute=target_time.minute, second=target_time.second,
                      day_of_week=','.join(config["days"]), misfire_grace_time=50)

@app.route("/on_tv", methods=["POST"])
def toggle_tv():
    """TV 전원 토글"""
    on_tv_power()
    return jsonify({"status": "success"})

@app.route("/off_tv", methods=["POST"])
def off_tv():
    """TV 전원 토글"""
    off_tv_power()
    return jsonify({"status": "success"})

@app.route("/play_now", methods=["POST"])
def play_now():
    """즉시 영상 재생"""
    create_m3u8_playlist()
    threading.Thread(target=play_random_video).start()
    return jsonify({"status": "success"})

@app.route("/stop_video", methods=["POST"])
def stop_video():
    """영상 종료"""
    threading.Thread(target=stop_current_video).start()
    global play_video
    play_video = 1
    return jsonify({"status": "success"})

@app.route("/file_list")
def file_list():
    """영상 파일 목록을 보여주는 페이지"""
    video_files = get_video_files()  # 영상 파일 목록 가져오기
    return render_template("file_list.html", video_files=video_files)

@app.route("/play_video",methods=["POST"])
def play_video():
    """즉시 영상 재생"""
    global current_process
    if current_process:
        current_process.terminate()
    video_path = request.form["video_path"]
    threading.Thread(target=play_single_video, args=(video_path,)).start()
    video_files = get_video_files()  # 영상 파일 목록 가져오기
    return render_template("file_list.html", video_files=video_files)

if __name__ == "__main__":
    schedule_task()
    app.run(host="0.0.0.0", port=5000)
