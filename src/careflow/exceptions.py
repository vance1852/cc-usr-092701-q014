"""特殊计划例外审批：偏离常规疗程节奏须经另一位医生对固定计划版本批准。"""

from __future__ import annotations

import json
from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import choice, object_value, parsed_timestamp, request_digest, text, timestamp


def plan_fingerprint(plan) -> str:
    """固定版本的计划内容摘要；状态或版本变化不影响内容，字段变化才影响。"""
    content = {key: plan[key] for key in
               ("kind", "clinical_owner", "assessment_id", "consent_id",
                "goal_json", "risk_json", "start_date", "target_date")}
    return request_digest(content)


class PlanExceptionService:
    """例外申请、交叉审批、失效与激活，全部在单事务内完成判定。"""

    def __init__(self, database: Database, clock):
        self.db, self.clock = database, clock

    # ---- 申请 ----------------------------------------------------------------

    def submit(self, clinic_id: str, actor_id: str, plan_id: str, *, deviation: dict,
               clinical_reason: str, assessment_id: str | None, valid_until: str,
               idempotency_key: str, revision_note: str | None = None) -> dict[str, Any]:
        deviation = object_value(deviation, "偏离的规则", allowed={"rule", "detail"})
        deviation["rule"] = text(deviation.get("rule", ""), "偏离规则", maximum=200)
        deviation["detail"] = text(deviation.get("detail", ""), "偏离说明", minimum=0, maximum=2000)
        clinical_reason = text(clinical_reason, "临床理由", maximum=3000)
        valid = timestamp(valid_until, "例外有效期")
        key = (idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise ValidationError("幂等键不能为空且不得超过 160 个字符")
        now = timestamp(self.clock.now())
        if parsed_timestamp(valid) <= parsed_timestamp(now):
            raise ValidationError("例外有效期必须晚于当前时间")
        note = text(revision_note or "", "修订说明", minimum=0, maximum=2000)
        request_hash = request_digest({"plan_id": plan_id, "deviation": deviation,
                                       "clinical_reason": clinical_reason, "assessment_id": assessment_id,
                                       "valid_until": valid})
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("只有医生或诊所负责人可以申请特殊计划例外")
            previous = connection.execute(
                "SELECT request_hash,response_json FROM idempotency WHERE scope='plan_exception_submit' AND key=?",
                (key,)).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise Conflict("幂等编号已用于其他申请内容")
                return {**json.loads(previous["response_json"]), "replayed": True}
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?", (plan_id, clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            if plan["state"] not in {"draft", "proposed"}:
                raise Conflict("仅草稿或已提议、尚未生效的计划可以申请例外")
            self._validate_assessment(connection, plan, assessment_id)
            self._validate_validity_bounds(connection, plan, valid, now)
            existing = connection.execute("SELECT * FROM plan_exceptions WHERE plan_id=?", (plan_id,)).fetchone()
            if existing is not None:
                if existing["state"] == "pending":
                    raise Conflict("该计划已有待审批的例外申请；退回后才能修订重提")
                if existing["state"] == "approved":
                    raise Conflict("该计划已有批准的例外；计划内容变更后需重新送审")
                # returned / invalidated / expired：作为对原申请的修订重新送审。
                result = self._resubmit(connection, existing, plan, actor_id, deviation, clinical_reason,
                                        assessment_id, valid, now, note)
            else:
                result = self._create(connection, plan, actor_id, deviation, clinical_reason,
                                      assessment_id, valid, now, note)
            connection.execute("INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) "
                               "VALUES('plan_exception_submit',?,?,?,?)",
                               (key, request_hash, json.dumps(result, ensure_ascii=False), now))
            return {**result, "replayed": False}

    def _create(self, connection, plan, actor_id, deviation, clinical_reason, assessment_id, valid, now, note):
        exception_id = new_id("exc")
        revision_no = 1
        digest = plan_fingerprint(plan)
        connection.execute(
            "INSERT INTO plan_exceptions(id,clinic_id,plan_id,patient_id,state,requested_by,current_revision,"
            "plan_version,plan_digest,created_at,updated_at) "
            "VALUES(?,?,?,?,'pending',?,?,?,?,?,?)",
            (exception_id, plan["clinic_id"], plan["id"], plan["patient_id"], actor_id, revision_no,
             plan["version"], digest, now, now))
        self._insert_revision(connection, exception_id, revision_no, plan, digest, deviation,
                              clinical_reason, assessment_id, valid, actor_id, now)
        self._event(connection, exception_id, 1, "submitted", actor_id, revision_no, plan["version"],
                    note or "提交特殊计划例外申请", now)
        audit.append_event(connection, clinic_id=plan["clinic_id"], actor_id=actor_id, patient_id=plan["patient_id"],
                           aggregate_type="plan_exception", aggregate_id=exception_id,
                           action="plan_exception.submitted", occurred_at=now,
                           payload={"plan_id": plan["id"], "plan_version": plan["version"], "revision": revision_no,
                                    "valid_until": valid, "rule": deviation["rule"]})
        row = connection.execute("SELECT * FROM plan_exceptions WHERE id=?", (exception_id,)).fetchone()
        return self._result(row, connection)

    def _resubmit(self, connection, existing, plan, actor_id, deviation, clinical_reason,
                  assessment_id, valid, now, note) -> dict[str, Any]:
        revision_no = existing["current_revision"] + 1
        digest = plan_fingerprint(plan)
        connection.execute(
            "UPDATE plan_exceptions SET state='pending',current_revision=?,plan_version=?,plan_digest=?,"
            "decided_by=NULL,approved_revision=NULL,approved_plan_version=NULL,approved_plan_digest=NULL,"
            "approved_at=NULL,valid_until=NULL,activated_at=NULL,updated_at=?,version=version+1 WHERE id=?",
            (revision_no, plan["version"], digest, now, existing["id"]))
        self._insert_revision(connection, existing["id"], revision_no, plan, digest, deviation,
                              clinical_reason, assessment_id, valid, actor_id, now)
        self._event(connection, existing["id"], None, "resubmitted", actor_id, revision_no, plan["version"],
                    note or f"第 {revision_no} 次修订后重新送审", now)
        audit.append_event(connection, clinic_id=existing["clinic_id"], actor_id=actor_id,
                           patient_id=plan["patient_id"], aggregate_type="plan_exception",
                           aggregate_id=existing["id"], action="plan_exception.resubmitted", occurred_at=now,
                           payload={"plan_id": plan["id"], "plan_version": plan["version"], "revision": revision_no,
                                    "previous_state": existing["state"], "valid_until": valid})
        row = connection.execute("SELECT * FROM plan_exceptions WHERE id=?", (existing["id"],)).fetchone()
        return self._result(row, connection)

    # ---- 审批 ----------------------------------------------------------------

    def decide(self, clinic_id: str, actor_id: str, exception_id: str, expected_version: int,
               action: str, *, note: str) -> dict[str, Any]:
        action = choice(action, "审批决定", {"approve", "return"})
        note = text(note, "审批意见", maximum=2000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("只有具备资质的医生可以审批特殊计划例外")
            row = connection.execute("SELECT * FROM plan_exceptions WHERE id=? AND clinic_id=?",
                                     (exception_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("例外申请不存在")
            if row["version"] != expected_version:
                raise Conflict("例外申请已被更新", details={"expected_version": expected_version,
                                                            "actual_version": row["version"]})
            if row["state"] != "pending":
                raise Conflict("只有待审批申请可以作出决定", details={"state": row["state"]})
            # 审批人不得是申请人；对修订后重新送审的申请同样适用。
            if row["requested_by"] == actor_id:
                raise Forbidden("审批人不能是申请人本人")
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?",
                                      (row["plan_id"], clinic_id)).fetchone()
            revision = connection.execute("SELECT * FROM plan_exception_revisions WHERE exception_id=? AND revision=?",
                                          (exception_id, row["current_revision"])).fetchone()
            if action == "return":
                connection.execute("UPDATE plan_exceptions SET state='returned',decided_by=?,updated_at=?,version=version+1 WHERE id=?",
                                   (actor_id, now, exception_id))
                self._event(connection, exception_id, None, "returned", actor_id, revision["revision"],
                            revision["plan_version"], note, now)
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                                   aggregate_type="plan_exception", aggregate_id=exception_id,
                                   action="plan_exception.returned", occurred_at=now,
                                   payload={"revision": revision["revision"], "note": note})
                result_state = "returned"
            else:
                # 批准前核验固定的计划内容；propose 等状态转换不改变内容，不阻断审批。
                if plan is None or plan_fingerprint(plan) != revision["plan_digest"]:
                    raise Conflict("送审计划内容与当前计划不一致；申请人须基于当前版本重新送审")
                blockers = self._approval_blockers(connection, plan, revision, now)
                if blockers:
                    raise Conflict("存在未解决的前置条件，不能批准例外", details={"blockers": blockers})
                valid_until = revision["valid_until"]
                connection.execute(
                    "UPDATE plan_exceptions SET state='approved',decided_by=?,approved_revision=?,"
                    "approved_plan_version=?,approved_plan_digest=?,approved_at=?,valid_until=?,updated_at=?,version=version+1 "
                    "WHERE id=?",
                    (actor_id, revision["revision"], plan["version"], revision["plan_digest"], now,
                     valid_until, now, exception_id))
                self._event(connection, exception_id, None, "approved", actor_id, revision["revision"],
                            plan["version"], note, now)
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                                   aggregate_type="plan_exception", aggregate_id=exception_id,
                                   action="plan_exception.approved", occurred_at=now,
                                   payload={"revision": revision["revision"], "plan_version": plan["version"],
                                            "valid_until": valid_until, "note": note})
                result_state = "approved"
            result = connection.execute("SELECT * FROM plan_exceptions WHERE id=?", (exception_id,)).fetchone()
            return self._result(result, connection, state_override=result_state)

    def _approval_blockers(self, connection, plan, revision, now: str) -> list[str]:
        blockers: list[str] = []
        from .clinical_flags import ClinicalFlagService

        if ClinicalFlagService(self.db, self.clock).blocking_flags(connection, plan["patient_id"], now):
            blockers.append("unreviewed_stop_flag")
        if plan["consent_id"]:
            consent = connection.execute("SELECT state,expires_at FROM consents WHERE id=?",
                                         (plan["consent_id"],)).fetchone()
            if consent is None or consent["state"] != "granted" \
                    or (consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now)):
                blockers.append("consent_unavailable")
        if parsed_timestamp(revision["valid_until"]) <= parsed_timestamp(now):
            blockers.append("validity_window_passed")
        if revision["assessment_id"]:
            assessment = connection.execute("SELECT status FROM assessments WHERE id=? AND patient_id=?",
                                            (revision["assessment_id"], plan["patient_id"])).fetchone()
            if assessment is None or assessment["status"] != "signed":
                blockers.append("assessment_not_signed")
        return blockers

    # ---- 撤回 ----------------------------------------------------------------

    def withdraw(self, clinic_id: str, actor_id: str, exception_id: str, expected_version: int,
                 reason: str) -> dict[str, Any]:
        """申请人放弃例外；撤回后计划可按常规安排生效，审批链仍保留。"""
        reason = text(reason, "撤回原因", maximum=2000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM plan_exceptions WHERE id=? AND clinic_id=?",
                                     (exception_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("例外申请不存在")
            if row["version"] != expected_version:
                raise Conflict("例外申请已被更新", details={"expected_version": expected_version,
                                                            "actual_version": row["version"]})
            if row["state"] == "withdrawn":
                return self._result(row, connection, replayed=True)
            if row["state"] == "activated":
                raise Conflict("例外已随计划生效，不能撤回")
            if row["requested_by"] != actor_id and principal.role != "owner":
                raise Forbidden("只有申请人或诊所负责人可以撤回例外申请")
            connection.execute("UPDATE plan_exceptions SET state='withdrawn',updated_at=?,version=version+1 WHERE id=?",
                               (now, exception_id))
            self._event(connection, exception_id, None, "withdrawn", actor_id, row["current_revision"],
                        row["plan_version"], reason, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="plan_exception", aggregate_id=exception_id,
                               action="plan_exception.withdrawn", occurred_at=now,
                               payload={"previous_state": row["state"], "reason": reason})
            result = connection.execute("SELECT * FROM plan_exceptions WHERE id=?", (exception_id,)).fetchone()
            return self._result(result, connection)

    # ---- 生效 ----------------------------------------------------------------

    def gate_activation(self, connection, exception_id: str, plan, actor_id: str,
                        new_plan_version: int, now: str):
        """在计划 proposed→active 同一事务内核验并消费批准的例外。

        返回 (exception_id, None) 表示通过；返回 (None, (message, details)) 表示拒绝，
        其中内容漂移会先把审批标记为失效（随事务提交），再由调用方在事务外抛出冲突。
        """
        row = connection.execute("SELECT * FROM plan_exceptions WHERE id=?", (exception_id,)).fetchone()
        if row is None or row["plan_id"] != plan["id"]:
            return None, ("例外申请不存在或不属于该计划", {"exception_id": exception_id})
        if row["state"] == "activated":
            return None, ("该例外已用于计划生效", {"plan_version": row["activated_activation_version"]})
        if row["state"] != "approved":
            return None, ("例外尚未获得批准，不能用于计划生效", {"state": row["state"]})
        if row["approved_plan_digest"] != plan_fingerprint(plan):
            # 失效已在计划内容修订的事务内记录；此处只读拒绝，不依赖回滚中的写入。
            return None, ("计划内容自批准后已变化，例外已失效，须重新送审", {"exception_id": exception_id})
        if row["valid_until"] is None or parsed_timestamp(row["valid_until"]) <= parsed_timestamp(now):
            self._mark_expired(connection, row, actor_id, now, "激活时有效期已过")
            return None, ("例外审批已过期，不能被迟到的激活请求使用", {"valid_until": row["valid_until"]})
        revision = connection.execute("SELECT * FROM plan_exception_revisions WHERE exception_id=? AND revision=?",
                                      (exception_id, row["approved_revision"])).fetchone()
        blockers = self._approval_blockers(connection, plan, revision, now)
        if blockers:
            return None, ("例外的临床前置条件不再满足，不能激活计划", {"blockers": blockers})
        connection.execute(
            "UPDATE plan_exceptions SET state='activated',activated_at=?,activated_activation_version=?,"
            "updated_at=?,version=version+1 WHERE id=?",
            (now, new_plan_version, now, exception_id))
        self._event(connection, exception_id, None, "activated", actor_id, row["approved_revision"],
                    new_plan_version, "例外已登记为计划生效依据", now)
        audit.append_event(connection, clinic_id=row["clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                           aggregate_type="plan_exception", aggregate_id=exception_id,
                           action="plan_exception.activated", occurred_at=now,
                           payload={"plan_id": plan["id"], "plan_version": new_plan_version,
                                    "approved_revision": row["approved_revision"], "valid_until": row["valid_until"]})
        return exception_id, None

    def expire_due(self, clinic_id: str, *, limit: int = 200) -> dict[str, Any]:
        """将已过有效期、尚未用于计划生效的批准标记为过期，便于运营区分。"""
        if not 1 <= limit <= 1000:
            raise ValidationError("处理数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_exceptions WHERE clinic_id=? AND state='approved' AND valid_until<=? "
                "ORDER BY valid_until,id LIMIT ?", (clinic_id, now, limit)).fetchall()
            for row in rows:
                connection.execute("UPDATE plan_exceptions SET state='expired',updated_at=?,version=version+1 WHERE id=?",
                                   (now, row["id"]))
                self._event(connection, row["id"], None, "expired", None, row["approved_revision"],
                            row["approved_plan_version"], "有效期届满且未用于计划生效", now)
                audit.append_event(connection, clinic_id=clinic_id, actor_id=None, patient_id=row["patient_id"],
                                   aggregate_type="plan_exception", aggregate_id=row["id"],
                                   action="plan_exception.expired", occurred_at=now,
                                   payload={"valid_until": row["valid_until"]})
        return {"expired": len(rows), "as_of": now}

    def invalidate_for_plan_change(self, connection, exception_row, actor_id: str | None, now: str,
                                   reason: str, plan_version) -> None:
        """计划内容修订后，由计划服务在同一事务内调用。"""
        self._invalidate(connection, exception_row, actor_id, now, reason, plan_version=plan_version)

    def _mark_expired(self, connection, row, actor_id: str | None, now: str, reason: str) -> None:
        """迟到激活触发的过期落库；调用方事务会正常提交，随后在事务外拒绝请求。"""
        connection.execute("UPDATE plan_exceptions SET state='expired',updated_at=?,version=version+1 WHERE id=?",
                           (now, row["id"]))
        self._event(connection, row["id"], None, "expired", actor_id, row["approved_revision"],
                    row["approved_plan_version"], reason, now)
        audit.append_event(connection, clinic_id=row["clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                           aggregate_type="plan_exception", aggregate_id=row["id"],
                           action="plan_exception.expired", occurred_at=now,
                           payload={"valid_until": row["valid_until"], "reason": reason})

    def _invalidate(self, connection, row, actor_id, now: str, reason: str, *, plan_version) -> None:
        if row["state"] not in {"pending", "approved", "activated"}:
            return
        connection.execute("UPDATE plan_exceptions SET state='invalidated',updated_at=?,version=version+1 WHERE id=?",
                           (now, row["id"]))
        self._event(connection, row["id"], None, "invalidated", actor_id, row["current_revision"],
                    plan_version, reason, now)
        audit.append_event(connection, clinic_id=row["clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                           aggregate_type="plan_exception", aggregate_id=row["id"],
                           action="plan_exception.invalidated", occurred_at=now,
                           payload={"reason": reason, "previous_state": row["state"], "plan_version": plan_version})
        if row["state"] == "activated":
            connection.execute("UPDATE plans SET approved_exception_id=NULL,updated_at=? WHERE id=?",
                               (now, row["plan_id"]))

    # ---- 查询与版本链 ----------------------------------------------------------

    def get(self, clinic_id: str, actor_id: str, exception_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM plan_exceptions WHERE id=? AND clinic_id=?",
                                     (exception_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("例外申请不存在")
            return self._result(row, connection)

    def list_for_patient(self, clinic_id: str, actor_id: str, patient_id: str) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone() is None:
                raise NotFound("患者不存在")
            rows = connection.execute("SELECT * FROM plan_exceptions WHERE clinic_id=? AND patient_id=? ORDER BY created_at,id",
                                      (clinic_id, patient_id)).fetchall()
            return [self._result(row, connection) for row in rows]

    def schedule_view(self, clinic_id: str, actor_id: str) -> list[dict[str, Any]]:
        """运营视图：只显示区分常规安排与批准例外所需的排程字段，不含临床理由与评估细节。"""
        from .security import ROLE_PERMISSIONS

        with self.db.transaction(write=False) as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            if not ({"appointment:read", "clinical:read"} & ROLE_PERMISSIONS.get(principal.role, set())):
                raise Forbidden("当前岗位无权查看计划排程视图")
            rows = connection.execute(
                "SELECT p.id AS plan_id,p.patient_id,p.kind,p.state,p.approved_exception_id,"
                "e.id AS exception_id,e.state AS exception_state,e.valid_until,e.decided_by,e.activated_at "
                "FROM plans p LEFT JOIN plan_exceptions e ON e.id=p.approved_exception_id "
                "WHERE p.clinic_id=? AND p.state IN ('proposed','active','paused') ORDER BY p.updated_at,p.id",
                (clinic_id,)).fetchall()
            return [{"plan_id": row["plan_id"], "patient_id": row["patient_id"], "kind": row["kind"],
                     "plan_state": row["state"], "arrangement": "approved_exception" if row["approved_exception_id"] else "routine",
                     "exception_id": row["exception_id"], "exception_state": row["exception_state"],
                     "exception_valid_until": row["valid_until"], "exception_activated_at": row["activated_at"]}
                    for row in rows]

    def chain(self, clinic_id: str, actor_id: str, exception_id: str) -> dict[str, Any]:
        """申请、修订、审批与最终生效之间的完整版本链。"""
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM plan_exceptions WHERE id=? AND clinic_id=?",
                                     (exception_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("例外申请不存在")
            revisions = connection.execute("SELECT * FROM plan_exception_revisions WHERE exception_id=? ORDER BY revision",
                                           (exception_id,)).fetchall()
            events = connection.execute("SELECT * FROM plan_exception_events WHERE exception_id=? ORDER BY sequence",
                                        (exception_id,)).fetchall()
            plan_revisions = connection.execute(
                "SELECT revision,snapshot_json,changed_by,change_reason,created_at "
                "FROM plan_revisions WHERE plan_id=? ORDER BY revision",
                (row["plan_id"],)).fetchall()
            return {"exception": self._result(row, connection),
                    "revisions": [{"revision": r["revision"], "plan_version": r["plan_version"],
                                   "plan_digest": r["plan_digest"], "deviation": decode_json(r["deviation_json"]),
                                   "clinical_reason": r["clinical_reason"], "assessment_id": r["assessment_id"],
                                   "valid_until": r["valid_until"], "submitted_by": r["submitted_by"],
                                   "created_at": r["created_at"]} for r in revisions],
                    "events": [{"sequence": e["sequence"], "type": e["event_type"], "actor_id": e["actor_id"],
                                "revision": e["revision"], "plan_version": e["plan_version"],
                                "note": e["note"], "created_at": e["created_at"]} for e in events],
                    "plan_revisions": [{"revision": r["revision"], "snapshot": decode_json(r["snapshot_json"]),
                                        "changed_by": r["changed_by"], "reason": r["change_reason"],
                                        "created_at": r["created_at"]} for r in plan_revisions]}

    # ---- 内部工具 --------------------------------------------------------------

    def _validate_assessment(self, connection, plan, assessment_id: str | None) -> None:
        if not assessment_id:
            raise ValidationError("例外申请必须关联评估")
        assessment = connection.execute("SELECT status FROM assessments WHERE id=? AND patient_id=?",
                                        (assessment_id, plan["patient_id"])).fetchone()
        if assessment is None:
            raise NotFound("关联评估不存在")
        if assessment["status"] != "signed":
            raise Conflict("关联评估必须已签署")

    def _validate_validity_bounds(self, connection, plan, valid_until: str, now: str) -> None:
        # 例外有效期不得超过关联授权到期时间。
        if plan["consent_id"]:
            consent = connection.execute("SELECT state,expires_at FROM consents WHERE id=?",
                                         (plan["consent_id"],)).fetchone()
            if consent is None or consent["state"] != "granted":
                raise Conflict("计划关联授权已不可用，不能申请例外")
            if consent["expires_at"] and parsed_timestamp(valid_until) > parsed_timestamp(consent["expires_at"]):
                raise ValidationError("例外有效期不能超过关联授权的到期时间")

    @staticmethod
    def _insert_revision(connection, exception_id, revision_no, plan, digest, deviation,
                         clinical_reason, assessment_id, valid, actor_id, now) -> None:
        connection.execute(
            "INSERT INTO plan_exception_revisions(exception_id,revision,plan_version,plan_digest,deviation_json,"
            "clinical_reason,assessment_id,valid_until,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (exception_id, revision_no, plan["version"], digest, encode_json(deviation),
             clinical_reason, assessment_id, valid, actor_id, now))

    @staticmethod
    def _event(connection, exception_id, sequence, event_type, actor_id, revision, plan_version, note, now) -> None:
        if sequence is None:
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM plan_exception_events WHERE exception_id=?",
                                          (exception_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO plan_exception_events(id,exception_id,sequence,event_type,actor_id,revision,plan_version,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("exe"), exception_id, sequence, event_type, actor_id, revision, plan_version, note, now))

    @staticmethod
    def _result(row, connection, *, replayed: bool = False, state_override: str | None = None) -> dict[str, Any]:
        revision = connection.execute("SELECT * FROM plan_exception_revisions WHERE exception_id=? AND revision=?",
                                      (row["id"], row["current_revision"])).fetchone()
        result = {"id": row["id"], "plan_id": row["plan_id"], "patient_id": row["patient_id"],
                  "state": state_override or row["state"], "requested_by": row["requested_by"],
                  "decided_by": row["decided_by"], "revision": row["current_revision"],
                  "plan_version": row["plan_version"], "plan_digest": row["plan_digest"],
                  "approved_revision": row["approved_revision"],
                  "approved_plan_version": row["approved_plan_version"],
                  "approved_at": row["approved_at"], "valid_until": row["valid_until"],
                  "activated_at": row["activated_at"], "created_at": row["created_at"],
                  "updated_at": row["updated_at"], "version": row["version"]}
        if revision is not None:
            result["deviation"] = decode_json(revision["deviation_json"])
            result["clinical_reason"] = revision["clinical_reason"]
            result["assessment_id"] = revision["assessment_id"]
        if replayed:
            result["replayed"] = True
        return result
