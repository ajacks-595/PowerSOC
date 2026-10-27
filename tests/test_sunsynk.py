"""Tests for the Sunsynk client. All HTTP is mocked — no live calls."""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import sunsynk
from sunsynk import (
    DayResult,
    SunsynkAuthError,
    SunsynkClient,
    integrate_series_kwh,
    integrate_signed_kwh,
    last_value,
    peak_value,
    resample_halfhourly_energy,
    resample_halfhourly_last,
)

# ---------- series maths --------------------------------------------------

def _const_series(kw, n=13, step_min=5):
    """n records of constant power `kw`, `step_min` apart from 00:00."""
    out = []
    t = 0
    for _ in range(n):
        h, m = divmod(t, 60)
        out.append({"time": f"{h:02d}:{m:02d}", "value": kw})
        t += step_min
    return out


def test_integrate_constant_power():
    # 2 kW held for exactly 1 hour (13 points 5 min apart) -> 2 kWh.
    assert integrate_series_kwh(_const_series(2.0), "kW") == pytest.approx(2.0)


def test_integrate_two_points_one_hour():
    recs = [{"time": "00:00", "value": 3.0}, {"time": "01:00", "value": 3.0}]
    assert integrate_series_kwh(recs, "kW") == pytest.approx(3.0)


def test_integrate_unit_watts():
    recs = [{"time": "00:00", "value": 2000.0}, {"time": "01:00", "value": 2000.0}]
    assert integrate_series_kwh(recs, "W") == pytest.approx(2.0)


def test_integrate_trapezoid_ramp():
    # Linear ramp 0->4 kW over 2h: area = 0.5*2*4 = 4 kWh.
    recs = [{"time": "00:00", "value": 0.0}, {"time": "02:00", "value": 4.0}]
    assert integrate_series_kwh(recs, "kW") == pytest.approx(4.0)


def test_integrate_empty_and_single():
    assert integrate_series_kwh([], "kW") == 0.0
    assert integrate_series_kwh([{"time": "00:00", "value": 5}], "kW") == 0.0
    assert integrate_series_kwh(None, "kW") == 0.0


def test_records_xy_tolerates_key_spellings():
    recs = [{"date": "00:00", "val": 1.0}, {"x": "01:00", "y": 1.0}]
    # Mixed spellings still integrate (1 kW for 1h = 1 kWh).
    assert integrate_series_kwh(recs, "kW") == pytest.approx(1.0)


def test_peak_and_last_value():
    soc = [{"time": "00:00", "value": 20},
           {"time": "12:00", "value": 100},
           {"time": "23:30", "value": 62}]
    assert peak_value(soc) == 100
    assert last_value(soc) == 62
    assert peak_value([]) is None
    assert last_value([]) is None


def test_resample_halfhourly_energy_sums_to_total():
    recs = _const_series(2.0, n=25, step_min=5)  # 2 kW for 2 hours
    buckets = resample_halfhourly_energy(recs, "2026-05-01", "kW")
    # 2h at 2kW = 4 kWh spread over 4 half-hour buckets.
    assert sum(buckets.values()) == pytest.approx(4.0, abs=1e-3)
    assert all(k.startswith("2026-05-01T") for k in buckets)


def test_resample_halfhourly_last_snapshot():
    recs = [{"time": "00:05", "value": 30}, {"time": "00:20", "value": 35},
            {"time": "00:40", "value": 50}]
    out = resample_halfhourly_last(recs, "2026-05-01")
    assert out["2026-05-01T00:00"] == 35   # last within first half hour
    assert out["2026-05-01T00:30"] == 50


# ---------- /day parsing --------------------------------------------------

