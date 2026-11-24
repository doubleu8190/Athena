import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import * as formatLib from "../../shared/lib/format";
import { makeMessage } from "../../test/fixtures";
import {
  ChatMessageList,
  type ChatAutoScrollRequest,
} from "./ChatMessageList";

const SESSION_ID = "session-scroll";

function makeMessageList(
  overrides: Partial<Parameters<typeof ChatMessageList>[0]> = {},
) {
  return (
    <ChatMessageList
      autoScrollRequest={null}
      error={null}
      hasMore={false}
      isLoading={false}
      isLoadingMore={false}
      messages={[
        makeMessage({
          id: "message-1",
          parts: [{ text: "较早消息", type: "text" }],
          role: "user",
          sequence: 1,
          session_id: SESSION_ID,
        }),
        makeMessage({
          id: "message-2",
          parts: [{ text: "最新回答", type: "text" }],
          sequence: 2,
          session_id: SESSION_ID,
        }),
      ]}
      onLoadMore={() => undefined}
      onRetry={() => undefined}
      sessionId={SESSION_ID}
      transientText=""
      transientUnfinished={false}
      {...overrides}
    />
  );
}

function setMessageListMetrics(node: HTMLElement, initialHeight: number) {
  let scrollHeight = initialHeight;
  Object.defineProperties(node, {
    clientHeight: {
      configurable: true,
      value: 300,
    },
    scrollHeight: {
      configurable: true,
      get: () => scrollHeight,
    },
    scrollTop: {
      configurable: true,
      value: 0,
      writable: true,
    },
  });
  return {
    setScrollHeight(nextHeight: number) {
      scrollHeight = nextHeight;
    },
  };
}

function stubScrollIntoView() {
  const scrollIntoView = vi.fn();
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: scrollIntoView,
    writable: true,
  });
  return scrollIntoView;
}

