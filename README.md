# LoopLake 回收物料数据台

这是面向社区回收运营分析员的本地数据湖与数仓服务。它将智能回收箱投递、地磅入出站、转运和兑付事件保存为不可改写的原始记录，并生成带有效期维度、物料对账、付款校验和冻结发布的街道月度视图。所有示例均为合成数据。

## 使用

运行环境为 Python 3.11+；依赖版本见 `requirements.txt`。测试：`python -m unittest discover -s tests -v`；语法检查：`python -m compileall -q looplake`；完整本地演示：`python -m looplake.demo` 与 `python -m looplake.relocation_demo`。HTTP 服务可运行 `uvicorn looplake.api:app`，并在 `/docs` 查阅接口；CLI 为 `python -m looplake.cli --help`。设置 `LOOPLAKE_DB` 可改变 API SQLite 路径。

先登记来源和带有效期的街道、社区、设备、物料、站点与人员维度，再按来源导入事件；同一来源和外部 ID 的不同正文会拒绝，完全重复事件幂等返回。维度未就绪的输入进入隔离状态，登记维度后可按 ingestion ID 重试；重试仍按事件发生时间解析维度，不按重试时刻。批量导入支持清单指纹和检查点，防止使用不同清单误续跑。

设备或社区迁址用 `relocate` 按实际生效日补录：旧版本在该日截止，新版本自该日起生效，同一设备任一时刻只有一个有效社区。迟到、重复或乱序到达的迁址通知都走同一路径；重复通知幂等返回原修订，同一天互相矛盾的通知报 `dimension_conflict`。录错的通知用 `retract` 撤回，会恢复被它覆盖的有效区间并把事实还原到原街道；只是字段写错（如名称）可用 `correct` 在原有效区间内更正。内核对事件发生日重新解析受影响投递事实的社区与街道，且保留旧值快照：`get("/facts/{id}")` 与 `get("/lineage/facts/{id}")` 可见 `reassignments`，`get("/months/{month}/attribution")` 可逐条核对“原街道 → 当前街道”。新解析链缺失或歧义时，事实保留最后一次有效归属并标记为冲突，预览会因此不可发布，而不是把体积悄悄挪去别的街道或丢掉。

同一物料批次的投递与地磅入站重量需在容差内相符；不同物料、出站超入站、付款与费率不符均产生可解释的差异。街道月度报表仅纳入已解决批次，不会将投递、地磅与转运三条记录相加。发布前读取预览指纹；发布时再次校验数据与不可变名称。冻结版保留聚合、对账和事实血缘，日后的新批次不会修改旧版。维度修订后重发布时使用新名称并通过 `supersedes` 指向被替代版本：旧版内容与指纹原样保留，只新增 `superseded_by`/`current` 关系，`compare` 会列出迁移前后的街道-物料行；同一前版只能有一个后继，试图复用旧名称写入新内容会报 `release_conflict`。预览同时冻结当时的 `dimension_revisions`（动作、生效日、原因、被重分配事实、冲突事实），因此旧版保留“当时的理解”，新版保留“修订后的理解”。

## 输入约定

时间带时区，重量用 kg，金额用元；业务日期由事件时间换算为 UTC 后记录。维度的 valid_from/valid_to 是业务日历日、`valid_to` 为开区间，迁址生效日也按业务日历日解释。来源 channel 与事件 channel 必须一致。演示代码 `looplake/demo.py` 展示完整输入和可观察输出；`python -m looplake.relocation_demo` 跑通“跨街道迁址、迟到投递、已冻结月份重发布”的完整样本。HTTP 错误返回 `code` 和 `detail`；422 表示输入形状不合法，409 表示版本或业务冲突。
