# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `POST /plans/{plan_id}/revise` 在计划生效前修订目标、风险、目标日期或关联评估；内容变化会立即使该计划待审批或已批准的例外失效。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 特殊计划例外审批

超出门诊常规疗程节奏的计划不能由改计划的医生自行生效，必须经过另一位医生对固定计划版本的书面审批：

- `POST /plans/{plan_id}/exceptions`（须提供 `Idempotency-Key`）提交申请，内容包含 `deviation.rule`（偏离的规则）与 `deviation.detail`、`clinical_reason`（临床理由）、`assessment_id`（关联的已签署评估）和 `valid_until`（例外有效期，不得晚于关联授权到期时间）。同一申请重复提交返回原结果；同一幂等键用于不同内容返回冲突。
- `POST /plan-exceptions/{exception_id}/approve` 或 `/return` 由**另一位**具备资质的医生（clinician/owner，且不得是申请人）对送审时的固定计划版本作出同意或退回决定，均须填写 `note` 和 `expected_version`。
- 申请被退回后，可用新的 `Idempotency-Key` 重新提交，形成同一申请下递增的修订版本；审批人仍不得是申请人。
- 批准的前置条件：不存在未复核的停止级安全关注项（已确认的关注项表示医生已知情）、关联授权仍有效、关联评估仍已签署、例外仍在有效期内。授权撤回会立即使引用该授权的待审批/已批准例外失效。
- 批准只绑定送审时的计划内容摘要。`revise` 修改计划内容后，待审批/已批准例外立即变为 `invalidated`，必须基于新版本重新送审；状态转换（提议等）不改变内容，不使审批失效。
- 计划生效（`proposed→active`）时：存在待审批申请不能生效；存在已批准例外必须在激活请求中携带该 `exception_id`，服务在同一事务内重新核验内容未漂移、有效期未过、前置条件仍满足，然后把例外登记为计划的生效依据（计划记录 `approved_exception_id`）。已失效或已过期的审批既不能凭以激活，也不能绕过它裸激活。
- `POST /plan-exceptions/{exception_id}/withdraw` 允许申请人或负责人在例外随计划生效前放弃例外；放弃后计划可按常规安排生效。
- `POST /plan-exceptions/expire-due` 把已过有效期但尚未用于计划生效的批准批量标记为过期；迟到的激活请求即便撞上过期审批也会被拒绝，并把审批落为过期。
- `GET /plan-exceptions/{exception_id}` 查看申请当前状态；`GET /plan-exceptions/{exception_id}/chain` 返回申请、每次送审修订、审批/退回/失效/过期/生效事件，以及计划自身的版本快照，构成申请→修订→审批→最终生效的完整版本链。
- `GET /patients/{patient_id}/plan-exceptions` 列出患者的例外申请（需临床读权限）。
- `GET /plans/exception-schedule` 是运营排程视图：以 `arrangement` 为 `routine` 或 `approved_exception` 区分常规安排与凭批准例外生效的计划，只返回排程字段，不返回临床理由、偏离说明或评估内容。

例外状态：待审批 →（退回 → 修订后重新送审）｜（批准 → 已随计划生效）；批准后计划内容变化、授权撤回或计划取消使审批失效；有效期届满未生效则过期。所有申请、修订、审批、失效与生效动作均进入不可变审计链。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。凭特殊计划例外生效的计划带有 `approved_exception_id`，运营排程视图标记为 `approved_exception`。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