def _day_body():
    """A representative (synthetic) /day response with labelled series."""
    pv = _const_series(3.0, n=13)            # 3 kW for 1h -> 3 kWh
    load = _const_series(1.0, n=13)          # 1 kWh
    imp = _const_series(0.5, n=13)           # 0.5 kWh
    soc = [{"time": "00:00", "value": 50}, {"time": "00:30", "value": 80},
           {"time": "01:00", "value": 95}]
    return {"success": True, "code": 0, "data": {"infos": [
        {"label": "PV", "unit": "kW", "records": pv},
        {"label": "Load", "unit": "kW", "records": load},
        {"label": "Grid Import", "unit": "kW", "records": imp},
        {"label": "Battery SOC", "unit": "%", "records": soc},
    ]}}


def test_parse_day_totals_and_intervals():
    c = SunsynkClient(username="u", password="p", plant_id="1")
    res = c._parse_day("2026-05-01", _day_body())
    assert isinstance(res, DayResult)
    assert res.pv_kwh == pytest.approx(3.0)
    assert res.house_load_kwh == pytest.approx(1.0)
    assert res.grid_import_kwh == pytest.approx(0.5)
    assert res.peak_soc_pct == 95
    assert res.end_soc_pct == 95
    assert res.intervals          # half-hourly rows present
    assert all("interval_start" in r for r in res.intervals)


def test_integrate_signed():
    # +2 kW for 1h -> (2 charge/import, 0).
    assert integrate_signed_kwh([{"time": "00:00", "value": 2000},
                                 {"time": "01:00", "value": 2000}], "W") == (2.0, 0.0)
    # -2 kW for 1h -> (0, 2 discharge/export).
    assert integrate_signed_kwh([{"time": "00:00", "value": -2000},
                                 {"time": "01:00", "value": -2000}], "W") == (0.0, 2.0)
    # +2 for 1h then -2 for 1h -> (2, 2).
    pos, neg = integrate_signed_kwh([{"time": "00:00", "value": 2},
                                     {"time": "01:00", "value": 2},
                                     {"time": "02:00", "value": -2},
                                     {"time": "03:00", "value": -2}], "kW")
    assert pos == pytest.approx(2.0) and neg == pytest.approx(2.0)


def _signed_day_body():
    """A /day response in the live shape: single signed Battery + Grid series."""
    def flat(v):
        return [{"time": "00:00", "value": v}, {"time": "01:00", "value": v}]
    return {"success": True, "code": 0, "data": {"infos": [
        {"label": "PV", "unit": "W", "records": flat("0")},
        {"label": "Battery", "unit": "W", "records": flat("-1000")},   # discharging
        {"label": "Grid", "unit": "W", "records": flat("2000")},        # importing
        {"label": "Load", "unit": "W", "records": flat("1000")},
        {"label": "SOC", "unit": "%", "records": [{"time": "00:00", "value": 50},
                                                  {"time": "01:00", "value": 48}]},
    ]}}


def test_parse_day_signed_battery_grid():
    c = SunsynkClient(username="u", password="p", plant_id="1")
    r = c._parse_day("2026-05-01", _signed_day_body())
    assert r.grid_import_kwh == pytest.approx(2.0)     # +grid = import
    assert r.grid_export_kwh == pytest.approx(0.0)
    assert r.battery_charge_kwh == pytest.approx(0.0)
    assert r.battery_discharge_kwh == pytest.approx(1.0)  # -battery = discharge
    assert r.house_load_kwh == pytest.approx(1.0)
    assert r.end_soc_pct == 48
    # Interval rows carry the split columns.
    row = r.intervals[0]
    assert row["grid_import_kwh"] is not None
    assert row["battery_discharge_kwh"] is not None


def test_parse_day_empty():
    c = SunsynkClient(username="u", password="p", plant_id="1")
    assert c._parse_day("2026-05-01", {"success": True, "data": {"infos": []}}).empty
    assert c._parse_day("2026-05-01", {"data": None}).empty


# ---------- auth: RSA round-trip + flow fallback --------------------------

class _Resp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self.ok = 200 <= status < 400
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    """Routes get/post by URL substring to queued responses; records calls."""
    def __init__(self):
        self.headers = {}
        self.routes = {}      # url-substring -> _Resp (or list to pop)
        self.calls = []

    def _resp(self, url):
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, list):
                    return resp.pop(0)
                return resp
        return _Resp(404, text="no route")

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        return self._resp(url)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        self._last_post_json = json
        return self._resp(url)

    def close(self):
        pass


