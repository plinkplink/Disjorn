import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Every server-owned REST prefix is proxied to the FastAPI dev server so the
// client can use relative URLs (same-origin cookies) in dev and prod alike.
const API_PREFIXES = [
  "/auth",
  "/me",
  "/channels",
  "/dms",
  "/bots",
  "/messages",
  "/search",
  "/media",
  "/avatars",
  "/picker",
  "/upload",
  "/attachments",
  "/unfurl",
  "/summarize",
  "/stt",
  "/chibi",
  "/push",
  "/vapid-public-key",
  "/notify-prefs",
  "/healthz",
];

const proxy: Record<string, object> = Object.fromEntries(
  API_PREFIXES.map((prefix) => [prefix, { target: "http://localhost:8000" }]),
);
proxy["/ws"] = { target: "ws://localhost:8000", ws: true };

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "autoUpdate",
      injectRegister: false, // we register in main.tsx via virtual:pwa-register
      manifest: {
        name: "Disjorn",
        short_name: "Disjorn",
        description: "Light self-hosted chat",
        start_url: "/",
        display: "standalone",
        background_color: "#1e1f24",
        theme_color: "#1e1f24",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "/icons/icon-512-maskable.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png,webmanifest}"],
      },
      devOptions: { enabled: false },
    }),
  ],
  server: { port: 5173, proxy },
});
