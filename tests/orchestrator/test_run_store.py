from datetime import date

from trader.orchestrator import run_store


def test_run_dir_layout(project):
    p = run_store.run_dir(date(2026, 10, 9), "KR", "005930")
    assert p == project / "state" / "runs" / "2026-10-09" / "KR" / "005930" and p.is_dir()


def test_write_and_read_checkpoint(project):
    p = run_store.run_dir(date(2026, 10, 9), "KR", "005930")
    run_store.write_checkpoint(p, "00_inputs", {"가": 1})
    assert (p / "00_inputs.json").read_text(encoding="utf-8").count("가") == 1
    assert run_store.read_checkpoint(p, "00_inputs") == {"가": 1}
    assert run_store.read_checkpoint(p, "05_quant") is None
    assert not list(p.glob("*.tmp"))


def test_list_checkpoints(project):
    p = run_store.run_dir(date(2026, 10, 9), "KR", "005930")
    run_store.write_checkpoint(p, "10_analyst_technical", {})
    run_store.write_checkpoint(p, "00_inputs", {})
    assert run_store.list_checkpoints(p) == ["00_inputs", "10_analyst_technical"]
