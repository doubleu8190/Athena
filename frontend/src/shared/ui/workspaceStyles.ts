/**
 * 全站页面骨架、表单控件与管理卡片的 Tailwind v4 轻量样式常量。
 * 统一消费 tokens.css 语义变量，避免在各业务模块中重复手写长串原子类名。
 */

/** 标准业务页面容器与页眉排版 */
export const PAGE_SHELL_CLASS =
  "mx-auto w-full max-w-[1240px] px-4 pt-5 pb-12 sm:px-[30px] sm:pt-7 sm:pb-[68px] lg:px-10 lg:pt-10";

export const PAGE_HEADING_CLASS =
  "mb-5 flex flex-col items-start justify-between gap-3.5 sm:mb-6 sm:flex-row sm:items-end sm:gap-6 max-sm:[&>button]:w-full";

export const PAGE_HEADING_COPY_CLASS = "max-w-[720px]";

export const PAGE_TITLE_CLASS =
  "text-[clamp(24px,2.6vw,32px)] leading-[1.2] font-semibold tracking-[-0.025em] text-text-primary";

export const PAGE_DESC_CLASS =
  "mt-2 max-w-[650px] text-sm leading-[1.65] text-text-secondary";

export const PAGE_LOADING_CLASS =
  "flex min-h-[220px] items-center justify-center rounded-xl border border-border-subtle bg-canvas text-[13px] text-text-secondary";

/** 行内文字操作按钮与紧凑图标按钮 */
export const TEXT_BUTTON_CLASS =
  "shrink-0 rounded-[5px] bg-transparent px-1.5 py-0.5 text-[inherit] font-semibold text-brand-hover underline underline-offset-2 transition-colors hover:enabled:text-brand disabled:opacity-55";

export const ICON_BUTTON_CLASS =
  "inline-grid h-[31px] w-[31px] shrink-0 place-items-center rounded-[7px] border border-transparent bg-transparent text-text-secondary transition-colors hover:enabled:border-border-default hover:enabled:bg-subtle hover:enabled:text-text-primary disabled:opacity-40";

export const ICON_BUTTON_DANGER_CLASS =
  "hover:enabled:border-danger-border hover:enabled:bg-danger-bg hover:enabled:text-danger";

/** 表单字段组、原生控件与复选框 */
const CONTROL_BASE_CLASS =
  "w-full min-w-0 rounded-lg border border-border-default bg-canvas text-sm text-text-primary transition-colors " +
  "placeholder:text-text-placeholder " +
  "focus:border-ring-focus focus:ring-3 focus:ring-ring-focus/15 focus-visible:border-ring-focus focus-visible:ring-3 focus-visible:ring-ring-focus/15 " +
  "disabled:cursor-not-allowed disabled:bg-muted disabled:text-text-secondary disabled:opacity-70 " +
  "aria-[invalid=true]:border-danger aria-[invalid=true]:bg-danger-bg";

export const FIELD_CLASS =
  "flex min-w-0 flex-col gap-1.5 [&>label]:text-[13px] [&>label]:font-semibold [&>label]:text-text-primary [&>span]:text-[13px] [&>span]:font-semibold [&>span]:text-text-primary [&>small]:text-xs [&>small]:leading-[1.55] [&>small]:text-text-tertiary [&>small]:[overflow-wrap:anywhere]";

export const FIELD_INPUT_CLASS = `${CONTROL_BASE_CLASS} min-h-10 px-3`;

export const FIELD_SELECT_CLASS = `${CONTROL_BASE_CLASS} min-h-10 cursor-pointer px-3 accent-brand-hover`;

export const COMPACT_SELECT_CLASS = `${CONTROL_BASE_CLASS} min-h-[38px] w-auto cursor-pointer px-2.5 accent-brand-hover`;

export const FIELD_TEXTAREA_CLASS = `${CONTROL_BASE_CLASS} resize-y px-3 py-2.5 leading-[1.65]`;

export const CHECKBOX_LABEL_CLASS =
  "flex cursor-pointer items-center gap-[9px] text-xs text-text-secondary";

export const CHECKBOX_INPUT_CLASS = "m-0 h-4 w-4 shrink-0 accent-brand-hover";

export const CHECKBOX_INPUT_DANGER_CLASS = "m-0 h-4 w-4 shrink-0 accent-danger";

export const CONFIRM_COPY_CLASS =
  "text-[13px] leading-[1.7] text-text-secondary";

/** 表单布局容器与状态提示条 */
export const FORM_STACK_CLASS = "flex min-w-0 flex-col gap-[13px]";

export const FORM_GRID_2_CLASS = "grid grid-cols-1 gap-3 sm:grid-cols-2";

export const FORM_GRID_3_CLASS = "grid grid-cols-1 gap-3 sm:grid-cols-3";

export const CHECK_GRID_CLASS =
  "flex flex-wrap gap-x-4 gap-y-[9px] rounded-[9px] border border-border-default bg-subtle px-3 py-[11px]";

export const NOTICE_WARNING_CLASS =
  "rounded-lg border border-warning-border bg-warning-bg px-[11px] py-2.5 text-xs leading-[1.65] text-warning";

