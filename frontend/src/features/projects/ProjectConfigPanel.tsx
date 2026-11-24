import {
  BookOpen,
  ExternalLink,
  Plus,
  RotateCcw,
  Save,
  Search,
  Sparkles,
  Unlink,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
  Button,
  Checkbox,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  InlineAlert,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Skeleton,
  Textarea,
  VisibilityBadge,
  cn,
  toast,
} from "../../shared/ui";
import { useAuth } from "../auth/AuthProvider";
import { resolveUserDepartmentName } from "../auth/model";
import { knowledgeErrorMessage } from "../knowledge/errorMessages";
import {
  useBindProjectKnowledgeMutation,
  useKnowledgeDirectory,
  useProjectKnowledgeBases,
  useUnbindProjectKnowledgeMutation,
} from "../knowledge/hooks";
import {
  PROJECT_KNOWLEDGE_LIMIT,
  knowledgeBaseScope,
  type KnowledgeBase,
} from "../knowledge/model";
import { usePublicSkills } from "../skills/hooks";
import { skillErrorMessage } from "../skills/errorMessages";
import { SkillScopeBadge } from "../skills/SkillScopeBadge";
import { projectErrorMessage } from "./errorMessages";
import { useUpdateProjectMutation } from "./hooks";
import {
  PROJECT_DEFAULT_SKILLS_MAX,
  PROJECT_INSTRUCTIONS_MAX_LENGTH,
  countProjectCodePoints,
  normalizeProjectInstructions,
  validateProjectInstructions,
  type Project,
} from "./model";

function stringArraysEqual(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((value, index) => value === right[index]);
}

interface AssociateKnowledgeDialogProps {
  boundIds: Set<string>;
  onClose: () => void;
  open: boolean;
  projectId: string;
}

