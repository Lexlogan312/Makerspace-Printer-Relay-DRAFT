import pytest
import requests

from relay.config import ConfigError, load_config
from relay.database import Database, DatabaseError
from tests.fixtures import record


class FakeResponse:
    def __init__(self, body=None, status=200):
        self.status_code, self.ok, self.text = status, status < 400, "error body"
        self._body = body
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.headers, self.calls = {}, []
        self.response, self.error = response or FakeResponse(), error

    def request(self, method, url, **kw):
        self.calls.append((method, url.rsplit("/", 1)[1], kw))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        pass


def make_db(**session_kw):
    db = Database("https://x.supabase.co", "sb_secret_test")
    db.session = FakeSession(**session_kw)
    return db


def test_load_printers_reads_embedded_connection():
    rows = [
        {"id": "a", "serial": "S1", "label": "Fred", "model": "A1", "maintenance_status": "operational",
         "firmware_version": None, "filament_color": "#FFFFFF",
         "printer_connections": {"host": "10.42.0.5", "access_code": "123", "last_error": None}},
        {"id": "b", "serial": "S2", "label": "Flint", "model": "A1", "maintenance_status": "operational",
         "firmware_version": None, "filament_color": None, "printer_connections": None},  # no connection row yet
    ]
    db = make_db(response=FakeResponse(rows))
    fred, flint = db.load_printers()
    assert (fred.host, fred.access_code, fred.filament_color) == ("10.42.0.5", "123", "#FFFFFF")
    assert flint.access_code == "" and flint.host is None


def test_offline_status_row_only_flags_offline():
    db = make_db()
    db.upsert_status([record("RUNNING"), record(online=False)])
    (_, table, kw1), (_, _, kw2) = db.session.calls
    assert table == "printer_status"
    rows = kw1["json"] + kw2["json"]
    online = next(r for r in rows if r["is_online"])
    offline = next(r for r in rows if not r["is_online"])
    assert online["gcode_state"] == "RUNNING" and online["print_error"] == 0
    assert set(offline) == {"printer_id", "is_online", "last_seen"}  # last job data is left alone


def test_secret_key_goes_in_apikey_header_only():
    db = Database("https://x.supabase.co", "sb_secret_test")
    assert db.session.headers["apikey"] == "sb_secret_test"
    assert "Authorization" not in db.session.headers
    legacy = Database("https://x.supabase.co", "eyJlegacy")
    assert legacy.session.headers["Authorization"] == "Bearer eyJlegacy"


def test_errors_say_whether_to_retry():
    with pytest.raises(DatabaseError) as e:
        make_db(error=requests.ConnectionError("down")).insert_heartbeat({})
    assert e.value.retryable
    with pytest.raises(DatabaseError) as e:
        make_db(response=FakeResponse(status=409)).insert_events([{}])
    assert not e.value.retryable
    with pytest.raises(DatabaseError) as e:
        make_db(response=FakeResponse(status=503)).insert_events([{}])
    assert e.value.retryable


def test_config_rejects_publishable_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    path = tmp_path / "relay.toml"
    path.write_text('[supabase]\nurl = "https://x.supabase.co"\nkey = "sb_publishable_abc"\n')
    with pytest.raises(ConfigError, match="publishable"):
        load_config(path)


def test_config_reads_printer_network_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "relay.toml"
    path.write_text('[supabase]\nurl = "https://x.supabase.co"\nkey = "sb_secret_file"\n'
                    '[relay]\nprinter_network = "10.42.0.0/24"\n')
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_env")
    config = load_config(path)
    assert config.supabase_key == "sb_secret_env"
    assert str(config.printer_network) == "10.42.0.0/24"
