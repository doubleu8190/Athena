import { useEffect, useId, useState } from "react";

import {
  Button,
  COMPACT_SELECT_CLASS,
  FIELD_CLASS,
  FIELD_TEXTAREA_CLASS,
  InlineAlert,
  MODAL_WIDTH_XL,
  NOTICE_BRAND_CLASS,
  NOTICE_WARNING_CLASS,
  TEXT_BUTTON_CLASS,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { Modal } from "../../shared/ui/Modal";
import {
  formatChannels,
  formatDegradedReasons,
  formatKnowledgeLocators,
} from "./display";
import { knowledgeErrorMessage } from "./errorMessages";
import {
  useKnowledgeChunk,
  useSearchKnowledgeMutation,
} from "./hooks";
import { KnowledgeSourceButton } from "./KnowledgeSourceButton";
import {
  KNOWLEDGE_SEARCH_DEFAULT_LIMIT,
  validateKnowledgeSearchQuery,
  type KnowledgeSearchHit,
} from "./model";

const SEARCH_LIMIT_OPTIONS = Array.from(
  { length: 20 },
  (_, index) => index + 1,
);

interface KnowledgeSearchDialogProps {
  baseId: string;
  onClose: () => void;
  open: boolean;
}

const HIT_META_CLASS =
  "mt-1 font-mono text-xs leading-[1.6] text-text-tertiary";

function SearchHitDetails({
  baseId,
  hit,
}: {
  baseId: string;
  hit: KnowledgeSearchHit;
}) {
  return (
    <>
      <p className={HIT_META_CLASS}>
        {formatChannels(hit.channels)} ·{" "}
        {formatKnowledgeLocators(hit.locators)}
      </p>
      <p className="mt-2 max-h-[9.8em] overflow-hidden text-xs leading-[1.65] whitespace-pre-wrap text-text-secondary">
        {hit.text}
      </p>
      <div className="mt-[9px] flex flex-wrap items-center justify-end gap-2">
        <KnowledgeSourceButton
          baseId={baseId}
          compact
          documentId={hit.document_id}
          versionId={hit.version_id}
        />
      </div>
    </>
  );
}

function ChunkDetailDialog({
  baseId,
  chunkId,
  onClose,
}: {
  baseId: string;
  chunkId: string;
  onClose: () => void;
}) {
  const chunk = useKnowledgeChunk(baseId, chunkId);

  return (
    <Modal
      description="片段内容以纯文本显示，并绑定具体文档版本。"
      footer={
        <Button onClick={onClose} type="button" variant="outline">
          关闭
        </Button>
      }
      onClose={onClose}
      open
      panelClassName={MODAL_WIDTH_XL}
      title="来源片段"
    >
      {chunk.isPending ? (
        <p className={NOTICE_BRAND_CLASS} role="status">
          正在加载来源片段…
        </p>
      ) : chunk.isError ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void chunk.refetch()}
              type="button"
            >
              重试
            </button>
          }
          size="compact"
          variant="danger"
        >
          {knowledgeErrorMessage(chunk.error)}
        </InlineAlert>
      ) : (
        <article>
          <h3 className="text-[15px] font-semibold text-text-primary [overflow-wrap:anywhere]">
            {chunk.data.title}
          </h3>
          <p className={HIT_META_CLASS}>
            {formatChannels(chunk.data.channels)} ·{" "}
            {formatKnowledgeLocators(chunk.data.locators)}
          </p>
          <pre className="my-3 max-h-[52vh] overflow-auto rounded-[9px] border border-border-default bg-subtle p-[13px] font-sans text-xs leading-[1.7] whitespace-pre-wrap text-text-primary">
            {chunk.data.text}
          </pre>
          <KnowledgeSourceButton
            baseId={baseId}
            documentId={chunk.data.document_id}
            versionId={chunk.data.version_id}
          />
        </article>
      )}
    </Modal>
  );
}

