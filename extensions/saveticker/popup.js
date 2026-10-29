const $ = id => document.getElementById(id);
const errors = { window_minimized: '전용 Chrome 창을 복원한 뒤 기다려 주세요', tab_activation_failed: '전용 탭을 활성화하지 못했습니다', selection_unverified: '화면의 선택 상태를 확인하지 못했습니다', begin_failed: '수집 작업을 시작하지 못했습니다', lane_busy: '이전 수집 작업의 종료를 기다립니다', state_failed: '호스트에 진행 상태를 전달하지 못했습니다', navigation_failed: '전용 탭을 열지 못했습니다', content_unavailable: '전용 탭의 화면에 연결하지 못했습니다', snapshot_failed: '화면의 선택 상태 또는 저장 요청을 확인하지 못했습니다', postprocess_failed: '수집 후 처리를 마치지 못했습니다', job_release_failed: '실패한 수집 작업을 정리하지 못했습니다', native_tick_failed: '호스트 연결을 확인해 주세요' };
const pdfStates = { handoff_aborted: 'PDF 연결이 취소되었습니다', waiting_hook: 'PDF 연결 준비 중', hook_not_ready: 'PDF 연결 도우미가 준비되지 않았습니다', url_unverified: 'PDF 주소를 확인하지 못했습니다', starting: 'PDF 다운로드 시작 중', downloading: 'PDF 다운로드 중', saved: 'PDF 저장 완료', download_start_failed: 'PDF 다운로드를 시작하지 못했습니다', download_interrupted: 'PDF 다운로드가 중단되었습니다', native_failed: 'PDF 저장 검증을 통과하지 못했습니다', filename_unverified: 'PDF 파일명을 확인하지 못했습니다', pdf_begin_busy: 'PDF 발급 대기: 이전 요청의 유효 시간이 남아 있습니다', pdf_begin_unknown_report: 'PDF 발급 실패: 현재 리포트를 확인하지 못했습니다', pdf_begin_cached: 'PDF 발급 생략: 이미 저장된 파일입니다', pdf_begin_failed: 'PDF 발급 단계에서 실패했습니다', pdf_save_ticket: 'PDF 저장 실패: 발급된 다운로드 요청을 확인하지 못했습니다', pdf_save_path: 'PDF 저장 실패: 발급된 파일 경로를 확인하지 못했습니다', pdf_save_invalid: 'PDF 저장 실패: 유효한 PDF 파일이 아닙니다', pdf_save_failed: 'PDF 저장 검증 단계에서 실패했습니다' };
const laneNames = { news: '뉴스', community: '커뮤니티', market: '증시', background: '캘린더·리포트' };
async function render() {
    const s = await chrome.storage.local.get(null);
    const knownVersion = typeof s.workerVersion === 'string' && /^\d+\.\d+\.\d+$/.test(s.workerVersion);
    const workerVersion = knownVersion ? s.workerVersion : s.enabled ? '연결 중' : '시작 전';
    $('version').textContent = '확장 1.0.7 · 수집기 ' + workerVersion + (knownVersion && workerVersion !== '1.0.7' ? ' · 확장 새로고침 필요' : '');
    $('pdf-state').textContent = Object.hasOwn(pdfStates, s.pdfState) ? pdfStates[s.pdfState] + ' (' + s.pdfState + ')' : '';
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
