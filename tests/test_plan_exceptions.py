from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, ValidationError
from careflow.service import Careflow


class PlanExceptionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.doctor_a = self.app.create_staff(self.clinic, "甲医生", "clinician", actor_id=self.owner)["id"]
        self.doctor_b = self.app.create_staff(self.clinic, "乙医生", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-ex-1", "陈先生")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def proposed_plan(self, *, valid_until="2026-10-27T12:00:00Z", revision=1):
        digest = hashlib.sha256(f"weight-r{revision}".encode()).hexdigest()
        consent = self.app.grant_consent(self.clinic, self.doctor_a, self.patient, "weight_program",
                                         revision, digest)
        assessment = self.app.create_assessment(self.clinic, self.doctor_a, self.patient, "weight",
                                                {"weight_kg": "95"}, {"goal": "减重"})
        self.app.sign_assessment(self.clinic, self.doctor_a, assessment["id"], expected_version=1)
        plan = self.app.create_plan(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a,
            {"description": "超出常规疗程节奏的强化方案", "review_interval_days": 14},
            {"screening": "reviewed"}, "2026-09-27", target_date="2027-03-27",
            assessment_id=assessment["id"], consent_id=consent["id"])
        self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 1, "propose")
        return plan, assessment

    def submit(self, plan, assessment, key="exc-1", valid_until="2026-10-27T12:00:00Z", doctor=None):
        return self.app.plan_exceptions.submit(
            self.clinic, doctor or self.doctor_a, plan["id"],
            rule="复核间隔由门诊常规 30 天缩短为 14 天",
            clinical_reason="患者合并代谢风险因素，需要更密集随访",
            assessment_id=assessment["id"], valid_until=valid_until, idempotency_key=key)

    def test_full_chain_submission_independent_review_then_activation(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        self.assertEqual(application["status"], "pending")
        self.assertEqual(application["plan_version"], 2)
        # 未批准不能生效。
        with self.assertRaises(Conflict):
            self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        # 申请人不能审批自己的申请，护士也不具备资质。
        with self.assertRaises(Conflict):
            self.app.plan_exceptions.decide(self.clinic, self.doctor_a, application["id"], "approve", "同意",
                                            expected_version=1)
        with self.assertRaises(Forbidden):
            self.app.plan_exceptions.decide(self.clinic, self.nurse, application["id"], "approve", "同意",
                                            expected_version=1)
        # 另一位医生同意，版本保护生效。
        with self.assertRaises(Conflict):
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                            expected_version=99)
        decision = self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve",
                                                   "已核对评估与授权", expected_version=1)
        self.assertEqual(decision["status"], "approved")
        self.assertEqual(decision["reviewer_id"], self.doctor_b)
        activated = self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        self.assertEqual(activated["state"], "active")
        detail = self.app.plan_exceptions.get(self.clinic, self.doctor_b, application["id"])
        self.assertIsNotNone(detail["effectuated_at"])
        self.assertEqual([event["type"] for event in detail["events"]],
                         ["submitted", "approved", "effectuated"])

    def test_returned_application_blocks_activation_and_resubmission_after_revision_works(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        with self.assertRaises(ValidationError):
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "return", "",
                                            expected_version=1)
        self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "return",
                                        "理由不足以缩短间隔，请补充客观指标", expected_version=1)
        with self.assertRaises(Conflict):
            self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        self.app.revise_plan(self.clinic, self.doctor_a, plan["id"], 2, reason="按退回意见补充风险摘要",
                             risk={"screening": "reviewed", "contraindications": [], "review_required": True})
        second = self.submit(plan, assessment, key="exc-2")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["plan_version"], 3)
        self.app.plan_exceptions.decide(self.clinic, self.doctor_b, second["id"], "approve", "补充材料充分",
                                        expected_version=1)
        activated = self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 3, "activate")
        self.assertEqual(activated["state"], "active")

    def test_unreviewed_stop_flag_blocks_approval_until_confirmed(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        self.app.clinical_flags.report(self.clinic, self.nurse, self.patient, "prior_reaction", "stop",
                                       "既往同类药物出现不良反应")
        with self.assertRaises(Conflict) as blocked:
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                            expected_version=1)
        self.assertIn("停止级安全关注项", blocked.exception.message)
        # 停止级关注项经另一位医生复核确认后不再构成"未复核"阻塞。
        flags = self.app.clinical_flags.list_for_patient(self.clinic, self.doctor_b, self.patient)
        self.app.clinical_flags.review(self.clinic, self.doctor_b, flags[0]["id"], 1, "confirm", "已核实原始记录")
        decision = self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve",
                                                   "关注项已复核", expected_version=1)
        self.assertEqual(decision["status"], "approved")

    def test_withdrawn_consent_blocks_approval(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        consent = self.app.consent_history(self.clinic, self.doctor_a, self.patient, "weight_program")[0]
        self.app.withdraw_consent(self.clinic, self.doctor_a, consent["id"], "患者改变意愿")
        with self.assertRaises(Conflict) as blocked:
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                            expected_version=1)
        self.assertIn("授权", blocked.exception.message)

    def test_content_change_after_approval_voids_it_and_activation_is_rejected(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                        expected_version=1)
        result = self.app.revise_plan(self.clinic, self.doctor_a, plan["id"], 2,
                                      reason="根据新化验调整目标", goal={"description": "再次缩短间隔",
                                                                       "review_interval_days": 7})
        self.assertEqual(result["voided_exceptions"], [application["id"]])
        with self.assertRaises(Conflict) as blocked:
            self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], result["version"], "activate")
        self.assertIn("重新送审", blocked.exception.message)
        detail = self.app.plan_exceptions.get(self.clinic, self.doctor_a, application["id"])
        self.assertEqual(detail["status"], "voided")
        self.assertTrue(any(event["type"] == "voided" for event in detail["events"]))
        # 已失效的申请不能再被审批。
        with self.assertRaises(Conflict):
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "补批",
                                            expected_version=2)

    def test_pending_application_is_voided_when_plan_revised_before_decision(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        self.app.revise_plan(self.clinic, self.doctor_a, plan["id"], 2, reason="修订目标描述",
                             goal={"description": "调整后的强化方案", "review_interval_days": 21})
        queue = self.app.plan_exceptions.pending_queue(self.clinic, self.doctor_b)
        self.assertEqual(queue["items"], [])
        with self.assertRaises(Conflict):
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                            expected_version=1)

    def test_duplicate_submission_returns_original_result_even_after_decision(self):
        plan, assessment = self.proposed_plan()
        first = self.submit(plan, assessment, key="dup-1")
        replay = self.submit(plan, assessment, key="dup-1")
        self.assertEqual(replay["id"], first["id"])
        self.assertTrue(replay["replayed"])
        self.app.plan_exceptions.decide(self.clinic, self.doctor_b, first["id"], "approve", "同意",
                                        expected_version=1)
        replay_after = self.submit(plan, assessment, key="dup-1")
        self.assertEqual(replay_after["id"], first["id"])
        self.assertEqual(replay_after["status"], "approved")
        self.assertTrue(replay_after["replayed"])
        with self.assertRaises(Conflict):
            self.submit(plan, assessment, key="dup-1", valid_until="2026-11-27T12:00:00Z")

    def test_expired_approval_cannot_be_used_by_late_activation(self):
        plan, assessment = self.proposed_plan(valid_until="2026-09-28T12:00:00Z")
        application = self.submit(plan, assessment, valid_until="2026-09-28T12:00:00Z")
        self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                        expected_version=1)
        self.clock.set(datetime(2026, 9, 28, 12, 1, tzinfo=UTC))
        with self.assertRaises(Conflict) as blocked:
            self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        self.assertIn("过期", blocked.exception.message)
        # 超过有效期的待审批申请同样不能补批。
        self.clock.set(datetime(2026, 9, 27, 13, 0, tzinfo=UTC))
        plan2, assessment2 = self.proposed_plan(revision=2)
        late = self.submit(plan2, assessment2, key="exc-late", valid_until="2026-09-27T14:00:00Z")
        self.clock.set(datetime(2026, 9, 27, 15, 0, tzinfo=UTC))
        with self.assertRaises(Conflict):
            self.app.plan_exceptions.decide(self.clinic, self.doctor_b, late["id"], "approve", "补批",
                                            expected_version=1)

    def test_application_requires_signed_assessment_and_future_validity(self):
        plan, _ = self.proposed_plan()
        unsigned = self.app.create_assessment(self.clinic, self.doctor_a, self.patient, "weight",
                                              {"weight_kg": "94"}, {})
        with self.assertRaises(Conflict):
            self.app.plan_exceptions.submit(
                self.clinic, self.doctor_a, plan["id"], rule="缩短间隔", clinical_reason="代谢风险",
                assessment_id=unsigned["id"], valid_until="2026-10-27T12:00:00Z", idempotency_key="exc-x")
        with self.assertRaises(ValidationError):
            self.app.plan_exceptions.submit(
                self.clinic, self.doctor_a, plan["id"], rule="缩短间隔", clinical_reason="代谢风险",
                assessment_id="asm_not_exist", valid_until="2026-09-27T11:00:00Z", idempotency_key="exc-y")

    def test_version_chain_covers_revisions_applications_decisions_and_effectuation(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        self.app.plan_exceptions.decide(self.clinic, self.doctor_b, application["id"], "approve", "同意",
                                        expected_version=1)
        self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        chain = self.app.plan_exceptions.chain_for_plan(self.clinic, self.owner, plan["id"])
        self.assertEqual([item["revision"] for item in chain["revisions"]], [1, 2, 3])
        self.assertEqual(chain["plan_version"], 3)
        self.assertEqual(chain["exceptions"][0]["plan_digest"], chain["plan_digest"])
        self.assertEqual([event["type"] for event in chain["exceptions"][0]["events"]],
                         ["submitted", "approved", "effectuated"])
        self.assertEqual([event["action"] for event in chain["plan_events"]],
                         ["plan.propose", "plan.activate"])

    def test_coordinator_can_read_chain_but_not_submit_or_review(self):
        plan, assessment = self.proposed_plan()
        application = self.submit(plan, assessment)
        # 运营人员可查看申请与版本链，以区分常规安排和已批准例外。
        detail = self.app.plan_exceptions.get(self.clinic, self.coordinator, application["id"])
        self.assertEqual(detail["status"], "pending")
        queue = self.app.plan_exceptions.pending_queue(self.clinic, self.coordinator)
        self.assertEqual([item["id"] for item in queue["items"]], [application["id"]])
        chain = self.app.plan_exceptions.chain_for_plan(self.clinic, self.coordinator, plan["id"])
        self.assertEqual(len(chain["exceptions"]), 1)
        # 但运营岗位不能提交或审批。
        with self.assertRaises(Forbidden):
            self.submit(plan, assessment, key="exc-by-ops", doctor=self.coordinator)
        with self.assertRaises(Forbidden):
            self.app.plan_exceptions.decide(self.clinic, self.coordinator, application["id"], "approve", "同意",
                                            expected_version=1)

    def test_conventional_plan_without_exception_still_activates_normally(self):
        digest = hashlib.sha256(b"aes-r1").hexdigest()
        consent = self.app.grant_consent(self.clinic, self.doctor_a, self.patient, "aesthetic_procedure", 1, digest)
        plan = self.app.create_plan(
            self.clinic, self.doctor_a, self.patient, "aesthetic", self.doctor_a,
            {"description": "常规疗程"}, {}, "2026-09-27", consent_id=consent["id"])
        self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 1, "propose")
        activated = self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        self.assertEqual(activated["state"], "active")


if __name__ == "__main__":
    unittest.main()
