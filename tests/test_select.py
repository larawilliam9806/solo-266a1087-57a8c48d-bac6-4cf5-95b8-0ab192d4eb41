import json
import os
import secrets
import sqlite3
import sys
import tempfile
import threading

import pytest

if "app" not in sys.modules:
    os.environ["APP_DB"] = os.path.join(tempfile.mkdtemp(prefix="metrosel-"), "test.db")

import app as app_module  # noqa: E402

DB = app_module.DB_PATH

CSV = (
    "乘客ID,进站,出站\n"
    "p1,A,X\np2,A,X\np3,A,X\np4,A,X\np5,A,X\n"   # 候选0: 5 人
    "p6,B,X\n"                                    # 候选1: 1 人
    "p7,C,Z\np8,C,Z\np9,C,Z\n"                    # 候选2: 3 人
    "p10,D,W\np11,E,V\n"                          # 候选3: 2 人
)

CANDIDATES = [
    {"in_stations": ["A"], "out_stations": ["X"]},
    {"in_stations": ["B"], "out_stations": ["X"]},
    {"in_stations": ["C"], "out_stations": ["Z"]},
    {"in_stations": ["D", "E"], "out_stations": ["W", "V"]},
]


@pytest.fixture(autouse=True)
def clean_db():
    conn = sqlite3.connect(DB)
    conn.executescript(
        "DELETE FROM selections; DELETE FROM queries;"
        " DELETE FROM day_state; DELETE FROM passengers;")
    conn.close()
    yield


@pytest.fixture
def client():
    return app_module.app.test_client()


def upload(client, date, csv_text=CSV):
    return client.post("/upload", data={"date": date, "csv": csv_text})


def select(client, date, candidates=None, slots=1):
    return client.post("/select", json={
        "date": date,
        "candidates": CANDIDATES if candidates is None else candidates,
        "slots": slots,
    })


def query(client, date, ins, outs):
    return client.post("/query", json={"date": date, "in_stations": ins,
                                       "out_stations": outs})


def status(client, date):
    return client.get(f"/api/status?date={date}").get_json()


def selection_rows(date):
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(
            "SELECT request_key, slots, selected FROM selections WHERE date = ?",
            (date,)).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------- 页面与基本输出

def test_priority_page_renders(client):
    r = client.get("/priority")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "优先调查" in body and "/select" in body
    assert client.get("/").status_code == 200


def test_select_returns_only_selected_in_priority_order(client):
    upload(client, "2026-12-01")
    r = select(client, "2026-12-01", slots=2)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["cached"] is False
    assert len(body["selected"]) == 2
    assert body["selected_count"] == 2 and body["slots"] == 2
    for item in body["selected"]:
        assert set(item) == {"in_stations", "out_stations"}
        assert item["in_stations"] and item["out_stations"]

    text = json.dumps(body, ensure_ascii=False)
    assert "noisy_count" not in body and "scores" not in body
    assert "p1" not in text and "passenger" not in text.lower()
    # 选中项均来自候选集合(站点集合按规范化顺序返回)
    canon = {json.dumps({"in_stations": sorted(c["in_stations"]),
                         "out_stations": sorted(c["out_stations"])},
                        sort_keys=True, ensure_ascii=False)
             for c in CANDIDATES}
    for item in body["selected"]:
        assert json.dumps(item, sort_keys=True, ensure_ascii=False) in canon

    st = status(client, "2026-12-01")
    assert st["budget_spent"] == app_module.SELECTION_COST
    assert st["locked"] is True


def test_selected_items_are_distinct(client):
    upload(client, "2026-12-02")
    body = select(client, "2026-12-02", slots=3).get_json()
    keys = [json.dumps(item, sort_keys=True) for item in body["selected"]]
    assert len(keys) == len(set(keys)) == 3


# ------------------------------------------------------------- 请求规范化与重查