export function KnowledgeSearchDialog({
  baseId,
  onClose,
  open,
}: KnowledgeSearchDialogProps) {
  const formId = useId();
  const queryErrorId = useId();
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(KNOWLEDGE_SEARCH_DEFAULT_LIMIT);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [selectedChunkId, setSelectedChunkId] = useState<string | null>(
    null,
  );
  const search = useSearchKnowledgeMutation(baseId);
  const { reset: resetSearch } = search;

  useEffect(() => {
    if (open) {
      setQuery("");
      setLimit(KNOWLEDGE_SEARCH_DEFAULT_LIMIT);
      setQueryError(null);
      setSelectedChunkId(null);
      resetSearch();
    }
  }, [open, resetSearch]);

  const handleSubmit = () => {
    const validation = validateKnowledgeSearchQuery(query);
    setQueryError(validation);
    if (validation) {
      return;
    }
    search.mutate({ limit, query: query.trim() });
  };

  return (
    <Modal
      description="检索结果和来源片段只来自当前知识库的后端真实数据。"
      footer={
        <Button onClick={onClose} type="button" variant="outline">
          关闭
        </Button>
      }
      onClose={onClose}
      open={open}
      panelClassName={MODAL_WIDTH_XL}
      title="检索测试"
    >
      <form
        className="flex flex-col gap-3"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          handleSubmit();
        }}
      >
        <label className={FIELD_CLASS} htmlFor={`${formId}-query`}>
          <span>检索内容</span>
          <textarea
            aria-label="检索内容"
            aria-describedby={queryError ? queryErrorId : undefined}
            aria-invalid={Boolean(queryError)}
            className={FIELD_TEXTAREA_CLASS}
            data-autofocus
            id={`${formId}-query`}
            onChange={(event) => {
              setQuery(event.target.value);
              setQueryError(null);
            }}
            placeholder="输入要验证的关键词或问题"
            rows={3}
            value={query}
          />
          <small>最多 4,000 个字符。</small>
        </label>
        <div className="flex flex-col items-stretch justify-between gap-[15px] min-[701px]:flex-row min-[701px]:items-end max-[700px]:[&>button]:w-full">
          <label
            className="flex items-center justify-between gap-2 text-xs text-text-secondary min-[701px]:justify-start"
            htmlFor={`${formId}-limit`}
          >
            <span>结果数量</span>
            <select
              className={COMPACT_SELECT_CLASS}
              id={`${formId}-limit`}
              onChange={(event) => setLimit(Number(event.target.value))}
              value={limit}
            >
              {SEARCH_LIMIT_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <Button
            disabled={search.isPending}
            type="submit"
            variant="primary"
          >
            {search.isPending ? "正在检索…" : "开始检索"}
          </Button>
        </div>
        {queryError ? (
          <p className="field-error" id={queryErrorId} role="alert">
            {queryError}
          </p>
        ) : null}
      </form>

      {search.isError ? (
        <InlineAlert size="compact" variant="danger">
          {knowledgeErrorMessage(search.error)}
        </InlineAlert>
      ) : null}

      {search.isSuccess ? (
        <section
          aria-label="检索结果"
          className="mt-[18px] flex flex-col gap-2.5 border-t border-border-subtle pt-4"
        >
          <div className="flex items-center justify-between gap-4">
            <strong className="text-xs text-text-primary">
              {search.data.hits.length} 个命中片段
            </strong>
            <span className="text-xs text-text-tertiary">
              {formatChannels(search.data.channels)}
            </span>
          </div>
          {search.data.degraded_reasons.length > 0 ? (
            <p className={NOTICE_WARNING_CLASS} role="status">
              检索已降级：{formatDegradedReasons(search.data.degraded_reasons)}
            </p>
          ) : null}
          {search.data.hits.length === 0 ? (
            <EmptyState
              compact
              description="后端没有返回命中片段，可调整检索内容后重试。"
              icon="search"
              title="没有检索结果"
            />
          ) : (
            <div className="flex flex-col gap-[9px]">
              {search.data.hits.map((hit) => (
                <article
                  className="rounded-[10px] border border-border-default bg-canvas p-3"
                  key={hit.chunk_id}
                >
                  <header className="flex flex-col items-start justify-between gap-3.5 min-[701px]:flex-row max-[700px]:[&>button]:w-full">
                    <h3 className="text-[15px] font-semibold text-text-primary [overflow-wrap:anywhere]">
                      {hit.title}
                    </h3>
                    <Button
                      onClick={() => setSelectedChunkId(hit.chunk_id)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      查看片段
                    </Button>
                  </header>
                  <SearchHitDetails baseId={baseId} hit={hit} />
                </article>
              ))}
            </div>
          )}
        </section>
      ) : null}

      {selectedChunkId ? (
        <ChunkDetailDialog
          baseId={baseId}
          chunkId={selectedChunkId}
          onClose={() => setSelectedChunkId(null)}
        />
      ) : null}
    </Modal>
  );
}
