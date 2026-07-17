import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { registerSW } from "virtual:pwa-register";

import "./theme.css";
import "./app.css";
import "./install"; // captures beforeinstallprompt at load (Settings uses it)
import { App } from "./App";

// Chat is a live app — take updates immediately rather than prompting.
registerSW({ immediate: true });

const rootEl = document.getElementById("root");
if (rootEl === null) throw new Error("missing #root");

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
