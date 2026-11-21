import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from fastapi.testclient import TestClient

from app.main import create_app


def archive(client, sample="dirty"):
    manifest = client.post("/api/batches", json={"sample": sample}).json()["manifest"]
    return manifest["run_id"]


def gate_rows(client, run_id):
    return {row["pass_id"]: row for row in client.get(f"/api/batches/{run_id}/gate").json()["rows"]}


def issued(client, run_id, pass_id):
    return gate_rows(client, run_id)[pass_id]["permit"]


def test_gate_scope_derives_from_one_snapshot_and_marks_holds(client):
    run_id = archive(client)
    gate = client.get(f"/api/batches/{run_id}/gate").json()
    assert gate["scope"] == {
        "total": 7, "release_count": 1, "hold_count": 6,
        "release_pass_ids": ["0007"],
        "hold_pass_ids": ["0012", "0019", "0023", "0028", "0031", "0040"],
    }
    rows = {row["pass_id"]: row for row in gate["rows"]}
    # 已核平且差额恰在容差边界（50.00 kg）的车才可放行。
    assert rows["0007"]["decision"] == "release"
    assert rows["0007"]["codes"] == []
    # 缺轮：即便差额为 0 也不能放行。
    assert (rows["0023"]["decision"], rows["0023"]["codes"]) == ("hold", ["WHEELS_INCOMPLETE"])
    # 磅单差额超限。
    assert "WEIGHT_DIFFERENCE" in rows["0012"]["codes"]
    # 磅单缺失、冲突、无效、车牌不符各自带原因与证据引用。
    assert rows["0031"]["codes"] == ["TICKET_MISSING", "WHEELS_INCOMPLETE"]
    assert rows["0019"]["codes"] == ["TICKET_CONFLICT", "WHEELS_INCOMPLETE"]
    assert rows["0028"]["ticket_refs"] and rows["0040"]["ticket_refs"]
    for row in gate["rows"]:
        assert row["permit"] is None


def test_unarchived_gate_is_preview_only_and_cannot_issue(client):
    preview = client.get("/api/gate?sample=dirty").json()
    assert preview["run_id"] is None
    assert preview["data"]["scope"]["release_pass_ids"] == ["0007"]
    missing = client.post("/api/batches/0000000000000000000000000000000000000000000000000000000000000000/gate/permits/0007")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "RUN_UNKNOWN")


def test_permit_issue_is_idempotent_and_never_rewritten(client):
    run_id = archive(client)
    first = client.post(f"/api/batches/{run_id}/gate/permits/0007")
    assert first.status_code == 201
    assert first.json()["reused"] is False
    permit = first.json()["permit"]
    assert permit["pass_id"] == "0007"
    assert permit["permit_id"].startswith("GP-")
    assert permit["run_id"] == run_id
    second = client.post(f"/api/batches/{run_id}/gate/permits/0007")
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["permit"] == permit
    view = gate_rows(client, run_id)["0007"]
    assert view["permit"]["permit_id"] == permit["permit_id"]
    assert view["permit"]["snapshot_sha256"] != ""
    events = client.get("/api/batches/events?limit=250").json()["events"]
    assert [event["code"] for event in events].count("PERMIT_ISSUED") == 1
    assert [event["code"] for event in events].count("PERMIT_DUPLICATE") == 1


def test_hold_vehicle_is_refused_and_keeps_old_permits_on_failure(client):
    run_id = archive(client)
    permit = client.post(f"/api/batches/{run_id}/gate/permits/0007").json()["permit"]
    refused = client.post(f"/api/batches/{run_id}/gate/permits/0023")
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "GATE_HOLD")
    assert refused.json()["error"]["detail"] == ["WHEELS_INCOMPLETE"]
    # 暂扣事务只追加一条复核事件，已发许可原样可读。
    assert issued(client, run_id, "0007")["permit_id"] == permit["permit_id"]
    events = client.get("/api/batches/events?limit=250").json()["events"]
    assert any(event["code"] == "GATE_HOLD" for event in events)


def test_replay_keeps_issued_permits_unchanged(client):
    run_id = archive(client)
    permit = client.post(f"/api/batches/{run_id}/gate/permits/0007").json()["permit"]
    replay = client.post(f"/api/batches/{run_id}/rerun")
    assert replay.status_code == 200
    assert issued(client, run_id, "0007")["permit_id"] == permit["permit_id"]
    # 同班证据重跑不产生新许可、不改写许可号。
    again = client.post(f"/api/batches/{run_id}/gate/permits/0007")
    assert again.json()["permit"]["permit_id"] == permit["permit_id"]


def test_changed_source_creates_new_version_without_touching_old_permits(client, settings):
    old_id = archive(client)
    old_permit = client.post(f"/api/batches/{old_id}/gate/permits/0007").json()["permit"]
    trigger = settings.source_dir / "dirty-triggers.csv"
    trigger.write_bytes(trigger.read_bytes() + b"\r\n")
    changed = client.post(f"/api/batches/{old_id}/rerun")
    assert (changed.status_code, changed.json()["error"]["code"]) == (409, "SOURCE_CHANGED")
    # 旧版许可在失败后仍然保留。
    assert issued(client, old_id, "0007")["permit_id"] == old_permit["permit_id"]
    new_id = archive(client)
    assert new_id != old_id
    # 晚到补证形成新版本；新版本尚未签发，旧许可不迁移、不改写。
    assert gate_rows(client, new_id)["0007"]["permit"] is None
    new_permit = client.post(f"/api/batches/{new_id}/gate/permits/0007").json()["permit"]
    assert new_permit["permit_id"] != old_permit["permit_id"]
    assert issued(client, old_id, "0007")["permit_id"] == old_permit["permit_id"]


def test_concurrent_permit_requests_commit_once(client):
    run_id = archive(client)
    url = f"/api/batches/{run_id}/gate/permits/0007"
    with ThreadPoolExecutor(max_workers=4) as workers:
        responses = list(workers.map(lambda _: client.post(url), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    permits = {response.json()["permit"]["permit_id"] for response in responses}
    assert len(permits) == 1
    with closing(sqlite3.connect(client.app.state.settings.db_file)) as db:
        count = db.execute("SELECT COUNT(*) FROM permits WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert count == 1


def test_clean_sample_releases_every_vehicle(client):
    run_id = archive(client, "clean")
    gate = client.get(f"/api/batches/{run_id}/gate").json()
    assert gate["scope"]["release_count"] == 7
    assert gate["scope"]["hold_count"] == 0
    for pass_id in gate["scope"]["release_pass_ids"]:
        response = client.post(f"/api/batches/{run_id}/gate/permits/{pass_id}")
        assert response.status_code == 201
    assert client.get(f"/api/batches/{run_id}/gate").json()["issued_count"] == 7


def test_unknown_pass_in_archived_run_is_rejected(client):
    run_id = archive(client)
    response = client.post(f"/api/batches/{run_id}/gate/permits/9999")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "GATE_HOLD")


def test_permits_survive_app_restart(client, settings):
    run_id = archive(client)
    permit = client.post(f"/api/batches/{run_id}/gate/permits/0007").json()["permit"]
    with TestClient(create_app(settings)) as restarted:
        view = restarted.get(f"/api/batches/{run_id}/gate").json()
    restored = next(row for row in view["rows"] if row["pass_id"] == "0007")["permit"]
    assert restored["permit_id"] == permit["permit_id"]
    assert restored["issued_at"] == permit["issued_at"]
    assert restored["snapshot_sha256"] == permit["snapshot_sha256"]
