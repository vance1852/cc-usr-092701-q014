"""特殊诊疗计划例外审批。

申请固定在提交时的计划内容摘要上：另一位具备资质的医生只能对该版本同意或退回。
计划内容一旦变化，未结束的申请与已同意审批同步失效；激活时再次核对内容、有效期、
授权与停止级安全关注项，保证迟到的激活请求不能复用过期审批。
"""

from __future__ import annotations

from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id, require_idempotency_key
from .security import authorize, principal_for
from .validation import (
    parsed_timestamp,
    request_digest,
    require_match,
    text,
    timestamp,
)

PLAN_CONTENT_FIELDS = (
    "kind", "clinical_owner", "assessment_id", "consent_id",
    "goal_json", "risk_json", "start_date", "target_date",
)


def authorize_read(principal, clinic_id: str) -> None:
    """运营人员也需要识别常规安排与已批准例外，患者档案读权限即可查看状态。"""
    from .security import ROLE_PERMISSIONS

    if not principal.active:
        from .errors import Unauthorized
        raise Unauthorized("账号已停用")
    if principal.clinic_id != clinic_id:
        raise NotFound("记录不存在")
    if not ({"clinical:read", "patient:read"} & ROLE_PERMISSIONS.get(principal.role, set())):
        raise Forbidden("当前岗位无权查看例外审批")


def plan_content_digest(plan_row) -> str:
    """计划“内容”的稳定摘要；仅状态或版本号变化不改变摘要。"""
    return request_digest({key: plan_row[key] for key in PLAN_CONTENT_FIELDS})


