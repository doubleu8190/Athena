import {
  BarChart3,
  BookOpenCheck,
  Check,
  ClipboardList,
  FilePenLine,
  FileSearch,
  FolderKanban,
  Paperclip,
  RefreshCw,
  Sparkles,
  Wand2,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { countCodePoints } from "../../shared/lib/text";
import {
  Badge,
  Button,
  InlineAlert,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tooltip,
  VisibilityBadge,
  cn,
  toast,
} from "../../shared/ui";
import { useAuth } from "../auth/AuthProvider";
import { getPrimaryDepartment, getUserDisplayName } from "../auth/model";
import { uploadSessionFile } from "../files/api";
import {
  fileErrorMessage,
  fileRequestCanBeReused,
} from "../files/errorMessages";
import {
  isPreviewableImageFile,
  validateSessionUpload,
  type PendingSessionUpload,
} from "../files/model";
import { projectErrorMessage } from "../projects/errorMessages";
import { useProject, useProjectDirectory } from "../projects/hooks";
import { useCreateSessionMutation } from "../sessions/hooks";
import { usePublicSkills } from "../skills/hooks";
import { skillScope, type Skill } from "../skills/model";
import { submitRun } from "./api";
import {
  isImageAttachmentFile,
  type ComposerAttachmentChip,
  type ComposerSkillChip,
} from "./ComposerAttachmentBar";
import { useDraftChat } from "./DraftChatContext";
import { chatErrorMessage, requestIdCanBeReused } from "./errorMessages";
import { newRequestId } from "../../shared/lib/requestId";
import { ChatComposer } from "./SessionComposer";

const UNBOUND_PROJECT_VALUE = "__unbound__";
const MAX_FEATURED_SKILLS = 4;
const TOTAL_WORKBENCH_CARDS = 6;

interface OfficeScenarioTemplate {
  id: string;
  title: string;
  description: string;
  prompt: string;
  icon: LucideIcon;
}

const OFFICE_SCENARIO_TEMPLATES: readonly OfficeScenarioTemplate[] = [
  {
    description: "快速起草工作周报、汇报材料与正式通知",
    icon: FilePenLine,
    id: "template-weekly-report",
    prompt: "请帮我起草一份本周工作周报，包含核心进展、问题风险与下周计划：",
    title: "公文与周报起草",
  },
  {
    description: "基于企业与部门知识库，解答报销、人事与审批制度",
    icon: BookOpenCheck,
    id: "template-policy-qa",
    prompt: "请结合公司与部门制度说明以下流程与标准：",
    title: "制度与流程问答",
  },
  {
    description: "上传 PDF、Word 或汇报材料，快速提炼核心结论与要点",
    icon: FileSearch,
    id: "template-doc-summary",
    prompt: "请帮我提炼以下文档的核心结论、关键数据与待办事项：",
    title: "长文档提炼总结",
  },
  {
    description: "梳理讨论要点、决议事项与负责人行动清单",
    icon: ClipboardList,
    id: "template-meeting-minutes",
    prompt:
      "请根据以下会议记录整理结构化会议纪要与行动项（含负责人和截止时间）：",
    title: "会议纪要整理",
  },
  {
    description: "上传表格或数据摘要，发现业务趋势与异常指标",
    icon: BarChart3,
    id: "template-data-insight",
    prompt: "请分析以下业务数据的核心趋势、异常点并给出改进建议：",
    title: "数据分析与图表解读",
  },
  {
    description: "检查业务方案逻辑漏洞，优化专业表达与结构",
    icon: Wand2,
    id: "template-proposal-polish",
    prompt: "请从可行性、风险与表达清晰度角度评审并优化以下方案：",
    title: "方案评审与润色",
  },
];

export function DraftChatPage() {
  const selectId = useId();
  const navigate = useNavigate();
  const { user } = useAuth();
  const projects = useProjectDirectory();
  const skillsQuery = usePublicSkills();
  const createSession = useCreateSessionMutation();
  const {
    chooseProject,
    contextNotice,
    contextReady,
    draft,
    knowledgeBaseIds,
    modelProfileId,
    pendingUploads,
    projectId: selectedProjectId,
    selectedSkillIds,
    setDraft,
    setKnowledgeBaseIds,
    setModelProfileId,
    setPendingUploads,
    setSelectedSkillIds,
    setSessionHandoff,
  } = useDraftChat();
  const selectedProjectQuery = useProject(selectedProjectId);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [fileValidationErrors, setFileValidationErrors] = useState<string[]>(
    [],
  );
  const [submitStage, setSubmitStage] = useState<
    "idle" | "creating" | "uploading" | "submitting"
  >("idle");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // 草稿态本地图片缩略图 objectURL 映射：按 requestId 复用，移除文件或页面卸载时及时 revokeObjectURL 防泄漏。
  const localPreviewUrlsRef = useRef<Map<string, string>>(new Map());
  const [localPreviewUrls, setLocalPreviewUrls] = useState<
    Record<string, string>
  >({});

  useEffect(() => {
    if (
      typeof URL === "undefined" ||
      typeof URL.createObjectURL !== "function"
    ) {
      return;
    }
    const currentMap = localPreviewUrlsRef.current;
    const activeIds = new Set<string>();
    let changed = false;

    for (const pending of pendingUploads) {
      if (!isPreviewableImageFile(pending.file.name, pending.file.type)) {
        continue;
      }
      activeIds.add(pending.requestId);
      if (!currentMap.has(pending.requestId)) {
        currentMap.set(pending.requestId, URL.createObjectURL(pending.file));
        changed = true;
      }
    }

    for (const [requestId, url] of Array.from(currentMap.entries())) {
      if (!activeIds.has(requestId)) {
        if (typeof URL.revokeObjectURL === "function") {
          URL.revokeObjectURL(url);
        }
        currentMap.delete(requestId);
        changed = true;
      }
    }

    if (changed) {
      setLocalPreviewUrls(Object.fromEntries(currentMap.entries()));
    }
  }, [pendingUploads]);

  useEffect(() => {
    const map = localPreviewUrlsRef.current;
    return () => {
      if (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
        for (const url of map.values()) {
          URL.revokeObjectURL(url);
        }
      }
      map.clear();
    };
  }, []);

  const displayName = getUserDisplayName(user);
  const primaryDepartment = getPrimaryDepartment(user);
  const greetingSubtitle = primaryDepartment
    ? `${primaryDepartment.name} · 已自动连接企业与本部门知识库，随时协助处理工作`
    : "已自动连接企业共享知识库，随时协助处理工作";

  const allProjects = useMemo(() => projects.data ?? [], [projects.data]);
  const projectOptions = useMemo(() => {
    const items = allProjects.map((project) => ({
      label: project.name,
      value: project.id,
    }));
    if (
      selectedProjectQuery.data &&
      !allProjects.some(
        (project) => project.id === selectedProjectQuery.data?.id,
      )
    ) {
      items.push({
        label: selectedProjectQuery.data.name,
        value: selectedProjectQuery.data.id,
      });
    }
    return items;
  }, [allProjects, selectedProjectQuery.data]);

  const featuredSkills = useMemo(() => {
    const enabledOrgSkills = (skillsQuery.data ?? []).filter((skill) => {
      if (skill.enabled === false) {
        return false;
      }
      const scope = skillScope(skill);
      return scope === "department" || scope === "company";
    });
    const orderByScope = (items: Skill[]) => {
      const departmentSkills = items.filter(
        (skill) => skillScope(skill) === "department",
      );
      const companySkills = items.filter(
        (skill) => skillScope(skill) === "company",
      );
      return [...departmentSkills, ...companySkills];
    };

    // 对照契约 3.3：优先选取后台标记 featured === true 的推荐技能（保持 department > company 排序，最多 4 项）；
    // 若后台尚未配置任何推荐技能，则温和回退至既有策略（优先部门/公司已启用技能）。
    const markedFeatured = enabledOrgSkills.filter(
      (skill) => skill.featured === true,
    );
    const prioritized =
      markedFeatured.length > 0 ? markedFeatured : enabledOrgSkills;
    return orderByScope(prioritized).slice(0, MAX_FEATURED_SKILLS);
  }, [skillsQuery.data]);

  const featuredTemplates = useMemo(() => {
    const remainingSlots = Math.max(
      0,
      TOTAL_WORKBENCH_CARDS - featuredSkills.length,
    );
    return OFFICE_SCENARIO_TEMPLATES.slice(0, remainingSlots);
  }, [featuredSkills.length]);

  const selectedSkillSet = useMemo(
    () => new Set(selectedSkillIds),
    [selectedSkillIds],
  );

  const handleAddDraftFiles = (incomingFiles: File[]) => {
    if (submitting || incomingFiles.length === 0) {
      return;
    }
    const nextErrors: string[] = [];
    const accepted: PendingSessionUpload[] = [];
    for (const file of incomingFiles) {
      const error = validateSessionUpload(file);
      if (error) {
        nextErrors.push(error);
      } else {
        accepted.push({
          file,
          requestId: newRequestId(),
          reuseRequestId: false,
        });
      }
    }
    setFileValidationErrors(nextErrors);
    if (nextErrors.length > 0) {
      toast.error(nextErrors[0]);
    }
    if (accepted.length > 0) {
      setPendingUploads([...pendingUploads, ...accepted]);
    }
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const resetDraftState = () => {
    setDraft("");
    setPendingUploads([]);
    setSelectedSkillIds([]);
    setKnowledgeBaseIds(null);
    setModelProfileId(null);
  };

  const handleSubmit = async () => {
    const text = draft.trim();
    if (!text || submitting || !contextReady) {
      return;
    }
    if (countCodePoints(text) > 32_000) {
      setSubmitError("消息内容不能超过 32,000 个字符。");
      return;
    }

    setSubmitError(null);
    setSubmitting(true);
    setSubmitStage("creating");
    let createdSessionId: string;
    try {
      // 草稿直到用户首次发送才创建真实会话，避免浏览首页产生空会话垃圾。
      const session = await createSession.mutateAsync({
        project_id: selectedProjectId,
        title: null,
      });
      createdSessionId = session.id;
    } catch (error) {
      setSubmitError(chatErrorMessage(error));
      setSubmitting(false);
      setSubmitStage("idle");
      return;
    }

    const requestId = newRequestId();
    const uploadedFileIds: string[] = [];
    const remainingUploads = [...pendingUploads];
    if (pendingUploads.length > 0) {
      setSubmitStage("uploading");
    }
    for (const pending of pendingUploads) {
      try {
        const uploaded = await uploadSessionFile(
          createdSessionId,
          pending.file,
          pending.requestId,
        );
        uploadedFileIds.push(uploaded.id);
        remainingUploads.shift();
      } catch (error) {
        // 当前失败项依据错误类型决定是否复用原 Idempotency-Key（5xx/网络/不可解析响应复用，4xx 换新键）；
        // 后续尚未发出请求的待传文件保留原 requestId 并标记可直接使用。
        const handoffPendingUploads: PendingSessionUpload[] =
          remainingUploads.map((item, index) => ({
            ...item,
            reuseRequestId:
              index === 0 ? fileRequestCanBeReused(error) : true,
          }));
        // 会话已创建但附件上传中断：将已上传文件 ID、剩余文件及全部模型/知识库/技能选择完整移交至会话页。
        setSessionHandoff(createdSessionId, {
          draft: text,
          error: fileErrorMessage(error),
          inputFileIds: uploadedFileIds,
          knowledgeBaseIds,
          modelProfileId,
          pendingUploads: handoffPendingUploads,
          requestId,
          reuseRequestId: false,
          selectedSkillIds,
        });
        resetDraftState();
        setSubmitting(false);
        setSubmitStage("idle");
        navigate(`/chat/${encodeURIComponent(createdSessionId)}`);
        return;
      }
    }

    setSubmitStage("submitting");
    try {
      await submitRun(
        createdSessionId,
        requestId,
        text,
        selectedSkillIds,
        uploadedFileIds,
        {
          knowledgeBaseIds,
          modelProfileId,
        },
      );
      resetDraftState();
      setSubmitting(false);
      setSubmitStage("idle");
      navigate(`/chat/${encodeURIComponent(createdSessionId)}`);
    } catch (error) {
      // Run 提交结果不明时移交至已创建会话页继续重试，避免在草稿页重复创建空会话。
      setSessionHandoff(createdSessionId, {
        draft: text,
        error: chatErrorMessage(error),
        inputFileIds: uploadedFileIds,
        knowledgeBaseIds,
        modelProfileId,
        pendingUploads: [],
        requestId,
        reuseRequestId: requestIdCanBeReused(error),
        selectedSkillIds,
      });
      resetDraftState();
      setSubmitting(false);
      setSubmitStage("idle");
      navigate(`/chat/${encodeURIComponent(createdSessionId)}`);
    }
  };

  const attachmentChips = useMemo<ComposerAttachmentChip[]>(
    () =>
      pendingUploads.map((pending) => ({
        filename: pending.file.name,
        id: pending.requestId,
        mimeType: pending.file.type || null,
        onRemove: submitting
          ? undefined
          : () =>
              setPendingUploads(
                pendingUploads.filter(
                  (item) => item.requestId !== pending.requestId,
                ),
              ),
        previewUrl: localPreviewUrls[pending.requestId] ?? null,
        removeAriaLabel: `移除任务文件：${pending.file.name}`,
        sizeBytes: pending.file.size,
        status: "queued",
      })),
    [localPreviewUrls, pendingUploads, setPendingUploads, submitting],
  );

  const imageAttachmentCount = useMemo(
    () =>
      pendingUploads.filter((pending) =>
        isImageAttachmentFile(pending.file.name, pending.file.type),
      ).length,
    [pendingUploads],
  );
  const hasImageAttachment = imageAttachmentCount > 0;

  const skillChips = useMemo<ComposerSkillChip[]>(() => {
    const catalogMap = new Map(
      (skillsQuery.data ?? []).map((skill) => [skill.id, skill]),
    );
    return selectedSkillIds.map((skillId) => {
      const matched = catalogMap.get(skillId);
      const unavailable = skillsQuery.isSuccess && !matched;
      return {
        id: skillId,
        name: matched?.name ?? (unavailable ? "不可用技能" : skillId),
        onRemove: submitting
          ? undefined
          : () =>
              setSelectedSkillIds(
                selectedSkillIds.filter((id) => id !== skillId),
              ),
        unavailable,
      };
    });
  }, [
    selectedSkillIds,
    setSelectedSkillIds,
    skillsQuery.data,
    skillsQuery.isSuccess,
    submitting,
  ]);

  const handleSelectSkillCard = (skill: Skill) => {
    if (submitting) {
      return;
    }
    // 首次点击挂载技能；已挂载时再次点击保留选中状态，避免用户想触发引导语或聚焦输入时误取消挂载。
    if (!selectedSkillSet.has(skill.id)) {
      setSelectedSkillIds([...selectedSkillIds, skill.id]);
    }
    const starterPrompt = skill.starter_prompt?.trim() ?? "";
    const currentDraft = draft.trim();
    if (starterPrompt) {
      if (!currentDraft) {
        setDraft(starterPrompt);
      } else if (currentDraft !== starterPrompt) {
        // 仅当输入框已有不同于当前引导语的用户草稿时提示未覆盖，避免重复点击同一推荐卡弹出多余 Toast。
        toast.info("已挂载技能，当前输入框已有内容，未覆盖原草稿。");
      }
    }
    textareaRef.current?.focus();
    queueMicrotask(() => {
      const textarea = textareaRef.current;
      if (!textarea) {
        return;
      }
      textarea.focus();
      const length = textarea.value.length;
      textarea.setSelectionRange(length, length);
    });
  };

  const handleSelectTemplateCard = (template: OfficeScenarioTemplate) => {
    if (submitting) {
      return;
    }
    if (!draft.trim()) {
      setDraft(template.prompt);
    } else {
      // 用户已有草稿时绝不粗暴覆盖，保留原内容并平滑聚焦至输入框。
      toast.info("当前输入框已有内容，已为您聚焦输入框，未覆盖原草稿。");
    }
    queueMicrotask(() => {
      const textarea = textareaRef.current;
      if (!textarea) {
        return;
      }
      textarea.focus();
      const length = textarea.value.length;
      textarea.setSelectionRange(length, length);
    });
  };

  const submitButtonLabel = submitting
    ? submitStage === "creating"
      ? "正在创建对话…"
      : submitStage === "uploading"
        ? "正在上传附件…"
        : "正在发送…"
    : "发送消息";

  const attachmentControl = (
    <>
      <input
        aria-label="选择任务文件"
        className="sr-only"
        disabled={submitting}
        multiple
        onChange={(event) => {
          const files = event.target.files
            ? Array.from(event.target.files)
            : [];
          handleAddDraftFiles(files);
        }}
        ref={fileInputRef}
        type="file"
      />
      <Tooltip content="上传附件（支持拖拽或粘贴）">
        <button
          aria-label="添加附件"
          className={cn(
            "inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border-default bg-canvas text-text-secondary transition-colors",
            "hover:border-border-strong hover:bg-subtle hover:text-text-primary",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
          data-compact-control="true"
          disabled={submitting}
          onClick={() => fileInputRef.current?.click()}
          type="button"
        >
          <Paperclip aria-hidden="true" className="h-4 w-4" />
        </button>
      </Tooltip>
    </>
  );

  return (
    <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col px-4 py-5 sm:px-6 lg:px-8">
      {/* 极简顶部项目上下文条：支持日常对话与个人项目自由切换。 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <FolderKanban
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-text-tertiary"
          />
          <label
            className="text-xs font-medium text-text-secondary"
            htmlFor={selectId}
          >
            更改项目
          </label>
          <Select
            disabled={!contextReady || projects.isPending || submitting}
            onValueChange={(nextValue) =>
              chooseProject(
                nextValue === UNBOUND_PROJECT_VALUE ? null : nextValue,
              )
            }
            value={selectedProjectId ?? UNBOUND_PROJECT_VALUE}
          >
            <SelectTrigger
              aria-label="更改项目"
              className="h-8 w-auto min-w-[180px] max-w-[260px] rounded-lg px-2.5 text-xs"
              id={selectId}
            >
              <SelectValue
                placeholder={
                  !contextReady ? "正在确认项目…" : "日常对话（不归属项目）"
                }
              />
            </SelectTrigger>
            <SelectContent align="start">
              <SelectItem value={UNBOUND_PROJECT_VALUE}>
                日常对话（不归属项目）
              </SelectItem>
              {projectOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {!contextReady ? (
          <span className="text-xs text-text-tertiary" role="status">
            正在确认项目…
          </span>
        ) : null}
      </div>

      {contextNotice ? (
        <div className="mt-3">
          <InlineAlert description={contextNotice} variant="warning" />
        </div>
      ) : null}

      {projects.isError && !selectedProjectQuery.data ? (
        <div className="mt-3">
          <InlineAlert
            action={
              <Button
                onClick={() => void projects.refetch()}
                size="xs"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" className="h-3 w-3" />
                重试
              </Button>
            }
            description={projectErrorMessage(projects.error)}
            variant="danger"
          />
        </div>
      ) : null}

      {/* 居中办公工作台主体：问候区 + 一体化输入器 + 权限化办公场景卡片。 */}
      <div className="my-auto flex w-full flex-col items-center py-8 sm:py-12">
        <header className="w-full max-w-2xl text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-text-primary sm:text-3xl">
            你好，{displayName}
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-text-secondary">
            {greetingSubtitle}
          </p>
        </header>

        <div className="mt-7 w-full max-w-3xl">
          <ChatComposer
            ariaLabel="新建对话输入区"
            attachmentControl={attachmentControl}
            attachments={attachmentChips}
            attachmentsDisabled={submitting}
            busy={submitting}
            canSubmit={Boolean(draft.trim()) && !submitting && contextReady}
            capabilityControlsDisabled={submitting}
            disabled={submitting}
            draft={draft}
            errorBanner={submitError}
            footerHint={
              submitting
                ? "正在准备新对话，请稍候…"
                : "Enter 发送 · Shift + Enter 换行 · 支持拖拽或粘贴文件与截图"
            }
            hasImageAttachment={hasImageAttachment}
            imageAttachmentCount={imageAttachmentCount}
            hasProjectContext={Boolean(selectedProjectId)}
            knowledgeBaseIds={knowledgeBaseIds}
            modelProfileId={modelProfileId}
            onAddFiles={handleAddDraftFiles}
            onChangeDraft={setDraft}
            onChangeKnowledgeBaseIds={setKnowledgeBaseIds}
            onChangeModelProfileId={setModelProfileId}
            onChangeSelectedSkillIds={setSelectedSkillIds}
            onDismissValidationErrors={() => setFileValidationErrors([])}
            onSubmit={handleSubmit}
            placeholder="输入问题、粘贴截图或拖入办公资料，开始协作…"
            selectedSkillIds={selectedSkillIds}
            skillChips={skillChips}
            skillDisabledNote="正在发送本次消息，下一轮可调整技能。"
            submitButtonLabel={submitButtonLabel}
            submitLoading={submitting}
            textareaId="draft-message"
            textareaLabel="对话草稿"
            textareaRef={textareaRef}
            validationErrors={fileValidationErrors}
          />
        </div>

        <section
          aria-labelledby="workbench-scenarios-heading"
          className="mt-8 w-full max-w-3xl"
        >
          <div className="mb-3 flex items-center justify-between px-0.5">
            <h2
              className="text-xs font-semibold text-text-secondary"
              id="workbench-scenarios-heading"
            >
              常用办公场景与快捷应用
            </h2>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {featuredSkills.map((skill) => {
              const scope = skillScope(skill);
              const isSelected = selectedSkillSet.has(skill.id);
              const isFeatured = skill.featured === true;
              return (
                <button
                  className={cn(
                    "group flex flex-col justify-between rounded-xl border p-3.5 text-left transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                    isSelected
                      ? "border-brand-border bg-brand-subtle/60"
                      : "border-border-default bg-canvas hover:border-brand-border hover:bg-subtle/60",
                  )}
                  data-scenario-card="skill"
                  data-skill-featured={isFeatured ? "true" : "false"}
                  data-skill-scope={scope}
                  disabled={submitting}
                  key={skill.id}
                  onClick={() => handleSelectSkillCard(skill)}
                  type="button"
                >
                  <div className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <VisibilityBadge
                          className="px-1.5 py-0 text-[11px]"
                          scope={scope}
                        />
                        {isFeatured ? (
                          <Badge
                            className="px-1.5 py-0 text-[11px]"
                            variant="brand"
                          >
                            推荐
                          </Badge>
                        ) : null}
                      </div>
                      {isSelected ? (
                        <span className="inline-flex items-center gap-0.5 text-[11px] font-medium text-brand">
                          <Check aria-hidden="true" className="h-3 w-3" />
                          已挂载
                        </span>
                      ) : (
                        <Sparkles
                          aria-hidden="true"
                          className="h-3.5 w-3.5 text-text-tertiary transition-colors group-hover:text-brand"
                        />
                      )}
                    </div>
                    <div className="text-sm font-medium text-text-primary">
                      {skill.name}
                    </div>
                    <p className="line-clamp-2 text-xs leading-relaxed text-text-secondary">
                      {skill.description || "点击挂载该技能并开始对话"}
                    </p>
                  </div>
                </button>
              );
            })}

            {featuredTemplates.map((template) => {
              const TemplateIcon = template.icon;
              return (
                <button
                  className={cn(
                    "group flex flex-col justify-between rounded-xl border border-border-default bg-canvas p-3.5 text-left transition-colors",
                    "hover:border-brand-border hover:bg-subtle/60",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                  )}
                  data-scenario-card="template"
                  disabled={submitting}
                  key={template.id}
                  onClick={() => handleSelectTemplateCard(template)}
                  type="button"
                >
                  <div className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-subtle text-brand transition-colors group-hover:bg-brand-subtle">
                        <TemplateIcon aria-hidden="true" className="h-4 w-4" />
                      </span>
                      <Badge
                        className="px-1.5 py-0 text-[11px]"
                        variant="neutral"
                      >
                        办公模板
                      </Badge>
                    </div>
                    <div className="text-sm font-medium text-text-primary">
                      {template.title}
                    </div>
                    <p className="line-clamp-2 text-xs leading-relaxed text-text-secondary">
                      {template.description}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}
