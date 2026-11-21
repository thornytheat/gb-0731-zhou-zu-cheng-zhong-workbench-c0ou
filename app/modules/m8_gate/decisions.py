from app.config import TICKET_TOLERANCE
from app.reasons import REASONS

# 闸口只认勾稽结论：matched 表示轮载证据完整、磅单有效且差额在容差内。
# 任一复核原因码都意味着证据链未闭合，车辆必须暂扣等待补证。
GATE_RULES = {
    "rule_version": "gate-1.0.0",
    "ticket_tolerance_kg": str(TICKET_TOLERANCE),
}


def decide(reconcile_row):
    """依据同一批次勾稽行推导单台车的闸口结论，不重新读取任何源文件。"""
    codes = tuple(reconcile_row["codes"])
    releasable = reconcile_row["status"] == "matched" and not codes
    return {
        "pass_id": reconcile_row["pass_id"],
        "plate": reconcile_row["plate"],
        "decision": "release" if releasable else "hold",
        "codes": list(codes),
        "reasons": [REASONS[code] for code in codes],
        "pass_ref": reconcile_row["pass_ref"],
        "ticket_refs": list(reconcile_row["ticket_refs"]),
        "wheel_refs": list(reconcile_row["wheel_refs"]),
        "review_refs": list(reconcile_row["review_refs"]),
        "wheel_kg": reconcile_row["wheel_kg"],
        "difference_kg": reconcile_row["difference_kg"],
        "ticket_id": reconcile_row["ticket"]["id"] if reconcile_row["ticket"] else None,
        "ticket_revision": reconcile_row["ticket"]["revision"] if reconcile_row["ticket"] else None,
    }


def gate_decisions(results):
    rows = [decide(row) for row in results["reconcile"]["rows"]]
    release = [row["pass_id"] for row in rows if row["decision"] == "release"]
    hold = [row["pass_id"] for row in rows if row["decision"] == "hold"]
    return {
        "rules": GATE_RULES,
        "rows": rows,
        "scope": {
            "total": len(rows),
            "release_count": len(release),
            "hold_count": len(hold),
            "release_pass_ids": release,
            "hold_pass_ids": hold,
        },
    }
