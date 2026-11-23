import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
// 先引入 Design Tokens 与 Tailwind CSS v4，再加载旧样式桥接与各模块过渡样式。
import "./shared/styles/tokens.css";
import "./shared/styles/global.css";
import "./app/app.css";
import "./app/workspace.css";
import "./features/chat/chat.css";
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
