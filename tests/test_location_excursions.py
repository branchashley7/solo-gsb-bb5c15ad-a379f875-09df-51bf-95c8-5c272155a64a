"""按位置的越界区间(location-excursions)端到端与单元测试:

- 样品不存在 404 not_found;未登记测温配置 404 temp_profile_not_found
- 无位置变更:读数位置取样品当前位置(含未登记 null)
- 位置时间线重建:变更时刻起取新位置,首笔变更前取首笔变更的原位置;
  交接与冲正审计都纳入时间线,冲正恢复后的读数落回恢复位置
- 期间位置变更截断区间(即使同向且间隔不超配置值);正常读数/方向变化/超间隔同既有规则
- 同刻事件先交接后冲正、同类按明细编号升序(直接 SQL 构造同刻审计)
- 乱序补录实时重算:跨位置区间随补录拆分/更新
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import temperatures as tp
from app.db import connect, init_db
from app.main import app, get_conn


@pytest.fixture()
def client():
    mem = connect(":memory:")
    init_db(mem)

    def _override():
        yield mem

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _conn():
    return next(app.dependency_overrides[get_conn]())


def _sample(client, sid, location=None):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": f"OP-{sid}", "sample_id": sid,
        "material": "血清", "amount": 100, "unit": "mL", "location": location,
    })
    assert r.status_code == 200, r.text


def _profile(client, sid, lo=2.0, hi=8.0, interval=600):
    r = client.post("/api/v1/temperature-profiles", json={
        "sample_id": sid, "min_temp": lo, "max_temp": hi,
        "max_interval_seconds": interval,
    })
    assert r.status_code == 200, r.text


def _read(client, sid, at, temp):
    r = client.post(
        f"/api/v1/samples/{sid}/temperature-readings",
        json={"measured_at": at, "temperature": temp},
    )
    assert r.status_code == 200, r.text


def _transfer(client, no, sid, from_loc, to_loc):
    r = client.post("/api/v1/transfers", json={
        "transfer_no": no, "operator": "王交接",
        "items": [{"sample_id": sid, "from_location": from_loc, "to_location": to_loc}],
    })
    assert r.status_code == 200, r.text


def _reverse(client, rno, tno):
    r = client.post("/api/v1/transfers/reversals", json={
        "reversal_no": rno, "transfer_no": tno, "operator": "郑冲正",
    })
    assert r.status_code == 200, r.text


def _loc_excursions(client, sid):
    return client.get(f"/api/v1/samples/{sid}/temperature/location-excursions")


def _transfer_item_time(sid, transfer_no):
    row = _conn().execute(
        "SELECT created_at FROM location_transfer_items"
        " WHERE sample_id = ? AND transfer_no = ?",
        (sid, transfer_no),
    ).fetchone()
    return row["created_at"]


def _reversal_item_time(sid, reversal_no):
    row = _conn().execute(
        "SELECT created_at FROM location_transfer_reversal_items"
        " WHERE sample_id = ? AND reversal_no = ?",
        (sid, reversal_no),
    ).fetchone()
    return row["created_at"]


def _shift(ts_text, seconds):
    """把库内 UTC 时间串平移若干秒后重新归一化(用于在读数中引用事件时刻)。"""
    return tp.normalize_utc(tp.parse_utc(ts_text) + timedelta(seconds=seconds))


# ---------------------------------------------------------------- 404

def test_location_excursions_require_existing_sample_and_profile(client):
    r = _loc_excursions(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"

    _sample(client, "S1", location="冷库A")
    r = _loc_excursions(client, "S1")
    assert r.status_code == 404
    assert r.json()["error"] == "temp_profile_not_found"

    _profile(client, "S1")
    r = _loc_excursions(client, "S1")
    assert r.status_code == 200
    body = r.json()
    assert body["sample_id"] == "S1"
    assert body["min_temp"] == 2.0 and body["max_temp"] == 8.0
    assert body["max_interval_seconds"] == 600
    assert body["intervals"] == []


# ---------------------------------------------------------------- 无位置变更

def test_no_location_events_uses_current_location(client):
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=600)
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)
    _read(client, "S1", "2026-09-24T10:05:00Z", 9.5)

    body = _loc_excursions(client, "S1").json()
    assert body["intervals"] == [
        {"location": "冷库A", "direction": "above",
         "start_at": "2026-09-24T10:00:00.000000Z",
         "end_at": "2026-09-24T10:05:00.000000Z",
         "extreme_value": 9.5,
         "extreme_at": "2026-09-24T10:05:00.000000Z",
         "reading_count": 2},
    ]


def test_no_location_events_and_unregistered_location_is_null(client):
    _sample(client, "S1")  # 未登记位置
    _profile(client, "S1")
    _read(client, "S1", "2026-09-24T10:00:00Z", 1.0)

    body = _loc_excursions(client, "S1").json()
    assert len(body["intervals"]) == 1
    assert body["intervals"][0]["location"] is None
    assert body["intervals"][0]["direction"] == "below"


# ---------------------------------------------------------------- 位置时间线重建

def test_location_change_splits_segment_even_within_interval(client):
    """同向越界读数间隔不超配置值,但期间发生位置变更 -> 截断为两段。"""
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=600)  # 10 分钟
    _transfer(client, "TR-1", "S1", "冷库A", "冷库B")
    t_ev = _transfer_item_time("S1", "TR-1")

    # 事件前后各两条高温读数,相邻间隔均为 5 分钟(<= 600 秒)
    _read(client, "S1", _shift(t_ev, -600), 9.0)   # 冷库A
    _read(client, "S1", _shift(t_ev, -300), 9.5)   # 冷库A
    _read(client, "S1", _shift(t_ev, 300), 10.0)   # 冷库B
    _read(client, "S1", _shift(t_ev, 600), 9.0)    # 冷库B

    body = _loc_excursions(client, "S1").json()
    assert [(i["location"], i["start_at"], i["end_at"],
             i["extreme_value"], i["reading_count"])
            for i in body["intervals"]] == [
        ("冷库A", _shift(t_ev, -600), _shift(t_ev, -300), 9.5, 2),
        ("冷库B", _shift(t_ev, 300), _shift(t_ev, 600), 10.0, 2),
    ]


def test_reading_before_first_change_uses_original_location(client):
    """首笔变更前的读数取首笔变更的原位置;变更时刻起取新位置。"""
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=600)
    _transfer(client, "TR-1", "S1", "冷库A", "冷库B")
    t_ev = _transfer_item_time("S1", "TR-1")

    _read(client, "S1", _shift(t_ev, -60), 9.0)  # 首笔变更前 -> 冷库A
    _read(client, "S1", t_ev, 9.0)               # 恰在变更时刻 -> 新位置冷库B

    body = _loc_excursions(client, "S1").json()
    assert [(i["location"], i["start_at"]) for i in body["intervals"]] == [
        ("冷库A", _shift(t_ev, -60)),
        ("冷库B", tp.normalize_utc(tp.parse_utc(t_ev))),
    ]


def test_reversal_restores_location_in_timeline(client):
    """冲正事件接入时间线:冲正后的读数落回恢复位置(与变更前相同也分成两段)。"""
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=3600)
    _transfer(client, "TR-1", "S1", "冷库A", "冷库B")
    _reverse(client, "RV-1", "TR-1")
    t_tr = _transfer_item_time("S1", "TR-1")
    t_rv = _reversal_item_time("S1", "RV-1")

    _read(client, "S1", _shift(t_tr, -300), 9.0)  # 交接前 -> 冷库A
    _read(client, "S1", _shift(t_rv, 300), 9.0)   # 冲正后 -> 恢复为冷库A

    body = _loc_excursions(client, "S1").json()
    # 两段位置同为冷库A,但期间有位置变更(交接 + 冲正),不得并为一段
    assert [(i["location"], i["start_at"], i["reading_count"])
            for i in body["intervals"]] == [
        ("冷库A", _shift(t_tr, -300), 1),
        ("冷库A", _shift(t_rv, 300), 1),
    ]


def test_reading_between_transfer_and_reversal_uses_intermediate_location(client):
    """直接 SQL 构造可控时刻的交接/冲正审计:两事件之间的读数取中间位置。"""
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=3600)
    conn = _conn()
    conn.execute(
        "INSERT INTO location_transfers (transfer_no, operator, payload)"
        " VALUES ('TR-1', '王交接', '{}')"
    )
    conn.execute(
        "INSERT INTO location_transfer_items (transfer_no, sample_id, seq_index,"
        " from_location, to_location, operator, created_at)"
        " VALUES ('TR-1', 'S1', 0, '冷库A', '冷库B', '王交接',"
        " '2026-09-24T10:00:00.000Z')"
    )
    conn.execute(
        "INSERT INTO location_transfer_reversals (reversal_no, transfer_no,"
        " operator, payload) VALUES ('RV-1', 'TR-1', '郑冲正', '{}')"
    )
    conn.execute(
        "INSERT INTO location_transfer_reversal_items (reversal_no, transfer_no,"
        " sample_id, seq_index, from_location, to_location, operator, created_at)"
        " VALUES ('RV-1', 'TR-1', 'S1', 0, '冷库B', '冷库A', '郑冲正',"
        " '2026-09-24T12:00:00.000Z')"
    )
    conn.execute("UPDATE samples SET location = '冷库A' WHERE sample_id = 'S1'")

    _read(client, "S1", "2026-09-24T09:00:00Z", 9.0)   # 首笔变更前 -> 冷库A
    _read(client, "S1", "2026-09-24T11:00:00Z", 9.0)   # 交接后、冲正前 -> 冷库B
    _read(client, "S1", "2026-09-24T13:00:00Z", 9.0)   # 冲正后 -> 冷库A

    body = _loc_excursions(client, "S1").json()
    assert [(i["location"], i["start_at"]) for i in body["intervals"]] == [
        ("冷库A", "2026-09-24T09:00:00.000000Z"),
        ("冷库B", "2026-09-24T11:00:00.000000Z"),
        ("冷库A", "2026-09-24T13:00:00.000000Z"),
    ]


def test_same_instant_events_transfer_first_then_item_id(client):
    """同刻事件先交接后冲正、同类按明细编号升序:

    同刻三笔审计:TR-1(A->B,明细号小)、TR-2(B->C,明细号大)、RV-1(C->D,冲正)。
    同刻读数应落在冲正后的 D;同刻前一瞬的读数取首笔变更的原位置 A。
    """
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=3600)
    conn = _conn()
    ts = "2026-09-24T10:00:00.000Z"
    for no, frm, to in (("TR-1", "冷库A", "冷库B"), ("TR-2", "冷库B", "冷库C")):
        conn.execute(
            "INSERT INTO location_transfers (transfer_no, operator, payload)"
            " VALUES (?, '王交接', '{}')",
            (no,),
        )
        conn.execute(
            "INSERT INTO location_transfer_items (transfer_no, sample_id,"
            " seq_index, from_location, to_location, operator, created_at)"
            f" VALUES (?, 'S1', 0, ?, ?, '王交接', '{ts}')",
            (no, frm, to),
        )
    conn.execute(
        "INSERT INTO location_transfer_reversals (reversal_no, transfer_no,"
        " operator, payload) VALUES ('RV-1', 'TR-2', '郑冲正', '{}')"
    )
    conn.execute(
        "INSERT INTO location_transfer_reversal_items (reversal_no, transfer_no,"
        " sample_id, seq_index, from_location, to_location, operator, created_at)"
        f" VALUES ('RV-1', 'TR-2', 'S1', 0, '冷库C', '冷库D', '郑冲正', '{ts}')"
    )
    conn.execute("UPDATE samples SET location = '冷库D' WHERE sample_id = 'S1'")

    _read(client, "S1", "2026-09-24T09:59:59Z", 9.0)  # 同刻事件前 -> 冷库A
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)  # 同刻 -> 冲正后冷库D

    body = _loc_excursions(client, "S1").json()
    assert [(i["location"], i["start_at"]) for i in body["intervals"]] == [
        ("冷库A", "2026-09-24T09:59:59.000000Z"),
        ("冷库D", "2026-09-24T10:00:00.000000Z"),
    ]


# ---------------------------------------------------------------- 截断规则与乱序补录

def test_normal_reading_and_direction_change_still_truncate(client):
    """正常读数与方向变化的截断规则与不分位置的越界区间一致,位置随段返回。"""
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", lo=2.0, hi=8.0, interval=600)
    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)   # above 段
    _read(client, "S1", "2026-09-24T10:05:00Z", 5.0)   # 正常 -> 截断
    _read(client, "S1", "2026-09-24T10:10:00Z", 1.0)   # below 段
    _read(client, "S1", "2026-09-24T10:15:00Z", 9.0)   # 方向变化 -> 新 above 段
    _read(client, "S1", "2026-09-24T10:30:00Z", 9.5)   # 间隔 15 分钟 > 600 秒 -> 新段

    body = _loc_excursions(client, "S1").json()
    assert [(i["location"], i["direction"], i["reading_count"])
            for i in body["intervals"]] == [
        ("冷库A", "above", 1),
        ("冷库A", "below", 1),
        ("冷库A", "above", 1),
        ("冷库A", "above", 1),
    ]


def test_out_of_order_backfill_recomputes_across_locations(client):
    """乱序补录实时重算:先录变更后的读数,再补变更前的读数,区间按位置分段。"""
    _sample(client, "S1", location="冷库A")
    _profile(client, "S1", interval=600)
    _transfer(client, "TR-1", "S1", "冷库A", "冷库B")
    t_ev = _transfer_item_time("S1", "TR-1")

    # 先录变更时刻之后的两条高温读数
    _read(client, "S1", _shift(t_ev, 300), 9.0)
    _read(client, "S1", _shift(t_ev, 600), 9.5)
    before = _loc_excursions(client, "S1").json()["intervals"]
    assert [(i["location"], i["reading_count"]) for i in before] == [("冷库B", 2)]

    # 补录变更前的两条高温读数:与变更后读数间隔仅 5 分钟,但期间有位置变更
    _read(client, "S1", _shift(t_ev, -600), 9.0)
    _read(client, "S1", _shift(t_ev, -300), 10.0)
    after = _loc_excursions(client, "S1").json()["intervals"]
    assert [(i["location"], i["start_at"], i["end_at"],
             i["extreme_value"], i["reading_count"]) for i in after] == [
        ("冷库A", _shift(t_ev, -600), _shift(t_ev, -300), 10.0, 2),
        ("冷库B", _shift(t_ev, 300), _shift(t_ev, 600), 9.5, 2),
    ]

    # 再补一条变更后区间内的正常读数 -> 冷库B 段被拆成两段
    _read(client, "S1", _shift(t_ev, 450), 5.0)
    final = _loc_excursions(client, "S1").json()["intervals"]
    assert [(i["location"], i["start_at"], i["end_at"], i["reading_count"])
            for i in final] == [
        ("冷库A", _shift(t_ev, -600), _shift(t_ev, -300), 2),
        ("冷库B", _shift(t_ev, 300), _shift(t_ev, 300), 1),
        ("冷库B", _shift(t_ev, 600), _shift(t_ev, 600), 1),
    ]


def test_readings_do_not_mix_with_other_samples_events(client):
    """其他样品的位置变更与读数不影响本样品的按位置越界区间。"""
    _sample(client, "S1", location="冷库A")
    _sample(client, "S2", location="冷库X")
    _profile(client, "S1", interval=600)
    _transfer(client, "TR-1", "S2", "冷库X", "冷库Y")
    _read(client, "S2", "2026-09-24T10:00:00Z", 99.0)

    _read(client, "S1", "2026-09-24T10:00:00Z", 9.0)
    _read(client, "S1", "2026-09-24T10:05:00Z", 9.5)
    body = _loc_excursions(client, "S1").json()
    assert [(i["location"], i["reading_count"]) for i in body["intervals"]] == [
        ("冷库A", 2),
    ]
