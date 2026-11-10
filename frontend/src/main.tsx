import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
import "./shared/styles/global.css";
import "./features/auth/auth.css";
import "./app/app.css";
import "./app/workspace.css";
import "./features/chat/chat.css";
import "./features/knowledge/knowledge.css";
import "./features/admin/admin.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Missing #root element");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
