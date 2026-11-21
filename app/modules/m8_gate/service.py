from app.errors import DomainError
from app.manifest import canonical, clock, digest
from app.modules.m8_gate.decisions import gate_decisions
from app.store import add_events, add_permit, find


def permit_identifier(run_id, pass_id):
    """许可号只依赖批次与车次，同一证据重跑或重发得到同一许可号。"""
    key = {"run_id": run_id, "pass_id": pass_id, "kind": "gate-permit"}
    return "GP-" + digest(canonical(key).encode("utf-8"))[:16].upper()


def gate_view(store, run_id):
    """合并同一批次的闸口结论与已发许可，工作台只渲染这一个版本。"""
    manifest, results = store.read(run_id)
    decisions = gate_decisions(results)
    issued = {row["pass_id"]: row for row in store.permits(run_id)}
    for row in decisions["rows"]:
        permit = issued.get(row["pass_id"])
        row["permit"] = None if permit is None else {
            "permit_id": permit["permit_id"],
            "issued_at": permit["issued_at"],
            "plate": permit["plate"],
            "snapshot_sha256": permit["snapshot_sha256"],
        }
    decisions["issued_count"] = len(issued)
    decisions["manifest"] = {
        "run_id": manifest["run_id"],
        "sample": manifest["sample"],
        "created_at": manifest["created_at"],
        "result_sha256": manifest["result_sha256"],
        "rules": manifest["rules"],
    }
    return decisions


def issue_permit(store, run_id, pass_id):
    """按归档快照签发；已签发则原样返回，绝不重复或改写。

    放行写入在单个事务内提交；暂扣不写许可，拒绝事件单独落库后再抛错，
    因此失败路径也不会回滚掉既有许可与审计记录。
    """
    with store.transaction() as db:
        archived = find(db, run_id)
        if archived is None:
            raise DomainError("RUN_UNKNOWN", 404)
        existing = db.execute(
            "SELECT * FROM permits WHERE run_id = ? AND pass_id = ?", (run_id, pass_id),
        ).fetchone()
        if existing is not None:
            add_events(db, run_id, [(
                clock(), "info", "PERMIT_DUPLICATE",
                f"车次 {pass_id} 放行许可已签发 · 沿用 {existing['permit_id']}", None,
            )])
            return _permit_payload(dict(existing)), True
        manifest, results = archived
        chosen = next((row for row in gate_decisions(results)["rows"] if row["pass_id"] == pass_id), None)
        if chosen is None:
            raise DomainError("GATE_HOLD", 404, detail=f"车次 {pass_id} 不在批次 {run_id[:12]} 内")
        if chosen["decision"] != "release":
            hold_codes = list(chosen["codes"])
            hold_event = (
                clock(), "warn", "GATE_HOLD",
                f"车次 {pass_id} 暂扣 · " + " / ".join(hold_codes), None,
            )
        else:
            permit = {
                "run_id": run_id,
                "pass_id": pass_id,
                "permit_id": permit_identifier(run_id, pass_id),
                "plate": chosen["plate"],
                "issued_at": clock(),
                "decision": "release",
                "snapshot_sha256": manifest["result_sha256"],
            }
            add_permit(db, permit)
            add_events(db, run_id, [(
                permit["issued_at"], "ok", "PERMIT_ISSUED",
                f"车次 {pass_id}（{permit['plate']}）放行 · {permit['permit_id']}", None,
            )])
            return _permit_payload(permit), False
    # 事务已正常结束且未写许可；拒绝事件独立落库，保证失败可追溯、旧版不受影响。
    with store.transaction() as db:
        add_events(db, run_id, [hold_event])
    raise DomainError("GATE_HOLD", 409, detail=hold_codes)


def _permit_payload(permit):
    return {
        "run_id": permit["run_id"],
        "pass_id": permit["pass_id"],
        "permit_id": permit["permit_id"],
        "plate": permit["plate"],
        "issued_at": permit["issued_at"],
        "decision": permit["decision"],
        "snapshot_sha256": permit["snapshot_sha256"],
    }