describe("会话消息滚动", () => {
  it("首次载入定位最新，流式增长只跟随消息容器且不夺焦点", () => {
    const scrollHeightSpy = vi
      .spyOn(HTMLElement.prototype, "scrollHeight", "get")
      .mockReturnValue(900);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    const windowScrollSpy = vi
      .spyOn(window, "scrollTo")
      .mockImplementation(() => undefined);
    const scrollIntoView = stubScrollIntoView();
    const { rerender } = render(makeMessageList());
    const list = screen.getByRole("region", { name: "消息历史" });

    expect(list.scrollTop).toBe(900);
    list.focus();
    scrollHeightSpy.mockReturnValue(1200);
    rerender(makeMessageList({ transientText: "第一段实时输出" }));

    expect(list.scrollTop).toBe(1200);
    expect(list).toHaveFocus();
    expect(windowScrollSpy).not.toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("主动上滑后停止跟随，并可通过回到底部恢复", () => {
    const scrollHeightSpy = vi
      .spyOn(HTMLElement.prototype, "scrollHeight", "get")
      .mockReturnValue(900);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    const scrollIntoView = stubScrollIntoView();
    const { rerender } = render(makeMessageList());
    const list = screen.getByRole("region", { name: "消息历史" });

    list.scrollTop = 100;
    fireEvent.scroll(list);
    expect(
      screen.getByRole("button", { name: "回到底部" }),
    ).toBeInTheDocument();

    scrollHeightSpy.mockReturnValue(1200);
    rerender(
      makeMessageList({
        transientText: "用户离开底部后继续增长的实时输出",
      }),
    );
    expect(list.scrollTop).toBe(100);

    fireEvent.click(screen.getByRole("button", { name: "回到底部" }));
    expect(list.scrollTop).toBe(1200);
    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "nearest",
      inline: "nearest",
    });
    expect(
      screen.queryByRole("button", { name: "回到底部" }),
    ).not.toBeInTheDocument();
  });

  it("加载更早消息退出跟随，不把新增历史当成最新消息", () => {
    const onLoadMore = vi.fn();
    const { rerender } = render(
      makeMessageList({
        hasMore: true,
        onLoadMore,
      }),
    );
    const list = screen.getByRole("region", { name: "消息历史" });
    const metrics = setMessageListMetrics(list, 1000);

    list.scrollTop = 100;
    fireEvent.scroll(list);
    fireEvent.click(
      screen.getByRole("button", { name: "加载更早消息" }),
    );
    expect(onLoadMore).toHaveBeenCalledOnce();

    metrics.setScrollHeight(1300);
    rerender(
      makeMessageList({
        hasMore: false,
        isLoadingMore: true,
        messages: [
          makeMessage({
            id: "message-0",
            parts: [{ text: "刚加载的更早消息", type: "text" }],
            role: "user",
            sequence: 0,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-1",
            parts: [{ text: "较早消息", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-2",
            parts: [{ text: "最新回答", type: "text" }],
            sequence: 2,
            session_id: SESSION_ID,
          }),
        ],
        onLoadMore,
      }),
    );

    expect(list.scrollTop).toBe(100);
    expect(
      screen.getByRole("button", { name: "回到底部" }),
    ).toBeInTheDocument();
  });

  it("补充回答只在对应新用户消息进入列表后回到底部", () => {
    const request: ChatAutoScrollRequest = {
      afterSequence: 2,
      key: "answer-request",
      text: "补充回答内容",
    };
    const { rerender } = render(makeMessageList());
    const list = screen.getByRole("region", { name: "消息历史" });
    const metrics = setMessageListMetrics(list, 1000);
    const scrollIntoView = stubScrollIntoView();

    list.scrollTop = 100;
    fireEvent.scroll(list);
    rerender(makeMessageList({ autoScrollRequest: request }));
    expect(list.scrollTop).toBe(100);

    metrics.setScrollHeight(1200);
    rerender(
      makeMessageList({
        autoScrollRequest: request,
        messages: [
          makeMessage({
            id: "message-1",
            parts: [{ text: "较早消息", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-2",
            parts: [{ text: "最新回答", type: "text" }],
            sequence: 2,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-3",
            parts: [{ text: "补充回答内容", type: "text" }],
            role: "user",
            sequence: 3,
            session_id: SESSION_ID,
          }),
        ],
      }),
    );

    expect(list.scrollTop).toBe(1200);
    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "nearest",
      inline: "nearest",
    });
    expect(
      screen.queryByRole("button", { name: "回到底部" }),
    ).not.toBeInTheDocument();
  });

  it("流式 transientText 连续更新时已有历史消息行保持记忆化零重渲染", () => {
    const formatDateTimeSpy = vi.spyOn(formatLib, "formatDateTime");
    const messages = [
      makeMessage({
        id: "memo-1",
        parts: [{ text: "历史用户消息", type: "text" }],
        role: "user",
        sequence: 1,
        session_id: SESSION_ID,
      }),
      makeMessage({
        id: "memo-2",
        parts: [{ text: "历史助手回答", type: "text" }],
        role: "assistant",
        sequence: 2,
        session_id: SESSION_ID,
      }),
    ];
    const onLoadMore = vi.fn();
    const onRetry = vi.fn();

    const { rerender } = render(
      makeMessageList({
        messages,
        onLoadMore,
        onRetry,
        transientText: "",
      }),
    );
    expect(formatDateTimeSpy).toHaveBeenCalledTimes(2);
    formatDateTimeSpy.mockClear();

    for (const chunk of ["流式片段 1", "流式片段 1+2", "流式片段 1+2+3"]) {
      rerender(
        makeMessageList({
          messages,
          onLoadMore,
          onRetry,
          transientText: chunk,
        }),
      );
    }

    expect(
      screen.getByLabelText("Athena 的临时流式输出"),
    ).toHaveTextContent("流式片段 1+2+3");
    // 历史消息数组引用未变时，流式更新不得触发历史 MessageRow 重渲染。
    expect(formatDateTimeSpy).not.toHaveBeenCalled();
  });

  it("跟底态下仅 footerSlot 挂载增高时自动滚至底部，非跟底态下保持原滚动位置不变", () => {
    const messages = [
      makeMessage({
        id: "slot-msg-1",
        parts: [{ text: "请发起报销审批", type: "text" }],
        role: "user",
        sequence: 1,
        session_id: SESSION_ID,
      }),
      makeMessage({
        id: "slot-msg-2",
        parts: [{ text: "正在准备报销单操作…", type: "text" }],
        role: "assistant",
        sequence: 2,
        session_id: SESSION_ID,
      }),
    ];
    const onLoadMore = vi.fn();
    const onRetry = vi.fn();

    const { rerender } = render(
      makeMessageList({
        footerSlot: null,
        messages,
        onLoadMore,
        onRetry,
        transientText: "",
      }),
    );
    const list = screen.getByRole("region", { name: "消息历史" });
    const metrics = setMessageListMetrics(list, 800);
    list.scrollTop = 800;

    // 场景 1：跟底态下 messages 与 transientText 均不变，仅挂载 footerSlot 且高度增长 -> 自动滚到底部
    metrics.setScrollHeight(1150);
    rerender(
      makeMessageList({
        footerSlot: <section aria-label="等待工具审批">审批卡片</section>,
        messages,
        onLoadMore,
        onRetry,
        transientText: "",
      }),
    );
    expect(list.scrollTop).toBe(1150);

    // 场景 2：用户主动上滑离开底部，随后 footerSlot 更新增高 -> 严格保留原滚动位置
    list.scrollTop = 120;
    fireEvent.scroll(list);
    expect(
      screen.getByRole("button", { name: "回到底部" }),
    ).toBeInTheDocument();

    metrics.setScrollHeight(1450);
    rerender(
      makeMessageList({
        footerSlot: <section aria-label="等待工具审批">新增第二张审批卡片</section>,
        messages,
        onLoadMore,
        onRetry,
        transientText: "",
      }),
    );
    expect(list.scrollTop).toBe(120);
  });
});