def test_reordered_and_duplicated_request_reuses_priority_order(client):
    upload(client, "2026-12-03")
    first = select(client, "2026-12-03", slots=2).get_json()
    assert first["cached"] is False

    shuffled = [
        {"in_stations": ["E", "D", "D"], "out_stations": ["V", "W"]},  # 集合内乱序/重复
        {"in_stations": ["B"], "out_stations": ["X"]},
        {"in_stations": ["A"], "out_stations": ["X"]},
        {"in_stations": ["A"], "out_stations": ["X"]},                 # 重复候选
        {"in_stations": ["C"], "out_stations": ["Z"]},
    ]
    second = select(client, "2026-12-03", candidates=shuffled, slots=2).get_json()
    assert second["cached"] is True
    assert second["selected"] == first["selected"]   # 原优先顺序
    assert status(client, "2026-12-03")["budget_spent"] == 0.5


def test_duplicate_candidates_do_not_change_identity(client):
    upload(client, "2026-12-04")
    a = select(client, "2026-12-04",
               candidates=[CANDIDATES[0], CANDIDATES[1], CANDIDATES[0]], slots=1)
    b = select(client, "2026-12-04",
               candidates=[CANDIDATES[1], CANDIDATES[0]], slots=1)
    assert a.status_code == 200 and b.status_code == 200
    assert b.get_json()["cached"] is True
    assert status(client, "2026-12-04")["budget_spent"] == 0.5


def test_different_slots_is_a_new_request(client):
    upload(client, "2026-12-05")
    r1 = select(client, "2026-12-05", slots=1).get_json()
    r2 = select(client, "2026-12-05", slots=2).get_json()
    assert r1["cached"] is False and r2["cached"] is False
    assert len(r1["selected"]) == 1 and len(r2["selected"]) == 2
    assert status(client, "2026-12-05")["budget_spent"] == 1.0


def test_list_form_candidate_shortcut(client):
    upload(client, "2026-12-06")
    r = client.post("/select", json={
        "date": "2026-12-06",
        "candidates": [[["A"], ["X"]], [["B"], ["X"]]],
        "slots": 1,
    })
    assert r.status_code == 200 and len(r.get_json()["selected"]) == 1


# ------------------------------------------------------------- 输入校验

@pytest.mark.parametrize("candidates,slots,needle", [
    ([CANDIDATES[0]], 1, "候选数量"),
    (CANDIDATES * 4, 1, "候选数量"),                       # 16 > 12
    ([CANDIDATES[0], {"in_stations": [], "out_stations": ["X"]}], 1, "不能为空"),
    ([CANDIDATES[0], {"in_stations": ["A"], "out_stations": " "}], 1, "不能为空"),
    ([CANDIDATES[0], {"in_stations": ["A"]}], 1, "不能为空"),
    ([CANDIDATES[0], "A-X"], 1, "格式不正确"),
    ([CANDIDATES[0], {"in_stations": ["A" * 101], "out_stations": ["X"]}], 1, "过长"),
    (CANDIDATES, 0, "名额"),
    (CANDIDATES, 4, "名额"),
    (CANDIDATES[:2], 3, "不能超过候选数量"),
    (CANDIDATES, "two", "名额"),
    (CANDIDATES, True, "名额"),
])
def test_invalid_inputs_rejected(client, candidates, slots, needle):
    upload(client, "2026-12-07")
    r = select(client, "2026-12-07", candidates=candidates, slots=slots)
    assert r.status_code == 400
    assert needle in r.get_json()["error"]
    st = status(client, "2026-12-07")
    assert st["budget_spent"] == 0.0 and not st["locked"]


def test_candidates_deduped_to_one_rejected(client):
    upload(client, "2026-12-08")
    r = select(client, "2026-12-08",
               candidates=[CANDIDATES[0], CANDIDATES[0], CANDIDATES[0]], slots=1)
    assert r.status_code == 400
    assert "至少需要 2 个" in r.get_json()["error"]


def test_missing_source_rejected(client):
    r = select(client, "2026-12-09")
    assert r.status_code == 409
    assert "缺少源数据" in r.get_json()["error"]
    st = status(client, "2026-12-09")
    assert st["budget_spent"] == 0 and not st["locked"]
    assert selection_rows("2026-12-09") == []


