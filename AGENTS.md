# Multi-Agent Development Workflow

## 模型与角色

Root: 当前选择模型
Implementer: luna-implementer
Reviewer: terra-reviewer

## 固定工作流

Root → Implementer → Reviewer → Root

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