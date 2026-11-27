import { Plus } from "lucide-react";
import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import {
  ADMIN_CARD_ACTIONS_CLASS,
  ADMIN_CARD_CLASS,
  ADMIN_CARD_HEADING_CLASS,
  ADMIN_CARD_LIST_CLASS,
  ADMIN_CODE_LINE_CLASS,
  ADMIN_CODE_VALUE_CLASS,
  ADMIN_DATA_COUNT_CLASS,
  ADMIN_DETAIL_GRID_CLASS,
  ADMIN_DETAIL_WIDE_CLASS,
  ADMIN_MUTED_CLASS,
  ADMIN_SECTION_CLASS,
  ADMIN_SECTION_HEADING_CLASS,
  ADMIN_TITLE_LINE_CLASS,
  Badge,
  Button,
  InlineAlert,
  NOTICE_WARNING_CLASS,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import {
  CompressionPolicyDialog,
  DisableDefaultModelDialog,
  ModelProfileCreateDialog,
} from "./ModelProfileDialogs";
import { AdminPagination, lastPageNumber } from "./AdminPagination";
import {
  formatRatio,
  formatTokenCount,
  modelCapabilities,
} from "./display";
import { adminErrorMessage } from "./errorMessages";
import {
  useCompressionPolicy,
  useModelProfiles,
  useSetDefaultModelProfileMutation,
  useSetModelProfileEnabledMutation,
} from "./hooks";
import {
  compressionPolicyBudgets,
  modelCanBeDefault,
  type ModelProfile,
} from "./model";

const MODEL_PROFILE_PAGE_SIZE = 5;

function ModelCapabilityList({ profile }: { profile: ModelProfile }) {
  const capabilities = modelCapabilities(profile);
  if (capabilities.length === 0) {
    return <span className={ADMIN_MUTED_CLASS}>未声明流式或工具能力</span>;
  }
  return (
    <div
      aria-label="模型能力"
      className="flex shrink-0 flex-wrap justify-start gap-1 self-start sm:justify-end"
    >
      {capabilities.map((capability) => (
        <Badge className="rounded-full" key={capability} variant="neutral">
          {capability}
        </Badge>
      ))}
    </div>
  );
}

export function AdminModelsPage() {
  const profiles = useModelProfiles();
  const policy = useCompressionPolicy();
  const enabledMutation = useSetModelProfileEnabledMutation();
  const defaultMutation = useSetDefaultModelProfileMutation();
  const [createOpen, setCreateOpen] = useState(false);
  const [modelPage, setModelPage] = useState(1);
  const [compressionOpen, setCompressionOpen] = useState(false);
  const [disableTarget, setDisableTarget] =
    useState<ModelProfile | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // 默认版本始终置顶；其余版本在前端按创建时间倒序，后端继续返回完整列表。
  const sortedProfiles = profiles.data
    ? [...profiles.data.items].sort((left, right) => {
        const leftIsDefault =
          left.id === profiles.data.default_profile_id;
        const rightIsDefault =
          right.id === profiles.data.default_profile_id;
        if (leftIsDefault !== rightIsDefault) {
          return leftIsDefault ? -1 : 1;
        }

        const createdAtDifference =
          Date.parse(right.created_at) - Date.parse(left.created_at);
        return createdAtDifference || left.id.localeCompare(right.id);
      })
    : [];
  const modelLastPage = lastPageNumber(
    sortedProfiles.length,
    MODEL_PROFILE_PAGE_SIZE,
  );
  const visibleModelPage = Math.min(modelPage, modelLastPage);
  const visibleProfiles = sortedProfiles.slice(
    (visibleModelPage - 1) * MODEL_PROFILE_PAGE_SIZE,
    visibleModelPage * MODEL_PROFILE_PAGE_SIZE,
  );
  const defaultProfile =
    profiles.data?.items.find(
      (profile) => profile.id === profiles.data?.default_profile_id,
    ) ?? null;
  const actionPending =
    enabledMutation.isPending || defaultMutation.isPending;
  const policyBudgets = policy.data
    ? compressionPolicyBudgets(policy.data)
    : null;

  const handleEnabledChange = async (profile: ModelProfile) => {
    setActionError(null);
    if (profile.enabled && profile.id === defaultProfile?.id) {
      setDisableTarget(profile);
      return;
    }
    try {
      await enabledMutation.mutateAsync({
        enabled: !profile.enabled,
        profileId: profile.id,
      });
    } catch (error) {
      setActionError(adminErrorMessage(error));
    }
  };

  const handleSetDefault = async (profile: ModelProfile) => {
    setActionError(null);
    try {
      await defaultMutation.mutateAsync(profile.id);
    } catch (error) {
      setActionError(adminErrorMessage(error));
    }
  };

  return (
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>模型与压缩策略</h1>
          <p className={PAGE_DESC_CLASS}>
            管理模型配置、默认对话路由与上下文压缩策略。页面只展示后端安全投影，凭据不回显明文值。
          </p>
        </div>
        <Button
          onClick={() => setCreateOpen(true)}
          type="button"
          variant="primary"
        >
          <Plus aria-hidden="true" focusable="false" size={14} strokeWidth={1.7} />
          创建模型版本
        </Button>
      </header>

      {actionError ? (
        <div className="mb-4">
          <InlineAlert
            action={
              <button
                className={TEXT_BUTTON_CLASS}
                onClick={() => {
                  setActionError(null);
                  void profiles.refetch();
                }}
                type="button"
              >
                刷新模型版本
              </button>
            }
            size="compact"
            variant="danger"
          >
            {actionError}
          </InlineAlert>
        </div>
      ) : null}

      <section
        aria-labelledby="model-profile-list-title"
        className={ADMIN_SECTION_CLASS}
      >
        <header className={ADMIN_SECTION_HEADING_CLASS}>
          <div>
            <h2 id="model-profile-list-title">模型版本</h2>
          </div>
          {profiles.isSuccess ? (
            <span className={ADMIN_DATA_COUNT_CLASS}>
              共 {profiles.data.items.length} 个版本
            </span>
          ) : null}
        </header>

        {profiles.isPending ? (
          <div className={PAGE_LOADING_CLASS} role="status">
            正在读取模型版本…
          </div>
        ) : profiles.isError ? (
          <InlineAlert
            action={
              <button
                className={TEXT_BUTTON_CLASS}
                onClick={() => void profiles.refetch()}
                type="button"
              >
                重试
              </button>
            }
            size="compact"
            variant="danger"
          >
            {adminErrorMessage(profiles.error)}
          </InlineAlert>
        ) : profiles.data.items.length === 0 ? (
          <EmptyState
            action={
              <Button
                onClick={() => setCreateOpen(true)}
                type="button"
                variant="primary"
              >
                创建第一个模型版本
              </Button>
            }
            description="只有已启用且支持流式与工具调用的版本才能设为默认。"
            icon="sliders"
            title="还没有模型版本"
          />
        ) : (
          <>
            <ul className={ADMIN_CARD_LIST_CLASS}>
              {visibleProfiles.map((profile) => {
                const isDefault =
                  profile.id === profiles.data.default_profile_id;
                return (
                  <li
                    className={cn(
                      ADMIN_CARD_CLASS,
                      isDefault &&
                        "border-brand-border shadow-[inset_3px_0_0_var(--brand-hover)] hover:shadow-[inset_3px_0_0_var(--brand-hover),var(--shadow-xs)]",
                    )}
                    key={profile.id}
                  >
                    <div className={ADMIN_CARD_HEADING_CLASS}>
                      <div className="min-w-0">
                        <div className={ADMIN_TITLE_LINE_CLASS}>
                          <h3>{profile.name}</h3>
                          {isDefault ? (
                            <Badge className="rounded-full" variant="brand">
                              当前默认
                            </Badge>
                          ) : null}
                          <Badge
                            className="rounded-full"
                            variant={profile.enabled ? "success" : "neutral"}
                          >
                            {profile.enabled ? "已启用" : "已停用"}
                          </Badge>
                          {profile.scenario_label?.trim() ? (
                            <Badge variant="info">
                              {profile.scenario_label.trim()}
                            </Badge>
                          ) : null}
                          <Badge
                            variant={
                              profile.visible_to_users ? "brand" : "neutral"
                            }
                          >
                            {profile.visible_to_users
                              ? "用户可见"
                              : "仅内部使用"}
                          </Badge>
                          <Badge className="rounded-full" variant="neutral">
                            v{profile.version}
                          </Badge>
                        </div>
                        <p className={ADMIN_CODE_LINE_CLASS}>
                          {profile.protocol} · {profile.model}
                        </p>
                        {profile.scenario_description?.trim() ? (
                          <p className={ADMIN_MUTED_CLASS}>
                            {profile.scenario_description.trim()}
                          </p>
                        ) : null}
                      </div>
                      <ModelCapabilityList profile={profile} />
                    </div>

                    <dl className={ADMIN_DETAIL_GRID_CLASS}>
                      <div className={ADMIN_DETAIL_WIDE_CLASS}>
                        <dt>HTTPS Base URL</dt>
                        <dd className={ADMIN_CODE_VALUE_CLASS}>
                          {profile.base_url}
                        </dd>
                      </div>
                      <div>
                        <dt>API Key 状态</dt>
                        <dd className={ADMIN_CODE_VALUE_CLASS}>
                          已配置 API Key
                        </dd>
                      </div>
                      <div>
                        <dt>Tokenizer</dt>
                        <dd>{profile.tokenizer_encoding}</dd>
                      </div>
                      <div>
                        <dt>上下文 / 最大输出</dt>
                        <dd>
                          {profile.context_window_tokens.toLocaleString()} /{" "}
                          {profile.max_output_tokens.toLocaleString()} tokens
                        </dd>
                      </div>
                      <div>
                        <dt>超时 / 并发</dt>
                        <dd>
                          请求 {profile.request_timeout_seconds}s · 执行{" "}
                          {profile.execution_timeout_seconds}s · 并发{" "}
                          {profile.max_concurrency}
                        </dd>
                      </div>
                      <div>
                        <dt>Temperature</dt>
                        <dd>
                          {profile.temperature === null
                            ? "未指定"
                            : profile.temperature}
                        </dd>
                      </div>
                      <div>
                        <dt>创建时间</dt>
                        <dd>{formatDateTime(profile.created_at)}</dd>
                      </div>
                    </dl>

                    <footer className={ADMIN_CARD_ACTIONS_CLASS}>
                      <Button
                        disabled={actionPending}
                        onClick={() => void handleEnabledChange(profile)}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        {enabledMutation.isPending &&
                        enabledMutation.variables?.profileId === profile.id
                          ? "正在保存…"
                          : profile.enabled
                            ? "停用版本"
                            : "启用版本"}
                      </Button>
                      {isDefault ? (
                        <span className={ADMIN_MUTED_CLASS}>
                          默认版本由后端记录，停用后不会自动切换。
                        </span>
                      ) : (
                        <Button
                          disabled={
                            actionPending || !modelCanBeDefault(profile)
                          }
                          onClick={() => void handleSetDefault(profile)}
                          size="sm"
                          title={
                            modelCanBeDefault(profile)
                              ? undefined
                              : "仅已启用且支持流式与工具调用的版本可设为默认"
                          }
                          type="button"
                          variant="outline"
                        >
                          {defaultMutation.isPending &&
                          defaultMutation.variables === profile.id
                            ? "正在切换…"
                            : "设为默认"}
                        </Button>
                      )}
                    </footer>
                  </li>
                );
              })}
            </ul>
            <AdminPagination
              label="模型版本分页"
              onPageChange={setModelPage}
              page={visibleModelPage}
              pageSize={MODEL_PROFILE_PAGE_SIZE}
              total={sortedProfiles.length}
            />
          </>
        )}
      </section>

      <section
        aria-labelledby="compression-policy-title"
        className={cn(ADMIN_SECTION_CLASS, "mt-[18px]")}
      >
        <header className={ADMIN_SECTION_HEADING_CLASS}>
          <div>
            <h2 id="compression-policy-title">上下文压缩策略</h2>
          </div>
        </header>

        {policy.isPending ? (
          <div className={PAGE_LOADING_CLASS} role="status">
            正在读取压缩策略…
          </div>
        ) : policy.isError ? (
          <InlineAlert
            action={
              <button
                className={TEXT_BUTTON_CLASS}
                onClick={() => void policy.refetch()}
                type="button"
              >
                重试
              </button>
            }
            size="compact"
            variant="danger"
          >
            {adminErrorMessage(policy.error)}
          </InlineAlert>
        ) : policy.data.id === null ? (
          <EmptyState
            action={
              <Button
                disabled={
                  !profiles.isSuccess ||
                  !profiles.data.items.some((profile) => profile.enabled)
                }
                onClick={() => setCompressionOpen(true)}
                type="button"
                variant="primary"
              >
                配置压缩策略
              </Button>
            }
            description={
              profiles.isSuccess &&
              !profiles.data.items.some((profile) => profile.enabled)
                ? "当前没有已启用的模型版本，请先创建或启用模型版本。"
                : "上下文压缩尚未配置。配置后将固定到后续新 Run，已有 Run 仍保留原策略快照。"
            }
            icon="history"
            title="尚未配置上下文压缩"
          />
        ) : (
          /* 上下文压缩策略卡片上下单列排布，下方指标自适应网格展开 */
          <article className={cn(ADMIN_CARD_CLASS, "grid grid-cols-1 gap-4")}>
            <div>
              <h3 className="mt-1 text-[19px] font-semibold text-text-primary [overflow-wrap:anywhere]">
                {profiles.data?.items.find(
                  (profile) =>
                    profile.id ===
                    policy.data.compression_model_profile_id,
                )?.name ??
                  policy.data.compression_model_profile_id ??
                  "后端未返回模型标识"}
              </h3>
              <p className={ADMIN_MUTED_CLASS}>策略版本 {policy.data.id}</p>
              {(() => {
                const compressionProfile = profiles.data?.items.find(
                  (profile) =>
                    profile.id === policy.data.compression_model_profile_id,
                );
                return compressionProfile && !compressionProfile.enabled ? (
                  <p
                    className={cn(
                      NOTICE_WARNING_CLASS,
                      "mt-2.5 font-semibold",
                    )}
                    role="status"
                  >
                    当前策略仍会固定到新 Run；该模型未启用时，Run 会回退到完整上下文，超出模型上下文窗口时才会失败。
                  </p>
                ) : null;
              })()}
            </div>
            <dl
              aria-label="压缩策略 Token 与派生预算"
              className="m-0 grid w-full grid-cols-[repeat(auto-fit,minmax(120px,1fr))] gap-[5px] min-[391px]:gap-2 [&>div]:min-w-0 [&>div]:rounded-[9px] [&>div]:border [&>div]:border-border-default [&>div]:bg-subtle [&>div]:px-1.5 [&>div]:py-2 min-[391px]:[&>div]:p-[9px] sm:[&>div]:min-w-[72px] sm:[&>div]:px-3 sm:[&>div]:py-2.5 [&_dt]:text-xs [&_dt]:text-text-tertiary [&_dd]:mt-[3px] [&_dd]:text-base [&_dd]:font-semibold [&_dd]:text-text-primary min-[391px]:[&_dd]:text-lg"
            >
              <div>
                <dt>触发阈值</dt>
                <dd>{formatTokenCount(policy.data.trigger_tokens)}</dd>
              </div>
              <div>
                <dt>近期比例</dt>
                <dd>{formatRatio(policy.data.recent_ratio)}</dd>
              </div>
              <div>
                <dt>近期上限</dt>
                <dd>{formatTokenCount(policy.data.recent_token_cap)}</dd>
              </div>
              <div>
                <dt>摘要比例</dt>
                <dd>{formatRatio(policy.data.summary_ratio)}</dd>
              </div>
              <div>
                <dt>摘要上限</dt>
                <dd>{formatTokenCount(policy.data.summary_token_cap)}</dd>
              </div>
              <div>
                <dt>近期派生预算</dt>
                <dd>{formatTokenCount(policyBudgets?.recent ?? null)}</dd>
              </div>
              <div>
                <dt>摘要派生预算</dt>
                <dd>{formatTokenCount(policyBudgets?.summary ?? null)}</dd>
              </div>
            </dl>
            <div className="col-span-full flex flex-col items-stretch justify-between gap-3.5 border-t border-border-subtle pt-[13px] text-xs text-text-tertiary sm:flex-row sm:items-center max-sm:[&>button]:w-full">
              <span>
                {policy.data.created_at
                  ? `创建于 ${formatDateTime(policy.data.created_at)}`
                  : "后端未返回创建时间"}
              </span>
              <Button
                disabled={
                  !profiles.isSuccess ||
                  !profiles.data.items.some((profile) => profile.enabled)
                }
                onClick={() => setCompressionOpen(true)}
                size="sm"
                type="button"
                variant="outline"
              >
                保存新策略版本
              </Button>
            </div>
          </article>
        )}
      </section>

      <ModelProfileCreateDialog
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          // 新版本应出现在入口页，避免用户停留在旧页看不到创建结果。
          setModelPage(1);
        }}
        open={createOpen}
      />
      {disableTarget ? (
        <DisableDefaultModelDialog
          onClose={() => setDisableTarget(null)}
          open
          profile={disableTarget}
        />
      ) : null}
      {policy.data ? (
        <CompressionPolicyDialog
          onClose={() => setCompressionOpen(false)}
          open={compressionOpen}
          policy={policy.data}
          profiles={profiles.data?.items ?? []}
        />
      ) : null}
    </div>
  );
}
