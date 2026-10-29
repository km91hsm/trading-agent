const $ = id => document.getElementById(id);
async function render() { const s = await chrome.storage.local.get(null); $('connection').textContent = s.connection || (s.enabled ? '연결 대기' : '중지됨'); for (const lane of ['news', 'community', 'market', 'background'])
    $(lane).textContent = s['lane_' + lane] || '대기'; $('updated').textContent = s.lastCollected ? '최근 수집 ' + new Date(s.lastCollected).toLocaleString('ko-KR') : '아직 수집 기록이 없습니다.'; $('start').disabled = !!s.enabled; $('stop').disabled = !s.enabled; }
for (const action of ['start', 'stop', 'resume'])
    $(action).addEventListener('click', async () => { for (const b of document.querySelectorAll('button'))
        b.disabled = true; try {
        await chrome.runtime.sendMessage({ action });
    }
    finally {
        await render();
    } });
chrome.storage.onChanged.addListener(render);
void render();
