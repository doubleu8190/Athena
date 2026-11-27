import { ChevronRight } from "lucide-react";

import { ICON_BUTTON_CLASS, cn } from "../../shared/ui";

export function lastPageNumber(total: number, pageSize: number): number {
  return Math.max(
    1,
    Math.ceil(Math.max(0, total) / Math.max(1, pageSize)),
  );
}

interface AdminPaginationProps {
  label: string;
  onPageChange: (page: number) => void;
  page: number;
  pageSize: number;
  total: number;
}

export function AdminPagination({
  label,
  onPageChange,
  page,
  pageSize,
  total,
}: AdminPaginationProps) {
  const hasPrevious = page > 1;
  const hasNext = page * pageSize < total;
  const navBtnClass = cn(
    ICON_BUTTON_CLASS,
    "border-border-default bg-canvas",
  );
  return (
    <nav
      aria-label={label}
      className="mt-4 flex items-center justify-between gap-[15px] border-t border-border-subtle pt-3.5 text-xs text-text-tertiary"
    >
      <span>
        第 {page} 页 · 共 {total} 项
      </span>
      <div className="flex gap-[5px]">
        <button
          aria-label="上一页"
          className={cn(navBtnClass, "[&>svg]:rotate-180")}
          disabled={!hasPrevious}
          onClick={() => onPageChange(page - 1)}
          type="button"
        >
          <ChevronRight
            aria-hidden="true"
            focusable="false"
            size={14}
            strokeWidth={1.7}
          />
        </button>
        <button
          aria-label="下一页"
          className={navBtnClass}
          disabled={!hasNext}
          onClick={() => onPageChange(page + 1)}
          type="button"
        >
          <ChevronRight
            aria-hidden="true"
            focusable="false"
            size={14}
            strokeWidth={1.7}
          />
        </button>
      </div>
    </nav>
  );
}