/** 关联知识库弹窗（Radix Dialog）：展示当前用户可读且尚未关联的知识库及其三层可见性徽章。 */
function AssociateKnowledgeDialog({
  boundIds,
  onClose,
  open,
  projectId,
}: AssociateKnowledgeDialogProps) {
  const { user } = useAuth();
  const bases = useKnowledgeDirectory(open);
  const bind = useBindProjectKnowledgeMutation(projectId);
  const { reset: resetBind } = bind;
  const [requestError, setRequestError] = useState<string | null>(null);
  const limitReached = boundIds.size >= PROJECT_KNOWLEDGE_LIMIT;
  const available = bases.data?.filter((base) => !boundIds.has(base.id)) ?? [];

  useEffect(() => {
    if (open) {
      setRequestError(null);
      resetBind();
    }
  }, [open, resetBind]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !bind.isPending) {
      onClose();
    }
  };

  const handleBind = async (base: KnowledgeBase) => {
    setRequestError(null);
    try {
      await bind.mutateAsync(base.id);
      toast.success(`已关联知识库「${base.name}」`);
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent closeDisabled={bind.isPending} size="md">
        <DialogHeader>
          <DialogTitle>关联知识库</DialogTitle>
          <DialogDescription>
            每个项目最多关联 {PROJECT_KNOWLEDGE_LIMIT} 个知识库，关联后项目内对话会自动纳入检索范围。
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-3">
          {limitReached ? (
            <InlineAlert
              description={`该项目已关联 ${PROJECT_KNOWLEDGE_LIMIT} 个知识库，请先解除一个已有知识库。`}
              variant="warning"
            />
          ) : null}

          {bases.isPending ? (
            <div className="space-y-2" role="status">
              <span className="sr-only">正在加载知识库目录…</span>
              <Skeleton className="h-12 w-full rounded-lg" />
              <Skeleton className="h-12 w-full rounded-lg" />
            </div>
          ) : bases.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void bases.refetch()}
                  size="xs"
                  type="button"
                  variant="outline"
                >
                  重试
                </Button>
              }
              description={knowledgeErrorMessage(bases.error)}
              title="知识库目录加载失败"
              variant="danger"
            />
          ) : available.length === 0 ? (
            <EmptyState
              action={
                <Button asChild size="sm" variant="outline">
                  <Link to="/knowledge">前往知识库</Link>
                </Button>
              }
              compact
              description="没有可关联的新知识库，可先创建或解除其他已关联知识库。"
              icon="book"
              title="暂无可关联知识库"
            />
          ) : (
            <ul className="max-h-80 space-y-2 overflow-y-auto pr-1">
              {available.map((base) => {
                const scope = knowledgeBaseScope(base);
                const departmentName = resolveUserDepartmentName(
                  user,
                  base.owner_department_id,
                );
                const bindingCurrent =
                  bind.isPending && bind.variables === base.id;
                return (
                  <li
                    className="flex items-center justify-between gap-3 rounded-lg border border-border-subtle bg-subtle/50 px-3 py-2.5"
                    key={base.id}
                  >
                    <div className="min-w-0 flex-1 space-y-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="truncate text-sm font-medium text-text-primary">
                          {base.name}
                        </span>
                        <VisibilityBadge
                          departmentName={departmentName}
                          scope={scope}
                        />
                      </div>
                      <p className="truncate text-xs text-text-secondary">
                        {base.description || "未填写说明"}
                      </p>
                    </div>
                    <Button
                      disabled={limitReached || bind.isPending}
                      loading={bindingCurrent}
                      onClick={() => void handleBind(base)}
                      size="xs"
                      type="button"
                      variant="outline"
                    >
                      {bindingCurrent ? "正在关联…" : "关联"}
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}

          {requestError ? (
            <InlineAlert description={requestError} variant="danger" />
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={bind.isPending}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            完成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface UnbindKnowledgeAlertDialogProps {
  base: KnowledgeBase | null;
  onClose: () => void;
  projectId: string;
}

/** 解除知识库关联确认弹窗（Radix AlertDialog）：仅移除项目绑定，不删除知识库本体。 */
function UnbindKnowledgeAlertDialog({
  base,
  onClose,
  projectId,
}: UnbindKnowledgeAlertDialogProps) {
  const mutation = useUnbindProjectKnowledgeMutation(projectId);
  const { reset: resetMutation } = mutation;
  const [requestError, setRequestError] = useState<string | null>(null);

  useEffect(() => {
    if (base) {
      setRequestError(null);
      resetMutation();
    }
  }, [base, resetMutation]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !mutation.isPending) {
      onClose();
    }
  };

  const handleConfirm = async () => {
    if (!base || mutation.isPending) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync(base.id);
      toast.success(`已解除关联「${base.name}」`);
      onClose();
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <AlertDialog onOpenChange={handleOpenChange} open={Boolean(base)}>
      <AlertDialogContent closeDisabled={mutation.isPending}>
        <AlertDialogHeader>
          <AlertDialogTitle>解除知识库关联</AlertDialogTitle>
          <AlertDialogDescription>
            确定将「{base?.name ?? ""}」从当前项目解除关联吗？知识库本身及其中的文档不会被删除。
          </AlertDialogDescription>
        </AlertDialogHeader>

        {requestError ? (
          <AlertDialogBody>
            <InlineAlert description={requestError} variant="danger" />
          </AlertDialogBody>
        ) : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>
            取消
          </AlertDialogCancel>
          <AlertDialogAction
            loading={mutation.isPending}
            onClick={(event) => {
              event.preventDefault();
              void handleConfirm();
            }}
            variant="danger"
          >
            解除关联
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

interface ProjectConfigPanelProps {
  project: Project;
}

/**
 * 项目工作文件夹右栏配置区：
 * 1. 专属背景指令（instructions，上限 8,000 字，支持清空）；
 * 2. 默认挂载技能（default_skill_ids，上限 20，支持搜索选择与不可用技能降级移除）；
 * 3. 关联知识库（上限 20，展示三层可见性标签）。
 */
export function ProjectConfigPanel({ project }: ProjectConfigPanelProps) {
  const { user } = useAuth();
  const instructionsId = useId();
  const instructionsErrorId = useId();
  const skillSearchId = useId();

  // ---- 1. 专属背景指令 ----
  const updateInstructionsMutation = useUpdateProjectMutation(project.id);
  const savedInstructions = project.instructions ?? "";
  const [draftInstructions, setDraftInstructions] = useState(savedInstructions);
  const syncedInstructionsRef = useRef(savedInstructions);
  const syncedProjectIdRef = useRef(project.id);
  const [instructionsRequestError, setInstructionsRequestError] = useState<
    string | null
  >(null);

  useEffect(() => {
    if (syncedProjectIdRef.current !== project.id) {
      syncedProjectIdRef.current = project.id;
      syncedInstructionsRef.current = savedInstructions;
      setDraftInstructions(savedInstructions);
      setInstructionsRequestError(null);
      return;
    }
    // 后台刷新项目数据时，仅在未产生未保存修改时自动同步，绝不覆盖用户正在编辑的草稿。
    if (draftInstructions === syncedInstructionsRef.current) {
      setDraftInstructions(savedInstructions);
    }
    syncedInstructionsRef.current = savedInstructions;
  }, [draftInstructions, project.id, savedInstructions]);

  const instructionsDirty = draftInstructions !== savedInstructions;
  const instructionsValidationError =
    validateProjectInstructions(draftInstructions);
  const instructionsCount = countProjectCodePoints(draftInstructions.trim());
  const instructionsOverLimit =
    instructionsCount > PROJECT_INSTRUCTIONS_MAX_LENGTH;

  const handleSaveInstructions = async () => {
    if (
      !instructionsDirty ||
      instructionsValidationError ||
      updateInstructionsMutation.isPending
    ) {
      return;
    }
    setInstructionsRequestError(null);
    try {
      const normalized = normalizeProjectInstructions(draftInstructions);
      const updated = await updateInstructionsMutation.mutateAsync({
        // 清空时显式传 null 通知服务端清除专属背景指令。
        instructions: normalized,
      });
      const nextSaved = updated.instructions ?? "";
      setDraftInstructions(nextSaved);
      syncedInstructionsRef.current = nextSaved;
      toast.success(normalized ? "专属背景指令已保存" : "专属背景指令已清空");
    } catch (failure) {
      setInstructionsRequestError(projectErrorMessage(failure));
    }
  };

  // ---- 2. 默认挂载技能 ----
  const skillsQuery = usePublicSkills();
  const updateSkillsMutation = useUpdateProjectMutation(project.id);
  const savedSkillIds = useMemo(
    () => project.default_skill_ids ?? [],
    [project.default_skill_ids],
  );
  const [draftSkillIds, setDraftSkillIds] = useState<string[]>(savedSkillIds);
  const syncedSkillsRef = useRef<string[]>(savedSkillIds);
  const syncedSkillProjectIdRef = useRef(project.id);
  const [skillPopoverOpen, setSkillPopoverOpen] = useState(false);
  const [skillSearch, setSkillSearch] = useState("");
  const [skillsRequestError, setSkillsRequestError] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (syncedSkillProjectIdRef.current !== project.id) {
      syncedSkillProjectIdRef.current = project.id;
      syncedSkillsRef.current = savedSkillIds;
      setDraftSkillIds(savedSkillIds);
      setSkillsRequestError(null);
      return;
    }
    if (stringArraysEqual(draftSkillIds, syncedSkillsRef.current)) {
      setDraftSkillIds(savedSkillIds);
    }
    syncedSkillsRef.current = savedSkillIds;
  }, [draftSkillIds, project.id, savedSkillIds]);

  const skillsDirty = !stringArraysEqual(draftSkillIds, savedSkillIds);
  const skillById = useMemo(
    () => new Map((skillsQuery.data ?? []).map((skill) => [skill.id, skill])),
    [skillsQuery.data],
  );
  const filteredSkills = useMemo(() => {
    const keyword = skillSearch.trim().toLowerCase();
    const allSkills = skillsQuery.data ?? [];
    if (!keyword) {
      return allSkills;
    }
    return allSkills.filter(
      (skill) =>
        skill.name.toLowerCase().includes(keyword) ||
        skill.description.toLowerCase().includes(keyword) ||
        skill.slug.toLowerCase().includes(keyword),
    );
  }, [skillSearch, skillsQuery.data]);

  const toggleSkillSelection = (skillId: string) => {
    setSkillsRequestError(null);
    setDraftSkillIds((current) => {
      if (current.includes(skillId)) {
        return current.filter((id) => id !== skillId);
      }
      if (current.length >= PROJECT_DEFAULT_SKILLS_MAX) {
        return current;
      }
      return [...current, skillId];
    });
  };

  const handleSaveSkills = async () => {
    if (!skillsDirty || updateSkillsMutation.isPending) {
      return;
    }
    setSkillsRequestError(null);
    try {
      const updated = await updateSkillsMutation.mutateAsync({
        default_skill_ids: draftSkillIds,
      });
      const nextSaved = updated.default_skill_ids ?? [];
      setDraftSkillIds(nextSaved);
      syncedSkillsRef.current = nextSaved;
      toast.success("默认挂载技能已保存");
    } catch (failure) {
      setSkillsRequestError(projectErrorMessage(failure));
    }
  };

  // ---- 3. 关联知识库 ----
  const projectKnowledge = useProjectKnowledgeBases(project.id);
  const [associateOpen, setAssociateOpen] = useState(false);
  const [unbindBase, setUnbindBase] = useState<KnowledgeBase | null>(null);
  const boundKnowledgeIds = useMemo(
    () =>
      new Set(
        projectKnowledge.isSuccess
          ? projectKnowledge.data.map((base) => base.id)
          : [],
      ),
    [projectKnowledge.data, projectKnowledge.isSuccess],
  );
  const knowledgeLimitReached =
    projectKnowledge.isSuccess &&
    projectKnowledge.data.length >= PROJECT_KNOWLEDGE_LIMIT;

  useEffect(() => {
    if (projectKnowledge.isError) {
      setAssociateOpen(false);
    }
  }, [projectKnowledge.isError]);

  return (
    <aside aria-label="项目上下文配置" className="flex min-w-0 flex-col gap-4">
      {/* 配置卡片 1：专属背景指令 */}
      <section
        aria-labelledby="project-instructions-title"
        className="rounded-xl border border-border-subtle bg-canvas p-4 shadow-xs"
      >
        <div className="flex items-center justify-between gap-2">
          <h2
            className="text-sm font-semibold text-text-primary"
            id="project-instructions-title"
          >
            <label htmlFor={instructionsId}>专属背景指令</label>
          </h2>
          <span
            aria-live="polite"
            className={cn(
              "font-mono text-xs tabular-nums",
              instructionsOverLimit ? "text-danger" : "text-text-tertiary",
            )}
          >
            {instructionsCount} / {PROJECT_INSTRUCTIONS_MAX_LENGTH}
          </span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-text-secondary">
          该指令会自动注入该项目下的主对话，子任务不会重复注入。
        </p>

        <div className="mt-3 space-y-2">
          <Textarea
            aria-describedby={
              instructionsValidationError ? instructionsErrorId : undefined
            }
            disabled={updateInstructionsMutation.isPending}
            error={Boolean(instructionsValidationError)}
            id={instructionsId}
            onChange={(event) => {
              setDraftInstructions(event.target.value);
              if (instructionsRequestError) setInstructionsRequestError(null);
            }}
            placeholder="填写项目背景、输出规范或专属约束，清空并保存可移除…"
            rows={5}
            showCount={false}
            value={draftInstructions}
          />
          {instructionsValidationError ? (
            <p
              className="text-xs leading-snug text-danger"
              id={instructionsErrorId}
              role="alert"
            >
              {instructionsValidationError}
            </p>
          ) : null}
          {instructionsRequestError ? (
            <InlineAlert
              description={instructionsRequestError}
              variant="danger"
            />
          ) : null}

          <div className="flex items-center justify-end gap-2 pt-1">
            {instructionsDirty ? (
              <Button
                disabled={updateInstructionsMutation.isPending}
                onClick={() => {
                  setDraftInstructions(savedInstructions);
                  setInstructionsRequestError(null);
                }}
                size="xs"
                type="button"
                variant="outline"
              >
                <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
                撤销
              </Button>
            ) : null}
            <Button
              disabled={
                !instructionsDirty || Boolean(instructionsValidationError)
              }
              loading={updateInstructionsMutation.isPending}
              onClick={() => void handleSaveInstructions()}
              size="xs"
              type="button"
              variant="primary"
            >
              <Save aria-hidden="true" className="h-3.5 w-3.5" />
              保存指令
            </Button>
          </div>
        </div>
      </section>

      {/* 配置卡片 2：默认挂载技能 */}
      <section
        aria-labelledby="project-default-skills-title"
        className="rounded-xl border border-border-subtle bg-canvas p-4 shadow-xs"
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5">
            <Sparkles aria-hidden="true" className="h-4 w-4 text-brand" />
            <h2
              className="text-sm font-semibold text-text-primary"
              id="project-default-skills-title"
            >
              默认挂载技能
            </h2>
          </div>
          <span className="font-mono text-xs tabular-nums text-text-tertiary">
            {draftSkillIds.length} / {PROJECT_DEFAULT_SKILLS_MAX}
          </span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-text-secondary">
          项目内发起新对话时默认预选这些技能，最多配置 {PROJECT_DEFAULT_SKILLS_MAX} 个。
        </p>

        <div className="mt-3 space-y-3">
          {draftSkillIds.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border-default bg-subtle/50 px-3 py-2.5 text-xs text-text-tertiary">
              尚未配置默认技能，可在下方目录中勾选。
            </p>
          ) : (
            <ul aria-label="已选默认技能" className="flex flex-wrap gap-1.5">
              {draftSkillIds.map((skillId) => {
                const matched = skillById.get(skillId);
                const unavailable = skillsQuery.isSuccess && !matched;
                const displayName = matched?.name ?? skillId;
                return (
                  <li key={skillId}>
                    <span
                      className={cn(
                        "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
                        unavailable
                          ? "border-border-default bg-muted text-text-tertiary"
                          : "border-brand-border bg-brand-subtle text-brand",
                      )}
                    >
                      <span className="truncate">
                        {unavailable
                          ? `${displayName}（当前不可用）`
                          : displayName}
                      </span>
                      <button
                        aria-label={`移除默认技能：${displayName}`}
                        className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full hover:bg-canvas/70 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/50"
                        data-compact-control="true"
                        disabled={updateSkillsMutation.isPending}
                        onClick={() => toggleSkillSelection(skillId)}
                        type="button"
                      >
                        <X aria-hidden="true" className="h-3 w-3" />
                      </button>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2">
            <Popover
              onOpenChange={setSkillPopoverOpen}
              open={skillPopoverOpen}
            >
              <PopoverTrigger asChild>
                <Button
                  disabled={updateSkillsMutation.isPending}
                  size="xs"
                  type="button"
                  variant="outline"
                >
                  <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                  选择默认技能
                </Button>
              </PopoverTrigger>
              <PopoverContent align="start" className="w-80 p-3">
                <div className="space-y-2.5">
                  <label className="sr-only" htmlFor={skillSearchId}>
                    搜索可用技能
                  </label>
                  <Input
                    aria-label="搜索可用技能"
                    id={skillSearchId}
                    onChange={(event) => setSkillSearch(event.target.value)}
                    placeholder="搜索技能名称或描述…"
                    prefixSlot={
                      <Search aria-hidden="true" className="h-3.5 w-3.5" />
                    }
                    value={skillSearch}
                  />

                  {skillsQuery.isPending ? (
                    <div className="space-y-2 py-2" role="status">
                      <span className="sr-only">正在加载技能目录…</span>
                      <Skeleton className="h-8 w-full" />
                      <Skeleton className="h-8 w-full" />
                    </div>
                  ) : skillsQuery.isError ? (
                    <InlineAlert
                      action={
                        <Button
                          onClick={() => void skillsQuery.refetch()}
                          size="xs"
                          type="button"
                          variant="outline"
                        >
                          重试
                        </Button>
                      }
                      description={skillErrorMessage(skillsQuery.error)}
                      variant="danger"
                    />
                  ) : filteredSkills.length === 0 ? (
                    <p className="py-4 text-center text-xs text-text-tertiary">
                      {skillSearch.trim()
                        ? "未找到匹配的可用技能"
                        : "当前没有可用的技能"}
                    </p>
                  ) : (
                    <ul className="max-h-60 space-y-1 overflow-y-auto pr-1">
                      {filteredSkills.map((skill) => {
                        const checked = draftSkillIds.includes(skill.id);
                        const disabledByLimit =
                          !checked &&
                          draftSkillIds.length >= PROJECT_DEFAULT_SKILLS_MAX;
                        const deptName = resolveUserDepartmentName(
                          user,
                          skill.owner_department_id,
                        );
                        return (
                          <li key={skill.id}>
                            <label
                              className={cn(
                                "flex cursor-pointer items-start gap-2.5 rounded-lg px-2 py-2 transition-colors hover:bg-subtle",
                                disabledByLimit &&
                                  "cursor-not-allowed opacity-60",
                              )}
                            >
                              <Checkbox
                                checked={checked}
                                className="mt-0.5"
                                disabled={disabledByLimit}
                                onCheckedChange={() =>
                                  toggleSkillSelection(skill.id)
                                }
                              />
                              <span className="min-w-0 flex-1 space-y-0.5">
                                <span className="flex flex-wrap items-center gap-1.5">
                                  <span className="truncate text-xs font-medium text-text-primary">
                                    {skill.name}
                                  </span>
                                  <SkillScopeBadge
                                    compact
                                    departmentName={deptName}
                                    scope={skill.scope}
                                  />
                                </span>
                                <span className="block truncate text-[11px] text-text-tertiary">
                                  {skill.description || "未填写描述"}
                                </span>
                              </span>
                            </label>
                          </li>
                        );
                      })}
                    </ul>
                  )}

                  {draftSkillIds.length >= PROJECT_DEFAULT_SKILLS_MAX ? (
                    <p className="text-[11px] text-warning">
                      已达 {PROJECT_DEFAULT_SKILLS_MAX} 个默认技能上限。
                    </p>
                  ) : null}
                </div>
              </PopoverContent>
            </Popover>

            <div className="flex items-center gap-1.5">
              {skillsDirty ? (
                <Button
                  disabled={updateSkillsMutation.isPending}
                  onClick={() => {
                    setDraftSkillIds(savedSkillIds);
                    setSkillsRequestError(null);
                  }}
                  size="xs"
                  type="button"
                  variant="ghost"
                >
                  撤销
                </Button>
              ) : null}
              <Button
                disabled={!skillsDirty}
                loading={updateSkillsMutation.isPending}
                onClick={() => void handleSaveSkills()}
                size="xs"
                type="button"
                variant="primary"
              >
                保存默认技能
              </Button>
            </div>
          </div>

          {skillsRequestError ? (
            <InlineAlert description={skillsRequestError} variant="danger" />
          ) : null}
        </div>
      </section>

      {/* 配置卡片 3：关联知识库 */}
      <section
        aria-labelledby="project-knowledge-title"
        className="rounded-xl border border-border-subtle bg-canvas p-4 shadow-xs"
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5">
            <BookOpen aria-hidden="true" className="h-4 w-4 text-brand" />
            <h2
              className="text-sm font-semibold text-text-primary"
              id="project-knowledge-title"
            >
              项目知识库
            </h2>
          </div>
          <Button
            aria-describedby={
              knowledgeLimitReached ? "project-knowledge-limit" : undefined
            }
            disabled={!projectKnowledge.isSuccess || knowledgeLimitReached}
            onClick={() => setAssociateOpen(true)}
            size="xs"
            type="button"
            variant="outline"
          >
            <Plus aria-hidden="true" className="h-3.5 w-3.5" />
            关联知识库
          </Button>
        </div>

        <div className="mt-1.5 flex flex-wrap items-center justify-between gap-1 text-xs text-text-tertiary">
          {projectKnowledge.isPending ? (
            <span role="status">已关联数量读取中</span>
          ) : projectKnowledge.isError ? (
            <span role="status">已关联数量暂不可用</span>
          ) : (
            <span>
              已关联 {projectKnowledge.data.length} / {PROJECT_KNOWLEDGE_LIMIT}
            </span>
          )}
          {knowledgeLimitReached ? (
            <Badge id="project-knowledge-limit" variant="warning">
              已达到项目关联上限，请先解除一个知识库。
            </Badge>
          ) : null}
        </div>

        <div className="mt-3">
          {projectKnowledge.isPending ? (
            <div className="space-y-2" role="status">
              <span className="sr-only">正在加载项目知识库…</span>
              <Skeleton className="h-12 w-full rounded-lg" />
            </div>
          ) : projectKnowledge.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void projectKnowledge.refetch()}
                  size="xs"
                  type="button"
                  variant="outline"
                >
                  重试
                </Button>
              }
              description={knowledgeErrorMessage(projectKnowledge.error)}
              title="项目知识库加载失败"
              variant="danger"
            />
          ) : projectKnowledge.data.length === 0 ? (
            <EmptyState
              action={
                <Button
                  onClick={() => setAssociateOpen(true)}
                  size="xs"
                  type="button"
                  variant="primary"
                >
                  关联第一个知识库
                </Button>
              }
              compact
              description="关联后，项目中的对话会自动将这些知识库纳入检索上下文。"
              icon="book"
              title="还没有关联知识库"
            />
          ) : (
            <ul aria-label="已关联知识库列表" className="space-y-2">
              {projectKnowledge.data.map((base) => {
                const scope = knowledgeBaseScope(base);
                const departmentName = resolveUserDepartmentName(
                  user,
                  base.owner_department_id,
                );
                return (
                  <li
                    className="rounded-lg border border-border-subtle bg-subtle/50 p-3"
                    key={base.id}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1 space-y-1">
                        <Link
                          className="inline-flex max-w-full items-center gap-1 text-sm font-medium text-text-primary hover:text-brand focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40 rounded"
                          to={`/knowledge/${encodeURIComponent(base.id)}`}
                        >
                          <span className="truncate">{base.name}</span>
                          <ExternalLink
                            aria-hidden="true"
                            className="h-3 w-3 shrink-0 text-text-tertiary"
                          />
                        </Link>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <VisibilityBadge
                            departmentName={departmentName}
                            scope={scope}
                          />
                        </div>
                        <p className="line-clamp-2 text-xs text-text-secondary">
                          {base.description || "未填写说明"}
                        </p>
                      </div>
                      <Button
                        aria-label={`解除知识库关联：${base.name}`}
                        className="shrink-0 text-danger hover:bg-danger-bg hover:text-danger"
                        onClick={() => setUnbindBase(base)}
                        size="xs"
                        type="button"
                        variant="ghost"
                      >
                        <Unlink aria-hidden="true" className="h-3.5 w-3.5" />
                        解除关联
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <AssociateKnowledgeDialog
          boundIds={boundKnowledgeIds}
          onClose={() => setAssociateOpen(false)}
          open={associateOpen}
          projectId={project.id}
        />
        <UnbindKnowledgeAlertDialog
          base={unbindBase}
          onClose={() => setUnbindBase(null)}
          projectId={project.id}
        />
      </section>
    </aside>
  );
}
