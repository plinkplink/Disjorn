import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./theme.css";
import "./app.css";
import "./install"; // captures beforeinstallprompt at load (Settings uses it)
import "./pwa"; // registers the service worker + update polling at load
import { App } from "./App";

const rootEl = document.getElementById("root");
if (rootEl === null) throw new Error("missing #root");

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