export const NOTICE_BRAND_CLASS =
  "flex items-start gap-2 rounded-lg border border-brand-border bg-brand-subtle px-[11px] py-2.5 text-xs leading-[1.65] text-brand-hover";

export const NOTICE_SUCCESS_CLASS =
  "rounded-[7px] border border-success-border bg-success-bg px-2.5 py-[9px] text-xs leading-[1.6] text-success";

export const EMPTY_INLINE_CLASS =
  "rounded-lg border border-dashed border-border-default px-[11px] py-2.5 text-center text-xs leading-[1.65] text-text-tertiary";

/** 卡片面板与分页栏 */
export const SURFACE_CARD_CLASS =
  "min-w-0 rounded-xl border border-border-default bg-canvas shadow-xs";

export const LIST_FOOTER_CLASS =
  "flex min-h-[52px] flex-col items-start justify-between gap-4 bg-subtle px-[15px] py-3 text-xs text-text-tertiary sm:flex-row sm:items-center sm:px-[18px] sm:py-0";

export const PAGE_NAV_BUTTON_CLASS =
  "grid h-[29px] w-[29px] place-items-center rounded-[7px] border border-border-default bg-canvas text-text-primary transition-colors hover:enabled:border-border-strong hover:enabled:bg-subtle disabled:opacity-40";

/** 管理后台通用区块、目录卡片与详情网格 */
export const ADMIN_SECTION_CLASS =
  "min-w-0 rounded-xl border border-border-default bg-canvas p-3 shadow-xs min-[391px]:p-3.5 sm:rounded-[14px] sm:p-[18px] lg:p-[22px]";

export const ADMIN_SECTION_HEADING_CLASS =
  "mb-[17px] flex flex-col items-start justify-between gap-2.5 border-b border-border-subtle pb-3.5 sm:flex-row sm:gap-5 [&>div>h2]:mt-0.5 [&>div>h2]:text-xl [&>div>h2]:font-semibold [&>div>h2]:tracking-[-0.02em] [&>div>h2]:text-text-primary";

export const ADMIN_DATA_COUNT_CLASS =
  "shrink-0 self-start rounded-full border border-brand-border bg-brand-subtle px-2.5 py-1 text-xs font-semibold text-brand-hover";

export const ADMIN_CARD_LIST_CLASS =
  "m-0 flex min-w-0 list-none flex-col gap-[11px] p-0";

export const ADMIN_CARD_CLASS =
  "min-w-0 rounded-xl border border-border-default bg-canvas p-3.5 transition-[border-color,box-shadow] duration-150 hover:border-brand-border hover:shadow-xs sm:p-[18px]";

export const ADMIN_CARD_HEADING_CLASS =
  "flex min-w-0 flex-col items-start justify-between gap-2.5 sm:flex-row sm:gap-5";

export const ADMIN_TITLE_LINE_CLASS =
  "flex min-w-0 flex-wrap items-center gap-[7px] [&_h3]:min-w-0 [&_h3]:text-lg [&_h3]:leading-[1.25] [&_h3]:font-semibold [&_h3]:tracking-[-0.02em] [&_h3]:text-text-primary [&_h3]:[overflow-wrap:anywhere]";

export const ADMIN_CODE_LINE_CLASS =
  "mt-[5px] font-mono text-xs leading-[1.6] text-text-tertiary [overflow-wrap:anywhere]";

export const ADMIN_DETAIL_GRID_CLASS =
  "mt-[15px] grid min-w-0 grid-cols-1 gap-x-[18px] gap-y-3 border-t border-border-subtle pt-3.5 sm:grid-cols-2 lg:grid-cols-3 [&>div]:min-w-0 [&_dt]:mb-1 [&_dt]:text-xs [&_dt]:font-semibold [&_dt]:text-text-tertiary [&_dd]:m-0 [&_dd]:min-w-0 [&_dd]:text-sm [&_dd]:leading-[1.55] [&_dd]:text-text-primary [&_dd]:[overflow-wrap:anywhere]";

export const ADMIN_DETAIL_WIDE_CLASS = "sm:col-span-2";

export const ADMIN_CODE_VALUE_CLASS = "font-mono !text-xs";

export const ADMIN_CARD_ACTIONS_CLASS =
  "mt-[15px] flex min-h-[38px] flex-col items-stretch gap-[9px] border-t border-border-subtle pt-[13px] sm:flex-row sm:items-center max-sm:[&>button]:w-full";

export const ADMIN_MUTED_CLASS = "text-xs leading-[1.6] text-text-tertiary";

/** 业务 Modal 响应式宽度预设（通过 cn 与 Modal 默认宽度冲突合并） */
export const MODAL_WIDTH_MD = "w-[min(640px,100%)]";
export const MODAL_WIDTH_LG = "w-[min(690px,100%)]";
export const MODAL_WIDTH_XL = "w-[min(760px,100%)]";
export const MODAL_WIDTH_2XL = "w-[min(980px,100%)]";
export const MODAL_WIDTH_DETAIL =
  "w-[min(1100px,100%)] max-h-[min(92vh,1040px)] sm:max-h-[min(92vh,1040px)]";
