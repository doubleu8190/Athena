import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FakeSseStream,
  createSseStreamResponse,
} from "../../test/fakeSseStream";
import {
  getRequestUrl,
  listResponse,
  makeMessage,
  makeRun,
  makeSession,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { FilePreviewDialog } from "../files/FilePreviewDialog";
import * as fileHooks from "../files/hooks";
import * as modelHooks from "../models/hooks";
import * as projectHooks from "../projects/hooks";
import * as skillHooks from "../skills/hooks";
import { ChatHeader } from "./ChatHeader";
import { SessionComposer } from "./SessionComposer";
import { SessionFilesDrawer } from "./SessionFilesDrawer";
import { WaitingInputBanner } from "./WaitingInputBanner";

const REACT_MEMO_TYPE = Symbol.for("react.memo");

function isMemoComponent(component: unknown): boolean {
  return (
    typeof component === "object" &&
    component !== null &&
    "$$typeof" in component &&
    (component as { $$typeof: symbol }).$$typeof === REACT_MEMO_TYPE
  );
}

afterEach(() => {
  FakeSseStream.instances = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("流式渲染期间非流式兄弟组件重渲染隔离", () => {
  it("五大非流式兄弟组件均保持 React.memo 包装", () => {
    expect(isMemoComponent(ChatHeader)).toBe(true);
    expect(isMemoComponent(WaitingInputBanner)).toBe(true);
    expect(isMemoComponent(SessionComposer)).toBe(true);
    expect(isMemoComponent(SessionFilesDrawer)).toBe(true);
    expect(isMemoComponent(FilePreviewDialog)).toBe(true);
  });

  it("多帧高频 text_delta 流式更新期间，ChatHeader / SessionComposer / SessionFilesDrawer 零重渲染", async () => {
    const session = makeSession({
      id: "session-memo-isolation",
      project_id: PROJECT_A.id,
      title: "流式渲染隔离验证",
    });
    const running = makeRun({
      id: "run-memo-isolation",
      session_id: session.id,
      status: "running",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/models") {
          return jsonResponse({ default_model_id: null, items: [] });
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse({ items: [], page: 1, page_size: 100 });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 30, 1));
        }
        if (url.pathname === `/api/v1/sessions/${session.id}`) {
          return jsonResponse(session);
        }
        if (url.pathname === `/api/v1/sessions/${session.id}/messages`) {
          return jsonResponse({
            items: [
              makeMessage({
                id: running.trigger_message_id,
                parts: [{ text: "请流式输出一份报告", type: "text" }],
                role: "user",
                run_id: null,
                sequence: 1,
                session_id: session.id,
              }),
            ],
            next_before_sequence: null,
          });
        }
        if (url.pathname === `/api/v1/sessions/${session.id}/files`) {
          return jsonResponse({ items: [], next_after_file_id: null });
        }
        if (url.pathname === `/api/v1/sessions/${session.id}/runs`) {
          return jsonResponse({ items: [running], next_cursor: null });
        }
        if (
          url.pathname === `/api/v1/sessions/${session.id}/runs/${running.id}`
        ) {
          return jsonResponse(running);
        }
        if (
          url.pathname ===
          `/api/v1/sessions/${session.id}/runs/${running.id}/tool-calls`
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname ===
          `/api/v1/sessions/${session.id}/runs/${running.id}/stream`
        ) {
          return createSseStreamResponse(url.pathname, init);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    // 通过各兄弟组件独有 Hook 的调用次数间接观测真实组件渲染，零侵入生产代码。
    const projectDirectorySpy = vi.spyOn(projectHooks, "useProjectDirectory");
    const publicSkillsSpy = vi.spyOn(skillHooks, "usePublicSkills");
    const userModelsSpy = vi.spyOn(modelHooks, "useUserModels");
    const sessionFilesSpy = vi.spyOn(fileHooks, "useSessionFiles");

    renderApp(`/chat/${session.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    expect(
      await screen.findByRole("heading", { name: "流式渲染隔离验证" }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(FakeSseStream.instances).toHaveLength(1);
    });
    const source = FakeSseStream.instances[0];

    // 先发送初始 running 快照，等页面进入稳定流式接收态后再清空渲染计数基线。
    act(() => {
      source.emit("snapshot", {
        payload: {
          event_sequence: 1,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [],
          messages_before_sequence: null,
          messages_complete: true,
          report_message_id: null,
          status: "running",
        },
      });
    });

    await waitFor(() => {
      expect(source.closed).toBe(false);
    });

    projectDirectorySpy.mockClear();
    publicSkillsSpy.mockClear();
    userModelsSpy.mockClear();
    sessionFilesSpy.mockClear();

    // 连续推送 3 个独立动画帧的 text_delta，验证每次 transientText 更新都不会击穿兄弟组件 memo。
    for (const [index, chunk] of ["第一段，", "第二段，", "第三段。"].entries()) {
      act(() => {
        source.emit("text_delta", { payload: { text: chunk } });
      });
      const expectedText = ["第一段，", "第二段，", "第三段。"]
        .slice(0, index + 1)
        .join("");
      await waitFor(() => {
        expect(
          screen.getByLabelText("Athena 的临时流式输出"),
        ).toHaveTextContent(expectedText);
      });
    }

    // ChatHeader 内部调用 useProjectDirectory，流式期间必须零重渲染。
    expect(projectDirectorySpy).not.toHaveBeenCalled();
    // SessionComposer 内部调用 usePublicSkills / useUserModels，流式期间必须零重渲染。
    expect(publicSkillsSpy).not.toHaveBeenCalled();
    expect(userModelsSpy).not.toHaveBeenCalled();
    // SessionWorkbench 自身 3 次流式更新各调 1 次 useSessionFiles；
    // ChatHeader / SessionComposer / SessionFilesDrawer 均被 memo 拦截，因此总调用次数仅为 3 次而非 12 次。
    expect(sessionFilesSpy).toHaveBeenCalledTimes(3);
  });
});
