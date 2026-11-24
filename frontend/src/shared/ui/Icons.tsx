import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "arrow-right"
  | "book"
  | "chat"
  | "chevron"
  | "close"
  | "download"
  | "edit"
  | "eye"
  | "eye-off"
  | "folder"
  | "history"
  | "plus"
  | "refresh"
  | "search"
  | "settings"
  | "shield"
  | "spark"
  | "sliders"
  | "tools"
  | "trash"
  | "upload"
  | "user";

const ICON_CONTENT: Record<IconName, ReactNode> = {
  "arrow-right": (
    <path
      d="M4.5 12h14M13 6.5l5.5 5.5-5.5 5.5"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
    />
  ),
  book: (
    <>
      <path
        d="M5 4.5h9.5A2.5 2.5 0 0 1 17 7v12H7.5A2.5 2.5 0 0 1 5 16.5v-12Z"
        fill="none"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.55"
      />
      <path
        d="M8 8h5M8 11h6M8 14h4"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.55"
      />
    </>
  ),
  chat: (
    <path
      d="M20 11.5a8 8 0 0 1-8.4 8A9.7 9.7 0 0 1 7 18.4L3.5 20l1.4-3.6A7.9 7.9 0 0 1 4 11.8 8 8 0 0 1 12.2 3.7a8 8 0 0 1 7.8 7.8Z"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.55"
    />
  ),
  chevron: (
    <path
      d="m8.5 5.5 6.5 6.5-6.5 6.5"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.65"
    />
  ),
  close: (
    <path
      d="m6 6 12 12M18 6 6 18"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeWidth="1.65"
    />
  ),
  download: (
    <>
      <path
        d="M12 4v11m0 0 4-4m-4 4-4-4M5 19.5h14"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.55"
      />
    </>
  ),
  edit: (
    <>
      <path
        d="m4.5 16.9-.8 3.4 3.4-.8L18.2 8.4a2.4 2.4 0 0 0-3.4-3.4L4.5 16.9Z"
        fill="none"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.45"
      />
      <path
        d="m13.4 6.4 3.4 3.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.45"
      />
    </>
  ),
  eye: (
    <>
      <path
        d="M3.5 12s3.1-5.2 8.5-5.2 8.5 5.2 8.5 5.2-3.1 5.2-8.5 5.2S3.5 12 3.5 12Z"
        fill="none"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.45"
      />
      <circle
        cx="12"
        cy="12"
        r="2.3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.45"
      />
    </>
  ),
  "eye-off": (
    <>
      <path
        d="M4 4.5 20 20M10.1 6.9A8.9 8.9 0 0 1 12 6.8c5.4 0 8.5 5.2 8.5 5.2a14.5 14.5 0 0 1-2.3 2.9M6.2 8.2A14.4 14.4 0 0 0 3.5 12s3.1 5.2 8.5 5.2c1 0 1.9-.2 2.7-.5"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.45"
      />
      <path
        d="M9.8 9.8a3.1 3.1 0 0 0 4.4 4.4"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.45"
      />
    </>
  ),
  folder: (
    <path
      d="M3.5 7.5v10a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-7L9.6 5H5.5a2 2 0 0 0-2 2Z"
      fill="none"
      stroke="currentColor"
      strokeLinejoin="round"
      strokeWidth="1.55"
    />
  ),
  history: (
    <>
      <path
        d="M4.2 5.5V10h4.5M4.4 9.8a8 8 0 1 1-.2 4.1"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
      <path
        d="M12 7.5V12l3 1.8"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
    </>
  ),
  plus: (
    <path
      d="M12 5v14M5 12h14"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeWidth="1.7"
    />
  ),
  refresh: (
    <>
      <path
        d="M19 8.2A8 8 0 0 0 5.6 6.6L3.8 8.5M5 15.8a8 8 0 0 0 13.4 1.6l1.8-1.9"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
      <path
        d="M4 4.5v4h4M20 19.5v-4h-4"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
    </>
  ),
  search: (
    <>
      <circle
        cx="10.5"
        cy="10.5"
        r="6.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.55"
      />
      <path
        d="m15.2 15.2 4.3 4.3"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.55"
      />
    </>
  ),
  settings: (
    <>
      <circle
        cx="12"
        cy="12"
        r="3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="M19 13.5a7.6 7.6 0 0 0 0-3l2-1.5-2-3.5-2.4 1a8.3 8.3 0 0 0-2.6-1.5L13.5 2h-3L10 5a8.3 8.3 0 0 0-2.6 1.5l-2.4-1L3 9l2 1.5a7.6 7.6 0 0 0 0 3L3 15l2 3.5 2.4-1A8.3 8.3 0 0 0 10 19l.5 3h3l.5-3a8.3 8.3 0 0 0 2.6-1.5l2.4 1L21 15l-2-1.5Z"
        fill="none"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.25"
      />
    </>
  ),
  shield: (
    <>
      <path
        d="m12 3 7 2.7v5.5c0 4.4-2.9 7.8-7 9.8-4.1-2-7-5.4-7-9.8V5.7L12 3Z"
        fill="none"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
      <path
        d="m9 11.8 2 2 4-4.1"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
    </>
  ),
  spark: (
    <path
      d="M12 3.8c.7 4.8 3.4 7.5 8.2 8.2-4.8.7-7.5 3.4-8.2 8.2-.7-4.8-3.4-7.5-8.2-8.2 4.8-.7 7.5-3.4 8.2-8.2Z"
      fill="none"
      stroke="currentColor"
      strokeLinejoin="round"
      strokeWidth="1.45"
    />
  ),
  sliders: (
    <>
      <path
        d="M4 7h6M14 7h6M4 17h10M18 17h2"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.55"
      />
      <circle
        cx="12"
        cy="7"
        r="2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.55"
      />
      <circle
        cx="16"
        cy="17"
        r="2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.55"
      />
    </>
  ),
  tools: (
    <>
      <path
        d="m14.2 6.3 3.5-3.5 3.5 3.5-3.5 3.5M4 20l5.6-5.6M3 4l5 5-3.5 3.5a3.5 3.5 0 0 1-1.5-5.9L3 4Zm9.5 9.5 6.5 6.5"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.45"
      />
    </>
  ),
  trash: (
    <>
      <path
        d="M4.5 7h15M9 7V4.8h6V7M6.5 7l.8 12h9.4l.8-12M10 10.5v5M14 10.5v5"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.45"
      />
    </>
  ),
  upload: (
    <>
      <path
        d="M12 16V5m0 0L8 9m4-4 4 4M5 19.5h14"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.55"
      />
    </>
  ),
  user: (
    <>
      <circle
        cx="12"
        cy="8"
        r="3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="M5 20a7 7 0 0 1 14 0"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.5"
      />
    </>
  ),
};

interface IconProps extends Omit<SVGProps<SVGSVGElement>, "width" | "height"> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 20, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      {...props}
    >
      {ICON_CONTENT[name]}
    </svg>
  );
}