# ------------------------------------------------------------- 预算与源数据锁定

def test_selection_locks_source_data(client):
    upload(client, "2026-12-10")
    assert select(client, "2026-12-10").status_code == 200
    r = upload(client, "2026-12-10", "乘客ID,进站,出站\np1,A,X\n")
    assert r.status_code == 409
    assert "锁定" in r.get_json()["error"]


def test_selection_and_query_share_daily_budget(client):
    upload(client, "2026-12-11")
    assert query(client, "2026-12-11", ["A"], ["X"]).status_code == 200      # 0.5
    assert select(client, "2026-12-11", slots=2).status_code == 200          # 1.0
    assert query(client, "2026-12-11", ["B"], ["Y"]).status_code == 200      # 1.5
    assert select(client, "2026-12-11",
                  candidates=CANDIDATES[:3], slots=1).status_code == 200     # 2.0
    assert status(client, "2026-12-11")["budget_spent"] == 2.0

    # 额度用尽: 新的抽选被拒绝, 且不返回任何方向
    r = select(client, "2026-12-11", candidates=CANDIDATES[:3], slots=2)
    assert r.status_code == 403
    assert "预算不足" in r.get_json()["error"]
    assert "selected" not in r.get_json()
    assert status(client, "2026-12-11")["budget_spent"] == 2.0

    # 已发布的抽选仍可免费重查原顺序
    cached = select(client, "2026-12-11", slots=2)
    assert cached.status_code == 200 and cached.get_json()["cached"] is True


def test_budget_is_per_date(client):
    upload(client, "2026-12-12")
    upload(client, "2026-12-13")
    for i in range(4):
        cands = [{"in_stations": [f"P{i}a"], "out_stations": ["X"]},
                 {"in_stations": [f"P{i}b"], "out_stations": ["X"]}]
        assert select(client, "2026-12-12",
                      candidates=cands, slots=1).status_code == 200
    assert status(client, "2026-12-12")["budget_spent"] == 2.0
    assert select(client, "2026-12-13").status_code == 200


# ------------------------------------------------------------- 隐私机制

def test_multi_slot_selection_uses_total_epsilon_of_0_5(monkeypatch):
    """每个名额使用 ε/k 的指数机制, 整次抽选合计恰好 0.5(与名额数无关)."""
    seen = []

    def fake_select(scores, epsilon):
        seen.append(epsilon)
        return sorted(scores)[0]

    monkeypatch.setattr(app_module, "exponential_select", fake_select)
    for slots in (1, 2, 3):
        seen.clear()
        chosen = app_module.select_priority({0: 5.0, 1: 3.0, 2: 1.0}, slots)
        assert len(chosen) == slots == len(set(chosen))
        assert all(abs(e - 0.5 / slots) < 1e-12 for e in seen)
        assert abs(sum(seen) - app_module.SELECTION_EPSILON) < 1e-12
    assert app_module.SELECTION_EPSILON == 0.5


def test_larger_flow_selected_more_often():
    """客流差 10 的候选作为第一优先的几率显著高于客流小的候选."""
    trials = 5000
    wins = sum(1 for _ in range(trials)
               if app_module.select_priority({0: 10.0, 1: 0.0}, 1)[0] == 0)
    expected = 1.0 / (1.0 + 2.718281828459045 ** (-10.0 / 4.0))
    assert abs(wins / trials - expected) < 0.05


def test_selection_uses_cryptographic_rng():
    assert isinstance(app_module._rng, secrets.SystemRandom)
    values = {app_module.gumbel_noise(4.0) for _ in range(50)}
    assert len(values) > 1


# ------------------------------------------------------------- 并发与原子性

