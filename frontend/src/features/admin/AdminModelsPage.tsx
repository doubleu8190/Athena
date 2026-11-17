import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
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
    return <span className="admin-muted">未声明流式或工具能力</span>;
  }
  return (
    <div className="admin-tag-list" aria-label="模型能力">
      {capabilities.map((capability) => (
        <span className="admin-tag" key={capability}>
          {capability}
        </span>
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
    <div className="page-shell admin-page admin-models-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Admin / Models</p>
          <h1>模型与压缩策略</h1>
          <p className="page-description">
            管理不可变模型版本与默认路由。页面只展示后端安全投影，凭据仅引用环境变量名，不回显值。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => setCreateOpen(true)}
          type="button"
        >
          <Icon name="plus" size={14} />
          创建模型版本
        </button>
      </header>

      {actionError ? (
        <div className="admin-page-alert">
          <ErrorBanner
            message={actionError}
            onRetry={() => {
              setActionError(null);
              void profiles.refetch();
            }}
            retryLabel="刷新模型版本"
          />
        </div>
      ) : null}

      <section
        aria-labelledby="model-profile-list-title"
        className="admin-section"
      >
        <header className="admin-section-heading">
          <div>
            <p className="admin-section-kicker">Immutable versions</p>
            <h2 id="model-profile-list-title">模型版本</h2>
          </div>
          {profiles.isSuccess ? (
            <span className="admin-data-count">
              共 {profiles.data.items.length} 个版本
            </span>
          ) : null}
        </header>

        {profiles.isPending ? (
          <div className="page-loading" role="status">
            正在读取模型版本…
          </div>
        ) : profiles.isError ? (
          <ErrorBanner
            message={adminErrorMessage(profiles.error)}
            onRetry={() => void profiles.refetch()}
          />
        ) : profiles.data.items.length === 0 ? (
          <EmptyState
            action={
              <button
                className="button-primary"
                onClick={() => setCreateOpen(true)}
                type="button"
              >
                创建第一个模型版本
              </button>
            }
            description="创建后版本不可编辑或删除。只有已启用且支持流式与工具调用的版本才能设为默认。"
            icon="sliders"
            kicker="No model versions"
            title="还没有模型版本"
          />
        ) : (
          <>
            <ul className="admin-model-list">
              {visibleProfiles.map((profile) => {
                const isDefault =
                  profile.id === profiles.data.default_profile_id;
                return (
                  <li
                    className={`admin-model-card${
                      isDefault ? " is-default" : ""
                    }`}
                    key={profile.id}
                  >
                    <div className="admin-card-heading">
                      <div>
                        <div className="admin-title-line">
                          <h3>{profile.name}</h3>
                          {isDefault ? (
                            <span className="admin-status-badge is-accent">
                              当前默认
                            </span>
                          ) : null}
                          <span
                            className={`admin-status-badge${
                              profile.enabled
                                ? " is-success"
                                : " is-muted"
                            }`}
                          >
                            {profile.enabled ? "已启用" : "已停用"}
                          </span>
                        </div>
                        <p className="admin-code-line">
                          {profile.protocol} · {profile.model}
                        </p>
                      </div>
                      <ModelCapabilityList profile={profile} />
                    </div>

                    <dl className="admin-detail-grid">
                      <div className="admin-detail-wide">
                        <dt>HTTPS Base URL</dt>
                        <dd className="admin-code-value">
                          {profile.base_url}
                        </dd>
                      </div>
                      <div>
                        <dt>API Key 状态</dt>
                        <dd className="admin-code-value">
                          {profile.has_api_key
                            ? "已配置 API Key"
                            : "需要重新配置 API Key"}
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

                    <footer className="admin-card-actions">
                      <button
                        className="button-quiet"
                        disabled={actionPending}
                        onClick={() => void handleEnabledChange(profile)}
                        type="button"
                      >
                        {enabledMutation.isPending &&
                        enabledMutation.variables?.profileId === profile.id
                          ? "正在保存…"
                          : profile.enabled
                            ? "停用版本"
                            : "启用版本"}
                      </button>
                      {isDefault ? (
                        <span className="admin-action-note">
                          默认版本由后端记录，停用后不会自动切换。
                        </span>
                      ) : (
                        <button
                          className="button-quiet"
                          disabled={
                            actionPending || !modelCanBeDefault(profile)
                          }
                          onClick={() => void handleSetDefault(profile)}
                          title={
                            modelCanBeDefault(profile)
                              ? undefined
                              : "仅已启用且支持流式与工具调用的版本可设为默认"
                          }
                          type="button"
                        >
                          {defaultMutation.isPending &&
                          defaultMutation.variables === profile.id
                            ? "正在切换…"
                            : "设为默认"}
                        </button>
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
        className="admin-section"
      >
        <header className="admin-section-heading">
          <div>
            <p className="admin-section-kicker">Context policy</p>
            <h2 id="compression-policy-title">上下文压缩策略</h2>
          </div>
        </header>

        {policy.isPending ? (
          <div className="page-loading" role="status">
            正在读取压缩策略…
          </div>
        ) : policy.isError ? (
          <ErrorBanner
            message={adminErrorMessage(policy.error)}
            onRetry={() => void policy.refetch()}
          />
        ) : policy.data.id === null ? (
          <EmptyState
            action={
              <button
                className="button-primary"
                disabled={
                  !profiles.isSuccess ||
                  !profiles.data.items.some(
                    (profile) => profile.enabled && profile.has_api_key,
                  )
                }
                onClick={() => setCompressionOpen(true)}
                type="button"
              >
                配置压缩策略
              </button>
            }
            description={
              profiles.isSuccess &&
              !profiles.data.items.some(
                (profile) => profile.enabled && profile.has_api_key,
              )
                ? "当前没有同时启用且已配置 API Key 的模型版本，需先重新配置模型密钥。"
                : "上下文压缩尚未配置或其模型密钥暂不可用。升级后需重新选择模型并保存策略；已有 Run 仍保留原策略快照。"
            }
            icon="history"
            kicker="Not configured"
            title="尚未配置上下文压缩"
          />
        ) : (
          <article className="admin-policy-card">
            <div>
              <p className="admin-section-kicker">Active policy</p>
              <h3>
                {profiles.data?.items.find(
                  (profile) =>
                    profile.id ===
                    policy.data.compression_model_profile_id,
                )?.name ??
                  policy.data.compression_model_profile_id ??
                  "后端未返回模型标识"}
              </h3>
              <p className="admin-muted">
                策略版本 {policy.data.id} · 模板{" "}
                {policy.data.prompt_template_version ?? "未返回"}
              </p>
              {(() => {
                const compressionProfile = profiles.data?.items.find(
                  (profile) =>
                    profile.id === policy.data.compression_model_profile_id,
                );
                return compressionProfile &&
                  (!compressionProfile.enabled || !compressionProfile.has_api_key) ? (
                    <p className="admin-warning" role="status">
                      当前策略仍会固定到新 Run；该模型未启用或缺少 API Key 时，Run 会回退到完整上下文，超出模型上下文窗口时才会失败。
                    </p>
                  ) : null;
              })()}
            </div>
            <dl
              aria-label="压缩策略 Token 与派生预算"
              className="admin-policy-ratios"
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
            <div className="admin-policy-footer">
              <span>
                {policy.data.created_at
                  ? `创建于 ${formatDateTime(policy.data.created_at)}`
                  : "后端未返回创建时间"}
              </span>
              <button
                className="button-quiet"
                disabled={
                  !profiles.isSuccess ||
                  !profiles.data.items.some(
                    (profile) => profile.enabled && profile.has_api_key,
                  )
                }
                onClick={() => setCompressionOpen(true)}
                type="button"
              >
                保存新策略版本
              </button>
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
