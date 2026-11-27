import {
  ArrowRight,
  BookText,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  Folder,
  History,
  MessageCircle,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkle,
  Trash2,
  Upload,
  User,
  Wrench,
  X,
  type LucideIcon,
  type LucideProps,
} from "lucide-react";

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

// 统一将旧业务图标名代理至 lucide-react 标准组件，消除手写内联 SVG 路径维护成本。
const ICON_MAP: Record<IconName, LucideIcon> = {
  "arrow-right": ArrowRight,
  book: BookText,
  chat: MessageCircle,
  chevron: ChevronRight,
  close: X,
  download: Download,
  edit: Pencil,
  eye: Eye,
  "eye-off": EyeOff,
  folder: Folder,
  history: History,
  plus: Plus,
  refresh: RefreshCw,
  search: Search,
  settings: Settings,
  shield: ShieldCheck,
  spark: Sparkle,
  sliders: SlidersHorizontal,
  tools: Wrench,
  trash: Trash2,
  upload: Upload,
  user: User,
};

export interface IconProps
  extends Omit<LucideProps, "width" | "height" | "size"> {
  name: IconName;
  size?: number;
}

export function Icon({
  name,
  size = 20,
  strokeWidth = 1.7,
  ...props
}: IconProps) {
  const IconComponent = ICON_MAP[name];
  return (
    <IconComponent
      aria-hidden="true"
      focusable="false"
      size={size}
      strokeWidth={strokeWidth}
      {...props}
    />
  );
}
