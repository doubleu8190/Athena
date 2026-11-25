import { MessageCircleQuestion } from "lucide-react";
import { memo } from "react";

import { Badge, cn } from "../../shared/ui";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import type { PendingSubagentInput } from "./model";
import { subagentInputKey } from "./SubagentHitlPanels";

export interface WaitingInputBannerProps {
  activeSubagentInput: PendingSubagentInput | null;
  isWaitingMainInput: boolean;
  mainQuestionText: string | null;
  onSelectSubagentInput: (item: PendingSubagentInput) => void;
  pendingSubagentInputs: PendingSubagentInput[];
}

/**
 * 澄清问答引导条：
 * 仅在主任务等待补充信息或存在待回复协作助手提问时，紧贴输入器上方以柔和浅蓝卡片展示。
 * 当多个协作助手同时提问时，顶部展示紧凑 Pill 支持切换当前回复目标并同步更新输入器路由。
 * 包裹 React.memo 隔离流式输出期间的无关重渲染。
 */
export const WaitingInputBanner = memo(function WaitingInputBanner({
  activeSubagentInput,
  isWaitingMainInput,
  mainQuestionText,
  onSelectSubagentInput,
  pendingSubagentInputs,
}: WaitingInputBannerProps) {
  if (pendingSubagentInputs.length > 0 && activeSubagentInput) {
    const activeKey = subagentInputKey(activeSubagentInput);

    return (
      <section
        aria-labelledby="waiting-subagent-input-heading"
        className="mb-2 rounded-xl border border-info-border bg-info-bg/75 px-4 py-3 text-text-primary shadow-xs"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <MessageCircleQuestion
              aria-hidden="true"
              className="h-4 w-4 shrink-0 text-info"
            />
            <h2
              className="text-xs font-semibold text-info"
              id="waiting-subagent-input-heading"
            >
              需要补充信息
            </h2>
            {pendingSubagentInputs.length > 1 ? (
              <Badge variant="info">
                {pendingSubagentInputs.length} 项待回复
              </Badge>
            ) : null}
          </div>
          <span className="text-xs text-text-secondary">
            当前回复：#{activeSubagentInput.task_index}「
            {activeSubagentInput.subagent_name}」
          </span>
        </div>

        {/* 多个协作助手同时等待输入时，使用紧凑 Pill 切换回复目标；同源并发任务通过序号与任务摘要区分。 */}
        {pendingSubagentInputs.length > 1 ? (
          <div
            aria-label="切换待回复协作助手"
            className="mt-2.5 flex flex-wrap items-center gap-1.5"
            role="group"
          >
            {pendingSubagentInputs.map((item) => {
              const key = subagentInputKey(item);
              const selected = key === activeKey;
              return (
                <button
                  aria-pressed={selected}
                  className={cn(
                    "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    selected
                      ? "border-info bg-info text-white"
                      : "border-info-border bg-canvas text-text-secondary hover:bg-info-bg hover:text-info",
                  )}
                  data-compact-control="true"
                  key={key}
                  onClick={() => onSelectSubagentInput(item)}
                  type="button"
                >
                  <span className="shrink-0 opacity-85">#{item.task_index}</span>
                  <span className="shrink-0">{item.subagent_name}</span>
                  {item.task_summary ? (
                    <span className="max-w-[160px] truncate opacity-85">
                      · {item.task_summary}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        ) : null}

        <div className="mt-2.5 rounded-lg border border-info-border/70 bg-canvas/90 px-3 py-2.5">
          {activeSubagentInput.task_summary ? (
            <p className="mb-1 text-xs font-medium text-text-secondary">
              任务：{activeSubagentInput.task_summary}
            </p>
          ) : null}
          <MarkdownContent
            className="text-sm"
            text={activeSubagentInput.question_text}
          />
        </div>
      </section>
    );
  }

  if (!isWaitingMainInput) {
    return null;
  }

  return (
    <section
      aria-labelledby="waiting-main-input-heading"
      className="mb-2 rounded-xl border border-info-border bg-info-bg/75 px-4 py-3 text-text-primary shadow-xs"
    >
      <div className="flex items-center gap-2">
        <MessageCircleQuestion
          aria-hidden="true"
          className="h-4 w-4 shrink-0 text-info"
        />
        <h2
          className="text-xs font-semibold text-info"
          id="waiting-main-input-heading"
        >
          需要补充信息以继续
        </h2>
      </div>
      <div className="mt-2 rounded-lg border border-info-border/70 bg-canvas/90 px-3 py-2.5">
        {mainQuestionText ? (
          <MarkdownContent className="text-sm" text={mainQuestionText} />
        ) : (
          <p className="text-xs leading-relaxed text-text-secondary">
            请结合上方对话内容在输入框中补充所需信息，提交后助手将继续处理。
          </p>
        )}
      </div>
    </section>
  );
});
