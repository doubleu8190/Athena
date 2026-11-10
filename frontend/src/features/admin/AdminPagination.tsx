import { Icon } from "../../shared/ui/Icons";

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
  return (
    <nav className="admin-pagination" aria-label={label}>
      <span>
        第 {page} 页 · 共 {total} 项
      </span>
      <div>
        <button
          aria-label="上一页"
          className="icon-button admin-page-button"
          disabled={!hasPrevious}
          onClick={() => onPageChange(page - 1)}
          type="button"
        >
          <Icon name="chevron" size={14} />
        </button>
        <button
          aria-label="下一页"
          className="icon-button admin-page-button admin-page-button--next"
          disabled={!hasNext}
          onClick={() => onPageChange(page + 1)}
          type="button"
        >
          <Icon name="chevron" size={14} />
        </button>
      </div>
    </nav>
  );
}

