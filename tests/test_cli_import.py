"""`python -m trader tool …` 서브프로세스는 오케스트레이터 세션당 20여 회 뜬다. cli 모듈 import 가
pandas/pyarrow(0.6 초)를 끌어오지 않아야 한다 (수집 명령만 지연 import)."""

import subprocess
import sys


def test_cli_import_does_not_load_pandas():
    code = "import sys, trader.cli; print('pandas' in sys.modules, 'pyarrow' in sys.modules)"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=True,
    )
    assert r.stdout.strip() == "False False", r.stdout
