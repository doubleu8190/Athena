import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
// 按 Cascade 层级顺序引入 Design Tokens 与全局基础排版样式；全站业务模块均已统一使用 Tailwind v4。
import "./shared/styles/tokens.css";
import "./shared/styles/global.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Missing #root element");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