@pytest.fixture
def rsa_pubkey_b64():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    body = "".join(ln for ln in pem.splitlines() if "-----" not in ln)
    return key, body


def _client_with_fake(tmp_path, fake):
    c = SunsynkClient(username="user@example.com", password="pw",
                      plant_id="42",
                      token_cache_path=str(tmp_path / "tok.json"))
    c.session = fake
    return c


def test_rsa_encrypt_roundtrip(rsa_pubkey_b64):
    priv, body = rsa_pubkey_b64
    enc = SunsynkClient._rsa_encrypt(body, "my-password")
    decrypted = priv.decrypt(base64.b64decode(enc), padding.PKCS1v15())
    assert decrypted.decode() == "my-password"


def test_login_modern_success(tmp_path, rsa_pubkey_b64):
    priv, body = rsa_pubkey_b64
    fake = _FakeSession()
    fake.routes = {
        "anonymous/publicKey": _Resp(200, {"success": True, "data": body}),
        "oauth/token/new": _Resp(200, {"success": True, "data": {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 500000}}),
    }
    c = _client_with_fake(tmp_path, fake)
    c.login()
    assert c._token == "AT"
    # The posted password must decrypt back to the plaintext.
    posted = fake._last_post_json
    assert priv.decrypt(base64.b64decode(posted["password"]),
                        padding.PKCS1v15()).decode() == "pw"
    assert "sign" in posted and "nonce" in posted


def test_login_falls_back_to_legacy(tmp_path):
    fake = _FakeSession()
    fake.routes = {
        # Modern path DEFINITIVELY rejected (200 success:false) -> legacy runs.
        "anonymous/publicKey": _Resp(200, {"success": False}),
        "oauth/token/new": _Resp(401),
        "oauth/token": _Resp(200, {"success": True, "code": 0, "data": {
            "access_token": "LEGACY", "refresh_token": "R", "expires_in": 1000}}),
    }
    c = _client_with_fake(tmp_path, fake)
    c.login()
    assert c._token == "LEGACY"


def test_login_transient_5xx_does_not_fall_back_to_plaintext(tmp_path):
    """A 5xx/429 on the modern endpoints is platform flakiness (CLAUDE.md
    gotcha #4), NOT an auth rejection — the client must never react to it by
    re-sending the raw password via the legacy plaintext flow."""
    fake = _FakeSession()
    fake.routes = {
        "anonymous/publicKey": _Resp(502, text="region migration"),
        "oauth/token": _Resp(200, {"success": True, "code": 0, "data": {
            "access_token": "LEGACY", "expires_in": 1000}}),
    }
    c = _client_with_fake(tmp_path, fake)
    with pytest.raises(sunsynk.SunsynkError) as ei:
        c.login()
    assert not isinstance(ei.value, SunsynkAuthError)
    # The legacy plaintext endpoint must never have been called.
    legacy_posts = [u for (m, u, *_) in fake.calls
                    if m == "POST" and u.endswith("/oauth/token")]
    assert legacy_posts == []


def test_login_both_flows_fail(tmp_path):
    fake = _FakeSession()
    fake.routes = {
        "anonymous/publicKey": _Resp(200, {"success": False}),
        "oauth/token/new": _Resp(401),
        "oauth/token": _Resp(401),
    }
    c = _client_with_fake(tmp_path, fake)
    with pytest.raises(SunsynkAuthError):
        c.login()


def test_login_missing_credentials(tmp_path):
    c = SunsynkClient(username="", password="", plant_id="1",
                      token_cache_path=str(tmp_path / "t.json"))
    with pytest.raises(SunsynkAuthError):
        c.login()


# ---------- token cache + 401 re-login ------------------------------------

