const $ = id => document.getElementById(id);
const errors = { selection_unverified:'화면의 선택 상태를 확인하지 못했습니다', begin_failed: '수집 작업을 시작하지 못했습니다', lane_busy: '이전 수집 작업의 종료를 기다립니다', state_failed: '호스트에 진행 상태를 전달하지 못했습니다', navigation_failed: '전용 탭을 열지 못했습니다', content_unavailable: '전용 탭의 화면에 연결하지 못했습니다', snapshot_failed: '화면의 선택 상태 또는 저장 요청을 확인하지 못했습니다', postprocess_failed: '수집 후 처리를 마치지 못했습니다', job_release_failed: '실패한 수집 작업을 정리하지 못했습니다', native_tick_failed: '호스트 연결을 확인해 주세요' };
const laneNames = { news: '뉴스', community: '커뮤니티', market: '증시', background: '캘린더·리포트' };
async function render() {
    const s = await chrome.storage.local.get(null);
    const workerVersion=typeof s.workerVersion==='string'&&/^\d+\.\d+\.\d+$/.test(s.workerVersion)?s.workerVersion:'미확인';$('version').textContent='확장 1.0.2 · 수집기 '+workerVersion+(workerVersion==='1.0.2'?'':' · 확장 새로고침 필요');
    $('connection').textContent = s.connection || (s.enabled ? '연결 대기' : '중지됨');
    for (const lane of ['news', 'community', 'market', 'background'])
        $(lane).textContent = s['lane_' + lane] || '대기';
    $('diagnostics').replaceChildren();
    for (const [lane, name] of Object.entries(laneNames)) {
        for (const key of ['lane_error_' + lane, 'lane_release_error_' + lane]) {
            const code = s[key];
            if (errors[code]) {
                const li = document.createElement('li');
                li.textContent = name + ': ' + errors[code] + ' (' + code + ')';
                $('diagnostics').append(li);
            }
        }
    }
    if (!$('diagnostics').children.length) {
        const li = document.createElement('li');
        li.textContent = '현재 기록된 수집 오류가 없습니다.';
        $('diagnostics').append(li);
    }
    $('updated').textContent = s.lastCollected ? '최근 수집 ' + new Date(s.lastCollected).toLocaleString('ko-KR') : '아직 수집 기록이 없습니다.';
    $('start').disabled = !!s.enabled;
    $('stop').disabled = !s.enabled;
    $('resume').disabled = false;
}
for (const action of ['start', 'stop', 'resume'])
    $(action).addEventListener('click', async () => {
        for (const b of document.querySelectorAll('button'))
            b.disabled = true;
        try {
            await chrome.runtime.sendMessage({ action });
        }
        finally {
            await render();
        }
    });
chrome.storage.onChanged.addListener(render);
void render();
