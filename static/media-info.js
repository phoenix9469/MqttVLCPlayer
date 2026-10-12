// 영상 목록의 코덱·재생 시간·용량 표시: data-media 속성이 있는 줄의 .codec 칸을 채우고 시간과 용량도 함께 넣는다.
// 처음 보는 영상은 서버가 ffprobe로 읽어야 해서 느릴 수 있으므로 위에서부터 몇 개씩 나눠서 요청한다.
(function () {
    const list = document.querySelector('[data-media-kind]');
    if (!list) return;
    const kind = list.dataset.mediaKind;
    const rows = Array.from(list.querySelectorAll('[data-media]'));
    const BATCH = 8;

    function formatTime(seconds) {
        seconds = Math.round(seconds);
        const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
        return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
    }

    function formatSize(bytes) {
        if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(bytes >= 10 * 1024 ** 3 ? 0 : 1) + 'GB';
        return Math.max(1, Math.round(bytes / 1024 ** 2)) + 'MB';
    }

    function fill(row, info) {
        const cell = row.querySelector('.codec');
        if (!cell) return;
        if (!info) {
            cell.textContent = '정보 없음';
            cell.className = 'codec unknown';
            return;
        }
        // 재생 시간 · 용량 · 코덱 · 해상도
        cell.textContent = [info.duration ? formatTime(info.duration) : '', info.size ? formatSize(info.size) : '', info.label]
            .filter(Boolean).join(' · ');
        cell.className = 'codec' + (info.hw === false ? ' cpu' : info.hw ? ' hw' : '');
        cell.title = info.hw === false ? '이 기기에서는 하드웨어 디코딩이 안 되어 CPU로 재생합니다'
                   : info.hw ? '하드웨어 디코딩' : '';
    }

    async function load() {
        for (let i = 0; i < rows.length; i += BATCH) {
            const batch = rows.slice(i, i + BATCH);
            try {
                const response = await fetch('/media/info', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ kind: kind, items: batch.map(row => row.dataset.media) })
                });
                const infos = await response.json();
                batch.forEach(row => fill(row, infos[row.dataset.media]));
            } catch (error) {
                console.error('코덱 정보 오류:', error);
                return;
            }
        }
    }
    load();
})();
