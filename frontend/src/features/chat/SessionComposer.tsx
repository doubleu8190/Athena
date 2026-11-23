import { useState } from "react";

import { Icon } from "../../shared/ui/Icons";
import { SkillCatalogPicker } from "../skills/SkillCatalogPicker";
import type { PendingSubagentInput, RunStatus } from "./model";

function ComposerHint({
  hasPendingUploads,
  status,
  targetedSubagentName,
}: {
  hasPendingUploads: boolean;
  status: RunStatus | null;
  targetedSubagentName?: string | null;
}) {
  if (hasPendingUploads) {
    return (
      <span>
        <Icon name="upload" size={13} />
        请先在会话文件中上传或移除待上传文件，再提交消息或补充回答。
      </span>
    );
  }
  if (targetedSubagentName) {
    return (
      <span>
        正在向子智能体「{targetedSubagentName}」提交定向回答；失败时输入和请求标识都会保留。
      </span>
    );
  }
  if (status === "queued") {
    return <span>任务已排队，重复提交已锁定；停止后以服务端状态为准。</span>;
  }
  if (status === "running") {
    return <span>任务运行中，新消息暂不可提交；已输入内容会保留。</span>;
  }
  if (status === "waiting_for_subagents") {
    return (
      <span>
        正在等待子智能体完成任务，完成后将自动继续回复；您可随时点击停止以取消任务
      </span>
    );
  }
  if (status === "waiting_for_input") {
    return <span>请回答服务端当前提问；失败时输入和请求标识都会保留。</span>;
  }
  if (status === "waiting_for_approval") {
    return <span>请逐项处理真实工具审批，全部决定后 Run 会自动继续。</span>;
  }
  if (status === "cancelling") {
    return <span>正在等待服务端确认取消，不能提前视为已取消。</span>;
  }
  return (
    <span>
      <Icon name="shield" size={13} />
      每次提交使用独立请求标识；网络失败重试会复用同一标识。
    </span>
  );
}

export interface SessionComposerProps {
  activeSubagentInput: PendingSubagentInput | null;
  canSendWithoutDraft: boolean;
  commandPending: boolean;
  filePanelBusy: boolean;
  hasPendingUploads: boolean;
  hasRun: boolean;
  initialDraft: string;
  isActive: boolean;
  isTargetedSubagentAnswer: boolean;
  isWaitingInput: boolean;
  isWaitingSubagents: boolean;
  onCancel: () => Promise<void> | void;
  onChangeSelectedSkillIds: (skillIds: string[]) => void;
  onSubmit: (submittedDraft: string) => Promise<boolean>;
  selectedSkillIds: string[];
  sending: boolean;
  status: RunStatus | null;
  stopping: boolean;
}

// 将高频击键的草稿状态隔离在独立输入区组件内，避免每次输入触发消息历史与执行轨迹面板重渲染。
export function SessionComposer({
  activeSubagentInput,
  canSendWithoutDraft,
  commandPending,
  filePanelBusy,
  hasPendingUploads,
  hasRun,
  initialDraft,
  isActive,
  isTargetedSubagentAnswer,
  isWaitingInput,
  isWaitingSubagents,
  onCancel,
  onChangeSelectedSkillIds,
  onSubmit,
  selectedSkillIds,
  sending,
  status,
  stopping,
}: SessionComposerProps) {
  const [draft, setDraft] = useState(initialDraft);
  const canSubmit = canSendWithoutDraft && Boolean(draft.trim());

  const submitCurrentDraft = async () => {
    const submittedDraft = draft;
    const accepted = await onSubmit(submittedDraft);
    if (accepted) {
      // 请求期间用户可能继续编辑；只清空仍与提交时完全一致的原草稿。
      setDraft((current) => (current === submittedDraft ? "" : current));
    }
  };

  return (
    <section
      aria-busy={commandPending}
      aria-label={
        activeSubagentInput
          ? `正在回答子智能体「${activeSubagentInput.subagent_name}」的提问`
          : isWaitingInput
            ? "补充信息输入区"
            : "消息输入区"
      }
      className={`chat-composer${
        isActive && !isTargetedSubagentAnswer ? " is-locked" : ""
      }`}
    >
      <header className="chat-composer-header">
        <label htmlFor="session-message">
          {activeSubagentInput
            ? `正在回答子智能体「${activeSubagentInput.subagent_name}」的提问`
            : isWaitingInput
              ? "补充信息"
              : "对话草稿"}
        </label>
        {isActive && !isTargetedSubagentAnswer ? (
          <span className="chat-composer-lock">
            <Icon name="lock" size={12} />
            已锁定重复提交
          </span>
        ) : null}
      </header>
      <textarea
        id="session-message"
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // 支持 Cmd/Ctrl + Enter 快捷提交消息或补充回答，忽略输入法组合态及当前不可提交状态。
          if (
            (event.metaKey || event.ctrlKey) &&
            event.key === "Enter" &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault();
            if (canSubmit) {
              void submitCurrentDraft();
            }
          }
        }}
        placeholder={
          activeSubagentInput
            ? `输入对「${activeSubagentInput.subagent_name}」的回答。`
            : isWaitingInput
              ? "回答服务端的当前提问。"
              : isWaitingSubagents
                ? "正在等待子智能体完成任务…"
                : "描述下一步目标，或继续当前对话。"
        }
        rows={4}
        value={draft}
      />
      <div className="chat-composer-toolbar">
        <SkillCatalogPicker
          disabled={commandPending || isActive || filePanelBusy}
          disabledNote={
            isActive
              ? "任务运行中，本次指定技能已锁定；下一轮可继续调整。"
              : "当前操作进行中，下一轮可调整技能。"
          }
          onChange={onChangeSelectedSkillIds}
          selectedSkillIds={selectedSkillIds}
        />
      </div>
      <div className="chat-composer-footer">
        <ComposerHint
          hasPendingUploads={hasPendingUploads}
          status={status}
          targetedSubagentName={activeSubagentInput?.subagent_name ?? null}
        />
        <div className="chat-composer-actions">
          {isActive && hasRun ? (
            <button
              className="button-danger-outline"
              disabled={commandPending}
              onClick={() => void onCancel()}
              type="button"
            >
              {stopping
                ? "正在请求停止…"
                : status === "cancelling"
                  ? "再次请求停止"
                  : "停止任务"}
            </button>
          ) : null}
          <button
            className="button-primary"
            disabled={!canSubmit}
            onClick={() => void submitCurrentDraft()}
            type="button"
          >
            {sending
              ? isWaitingInput || isTargetedSubagentAnswer
                ? "正在提交回答…"
                : "正在提交…"
              : activeSubagentInput
                ? `提交给 ${activeSubagentInput.subagent_name}`
                : isWaitingInput
                  ? "提交回答"
                  : isWaitingSubagents
                    ? "等待子智能体完成…"
                    : isActive
                      ? "运行中，暂不可发送"
                      : "发送消息"}
          </button>
        </div>
      </div>
    </section>
  );
}
