import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
// 按 Cascade 层级顺序引入 Design Tokens、全局样式、最小兼容层与未迁移模块样式表。
import "./shared/styles/tokens.css";
import "./shared/styles/global.css";
import "./shared/styles/legacy-compat.css";
import "./features/knowledge/knowledge.css";
import "./features/admin/admin.css";
import "./features/skills/personalSkills.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Missing #root element");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
