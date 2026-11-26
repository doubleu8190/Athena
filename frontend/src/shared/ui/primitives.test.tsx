import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
  AutoResizeTextarea,
  Badge,
  Brand,
  Button,
  Checkbox,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  EmptyState,
  InlineAlert,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
  Skeleton,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
  Toaster,
  Tooltip,
  TooltipProvider,
  VisibilityBadge,
  cn,
  toast,
} from "./index";

afterEach(() => {
  toast.dismiss();
});

describe("shared/ui primitives", () => {
  it("cn 能够合并条件类名并消解 Tailwind 冲突", () => {
    const disabled = false;
    expect(cn("px-2 py-1 text-sm", disabled && "opacity-50", "px-4")).toBe(
      "py-1 text-sm px-4",
    );
  });

  it("Button 默认 type='button'，支持变体、loading 防连点与 asChild", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();

    const { rerender } = render(
      <Button onClick={onClick} size="sm" variant="secondary">
        保存草稿
      </Button>,
    );

    const button = screen.getByRole("button", { name: "保存草稿" });
    expect(button).toHaveAttribute("type", "button");
    await user.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);

    // loading 态自动禁用按钮并标注 aria-busy，阻止重复点击。
    rerender(
      <Button loading onClick={onClick} variant="danger">
        提交中
      </Button>,
    );
    const loadingButton = screen.getByRole("button", { name: "提交中" });
    expect(loadingButton).toBeDisabled();
    expect(loadingButton).toHaveAttribute("aria-busy", "true");
    await user.click(loadingButton);
    expect(onClick).toHaveBeenCalledTimes(1);

    // asChild 模式下安全渲染子元素且支持 loading 不触发 React.Children.only 异常。
    rerender(
      <Button asChild loading variant="outline">
        <a href="/knowledge">前往知识库</a>
      </Button>,
    );
    const link = screen.getByRole("link", { name: "前往知识库" });
    expect(link).toHaveAttribute("aria-busy", "true");
    expect(link).toHaveAttribute("aria-disabled", "true");

    // 图标按钮标注 data-icon-button，配合粗指针触控设备 44px 热区规则。
    rerender(
      <Button aria-label="刷新" size="icon" variant="ghost">
        <span>R</span>
      </Button>,
    );
    expect(screen.getByRole("button", { name: "刷新" })).toHaveAttribute(
      "data-icon-button",
      "true",
    );
  });

  it("Input 支持 error 态 aria-invalid、前后置槽位及密码显隐切换 aria-pressed", async () => {
    const user = userEvent.setup();
    render(
      <Input
        aria-label="登录密码"
        error
        passwordToggle
        prefixSlot={<span>前缀</span>}
        suffixSlot={<span>后缀</span>}
      />,
    );

    const input = screen.getByLabelText("登录密码");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("type", "password");
    expect(screen.getByText("前缀")).toBeInTheDocument();
    expect(screen.getByText("后缀")).toBeInTheDocument();

    const toggleButton = screen.getByRole("button", { name: "显示密码" });
    expect(toggleButton).toHaveAttribute("type", "button");
    expect(toggleButton).toHaveAttribute("aria-pressed", "false");

    await user.click(toggleButton);
    expect(input).toHaveAttribute("type", "text");
    expect(toggleButton).toHaveAttribute("aria-pressed", "true");
    expect(toggleButton).toHaveAccessibleName("隐藏密码");

    await user.click(toggleButton);
    expect(input).toHaveAttribute("type", "password");
    expect(toggleButton).toHaveAttribute("aria-pressed", "false");

    // 纯输入无槽位与禁用态测试。
    render(<Input aria-label="只读账号" disabled passwordToggle />);
    expect(screen.getByLabelText("只读账号")).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "显示密码" })[1]).toBeDisabled();
  });

  it("Textarea 与 AutoResizeTextarea 支持 error 态、实时字数统计、min-h-0 与高度边界", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Textarea aria-label="简介" error maxLength={20} placeholder="输入简介" />
        <AutoResizeTextarea
          aria-label="指令输入"
          maxHeight={120}
          minRows={2}
        />
        <AutoResizeTextarea
          aria-label="单行输入"
          minRows={1}
        />
        <AutoResizeTextarea
          aria-label="限高小于最小行高"
          maxHeight={60}
          minRows={4}
        />
      </div>,
    );

    const intro = screen.getByLabelText("简介");
    expect(intro).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("0 / 20")).toBeInTheDocument();

    await user.type(intro, "Athena");
    expect(screen.getByText("6 / 20")).toBeInTheDocument();

    const autoInput = screen.getByLabelText("指令输入") as HTMLTextAreaElement;
    expect(autoInput.className).toContain("min-h-0");
    expect(autoInput.className).not.toContain("min-h-[84px]");

    // 模拟内容 scrollHeight 超过 maxHeight 时自动启用纵向滚动而非禁止滚动。
    Object.defineProperty(autoInput, "scrollHeight", {
      configurable: true,
      value: 260,
    });
    fireEvent.input(autoInput, { target: { value: "很长的多行内容" } });
    expect(autoInput.style.height).toBe("120px");
    expect(autoInput.style.overflowY).toBe("auto");

    // minRows={1} 不再被 min-h-[84px] 锁死，可收敛至单行高度（约 40px < 84px）。
    const singleRowInput = screen.getByLabelText("单行输入") as HTMLTextAreaElement;
    Object.defineProperty(singleRowInput, "scrollHeight", {
      configurable: true,
      value: 38,
    });
    fireEvent.input(singleRowInput, { target: { value: "一行" } });
    expect(Number.parseFloat(singleRowInput.style.height)).toBeLessThan(84);
    expect(singleRowInput.style.overflowY).toBe("hidden");

    // maxHeight < minPx 时应受限于 maxHeight，并在内容超出时启用滚动。
    const clampedInput = screen.getByLabelText(
      "限高小于最小行高",
    ) as HTMLTextAreaElement;
    Object.defineProperty(clampedInput, "scrollHeight", {
      configurable: true,
      value: 90,
    });
    fireEvent.input(clampedInput, { target: { value: "超出限高" } });
    expect(clampedInput.style.height).toBe("60px");
    expect(clampedInput.style.overflowY).toBe("auto");
  });

  it("Dialog 支持打开关闭、尺寸档位及 closeDisabled 拦截提交中误关", async () => {
    const user = userEvent.setup();

    function DemoDialog({ closeDisabled }: { closeDisabled: boolean }) {
      const [open, setOpen] = useState(false);
      return (
        <Dialog onOpenChange={setOpen} open={open}>
          <DialogTrigger asChild>
            <Button>新建项目</Button>
          </DialogTrigger>
          <DialogContent closeDisabled={closeDisabled} size="lg">
            <DialogHeader>
              <DialogTitle>创建项目</DialogTitle>
              <DialogDescription>填写项目基本信息。</DialogDescription>
            </DialogHeader>
            <DialogBody>表单正文</DialogBody>
            <DialogFooter>
              <Button variant="primary">确定</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      );
    }

    const { rerender } = render(<DemoDialog closeDisabled />);
    await user.click(screen.getByRole("button", { name: "新建项目" }));

    const dialog = screen.getByRole("dialog", { name: "创建项目" });
    expect(dialog).toBeInTheDocument();
    expect(dialog.className).toContain("max-w-[680px]");

    const closeButton = screen.getByRole("button", { name: "关闭" });
    expect(closeButton).toBeDisabled();

    // closeDisabled 为 true 时按 Escape 与点击外部区域均不应关闭弹窗。
    await user.keyboard("{Escape}");
    fireEvent.pointerDown(document.body);
    expect(screen.getByRole("dialog", { name: "创建项目" })).toBeInTheDocument();

    // 解除禁用后允许 Escape 与关闭按钮关闭。
    rerender(<DemoDialog closeDisabled={false} />);
    expect(screen.getByRole("button", { name: "关闭" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("AlertDialog 支持高危确认、额外复选框槽位与 loading 态", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();

    render(
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button variant="danger-outline">删除知识库</Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除知识库？</AlertDialogTitle>
            <AlertDialogDescription>此操作不可恢复。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogBody>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox aria-label="同步删除关联文档" />
              <span>同步删除关联文档</span>
            </label>
          </AlertDialogBody>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={onConfirm} variant="danger">
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>,
    );

    await user.click(screen.getByRole("button", { name: "删除知识库" }));
    expect(screen.getByRole("alertdialog", { name: "确认删除知识库？" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "同步删除关联文档" })).toBeInTheDocument();

    // 高危确认弹窗不因点击外部遮罩区域而误关。
    fireEvent.pointerDown(document.body);
    expect(screen.getByRole("alertdialog", { name: "确认删除知识库？" })).toBeInTheDocument();

    // 点击取消关闭对话框且不触发确认回调。
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();

    // 再次打开并确认。
    await user.click(screen.getByRole("button", { name: "删除知识库" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("AlertDialogAction 支持 loading、closeDisabled 拦截 Escape 及 asChild 语义", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();

    const { rerender } = render(
      <AlertDialog onOpenChange={onOpenChange} open>
        <AlertDialogContent closeDisabled>
          <AlertDialogHeader>
            <AlertDialogTitle>正在删除</AlertDialogTitle>
            <AlertDialogDescription>请稍候</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled>取消</AlertDialogCancel>
            <AlertDialogAction loading variant="danger">
              删除中
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>,
    );

    const actionBtn = screen.getByRole("button", { name: "删除中" });
    expect(actionBtn).toBeDisabled();
    expect(actionBtn).toHaveAttribute("aria-busy", "true");

    // closeDisabled 时按 Escape 不触发关闭。
    await user.keyboard("{Escape}");
    expect(onOpenChange).not.toHaveBeenCalled();

    // asChild 模式下支持 Slottable + loading 且不给 <a> 注入非法的 type="button"。
    rerender(
      <AlertDialog open>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>跳转确认</AlertDialogTitle>
            <AlertDialogDescription>即将前往外部文档</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <a href="/back">返回列表</a>
            </AlertDialogCancel>
            <AlertDialogAction asChild loading variant="danger">
              <a href="/danger-confirm">继续跳转</a>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>,
    );

    const cancelLink = screen.getByRole("link", { name: "返回列表" });
    expect(cancelLink).not.toHaveAttribute("type");
    const actionLink = screen.getByRole("link", { name: "继续跳转" });
    expect(actionLink).not.toHaveAttribute("type");
    expect(actionLink).toHaveAttribute("aria-busy", "true");
    expect(actionLink).toHaveAttribute("aria-disabled", "true");
  });

  it("Sheet 支持左侧与右侧抽屉、关闭按钮及 closeDisabled 拦截", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Sheet>
          <SheetTrigger asChild>
            <Button>打开左侧抽屉</Button>
          </SheetTrigger>
          <SheetContent side="left">
            <SheetHeader>
              <SheetTitle>导航菜单</SheetTitle>
              <SheetDescription>移动端工作区导航</SheetDescription>
            </SheetHeader>
            <SheetBody>左侧内容</SheetBody>
            <SheetFooter>底部区域</SheetFooter>
          </SheetContent>
        </Sheet>

        <Sheet>
          <SheetTrigger asChild>
            <Button>打开右侧抽屉</Button>
          </SheetTrigger>
          <SheetContent closeDisabled side="right">
            <SheetHeader>
              <SheetTitle>文档详情</SheetTitle>
              <SheetDescription>查看片段元信息</SheetDescription>
            </SheetHeader>
            <SheetBody>右侧内容</SheetBody>
          </SheetContent>
        </Sheet>
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "打开左侧抽屉" }));
    const leftSheet = screen.getByRole("dialog", { name: "导航菜单" });
    expect(leftSheet).toHaveAttribute("data-side", "left");
    expect(leftSheet.className).toContain("w-[280px]");
    await user.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开右侧抽屉" }));
    const rightSheet = screen.getByRole("dialog", { name: "文档详情" });
    expect(rightSheet).toHaveAttribute("data-side", "right");
    expect(rightSheet.className).toContain("w-[380px]");
    expect(screen.getByRole("button", { name: "关闭" })).toBeDisabled();

    // closeDisabled 时拦截 Escape 与外部点击。
    await user.keyboard("{Escape}");
    fireEvent.pointerDown(document.body);
    expect(screen.getByRole("dialog", { name: "文档详情" })).toBeInTheDocument();
  });

  it("DropdownMenu 支持分组标签、分割线与危险操作项点击", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();

    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button>更多操作</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuLabel>文档管理</DropdownMenuLabel>
          <DropdownMenuItem>重命名</DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem destructive onSelect={onDelete}>
            删除文档
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    );

    await user.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByText("文档管理")).toBeInTheDocument();
    const deleteItem = screen.getByRole("menuitem", { name: "删除文档" });
    expect(deleteItem).toHaveAttribute("data-destructive", "true");

    await user.click(deleteItem);
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it("Popover 支持点击打开与 Escape 关闭", async () => {
    const user = userEvent.setup();
    render(
      <Popover>
        <PopoverTrigger asChild>
          <Button>筛选条件</Button>
        </PopoverTrigger>
        <PopoverContent>按更新时间筛选</PopoverContent>
      </Popover>,
    );

    await user.click(screen.getByRole("button", { name: "筛选条件" }));
    expect(screen.getByText("按更新时间筛选")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByText("按更新时间筛选")).not.toBeInTheDocument();
  });

  it("Select 支持选项切换与 error 态无障碍属性", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();

    render(
      <Select onValueChange={onValueChange}>
        <SelectTrigger aria-label="可见范围" error>
          <SelectValue placeholder="请选择范围" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectLabel>范围选项</SelectLabel>
            <SelectItem value="company">全公司</SelectItem>
            <SelectSeparator />
            <SelectItem value="personal">个人</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>,
    );

    const trigger = screen.getByRole("combobox", { name: "可见范围" });
    expect(trigger).toHaveAttribute("aria-invalid", "true");

    await user.click(trigger);
    await user.click(screen.getByRole("option", { name: "全公司" }));
    expect(onValueChange).toHaveBeenCalledWith("company");
    expect(trigger).toHaveTextContent("全公司");
  });

  it("Tabs 支持 underline 与 segmented 变体以及键盘切换", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Tabs defaultValue="overview" variant="underline">
          <TabsList>
            <TabsTrigger value="overview">概览</TabsTrigger>
            <TabsTrigger value="members">成员</TabsTrigger>
          </TabsList>
          <TabsContent value="overview">概览内容</TabsContent>
          <TabsContent value="members">成员内容</TabsContent>
        </Tabs>

        <Tabs defaultValue="all" variant="segmented">
          <TabsList>
            <TabsTrigger value="all">全部</TabsTrigger>
            <TabsTrigger value="mine">我的</TabsTrigger>
          </TabsList>
          <TabsContent value="all">全部资源</TabsContent>
          <TabsContent value="mine">我的资源</TabsContent>
        </Tabs>
      </div>,
    );

    const overviewTab = screen.getByRole("tab", { name: "概览" });
    const membersTab = screen.getByRole("tab", { name: "成员" });
    expect(overviewTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("概览内容")).toBeInTheDocument();

    // 支持键盘方向键切换标签页。
    overviewTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(membersTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("成员内容")).toBeInTheDocument();

    const mineTab = screen.getByRole("tab", { name: "我的" });
    await user.click(mineTab);
    expect(screen.getByText("我的资源")).toBeInTheDocument();
  });

  it("Switch 支持 sm/md 尺寸与状态切换", async () => {
    const user = userEvent.setup();
    const onCheckedChange = vi.fn();

    render(
      <Switch
        aria-label="启用技能"
        onCheckedChange={onCheckedChange}
        size="sm"
      />,
    );

    const sw = screen.getByRole("switch", { name: "启用技能" });
    expect(sw).toHaveAttribute("data-size", "sm");
    expect(sw).toHaveAttribute("aria-checked", "false");

    await user.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });

  it("Checkbox 支持普通勾选与 indeterminate 半选态", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Checkbox aria-label="全选列表" checked="indeterminate" />
        <Checkbox aria-label="同意协议" defaultChecked="indeterminate" />
      </div>,
    );

    const controlled = screen.getByRole("checkbox", { name: "全选列表" });
    expect(controlled).toHaveAttribute("aria-checked", "mixed");
    expect(controlled).toHaveAttribute("data-state", "indeterminate");

    const uncontrolled = screen.getByRole("checkbox", { name: "同意协议" });
    expect(uncontrolled).toHaveAttribute("aria-checked", "mixed");
    await user.click(uncontrolled);
    expect(uncontrolled).toHaveAttribute("aria-checked", "true");
  });

  it("Badge 与 VisibilityBadge 正确渲染语义标签与三层可见性范围", () => {
    render(
      <div>
        <Badge variant="brand">推荐</Badge>
        <Badge variant="success">已就绪</Badge>
        <Badge variant="danger">失败</Badge>
        <VisibilityBadge scope="company" />
        <VisibilityBadge scope="department" />
        <VisibilityBadge departmentName="研发中心" scope="department" />
        <VisibilityBadge scope="personal" />
      </div>,
    );

    expect(screen.getByText("推荐")).toBeInTheDocument();
    expect(screen.getByText("已就绪")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("全公司")).toBeInTheDocument();
    expect(screen.getByText("部门共享")).toBeInTheDocument();
    expect(screen.getByText("研发中心")).toBeInTheDocument();
    expect(screen.getByText("个人")).toBeInTheDocument();
  });

  it("Collapsible 支持展开与折叠切换", async () => {
    const user = userEvent.setup();
    render(
      <Collapsible>
        <CollapsibleTrigger asChild>
          <Button>高级配置</Button>
        </CollapsibleTrigger>
        <CollapsibleContent>超时时间设置</CollapsibleContent>
      </Collapsible>,
    );

    const trigger = screen.getByRole("button", { name: "高级配置" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("超时时间设置")).not.toBeInTheDocument();

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("超时时间设置")).toBeInTheDocument();
  });

  it("Tooltip 在聚焦/悬停时展示提示，disabled 或布尔值 content 不挂载浮层", async () => {
    const user = userEvent.setup();
    render(
      <TooltipProvider delayDuration={0}>
        <Tooltip content="复制对话链接">
          <Button>复制</Button>
        </Tooltip>
        <Tooltip content="不会显示" disabled>
          <Button>已禁用提示</Button>
        </Tooltip>
        <Tooltip content={false}>
          <Button>条件关闭提示</Button>
        </Tooltip>
      </TooltipProvider>,
    );

    await user.hover(screen.getByRole("button", { name: "复制" }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("复制对话链接");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    });

    await user.hover(screen.getByRole("button", { name: "已禁用提示" }));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    await user.hover(screen.getByRole("button", { name: "条件关闭提示" }));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("Toaster 挂载后可通过 toast 触发中性与语义通知并绑定 Design Tokens 变量", async () => {
    render(<Toaster />);

    act(() => {
      toast("普通中性提示");
      toast.success("设置已保存");
    });

    await waitFor(() => {
      expect(screen.getByText("普通中性提示")).toBeInTheDocument();
      expect(screen.getByText("设置已保存")).toBeInTheDocument();
    });

    const toasterList = document.querySelector("[data-sonner-toaster]");
    expect(toasterList).not.toBeNull();
    const toasterStyle = toasterList?.getAttribute("style") ?? "";
    expect(toasterStyle).toContain("--normal-bg: var(--bg-canvas)");
    expect(toasterStyle).toContain("--border-radius: 0.75rem");
    expect(toasterStyle).toContain("--success-bg: var(--toast-success-bg)");
    // 禁止同元素同名 CSS 变量自引用循环（如 --success-bg: var(--success-bg)）。
    expect(toasterStyle).not.toMatch(/(--[a-z0-9-]+)\s*:\s*var\(\s*\1\s*[,)]/i);
  });

  it("Skeleton 与 InlineAlert 具备正确的无障碍语义与操作槽位", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();

    const { container } = render(
      <div>
        <Skeleton className="h-4 w-32" data-testid="skeleton-block" />
        <InlineAlert
          action={
            <Button onClick={onRetry} size="xs" variant="outline">
              重试
            </Button>
          }
          description="请检查网络连接后再次尝试。"
          title="加载知识库失败"
          variant="danger"
        />
        <InlineAlert
          description="系统将在 10 分钟后开始例行维护。"
          title="维护提醒"
          variant="warning"
        />
        <InlineAlert description="文档向量化已完成。" title="同步成功" variant="success" />
        <InlineAlert description="当前为只读预览模式。" variant="info" />
      </div>,
    );

    const skeleton = screen.getByTestId("skeleton-block");
    expect(skeleton).toHaveAttribute("aria-hidden", "true");
    expect(skeleton.className).toContain("motion-reduce:animate-none");

    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(2);
    expect(alerts[0]).toHaveTextContent("加载知识库失败");
    expect(alerts[1]).toHaveTextContent("维护提醒");

    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[0]).toHaveTextContent("同步成功");
    expect(statuses[1]).toHaveTextContent("当前为只读预览模式。");

    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(container).toBeTruthy();
  });

  it("Brand 与 EmptyState 正确渲染品牌标识与标准空状态面板", () => {
    const { rerender } = render(
      <div>
        <Brand />
        <EmptyState
          action={<Button>新建文档</Button>}
          description="上传文档后即可在对话中检索引用。"
          icon="book"
          title="暂无知识文档"
        />
      </div>,
    );

    expect(screen.getByText("Athena")).toBeInTheDocument();
    expect(screen.getByText("企业智能工作台")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "暂无知识文档" })).toBeInTheDocument();
    expect(screen.getByText("上传文档后即可在对话中检索引用。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建文档" })).toBeInTheDocument();

    rerender(<Brand compact light />);
    expect(screen.getByText("Athena")).toBeInTheDocument();
    expect(screen.queryByText("企业智能工作台")).not.toBeInTheDocument();

    rerender(<Brand markOnly />);
    expect(screen.queryByText("Athena")).not.toBeInTheDocument();
  });
});