def test_token_cache_roundtrip(tmp_path, rsa_pubkey_b64):
    _, body = rsa_pubkey_b64
    fake = _FakeSession()
    fake.routes = {
        "anonymous/publicKey": _Resp(200, {"success": True, "data": body}),
        "oauth/token/new": _Resp(200, {"success": True, "data": {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 500000}}),
    }
    c = _client_with_fake(tmp_path, fake)
    c.login()
    # A fresh client with the same cache file + username reuses the token
    # without hitting the network.
    c2 = SunsynkClient(username="user@example.com", password="pw", plant_id="42",
                       token_cache_path=str(tmp_path / "tok.json"))
    c2.session = _FakeSession()   # empty routes -> any call would 404
    c2.login()
    assert c2._token == "AT"


def _realtime_client(tmp_path, *, pv, grid_pac, batt_power, soc,
                     etoday="41.9", from_="7.4", to="29.4"):
    c = SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN1",
                      token_cache_path=str(tmp_path / "t.json"))

    def fake_get(path, params=None):
        if "realtime/input" in path:
            return {"data": {"pac": pv, "etoday": etoday}}
        if "/grid/" in path:
            return {"data": {"pac": grid_pac, "etodayFrom": from_, "etodayTo": to}}
        if "/battery/" in path:
            return {"data": {"power": batt_power, "soc": soc,
                             "etodayChg": "5.3", "etodayDischg": "4.3"}}
        return {"data": {}}

    c._get = fake_get
    return c


def test_get_parallel_preserves_order(tmp_path):
    c = SunsynkClient(username="u", password="p", plant_id="1",
                      token_cache_path=str(tmp_path / "t.json"))
    c._get = lambda path, params=None: {"path": path}
    out = c._get_parallel("a", "b", "c")
    assert [o["path"] for o in out] == ["a", "b", "c"]


def test_get_realtime_battery_charging(tmp_path):
    # Live-verified case: solar surplus charging a near-full battery. The
    # realtime endpoint reports +discharge / -charge, so raw power -958 = the
    # battery CHARGING at 958 W. House load must NOT include that as supply:
    #   load = pv - battery + grid = 1764 - 958 + 62 = 868 W.
    c = _realtime_client(tmp_path, pv=1764, grid_pac=62, batt_power=-958, soc="99.0")
    r = c.get_realtime()
    assert r["pv_w"] == 1764
    assert r["grid_w"] == 62 and r["grid_dir"] == "import"
    assert r["battery_w"] == 958 and r["battery_dir"] == "charge"
    assert r["soc_pct"] == 99.0
    assert r["load_w"] == 868


def test_get_realtime_battery_discharging(tmp_path):
    # Evening: no solar, battery discharging (raw +800 = discharge), small grid
    # import. Battery + grid supply the house: load = 0 - (-800) + 120 = 920 W.
    c = _realtime_client(tmp_path, pv=0, grid_pac=120, batt_power=800, soc="64.0")
    r = c.get_realtime()
    assert r["battery_w"] == -800 and r["battery_dir"] == "discharge"
    assert r["load_w"] == 920
    assert r["today"]["pv_kwh"] == 41.9
    assert r["today"]["export_kwh"] == 29.4


def test_get_realtime_raises_on_empty(tmp_path):
    c = SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN1",
                      token_cache_path=str(tmp_path / "t.json"))
    c._get = lambda path, params=None: {"data": {}}   # empty 200s
    with pytest.raises(sunsynk.SunsynkError):
        c.get_realtime()


