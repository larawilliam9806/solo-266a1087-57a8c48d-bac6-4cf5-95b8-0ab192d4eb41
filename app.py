"""地铁首程客流差分隐私查询服务.

- 按日期上传 CSV(乘客ID, 进站, 出站), 每位乘客仅一行
- 选择进/出站集合查询匹配人数, 结果加 Laplace(0, 2) 噪声(安全随机源)
- 从 2~12 个候选中按客流大小用指数机制抽选 1~3 个优先调查方向
  (逐名额 Gumbel-max 顺序选择, 每个名额 ε/k, 整次抽选合计 ε=0.5; 只输出选中方向)
- 每次新查询/新抽选扣 0.5 隐私预算, 每日(每个数据日期)总额 2, 两者共享
- 相同请求(候选及站点与顺序、重复无关)复用原结果/原优先顺序, 不再扣费
- 首个成功发布锁定当天源数据, 之后拒绝替换或缺失源数据
- 扣费 / 保存结果 / 锁定源数据在同一事务中, 要么全部成功要么全部不生效
"""

import csv
import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("APP_DB", os.path.join(BASE_DIR, "app.db"))

DAILY_BUDGET = 2.0        # 每日隐私预算总额
QUERY_COST = 0.5          # 每次新查询的预算开销
NOISE_SCALE = 2.0         # Laplace(0, 2)

SELECTION_COST = 0.5      # 每次新抽选的预算开销(与查询共享每日额度)
SELECTION_EPSILON = 0.5   # 整次抽选(无论几个名额)的 ε
MIN_CANDIDATES = 2
MAX_CANDIDATES = 12
MAX_SLOTS = 3
MAX_RAW_CANDIDATES = 200  # 去重前的输入上限, 防止异常大请求
MAX_STATIONS_PER_SIDE = 200  # 单个候选每侧站点数上限
MAX_STATION_LEN = 100        # 单个站点名长度上限
COUNT_SENSITIVITY = 1.0   # 增删一名乘客对任一候选人数的影响至多 1

app = Flask(__name__)
_rng = secrets.SystemRandom()  # 安全随机源

SCHEMA = """
CREATE TABLE IF NOT EXISTS passengers (
    date         TEXT NOT NULL,
    passenger_id TEXT NOT NULL,
    in_station   TEXT NOT NULL,
    out_station  TEXT NOT NULL,
    PRIMARY KEY (date, passenger_id)
);

CREATE TABLE IF NOT EXISTS day_state (
    date         TEXT PRIMARY KEY,
    locked       INTEGER NOT NULL DEFAULT 0,
    budget_spent REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS queries (
    date         TEXT NOT NULL,
    in_key       TEXT NOT NULL,
    out_key      TEXT NOT NULL,
    in_stations  TEXT NOT NULL,
    out_stations TEXT NOT NULL,
    noisy_count  REAL NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (date, in_key, out_key)
);

CREATE TABLE IF NOT EXISTS selections (
    date         TEXT NOT NULL,
    request_key  TEXT NOT NULL,
    candidates   TEXT NOT NULL,   -- 规范化后的候选(站点集合已排序去重)
    count        INTEGER NOT NULL,
    slots        INTEGER NOT NULL,
    selected     TEXT NOT NULL,   -- 按优先顺序的候选下标列表(0 起)
    created_at   TEXT NOT NULL,
    PRIMARY KEY (date, request_key)
);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


init_db()


def get_db():
    """每个请求一个连接; autocommit 模式, 事务用显式 BEGIN IMMEDIATE."""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------- 工具函数

def laplace_noise(scale=NOISE_SCALE):
    """用安全随机源生成 Laplace(0, scale) 噪声(逆变换采样)."""
    u = _rng.random() - 0.5  # [-0.5, 0.5)
    if u == 0.0:
        return 0.0
    return -scale * math.copysign(1.0, u) * math.log1p(-2.0 * abs(u))


def uniform_open_unit():
    """安全随机源产生 (0, 1) 开区间内的均匀随机数, 供 Gumbel 变换使用."""
    while True:
        r = _rng.random()
        if 0.0 < r < 1.0:
            return r


def normalize_date(raw):
    """校验并规范化日期为 YYYY-MM-DD; 非法返回 None."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def canon_key(stations):
    """站点集合的规范化表示: 去空白、去重、排序, 与顺序和重复选择无关."""
    return json.dumps(sorted({s.strip() for s in stations if s and s.strip()}),
                      ensure_ascii=False)


