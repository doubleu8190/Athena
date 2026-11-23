import { useEffect, useId, useState } from "react";

import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
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

function SearchHitDetails({
  baseId,
  hit,
}: {
  baseId: string;
  hit: KnowledgeSearchHit;
}) {
  return (
    <>
      <p className="knowledge-search-hit-meta">
        {formatChannels(hit.channels)} ·{" "}
        {formatKnowledgeLocators(hit.locators)}
      </p>
      <p className="knowledge-search-hit-text">{hit.text}</p>
      <div className="knowledge-search-hit-actions">
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
        <button className="button-quiet" onClick={onClose} type="button">
          关闭
        </button>
      }
      onClose={onClose}
      open
      panelClassName="knowledge-chunk-dialog"
      title="来源片段"
    >
      {chunk.isPending ? (
        <p className="knowledge-form-status" role="status">
          正在加载来源片段…
        </p>
      ) : chunk.isError ? (
        <ErrorBanner
          message={knowledgeErrorMessage(chunk.error)}
          onRetry={() => void chunk.refetch()}
        />
      ) : (
        <article className="knowledge-chunk-detail">
          <h3>{chunk.data.title}</h3>
          <p className="knowledge-search-hit-meta">
            {formatChannels(chunk.data.channels)} ·{" "}
            {formatKnowledgeLocators(chunk.data.locators)}
          </p>
          <pre>{chunk.data.text}</pre>
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
        <button className="button-quiet" onClick={onClose} type="button">
          关闭
        </button>
      }
      onClose={onClose}
      open={open}
      panelClassName="knowledge-search-dialog"
      title="检索测试"
    >
      <form
        className="knowledge-search-form"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={`${formId}-query`}>
          <span>检索内容</span>
          <textarea
            aria-label="检索内容"
            aria-describedby={queryError ? queryErrorId : undefined}
            aria-invalid={Boolean(queryError)}
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
        <div className="knowledge-search-controls">
          <label htmlFor={`${formId}-limit`}>
            <span>结果数量</span>
            <select
              className="workspace-select"
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
          <button
            className="button-primary"
            disabled={search.isPending}
            type="submit"
          >
            {search.isPending ? "正在检索…" : "开始检索"}
          </button>
        </div>
        {queryError ? (
          <p className="field-error" id={queryErrorId} role="alert">
            {queryError}
          </p>
        ) : null}
      </form>

      {search.isError ? (
        <ErrorBanner message={knowledgeErrorMessage(search.error)} />
      ) : null}

      {search.isSuccess ? (
        <section
          aria-label="检索结果"
          className="knowledge-search-results"
        >
          <div className="knowledge-search-summary">
            <strong>{search.data.hits.length} 个命中片段</strong>
            <span>{formatChannels(search.data.channels)}</span>
          </div>
          {search.data.degraded_reasons.length > 0 ? (
            <p className="knowledge-degraded-note" role="status">
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
            <div className="knowledge-search-hit-list">
              {search.data.hits.map((hit) => (
                <article className="knowledge-search-hit" key={hit.chunk_id}>
                  <header>
                    <h3>{hit.title}</h3>
                    <button
                      className="button-quiet"
                      onClick={() => setSelectedChunkId(hit.chunk_id)}
                      type="button"
                    >
                      查看片段
                    </button>
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
