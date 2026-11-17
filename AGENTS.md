# Multi-Agent Development Workflow

## 模型与角色

Root: 当前选择模型
Product Manager: product-manager
Designer: ui-designer
Implementer: implementer (后端) / frontend-implementer (前端)
Reviewer: reviewer

## 固定工作流

Root → Implementer → Reviewer → Root

按任务需要，在实施前加入产品和设计环节：
Root → Product Manager → Designer → Root → Implementer → Reviewer → Root

- Product Manager：需求不明确或需要梳理产品方案时，由 Root 委派；澄清目标、用户行为、范围、边界及验收标准，汇报待决问题，不直接修改代码。简单且需求明确的任务可跳过。
- Designer：涉及界面或交互设计且需要设计方案时，由 Root 委派；基于需求提出页面结构、交互、状态与响应式方案，说明设计取舍，不直接实施代码。无需设计的任务可跳过。
- 两个角色均由 Root 按需调度；设计需要产品输入时先完成产品梳理，否则可独立进行。Root 汇总结果，与用户确认方案后再委派实施。
- 前端实现委派 frontend-implementer，后端实现委派 implementer；涉及前后端时分别明确职责和交接。Reviewer 独立审查实际变更。

当 Review 需要修复时：
循环 Implementer → Reviewer，最终由Root验收

## 执行规则

### 1. Root：Plan
- 分析需求、设计方案。
- 未经用户确认，不实施代码。
- 用户确认后，将方案委派给 Implementer。
- 主 Agent 不切换模型。

### 2. Implementer：Implement
- 按照已确认的方案实施代码。
- 编写并执行必要的测试。
- 返回修改文件、测试结果和遗留问题。
- 完成后将执行结果交回 Root。

### 3. Reviewer：Review
- Root 在 Implementer 完成后启动 Reviewer。
- Reviewer 必须独立审查实际代码 Diff。
- 核对实现与方案是否一致。
- 检查 Bug、并发安全、异常处理、
  数据一致性及测试覆盖。
- 结合 Athena 实际部署规模和运行机制评估并发，不假设高并发或大规模分布式部署。优先审查功能正确性、异常处理、数据一致性和必要测试。
- 并发 Finding 须有代码依据，说明具体触发路径、实际影响，以及在当前运行方式下合理发生的可能性。极端情况或未来规模下才出现的风险，除非影响严重，否则作为可选建议，不阻塞 PASS；修复应与实际风险相称。
- 只进行审查，不修改代码。
- 返回 PASS 或 CHANGES_REQUIRED。
- 每个 Finding 必须包含文件位置、
  触发条件、影响和修复建议。

### 4. Root：Decision
- 汇总并验证 Reviewer 的 Findings。
- 如果 PASS，进行最终验收。
- 如果存在有效问题，将问题交回 Implementer。
- Implementer 修复后必须重新经过 Reviewer Review。
- 优先复用原 Implementer Subagent 上下文。

### 5. 修复循环
- Implementer → Reviewer。
- 最多执行 3 轮修复。
- 超过上限时停止循环并汇报。
- 不允许跳过 Review 直接宣称完成。

### 6. 最终验收
- 确认方案目标实现。
- 确认必要的测试通过。
- 确认不存在未处理的阻断问题。
- 向用户汇报最终结果。

## 强制约定
- Root 负责决策与协调。
- Implementer 负责代码修改。
- Reviewer 负责独立审查。
- 角色之间不得随意替代。
- 改变既定架构需用户重新确认。
- 关键步骤必须写简体中文注释，说明业务意图、状态变更或事务边界；注释只服务理解，不替代清晰代码。