class PlanExceptionService:
    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ 申请

    def submit(self, clinic_id: str, actor_id: str, plan_id: str, *,
               rule: str, clinical_reason: str, assessment_id: str,
               valid_until: str, idempotency_key: str) -> dict[str, Any]:
        rule = text(rule, "偏离的规则", maximum=300)
        clinical_reason = text(clinical_reason, "临床理由", maximum=2000)
        valid_until = timestamp(valid_until, "例外有效期")
        key = require_idempotency_key(idempotency_key)
        now = timestamp(self.clock.now())
        if parsed_timestamp(valid_until) <= parsed_timestamp(now):
            raise ValidationError("例外有效期必须晚于当前时间")
        request = {"plan_id": plan_id, "rule": rule, "clinical_reason": clinical_reason,
                   "assessment_id": assessment_id, "valid_until": valid_until}
        digest = request_digest(request)
        exception_id = new_id("pex")
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("只有具备资质的医生可以提交特殊计划例外申请")
            previous = connection.execute(
                "SELECT * FROM idempotency WHERE scope='plan_exception' AND key=?", (key,)
            ).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise Conflict("例外申请幂等编号已用于其他内容")
                # 同一申请重复提交返回原记录（含其最新审批状态）。
                saved_id = decode_json(previous["response_json"])["id"]
                return self._load(connection, saved_id, replayed=True)
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?", (plan_id, clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            if plan["state"] not in {"draft", "proposed", "paused"}:
                raise Conflict("当前计划状态不能提交例外申请")
            assessment = connection.execute(
                "SELECT id,status FROM assessments WHERE id=? AND patient_id=? AND clinic_id=?",
                (assessment_id, plan["patient_id"], clinic_id)).fetchone()
            if assessment is None:
                raise ValidationError("关联评估不存在")
            if assessment["status"] != "signed":
                raise Conflict("关联评估必须已签署")
            fixed_digest = plan_content_digest(plan)
            connection.execute(
                "INSERT INTO plan_exceptions(id,plan_id,clinic_id,patient_id,status,applicant_id,plan_version,plan_digest,"
                "rule,clinical_reason,assessment_id,valid_until,requested_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (exception_id, plan_id, clinic_id, plan["patient_id"], "pending", actor_id, plan["version"],
                 fixed_digest, rule, clinical_reason, assessment_id, valid_until, now, key))
            self._event(connection, exception_id, "submitted", actor_id, None, plan["version"], now)
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                               ("plan_exception", key, digest, encode_json({"id": exception_id}), now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=plan["patient_id"],
                               aggregate_type="plan_exception", aggregate_id=exception_id,
                               action="plan_exception.submitted", occurred_at=now,
                               payload={"plan_id": plan_id, "plan_version": plan["version"],
                                        "plan_digest": fixed_digest, "rule": rule, "valid_until": valid_until})
            return self._load(connection, exception_id, replayed=False)

    # ------------------------------------------------------------------ 审批

    def decide(self, clinic_id: str, actor_id: str, exception_id: str, action: str,
               note: str, *, expected_version: int) -> dict[str, Any]:
        if action not in {"approve", "return"}:
            raise ValidationError("审批结论无效")
        note = text(note, "退回原因" if action == "return" else "审批备注",
                    minimum=1 if action == "return" else 0, maximum=2000)
        now = timestamp(self.clock.now())
        event_type = "approved" if action == "approve" else "returned"
        target_state = "approved" if action == "approve" else "returned"
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("只有具备资质的医生可以审批特殊计划例外")
            row = connection.execute("SELECT * FROM plan_exceptions WHERE id=? AND clinic_id=?",
                                     (exception_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("例外申请不存在")
            require_match(row["version"], expected_version, "例外申请")
            if row["status"] != "pending":
                raise Conflict("只有待审批申请可以作出决定", details={"status": row["status"]})
            if row["applicant_id"] == actor_id:
                raise Conflict("审批人不得是申请人本人")
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?", (row["plan_id"], clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            if plan_content_digest(plan) != row["plan_digest"]:
                raise Conflict("送审计划版本与当前内容不一致，申请须重新提交")
            if action == "approve":
                if parsed_timestamp(row["valid_until"]) <= parsed_timestamp(now):
                    raise Conflict("申请已超过其有效期，不能批准")
                self._assert_no_blocking_conditions(connection, plan, row, now)
            connection.execute(
                "UPDATE plan_exceptions SET status=?,reviewer_id=?,decided_at=?,decision_note=?,version=version+1 WHERE id=?",
                (target_state, actor_id, now, note, exception_id))
            self._event(connection, exception_id, event_type, actor_id, note, plan["version"], now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="plan_exception", aggregate_id=exception_id,
                               action=f"plan_exception.{event_type}", occurred_at=now,
                               payload={"plan_id": row["plan_id"], "plan_version": row["plan_version"],
                                        "applicant_id": row["applicant_id"], "note": note,
                                        "valid_until": row["valid_until"]})
            return self._load(connection, exception_id)

    def _assert_no_blocking_conditions(self, connection, plan, exception_row, now: str) -> None:
        flags = connection.execute(
            "SELECT id,category,severity FROM clinical_flags WHERE patient_id=? AND severity='stop' AND state='reported' "
            "AND effective_from<=? AND (effective_until IS NULL OR effective_until>?) ORDER BY id",
            (plan["patient_id"], now, now)).fetchall()
        if flags:
            raise Conflict("存在未复核的停止级安全关注项，不得批准例外",
                           details={"flag_ids": [item["id"] for item in flags]})
        if plan["consent_id"]:
            consent = connection.execute("SELECT state,expires_at FROM consents WHERE id=?", (plan["consent_id"],)).fetchone()
            if consent is None or consent["state"] != "granted" or \
                    (consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now)):
                raise Conflict("计划关联授权已撤回或过期，不得批准例外")

    # ----------------------------------------------------------- 激活前校验

    def require_usable_approval(self, connection, plan, now: str) -> dict[str, Any] | None:
        """在计划激活事务内调用。

        返回可用于本次激活的审批；计划从未提交过例外申请时返回 None（常规路径）。
        其余情况抛出冲突，说明待审批、已退回、版本不符或已过期。
        """
        rows = connection.execute(
            "SELECT * FROM plan_exceptions WHERE plan_id=? ORDER BY requested_at,id",
            (plan["id"],)).fetchall()
        if not rows:
            return None
        current_digest = plan_content_digest(plan)
        usable = [row for row in rows if row["status"] == "approved"
                  and row["plan_digest"] == current_digest
                  and parsed_timestamp(row["valid_until"]) > parsed_timestamp(now)]
        if usable:
            return usable[-1]
        pending = [row for row in rows if row["status"] == "pending"]
        if pending:
            latest_pending = pending[-1]
            if parsed_timestamp(latest_pending["valid_until"]) <= parsed_timestamp(now):
                raise Conflict("例外申请已超过有效期，须重新提交", details={"exception_id": latest_pending["id"]})
            raise Conflict("计划的例外申请尚待另一位医生审批", details={"exception_id": latest_pending["id"]})
        latest = rows[-1]
        if latest["status"] == "approved" and latest["plan_digest"] != current_digest:
            raise Conflict("已批准的例外针对另一计划版本，须重新送审", details={"exception_id": latest["id"]})
        if latest["status"] == "approved":
            raise Conflict("例外审批已过期，迟到的激活请求不能使用", details={"exception_id": latest["id"]})
        if latest["status"] == "returned":
            raise Conflict("例外申请已被退回，须按意见修订后重新送审", details={"exception_id": latest["id"]})
        raise Conflict("此前的例外审批已失效，须重新送审", details={"exception_id": latest["id"]})

    def mark_effectuated(self, connection, exception_row, plan, actor_id: str, now: str,
                         plan_version: int) -> bool:
        if exception_row["effectuated_at"] is not None:
            return False
        connection.execute("UPDATE plan_exceptions SET effectuated_at=? WHERE id=?", (now, exception_row["id"]))
        self._event(connection, exception_row["id"], "effectuated", actor_id, None, plan_version, now)
        audit.append_event(connection, clinic_id=plan["clinic_id"], actor_id=actor_id, patient_id=plan["patient_id"],
                           aggregate_type="plan_exception", aggregate_id=exception_row["id"],
                           action="plan_exception.effectuated", occurred_at=now,
                           payload={"plan_id": plan["id"], "plan_version": plan_version})
        return True

    # ------------------------------------------------------------- 修订作废

    def void_for_content_change(self, connection, plan, old_digest: str, new_digest: str,
                                actor_id: str, now: str) -> list[str]:
        """计划内容变化后，仍针对旧内容的待审批/已同意申请全部失效。"""
        if old_digest == new_digest:
            return []
        rows = connection.execute(
            "SELECT * FROM plan_exceptions WHERE plan_id=? AND status IN ('pending','approved')",
            (plan["id"],)).fetchall()
        voided = []
        for row in rows:
            reason = "计划内容在审批后发生变化，审批失效并须重新送审" if row["status"] == "approved" \
                else "送审计划版本已被后续修订取代，申请失效"
            connection.execute(
                "UPDATE plan_exceptions SET status='voided',voided_at=?,void_reason=?,version=version+1 WHERE id=?",
                (now, reason, row["id"]))
            self._event(connection, row["id"], "voided", actor_id, reason, plan["version"], now)
            audit.append_event(connection, clinic_id=plan["clinic_id"], actor_id=actor_id, patient_id=plan["patient_id"],
                               aggregate_type="plan_exception", aggregate_id=row["id"],
                               action="plan_exception.voided", occurred_at=now,
                               payload={"plan_id": plan["id"], "previous_status": row["status"], "reason": reason})
            voided.append(row["id"])
        return voided

    # ----------------------------------------------------------------- 查询

    def get(self, clinic_id: str, actor_id: str, exception_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize_read(principal_for(connection, actor_id, clinic_id), clinic_id)
            row = connection.execute("SELECT * FROM plan_exceptions WHERE id=? AND clinic_id=?",
                                     (exception_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("例外申请不存在")
            return self._load(connection, exception_id)

    def chain_for_plan(self, clinic_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        """申请、修订、审批与最终生效的完整版本链。"""
        with self.db.transaction(write=False) as connection:
            authorize_read(principal_for(connection, actor_id, clinic_id), clinic_id)
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?", (plan_id, clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            revisions = [
                {"revision": row["revision"], "changed_by": row["changed_by"], "reason": row["change_reason"],
                 "created_at": row["created_at"], "snapshot": decode_json(row["snapshot_json"])}
                for row in connection.execute("SELECT * FROM plan_revisions WHERE plan_id=? ORDER BY revision", (plan_id,)).fetchall()
            ]
            exceptions = [
                self._load(connection, row["id"])
                for row in connection.execute("SELECT id FROM plan_exceptions WHERE plan_id=? ORDER BY requested_at,id",
                                              (plan_id,)).fetchall()
            ]
            activation_events = [
                {"sequence": row["sequence"], "action": row["action"], "actor_id": row["actor_id"],
                 "occurred_at": row["occurred_at"], "payload": decode_json(row["payload_json"])}
                for row in connection.execute(
                    "SELECT * FROM audit_events WHERE clinic_id=? AND aggregate_type='plan' AND aggregate_id=? "
                    "AND action IN ('plan.activate','plan.propose','plan.paused','plan.paused.consent_withdrawn') "
                    "ORDER BY sequence", (clinic_id, plan_id)).fetchall()
            ]
            return {"plan_id": plan_id, "plan_state": plan["state"], "plan_version": plan["version"],
                    "plan_digest": plan_content_digest(plan), "revisions": revisions,
                    "exceptions": exceptions, "plan_events": activation_events}

    def pending_queue(self, clinic_id: str, actor_id: str, *, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValidationError("查询数量须为 1 至 500")
        with self.db.transaction(write=False) as connection:
            authorize_read(principal_for(connection, actor_id, clinic_id), clinic_id)
            rows = connection.execute(
                "SELECT id FROM plan_exceptions WHERE clinic_id=? AND status='pending' ORDER BY requested_at,id LIMIT ?",
                (clinic_id, limit)).fetchall()
            now = timestamp(self.clock.now())
            return {"clinic_id": clinic_id, "as_of": now,
                    "items": [self._load(connection, row["id"]) for row in rows], "returned": len(rows)}

    # ----------------------------------------------------------------- 内部

    def _event(self, connection, exception_id: str, event_type: str, actor_id: str,
               note: str | None, plan_version: int, now: str) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM plan_exception_events WHERE exception_id=?", (exception_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO plan_exception_events(id,exception_id,sequence,event_type,actor_id,note,plan_version,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (new_id("pee"), exception_id, sequence, event_type, actor_id, note, plan_version, now))

    @staticmethod
    def _load(connection, exception_id: str, *, replayed: bool = False) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM plan_exceptions WHERE id=?", (exception_id,)).fetchone()
        events = [
            {"sequence": item["sequence"], "type": item["event_type"], "actor_id": item["actor_id"],
             "note": item["note"], "plan_version": item["plan_version"], "occurred_at": item["occurred_at"]}
            for item in connection.execute(
                "SELECT * FROM plan_exception_events WHERE exception_id=? ORDER BY sequence", (exception_id,)).fetchall()
        ]
        result = {
            "id": row["id"], "plan_id": row["plan_id"], "patient_id": row["patient_id"],
            "status": row["status"], "applicant_id": row["applicant_id"], "reviewer_id": row["reviewer_id"],
            "plan_version": row["plan_version"], "plan_digest": row["plan_digest"],
            "rule": row["rule"], "clinical_reason": row["clinical_reason"],
            "assessment_id": row["assessment_id"], "valid_until": row["valid_until"],
            "requested_at": row["requested_at"], "decided_at": row["decided_at"],
            "decision_note": row["decision_note"], "voided_at": row["voided_at"],
            "void_reason": row["void_reason"], "effectuated_at": row["effectuated_at"],
            "version": row["version"], "events": events,
        }
        if replayed:
            result["replayed"] = True
        return result