def test_concurrent_selects_never_overspend(client):
    upload(client, "2026-12-14")
    codes = []

    def worker(i):
        c = app_module.app.test_client()
        cands = [{"in_stations": [f"S{i}a"], "out_stations": ["X"]},
                 {"in_stations": [f"S{i}b"], "out_stations": ["X"]}]
        codes.append(select(c, "2026-12-14", candidates=cands, slots=1).status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(200) == 4 and codes.count(403) == 8
    assert status(client, "2026-12-14")["budget_spent"] == 2.0


def test_concurrent_identical_request_single_publish(client):
    upload(client, "2026-12-15")
    bodies = []

    def worker():
        c = app_module.app.test_client()
        bodies.append(select(c, "2026-12-15", slots=2).get_json())

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b["ok"] for b in bodies)
    assert len({json.dumps(b["selected"], sort_keys=True) for b in bodies}) == 1
    assert status(client, "2026-12-15")["budget_spent"] == 0.5
    assert len(selection_rows("2026-12-15")) == 1


def test_failed_publish_leaves_no_partial_state(client, monkeypatch):
    upload(client, "2026-12-16")
    upload(client, "2026-12-17", "乘客ID,进站,出站\np1,Q,R\n")

    def boom(scores, slots):
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(app_module, "select_priority", boom)
    r = select(client, "2026-12-16")
    assert r.status_code == 500

    st = status(client, "2026-12-16")
    assert st["budget_spent"] == 0 and not st["locked"]
    assert selection_rows("2026-12-16") == []
    # 未锁定, 源数据仍可替换
    assert upload(client, "2026-12-16").status_code == 200

    # 恢复后可正常发布
    monkeypatch.undo()
    ok = select(client, "2026-12-16")
    assert ok.status_code == 200 and ok.get_json()["cached"] is False
    assert status(client, "2026-12-16")["budget_spent"] == 0.5


def test_upload_and_selection_interleave_on_one_snapshot(client):
    """上传与新抽选交错时, 抽选结果必须对应上传成功后的数据或拒绝后的原数据."""
    old_csv = "乘客ID,进站,出站\n" + "".join(
        f"a{i},A,X\n" for i in range(60)) + "".join(f"b{i},B,Y\n" for i in range(60))
    new_csv = "乘客ID,进站,出站\n" + "".join(
        f"a{i},A,Y\n" for i in range(60)) + "".join(f"b{i},B,X\n" for i in range(60))
    cand_a = {"in_stations": ["A"], "out_stations": ["X"]}
    cand_b = {"in_stations": ["B"], "out_stations": ["X"]}
    # 旧数据: A→X 60 人, B→X 0 人; 新数据相反
    for trial in range(8):
        date = f"2026-12-2{trial}"
        assert upload(client, date, old_csv).status_code == 200
        outcome = {}

        def do_upload():
            c = app_module.app.test_client()
            outcome["upload"] = c.post(
                "/upload", data={"date": date, "csv": new_csv}).status_code

        def do_select():
            c = app_module.app.test_client()
            outcome["select"] = select(
                c, date, candidates=[cand_a, cand_b], slots=1).get_json()

        t1 = threading.Thread(target=do_upload)
        t2 = threading.Thread(target=do_select)
        t1.start(); t2.start(); t1.join(); t2.join()

        chosen = outcome["select"]["selected"][0]
        if outcome["upload"] == 200:
            assert chosen == cand_b       # 抽选使用的是新数据
            assert status(client, date)["rows"] == 120
        else:
            assert outcome["upload"] == 409
            assert chosen == cand_a       # 抽选使用的是原数据
            assert status(client, date)["rows"] == 120
        # 扣费与锁定只发生一次
        assert status(client, date)["budget_spent"] == 0.5
        assert status(client, date)["locked"] is True


def test_restart_replays_priority_order(client):
    upload(client, "2026-12-18")
    first = select(client, "2026-12-18", slots=3).get_json()
    app_module.init_db()
    again = select(app_module.app.test_client(), "2026-12-18", slots=3).get_json()
    assert again["cached"] is True and again["selected"] == first["selected"]
    assert status(client, "2026-12-18")["budget_spent"] == 0.5


def test_selection_history_endpoint(client):
    upload(client, "2026-12-19")
    select(client, "2026-12-19", slots=2)
    body = client.get("/api/selections?date=2026-12-19").get_json()
    assert body["ok"] and len(body["selections"]) == 1
    assert len(body["selections"][0]["selected"]) == 2