def parse_station_list(value):
    """接受 JSON 数组或分隔符(逗号/顿号/分号/空白/换行)分隔的字符串."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        text = str(value)
        for sep in ("，", "、", "；", ";", "\n", "\t", " "):
            text = text.replace(sep, ",")
        items = text.split(",")
    seen, out = set(), []
    for item in items:
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse_candidates(value):
    """解析候选列表, 返回 [(进站集合, 出站集合), ...](保留输入顺序).

    每个候选可以是 {"in_stations": [...], "out_stations": [...]}
    (也接受 in/out 简写), 或 [进站集合, 出站集合] 两元数组.
    集合不能为空, 否则抛出 ValueError.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError("candidates 必须是 JSON 数组")
    if not isinstance(value, (list, tuple)):
        raise ValueError("candidates 必须是数组")
    if len(value) > MAX_RAW_CANDIDATES:
        raise ValueError(f"候选数量不能超过 {MAX_RAW_CANDIDATES} 个")

    result = []
    for idx, item in enumerate(value, 1):
        if isinstance(item, dict):
            in_raw = item.get("in_stations", item.get("in"))
            out_raw = item.get("out_stations", item.get("out"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            in_raw, out_raw = item
        else:
            raise ValueError(f"第 {idx} 个候选格式不正确"
                             "(应为 {in_stations, out_stations} 或 [进站集合, 出站集合])")
        ins = parse_station_list(in_raw)
        outs = parse_station_list(out_raw)
        if not ins or not outs:
            raise ValueError(f"第 {idx} 个候选的进站集合与出站集合均不能为空")
        if len(ins) > MAX_STATIONS_PER_SIDE or len(outs) > MAX_STATIONS_PER_SIDE:
            raise ValueError(f"第 {idx} 个候选每侧站点数不能超过 {MAX_STATIONS_PER_SIDE}")
        if any(len(s) > MAX_STATION_LEN for s in (*ins, *outs)):
            raise ValueError(f"第 {idx} 个候选存在过长的站点名(>{MAX_STATION_LEN} 字符)")
        result.append((ins, outs))
    return result


def canonical_candidates(candidates):
    """候选去重并规范化为与输入顺序、重复无关的形式.

    返回 (candidates, request_key):
    - candidates: [{"in_stations": [...], "out_stations": [...]}, ...],
      站点集合已排序去重, 候选按 (进站集, 出站集) 排序
    - request_key: 基于规范化候选的 SHA-256 摘要

    去重后仍保留每个候选内部的原始先后顺序是无意义的,
    因此响应中的站点集合也总是规范化顺序; 抽选的优先顺序另存.
    """
    unique = {}
    for ins, outs in candidates:
        in_key, out_key = canon_key(ins), canon_key(outs)
        unique[(in_key, out_key)] = None
    ordered = sorted(unique)
    canonical = [{"in_stations": json.loads(in_key),
                  "out_stations": json.loads(out_key)}
                 for in_key, out_key in ordered]
    material = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return canonical, hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_slots(value, candidate_count):
    """名额必须是 1~3 的整数, 且不超过(去重后的)候选数."""
    if isinstance(value, bool):
        raise ValueError("名额必须是整数")
    if isinstance(value, int):
        slots = value
    elif isinstance(value, float) and value.is_integer():
        slots = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        slots = int(value.strip())
    else:
        raise ValueError("名额必须是整数")
    if not 1 <= slots <= MAX_SLOTS:
        raise ValueError(f"名额必须在 1~{MAX_SLOTS} 之间")
    if slots > candidate_count:
        raise ValueError("名额不能超过候选数量")
    return slots


def gumbel_noise(scale):
    """用安全随机源生成 Gumbel(0, scale) 噪声(Gumbel-max 采样)."""
    u = uniform_open_unit()
    return -scale * math.log(-math.log(u))


def exponential_select(scores, epsilon):
    """指数机制: 从 {下标: 分数} 中选一个分数较大的下标.

    一名乘客可能同时命中多个候选, 但对任一候选的人数影响至多为 1,
    因此单次选择的分数敏感度为 COUNT_SENSITIVITY;
    Gumbel-max 采样的分布与 exp(epsilon * q / (2 * Δ)) 成正比,
    该次选择满足 epsilon-差分隐私.
    """
    scale = 2.0 * COUNT_SENSITIVITY / epsilon
    best_idx, best_value = None, None
    for idx in sorted(scores):
        value = scores[idx] + gumbel_noise(scale)
        if best_value is None or value > best_value:
            best_idx, best_value = idx, value
    return best_idx


def select_priority(scores, slots):
    """按分数从大到小抽取 slots 个互不重复的下标(按优先顺序).

    逐名额选择, 每个名额使用 epsilon = SELECTION_EPSILON / slots;
    每个名额的发布都是对同一份数据的一次 epsilon/slots-差分隐私机制,
    依据基本组合定理, 整次抽选(无论几个名额)合计至多 SELECTION_EPSILON,
    多名额共用同一份 0.5 预算. 该过程等价于以「按优先顺序的 k 元组」
    为值域、分数为各候选人数之和的指数机制.
    """
    per_slot_epsilon = SELECTION_EPSILON / slots
    remaining = dict(scores)
    chosen = []
    while remaining and len(chosen) < slots:
        idx = exponential_select(remaining, per_slot_epsilon)
        chosen.append(idx)
        del remaining[idx]
    return chosen


def count_candidate(conn, date, in_stations, out_stations):
    """候选命中的乘客数(进站在集合内 且 出站在集合内)."""
    ph_in = ",".join("?" for _ in in_stations)
    ph_out = ",".join("?" for _ in out_stations)
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM passengers"
        f" WHERE date = ? AND in_station IN ({ph_in})"
        f" AND out_station IN ({ph_out})",
        [date, *in_stations, *out_stations]).fetchone()["c"]


def source_exists(conn, date):
    return conn.execute(
        "SELECT 1 FROM passengers WHERE date = ? LIMIT 1", (date,)).fetchone() is not None


HEADER_ALIASES = {
    "passenger": {"乘客id", "passenger_id", "passengerid", "passenger", "id", "乘客编号"},
    "in": {"进站", "in_station", "instation", "entry", "entry_station", "入站", "进站站点"},
    "out": {"出站", "out_station", "outstation", "exit", "exit_station", "出站站点"},
}


def parse_csv_text(text):
    """解析 CSV 文本, 返回 [(passenger_id, in_station, out_station), ...].

    支持带表头(中/英文列名)或无表头(按 乘客ID,进站,出站 顺序)两种格式.
    乘客ID 重复或字段为空时抛出 ValueError.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError("CSV 内容为空")

    first = [c.strip().lower() for c in rows[0]]
    col_idx = {}
    for field, aliases in HEADER_ALIASES.items():
        for i, name in enumerate(first):
            if name in aliases:
                col_idx[field] = i
                break

    has_header = len(col_idx) == 3
    if not has_header:
        if col_idx:
            raise ValueError("CSV 表头无法完整识别, 请使用: 乘客ID, 进站, 出站")
        if len(first) < 3:
            raise ValueError("CSV 至少需要 3 列: 乘客ID, 进站, 出站")
        col_idx = {"passenger": 0, "in": 1, "out": 2}

    data_rows = rows[1:] if has_header else rows
    result, seen = [], set()
    for offset, r in enumerate(data_rows):
        lineno = offset + (2 if has_header else 1)
        if max(col_idx.values()) >= len(r):
            raise ValueError(f"第 {lineno} 行列数不足")
        pid = r[col_idx["passenger"]].strip()
        in_s = r[col_idx["in"]].strip()
        out_s = r[col_idx["out"]].strip()
        if not pid or not in_s or not out_s:
            raise ValueError(f"第 {lineno} 行存在空字段")
        if pid in seen:
            raise ValueError(f"乘客ID 重复: {pid}(每位乘客仅允许一行)")
        seen.add(pid)
        result.append((pid, in_s, out_s))
    if not result:
        raise ValueError("CSV 无有效数据行")
    return result


def get_payload():
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            return body
    return request.form


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return render_template("index.html",
                           daily_budget=DAILY_BUDGET, query_cost=QUERY_COST,
                           selection_cost=SELECTION_COST,
                           selection_epsilon=SELECTION_EPSILON,
                           min_candidates=MIN_CANDIDATES,
                           max_candidates=MAX_CANDIDATES,
                           max_slots=MAX_SLOTS)


@app.get("/priority")
def priority_page():
    return render_template("select.html",
                           daily_budget=DAILY_BUDGET, query_cost=QUERY_COST,
                           selection_cost=SELECTION_COST,
                           selection_epsilon=SELECTION_EPSILON,
                           min_candidates=MIN_CANDIDATES,
                           max_candidates=MAX_CANDIDATES,
                           max_slots=MAX_SLOTS)


# ---------------------------------------------------------------- 上传

@app.post("/upload")
def upload():
    payload = get_payload()
    date = normalize_date(payload.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确(应为 YYYY-MM-DD)"), 400

    text = None
    file = request.files.get("file")
    if file is not None and file.filename:
        try:
            text = file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify(ok=False, error="CSV 文件编码必须是 UTF-8"), 400
    elif payload.get("csv"):
        text = payload.get("csv")
    if text is None:
        return jsonify(ok=False, error="请上传 CSV 文件或提供 csv 文本"), 400

    try:
        rows = parse_csv_text(text)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute(
            "SELECT locked FROM day_state WHERE date = ?", (date,)).fetchone()
        if state and state["locked"]:
            conn.execute("ROLLBACK")
            return jsonify(ok=False,
                         error=f"{date} 的源数据已被首个成功查询或抽选锁定,"
                               " 拒绝替换"), 409
        # 未锁定: 整日替换
        conn.execute("DELETE FROM passengers WHERE date = ?", (date,))
        conn.executemany(
            "INSERT INTO passengers (date, passenger_id, in_station, out_station)"
            " VALUES (?, ?, ?, ?)",
            [(date, pid, in_s, out_s) for pid, in_s, out_s in rows])
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return jsonify(ok=True, date=date, rows=len(rows),
                   message=f"已替换 {date} 的源数据, 共 {len(rows)} 行")


# ---------------------------------------------------------------- 查询

@app.post("/query")
def query():
    payload = get_payload()
    date = normalize_date(payload.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确(应为 YYYY-MM-DD)"), 400

    in_stations = parse_station_list(payload.get("in_stations"))
    out_stations = parse_station_list(payload.get("out_stations"))
    if not in_stations or not out_stations:
        return jsonify(ok=False, error="进站集合与出站集合均不能为空"), 400

    in_key, out_key = canon_key(in_stations), canon_key(out_stations)
    conn = get_db()

    # 快速路径: 已存在的结果直接复用, 不扣费(只读, 无需写事务)
    cached = conn.execute(
        "SELECT noisy_count, in_stations, out_stations FROM queries"
        " WHERE date = ? AND in_key = ? AND out_key = ?",
        (date, in_key, out_key)).fetchone()
    if cached:
        return jsonify(ok=True, date=date,
                       in_stations=json.loads(cached["in_stations"]),
                       out_stations=json.loads(cached["out_stations"]),
                       noisy_count=cached["noisy_count"], cached=True)

    # 新查询: 扣费 + 保存结果 + 锁定源数据必须在同一事务中
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 事务内复查, 保证并发下相同查询只生成一份结果
        cached = conn.execute(
            "SELECT noisy_count, in_stations, out_stations FROM queries"
            " WHERE date = ? AND in_key = ? AND out_key = ?",
            (date, in_key, out_key)).fetchone()
        if cached:
            conn.execute("COMMIT")
            return jsonify(ok=True, date=date,
                           in_stations=json.loads(cached["in_stations"]),
                           out_stations=json.loads(cached["out_stations"]),
                           noisy_count=cached["noisy_count"], cached=True)

        if not source_exists(conn, date):
            conn.execute("ROLLBACK")
            return jsonify(ok=False,
                           error=f"{date} 缺少源数据, 请先上传后再查询"), 409

        state = conn.execute(
            "SELECT budget_spent FROM day_state WHERE date = ?", (date,)).fetchone()
        spent = state["budget_spent"] if state else 0.0
        if spent + QUERY_COST > DAILY_BUDGET + 1e-9:
            conn.execute("ROLLBACK")
            return jsonify(ok=False,
                           error=f"{date} 当日隐私预算不足"
                                 f"(已用 {spent:g}/{DAILY_BUDGET:g}, 本次需 {QUERY_COST:g}),"
                                 " 不返回新结果"), 403

        true_count = count_candidate(conn, date, in_stations, out_stations)

        noisy_count = true_count + laplace_noise()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO queries (date, in_key, out_key, in_stations, out_stations,"
            " noisy_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, in_key, out_key, json.dumps(in_stations, ensure_ascii=False),
             json.dumps(out_stations, ensure_ascii=False), noisy_count, now))
        conn.execute(
            "INSERT INTO day_state (date, locked, budget_spent) VALUES (?, 1, ?)"
            " ON CONFLICT(date) DO UPDATE SET locked = 1,"
            " budget_spent = budget_spent + excluded.budget_spent",
            (date, QUERY_COST))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return jsonify(ok=True, date=date, in_stations=in_stations,
                   out_stations=out_stations, noisy_count=noisy_count,
                   cached=False,
                   budget_remaining=round(DAILY_BUDGET - spent - QUERY_COST, 10))


# ---------------------------------------------------------------- 优先抽选

def selection_body(date, row, cached):
    """把 selections 表中的一行还原成响应体(优先顺序保持不变)."""
    candidates = json.loads(row["candidates"])
    selected_idx = json.loads(row["selected"])
    return {
        "ok": True, "date": date,
        # 只返回选中的方向, 按优先顺序排列; 不含人数、乘客ID或未选中的候选
        "selected": [candidates[idx] for idx in selected_idx],
        "selected_count": len(selected_idx),
        "slots": row["slots"],
        "cached": cached,
        "created_at": row["created_at"],
    }


def find_selection(conn, date, request_key):
    return conn.execute(
        "SELECT candidates, count, slots, selected, created_at FROM selections"
        " WHERE date = ? AND request_key = ?", (date, request_key)).fetchone()


@app.post("/select")
def select():
    payload = get_payload()
    date = normalize_date(payload.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确(应为 YYYY-MM-DD)"), 400

    try:
        raw_candidates = parse_candidates(payload.get("candidates"))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    if not MIN_CANDIDATES <= len(raw_candidates) <= MAX_CANDIDATES:
        return jsonify(ok=False,
                       error=f"候选数量必须在 {MIN_CANDIDATES}~{MAX_CANDIDATES} 个之间"
                             f"(当前 {len(raw_candidates)} 个)"), 400

    candidates, request_key = canonical_candidates(raw_candidates)
    count = len(candidates)
    if count == 1:
        return jsonify(ok=False, error="候选去重后只剩 1 个, 至少需要 2 个不同候选"), 400

    try:
        slots = parse_slots(payload.get("slots"), count)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    # 名额决定发布几个方向, 因此计入请求身份
    request_key = f"{request_key}:{slots}"
    conn = get_db()

    # 快速路径: 相同请求(与顺序/重复无关)复用原优先顺序, 不扣费
    cached = find_selection(conn, date, request_key)
    if cached:
        return jsonify(selection_body(date, cached, True))

    # 新抽选: 检查源数据与预算 + 计分抽选 + 扣费 + 保存 + 锁定, 同一事务
    conn.execute("BEGIN IMMEDIATE")
    try:
        cached = find_selection(conn, date, request_key)
        if cached:
            conn.execute("COMMIT")
            return jsonify(selection_body(date, cached, True))

        if not source_exists(conn, date):
            conn.execute("ROLLBACK")
            return jsonify(ok=False,
                           error=f"{date} 缺少源数据, 请先上传后再抽选"), 409

        state = conn.execute(
            "SELECT budget_spent FROM day_state WHERE date = ?", (date,)).fetchone()
        spent = state["budget_spent"] if state else 0.0
        if spent + SELECTION_COST > DAILY_BUDGET + 1e-9:
            conn.execute("ROLLBACK")
            return jsonify(ok=False,
                           error=f"{date} 当日隐私预算不足"
                                 f"(已用 {spent:g}/{DAILY_BUDGET:g},"
                                 f" 本次需 {SELECTION_COST:g}), 不返回抽选结果"), 403

        # 只使用候选的真实人数作为打分依据; 不把人数返回给调用方
        scores = {idx: float(count_candidate(conn, date,
                                             cand["in_stations"],
                                             cand["out_stations"]))
                  for idx, cand in enumerate(candidates)}
        selected_idx = select_priority(scores, slots)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO selections (date, request_key, candidates, count, slots,"
            " selected, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, request_key, json.dumps(candidates, ensure_ascii=False),
             count, slots, json.dumps(selected_idx), now))
        conn.execute(
            "INSERT INTO day_state (date, locked, budget_spent) VALUES (?, 1, ?)"
            " ON CONFLICT(date) DO UPDATE SET locked = 1,"
            " budget_spent = budget_spent + excluded.budget_spent",
            (date, SELECTION_COST))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    body = selection_body(date, {
        "candidates": json.dumps(candidates, ensure_ascii=False),
        "count": count, "slots": slots, "selected": json.dumps(selected_idx),
        "created_at": now}, False)
    body["budget_remaining"] = round(DAILY_BUDGET - spent - SELECTION_COST, 10)
    return jsonify(body)


# ---------------------------------------------------------------- 状态接口

@app.get("/api/stations")
def api_stations():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT in_station AS s FROM passengers WHERE date = ?"
        " UNION SELECT out_station FROM passengers WHERE date = ? ORDER BY s",
        (date, date)).fetchall()
    return jsonify(ok=True, date=date, stations=[r["s"] for r in rows])


@app.get("/api/status")
def api_status():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    state = conn.execute(
        "SELECT locked, budget_spent FROM day_state WHERE date = ?", (date,)).fetchone()
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM passengers WHERE date = ?", (date,)).fetchone()["c"]
    spent = state["budget_spent"] if state else 0.0
    return jsonify(ok=True, date=date,
                   locked=bool(state and state["locked"]),
                   rows=rows,
                   budget_spent=spent,
                   budget_remaining=round(DAILY_BUDGET - spent, 10),
                   daily_budget=DAILY_BUDGET, query_cost=QUERY_COST,
                   selection_cost=SELECTION_COST,
                   selection_epsilon=SELECTION_EPSILON)


@app.get("/api/queries")
def api_queries():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT in_stations, out_stations, noisy_count, created_at"
        " FROM queries WHERE date = ? ORDER BY created_at, rowid", (date,)).fetchall()
    return jsonify(ok=True, date=date, queries=[
        {"in_stations": json.loads(r["in_stations"]),
         "out_stations": json.loads(r["out_stations"]),
         "noisy_count": r["noisy_count"],
         "created_at": r["created_at"]} for r in rows])


@app.get("/api/selections")
def api_selections():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT candidates, count, slots, selected, created_at"
        " FROM selections WHERE date = ? ORDER BY created_at, rowid", (date,)).fetchall()
    return jsonify(ok=True, date=date, selections=[
        {"selected": [json.loads(r["candidates"])[i]
                      for i in json.loads(r["selected"])],
         "selected_count": len(json.loads(r["selected"])),
         "slots": r["slots"],
         "created_at": r["created_at"]}
        for r in rows])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
