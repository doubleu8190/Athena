import { useMemo } from "react";

import { InlineAlert } from "../../shared/ui";

/** CSV 在线表格渲染硬上限（最多 200 行数据 × 50 列），避免大表导致主线程卡顿。 */
export const CSV_PREVIEW_MAX_ROWS = 200;
export const CSV_PREVIEW_MAX_COLUMNS = 50;

export interface ParsedCsvPreview {
  headers: string[];
  rows: string[][];
  columnCount: number;
  rowsTruncated: boolean;
  columnsTruncated: boolean;
}

/**
 * 零依赖 RFC 4180 安全 CSV 解析器：
 * - 正确处理双引号转义（"" -> "）、引号内逗号与引号内换行（\r\n / \n）；
 * - 严格按上限（默认 200 行数据 × 50 列）提前截断，防止超大表格阻塞渲染；
 * - 所有单元格均作为纯文本交给 React 转义渲染，杜绝 HTML/脚本注入。
 */
export function parseCsvPreview(
  input: string,
  options: { maxRows?: number; maxColumns?: number } = {},
): ParsedCsvPreview {
  const maxRows = options.maxRows ?? CSV_PREVIEW_MAX_ROWS;
  const maxColumns = options.maxColumns ?? CSV_PREVIEW_MAX_COLUMNS;
  // 剥离常见 UTF-8 BOM，避免首列表头出现不可见前缀。
  const text = input.startsWith("\uFEFF") ? input.slice(1) : input;
  if (!text.trim()) {
    return {
      columnCount: 0,
      columnsTruncated: false,
      headers: [],
      rows: [],
      rowsTruncated: false,
    };
  }

  const allRows: string[][] = [];
  let currentRow: string[] = [];
  let currentField = "";
  let inQuotes = false;
  let fieldStarted = false;
  let rowsTruncated = false;
  let columnsTruncated = false;

  // 允许收集的总行数 = 1 行表头 + maxRows 行数据。
  const maxCollectedRows = maxRows + 1;

  const pushField = () => {
    if (currentRow.length < maxColumns) {
      currentRow.push(currentField);
    } else {
      columnsTruncated = true;
    }
    currentField = "";
    fieldStarted = false;
  };

  const pushRow = (): boolean => {
    if (allRows.length >= maxCollectedRows) {
      rowsTruncated = true;
      return false;
    }
    allRows.push(currentRow);
    currentRow = [];
    return true;
  };

  for (let index = 0; index < text.length; index += 1) {
    const ch = text[index];

    if (inQuotes) {
      if (ch === '"') {
        if (index + 1 < text.length && text[index + 1] === '"') {
          currentField += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else if (ch === "\r") {
        if (index + 1 < text.length && text[index + 1] === "\n") {
          index += 1;
        }
        currentField += "\n";
      } else {
        currentField += ch;
      }
      continue;
    }

    if (ch === '"' && !fieldStarted && currentField.length === 0) {
      inQuotes = true;
      fieldStarted = true;
      continue;
    }

    if (ch === ",") {
      pushField();
      continue;
    }

    if (ch === "\r" || ch === "\n") {
      if (ch === "\r" && index + 1 < text.length && text[index + 1] === "\n") {
        index += 1;
      }
      pushField();
      if (!pushRow()) {
        break;
      }
      continue;
    }

    currentField += ch;
    fieldStarted = true;
  }

  // 收尾最后一行（若文件以换行结尾则不生成空尾行）。
  if (
    !rowsTruncated &&
    (fieldStarted || currentField.length > 0 || currentRow.length > 0)
  ) {
    pushField();
    pushRow();
  }

  if (allRows.length === 0) {
    return {
      columnCount: 0,
      columnsTruncated: false,
      headers: [],
      rows: [],
      rowsTruncated: false,
    };
  }

  const columnCount = Math.min(
    maxColumns,
    allRows.reduce((max, row) => Math.max(max, row.length), 0),
  );
  const padRow = (row: string[]): string[] =>
    row.length >= columnCount
      ? row.slice(0, columnCount)
      : [...row, ...Array<string>(columnCount - row.length).fill("")];

  const [rawHeaders, ...rawDataRows] = allRows;
  return {
    columnCount,
    columnsTruncated,
    headers: padRow(rawHeaders),
    rows: rawDataRows.map(padRow),
    rowsTruncated,
  };
}

export interface FileCsvPreviewProps {
  contentText: string;
}

/** CSV 安全表格预览视图：按 RFC 4180 解析并应用行列上限截断保护。 */
export function FileCsvPreview({ contentText }: FileCsvPreviewProps) {
  const parsedCsv = useMemo(() => {
    if (!contentText) {
      return null;
    }
    return parseCsvPreview(contentText);
  }, [contentText]);

  return (
    <div className="space-y-2.5">
      {parsedCsv && parsedCsv.columnCount > 0 ? (
        <>
          {parsedCsv.rowsTruncated || parsedCsv.columnsTruncated ? (
            <InlineAlert
              description={`表格规模较大，在线预览仅展示前 ${CSV_PREVIEW_MAX_ROWS} 行数据 × ${CSV_PREVIEW_MAX_COLUMNS} 列以保持流畅，查看完整数据请下载原件。`}
              variant="info"
            />
          ) : null}
          <div className="w-full overflow-x-auto rounded-lg border border-border-default bg-canvas">
            <table
              className="w-full border-collapse text-left text-xs"
              data-testid="csv-preview-table"
            >
              <thead className="bg-subtle text-text-secondary">
                <tr>
                  {parsedCsv.headers.map((header, colIndex) => (
                    <th
                      className="border-b border-r border-border-default px-3 py-2 font-semibold whitespace-nowrap last:border-r-0"
                      key={`csv-head-${colIndex}`}
                      scope="col"
                    >
                      {header || `列 ${colIndex + 1}`}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {parsedCsv.rows.map((row, rowIndex) => (
                  <tr
                    className="border-b border-border-subtle transition-colors last:border-b-0 hover:bg-subtle/50"
                    key={`csv-row-${rowIndex}`}
                  >
                    {row.map((cell, colIndex) => (
                      <td
                        className="max-w-[280px] border-r border-border-subtle px-3 py-2 align-top break-words whitespace-pre-wrap text-text-primary last:border-r-0"
                        key={`csv-cell-${rowIndex}-${colIndex}`}
                      >
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <p className="rounded-lg border border-border-subtle bg-subtle px-4 py-6 text-center text-xs text-text-secondary">
          未解析到可展示的表格行，可切换「原始文本」查看。
        </p>
      )}
    </div>
  );
}
