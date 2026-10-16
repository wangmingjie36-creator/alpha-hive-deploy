"""experiments/restore_snapshot_cs_rank_v0491.py：按「同一次扫描」从 git 历史找回 cs_rank / created_at。

夹具是两个临时 git 仓库（代码仓库 + 数据备份仓库），复刻生产里实际出现过的三种历史：
  · 同日重扫：run-1 → run-2 → 回填抹掉。必须取 run-2 的 cs_rank（取「最早非空」就错成 run-1）；
  · 只在备份仓库里有历史的文件；
  · 同身份却有两个不同 cs_rank ⇒ ambiguous，不补。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import restore_snapshot_cs_rank_v0491 as tool  # noqa: E402


def _snap(score, cs, created, t1=None, votes=None):
    return {"ticker": "X", "date": "2026-08-25", "composite_score": score, "direction": "bullish",
            "entry_price": 100.0, "agent_votes": votes or {"ScoutBeeNova": score},
            "cs_rank": cs, "actual_prices": {"t1": t1, "t7": None, "t30": None}, "created_at": created}


def _repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "report_snapshots").mkdir()
    return path


def _commit(repo, files, msg):
    for name, data in files.items():
        (repo / "report_snapshots" / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", msg], check=True)


RUN1 = _snap(6.18, {"final": 0.778}, "2026-08-25T14:16:40")
RUN2 = _snap(5.37, {"final": 0.448}, "2026-08-25T22:14:23")
WIPED_A = _snap(5.37, None, "2026-10-09T14:43:08", t1=101.0)

B_ORIG = dict(_snap(4.0, {"final": 0.1}, "2026-09-24T14:40:00"), ticker="B")
B_WIPED = dict(_snap(4.0, None, "2026-10-12T15:00:00", t1=99.0), ticker="B")

C_V1 = dict(_snap(5.0, {"final": 0.2}, "2026-08-25T14:00:00"), ticker="C")
C_V2 = dict(_snap(5.0, {"final": 0.3}, "2026-08-25T15:00:00"), ticker="C")   # 同身份、不同 cs_rank
C_WIPED = dict(_snap(5.0, None, "2026-10-09T14:00:00", t1=1.0), ticker="C")


@pytest.fixture
def world(tmp_path):
    code = _repo(tmp_path / "code")
    _commit(code, {"A.json": RUN1, "C.json": C_V1}, "scan run-1")
    _commit(code, {"A.json": RUN2, "C.json": C_V2}, "scan run-2")
    _commit(code, {"A.json": WIPED_A, "C.json": C_WIPED}, "backfill")
    bk = _repo(tmp_path / "bk")
    _commit(bk, {"B.json": B_ORIG}, "backup 09-24")
    _commit(bk, {"B.json": B_WIPED}, "backup 10-12")

    snaps = tmp_path / "data" / "report_snapshots"
    snaps.mkdir(parents=True)
    for name, data in {"A.json": WIPED_A, "B.json": B_WIPED, "C.json": C_WIPED}.items():
        (snaps / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    prod = tmp_path / "prod"
    prod.mkdir()
    (prod / "feedback_loop.py").write_text("# 旧代码\n", encoding="utf-8")
    sources = [(code, "HEAD", "report_snapshots"), (bk, "HEAD", "report_snapshots")]
    return {"code": code, "bk": bk, "snaps": snaps, "prod": prod, "sources": sources, "tmp": tmp_path}


def _args(w, *extra):
    return ["--snap-dir", str(w["snaps"]), "--code-repo", str(w["code"]), "--code-ref", "HEAD",
            "--backup-repo", str(w["bk"]), "--prod-clone", str(w["prod"]), *extra]


def test_plan_picks_same_scan_version_not_first(world):
    rows, stat = tool.plan(world["snaps"], world["sources"])
    by = {r["file"]: r for r in rows}
    assert by["A.json"]["cs_rank"] == {"final": 0.448}          # run-2，不是 run-1 的 0.778
    assert by["A.json"]["created_at"] == "2026-08-25T22:14:23"  # run-2 自己的生成时刻
    assert by["B.json"]["cs_rank"] == {"final": 0.1}            # 只在备份仓库里有历史
    assert by["B.json"]["created_at"] == "2026-09-24T14:40:00"
    assert by["C.json"]["cs_rank"] is None and "ambiguous" in by["C.json"]
    assert stat["cs_rank:AMBIGUOUS"] == 1 and stat["cs_rank:restore"] == 2


def test_dry_run_writes_nothing(world):
    before = {p.name: p.read_bytes() for p in world["snaps"].iterdir()}
    assert tool.main(_args(world)) == 0
    assert {p.name: p.read_bytes() for p in world["snaps"].iterdir()} == before


def test_apply_refuses_ambiguous(world):
    with pytest.raises(SystemExit, match="ambiguous"):
        tool.main(_args(world, "--apply", "--backup", str(world["tmp"] / "bak"),
                        "--expect-cs", "2", "--expect-created", "3"))


def _drop_c(world):
    (world["snaps"] / "C.json").unlink()


def test_apply_refuses_before_fix_is_deployed(world):
    _drop_c(world)
    with pytest.raises(SystemExit, match="还没有 v0.45.491"):
        tool.main(_args(world, "--apply", "--backup", str(world["tmp"] / "bak"),
                        "--expect-cs", "2", "--expect-created", "2"))
    assert not (world["tmp"] / "bak").exists()


def test_apply_refuses_wrong_expect(world):
    _drop_c(world)
    (world["prod"] / "feedback_loop.py").write_text(f"def {tool.FIX_MARKER}(): pass\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="不符"):
        tool.main(_args(world, "--apply", "--backup", str(world["tmp"] / "bak"),
                        "--expect-cs", "1", "--expect-created", "2"))


def test_apply_backs_up_writes_only_two_fields_and_is_idempotent(world):
    _drop_c(world)
    (world["prod"] / "feedback_loop.py").write_text(f"def {tool.FIX_MARKER}(): pass\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in world["snaps"].iterdir()}
    bak = world["tmp"] / "bak"
    assert tool.main(_args(world, "--apply", "--backup", str(bak),
                           "--expect-cs", "2", "--expect-created", "2")) == 0

    assert {p.name: p.read_bytes() for p in bak.iterdir()} == before
    a = json.loads((world["snaps"] / "A.json").read_text(encoding="utf-8"))
    assert a == dict(WIPED_A, cs_rank={"final": 0.448}, created_at="2026-08-25T22:14:23")

    rows, _ = tool.plan(world["snaps"], world["sources"])
    assert tool._todo(rows) == []

    from hive_logger import PATHS
    ledger = Path(PATHS.migrations_state) / "applied.jsonl"
    recs = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert recs[-1]["tool"] == tool.TOOL and recs[-1]["rows_affected"] == 2 and recs[-1]["backup"] == str(bak)


def test_concurrent_change_is_skipped(world):
    """计划之后、写之前文件被别人改了 ⇒ 跳过，不拿旧计划盖新值。"""
    _drop_c(world)
    rows, _ = tool.plan(world["snaps"], world["sources"])
    b = json.loads((world["snaps"] / "B.json").read_text(encoding="utf-8"))
    b["cs_rank"] = {"final": 0.9}
    (world["snaps"] / "B.json").write_text(json.dumps(b), encoding="utf-8")
    n_files, n_cs, n_ca, skipped = tool.apply(world["snaps"], rows)
    assert (n_files, skipped) == (1, 1)
    assert json.loads((world["snaps"] / "B.json").read_text(encoding="utf-8"))["cs_rank"] == {"final": 0.9}
