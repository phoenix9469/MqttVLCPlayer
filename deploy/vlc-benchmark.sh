#!/bin/bash
# VLC 재생 설정 비교: 같은 영상을 설정별로 재생하며 CPU 사용률, 하드웨어 디코딩 여부, 늦은/버린 프레임 수를 표시한다.
# 사용법: bash deploy/vlc-benchmark.sh <영상 파일> [재생 초(기본 30)]
FILE="$1"
SECONDS_PER_RUN="${2:-30}"
if [ ! -f "$FILE" ]; then
    echo "사용법: bash $0 <영상 파일> [재생 초]" >&2
    exit 1
fi
LOG_DIR="$(mktemp -d)"
TICKS=$(getconf CLK_TCK)

CONFIGS=(
    "기본(VLC 자동)|"
    "하드웨어 끔(CPU)|--avcodec-hw=none"
    "VA-API + OpenGL|--avcodec-hw=vaapi --vout=gl"
    "VA-API + XVideo|--avcodec-hw=vaapi --vout=xcb_xv"
    "VA-API + X11|--avcodec-hw=vaapi --vout=xcb_x11"
)

cpu_ticks() { awk '{print $14 + $15}' "/proc/$1/stat" 2>/dev/null; }

if command -v ffprobe >/dev/null; then
    echo "파일 정보: $(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,r_frame_rate -of csv=p=0 "$FILE")" \
         "/ 오디오: $(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,channels -of csv=p=0 "$FILE")"
fi
printf "%-20s %8s  %-8s %-10s %s\n" "설정" "CPU(%)" "화면" "HW디코딩" "늦은/버린 프레임"
i=0
for entry in "${CONFIGS[@]}"; do
    name="${entry%%|*}"; opts="${entry#*|}"; log="$LOG_DIR/$i.log"; i=$((i + 1))
    # shellcheck disable=SC2086
    cvlc -v --fullscreen --no-osd --play-and-exit --run-time="$SECONDS_PER_RUN" $opts "$FILE" > "$log" 2>&1 &
    pid=$!
    sleep 3   # 시작 직후(파일 열기, 초기화)는 빼고 측정
    t0=$(cpu_ticks $pid); s0=$(date +%s.%N); t1=""; s1=""
    while kill -0 $pid 2>/dev/null; do
        sleep 1
        v=$(cpu_ticks $pid)
        # 종료된 뒤 읽은 값은 버리고, 살아 있을 때의 마지막 값만 사용
        if [ -n "$v" ]; then t1=$v; s1=$(date +%s.%N); fi
    done
    wait $pid 2>/dev/null
    cpu=$(awk -v a="$t0" -v b="$t1" -v s="$s0" -v e="$s1" -v hz="$TICKS" 'BEGIN { d = e - s; if (a == "" || b == "" || d <= 0 || b < a) print "-"; else printf "%.0f", (b - a) / hz / d * 100 }')
    if grep -qiE "for hardware decoding|Using .*va-?api|vaapi.*(direct|copy)" "$log"; then hw="사용"; else hw="안 씀"; fi
    late=$(grep -ciE "too late|picture might be displayed late|dropp" "$log")
    # 화면 출력을 만들지 못하면 CPU가 낮게 나와도 의미가 없음
    if grep -qiE "video output creation failed|failed to create video output" "$log"; then out="실패"; else out="OK"; fi
    printf "%-20s %8s  %-8s %-10s %s\n" "$name" "$cpu" "$out" "$hw" "$late"
done
echo
echo "로그: $LOG_DIR (설정 순서대로 0.log, 1.log ...)"
echo "화면이 OK이고, CPU가 낮고, 늦은/버린 프레임이 적은 설정을 VLC_EXTRA_ARGS에 넣으세요."
echo "(화면이 '실패'인 설정은 그 출력 방식을 쓸 수 없는 것이니 제외)"