def test_get_indepth_handles_sparse(tmp_path):
    c = SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN1",
                      token_cache_path=str(tmp_path / "t.json"))

    def fake_get(path, params=None):
        if "/battery/" in path:
            return {"data": {"soc": "82.0", "temp": "24.0", "power": -100,
                             "etodayChg": "5.3", "etotalChg": "5260.1",
                             "batterySoc1": "82.0", "batteryVolt1": "52.1",
                             "batteryCurrent1": "-1.9", "batteryPower1": "-100",
                             "batteryTemp1": "24.0"}}
        return {"data": {"etoday": "12.0", "etotal": "9999", "pac": 0, "pvIV": []}}

    c._get = fake_get
    d = c.get_indepth()
    assert d["battery"]["soc"] == 82.0
    # Raw power -100 (charging) flips to +100 under the +charge convention.
    assert d["battery"]["power"] == 100
    assert d["battery"]["number_of_batteries"] is None   # absent -> None, no crash
    assert len(d["battery"]["modules"]) == 1
    assert d["battery"]["modules"][0]["voltage"] == 52.1
    assert d["battery"]["modules"][0]["current"] == 1.9   # raw -1.9 -> +1.9 (charging)
    assert d["battery"]["modules"][0]["power"] == 100
    assert d["pv_today_kwh"] == 12.0
    assert d["pv_strings"] == []


def test_get_indepth_pv_strings_and_battery_fields(tmp_path):
    c = SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN1",
                      token_cache_path=str(tmp_path / "t.json"))

    def fake_get(path, params=None):
        if "/battery/" in path:
            return {"data": {"soc": "97.0", "voltage": "53.2", "temp": "25.0",
                             "bmsSoc": 97.0, "bmsVolt": 53.0, "bmsCurrent": -9.0,
                             "chargeCurrentLimit": 48.0, "etodayChg": "2.9",
                             "emonthChg": "116.8",
                             # per-module not reported by this BMS (volt 0):
                             "batterySoc1": 0.0, "batteryVolt1": 0.0}}
        return {"data": {"pac": 361, "etoday": 0.5, "pvIV": [
            {"pvNo": 1, "vpv": "237.5", "ipv": "0.8", "ppv": "205.0", "todayPv": "0.4"},
            {"pvNo": 2, "vpv": "240.1", "ipv": "0.5", "ppv": "120.0", "todayPv": "0.2"}]}}

    c._get = fake_get
    d = c.get_indepth()
    # Two PV strings parsed from pvNo/vpv/ipv/ppv/todayPv.
    assert len(d["pv_strings"]) == 2
    assert d["pv_strings"][0] == {"n": 1, "voltage": 237.5, "current": 0.8,
                                  "power": 205.0, "today_kwh": 0.4}
    # Expanded battery fields present.
    b = d["battery"]
    assert b["bms_soc"] == 97.0 and b["bms_current"] == 9.0   # raw -9.0 -> +9.0 (charging)
    assert b["charge_current_limit"] == 48.0 and b["month_charge_kwh"] == 116.8
    # No phantom module when per-module voltage is 0.
    assert d["battery"]["modules"] == []


def test_get_indepth_soh(tmp_db, tmp_path):
    import config
    config.set_number("battery_nameplate_kwh", 10)
    config.set_number("battery_rated_cycles", 6000)
    c = SunsynkClient(username="u", password="p", plant_id="1", inverter_sn="SN1",
                      token_cache_path=str(tmp_path / "t.json"))
    c._get = lambda path, params=None: (
        {"data": {"soc": "90", "etotalDischg": "6000"}} if "/battery/" in path
        else {"data": {"pvIV": []}})
    b = c.get_indepth()["battery"]
    assert b["cycle_equivalents"] == 600.0          # 6000 kWh / 10 kWh
    assert b["soh_pct"] == 98.0                      # 100 - (600/6000)*20


def test_get_reauths_on_401(tmp_path, rsa_pubkey_b64):
    _, body = rsa_pubkey_b64
    fake = _FakeSession()
    fake.routes = {
        "anonymous/publicKey": _Resp(200, {"success": True, "data": body}),
        "oauth/token/new": _Resp(200, {"success": True, "data": {
            "access_token": "AT", "expires_in": 500000}}),
        # First data GET 401s, second (after re-login) succeeds.
        "api/v1/plants": [_Resp(401), _Resp(200, {"data": {"infos": [
            {"id": 42, "name": "Home"}]}})],
    }
    c = _client_with_fake(tmp_path, fake)
    c.login()
    plants = c.get_plants()
    assert plants == [{"id": 42, "name": "Home"}]
