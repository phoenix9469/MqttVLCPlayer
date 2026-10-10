// 영상 목록의 코덱 표시: data-media 속성이 있는 줄의 .codec 칸을 채운다.
// 처음 보는 영상은 서버가 ffprobe로 읽어야 해서 느릴 수 있으므로 위에서부터 몇 개씩 나눠서 요청한다.
(function () {
    const list = document.querySelector('[data-media-kind]');
    if (!list) return;
    const kind = list.dataset.mediaKind;
    const rows = Array.from(list.querySelectorAll('[data-media]'));
    const BATCH = 8;

    function fill(row, info) {
        const cell = row.querySelector('.codec');
        if (!cell) return;
        if (!info) {
            cell.textContent = '정보 없음';
            cell.className = 'codec unknown';
            return;
        }
        cell.textContent = info.label;
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
