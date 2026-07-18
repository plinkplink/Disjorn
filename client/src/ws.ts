/* Realtime client for GET /ws.

   - Cookie auth on the handshake (browser sends `disjorn_session` itself).
   - Server has no heartbeat: liveness = the socket staying open. On close we
     reconnect with exponential backoff (1s -> 30s, +/- jitter).
   - On RECONNECT (any ready after the first): refetch GET /channels and, for
     every channel with local messages, backfill `?from_seq=lastSeq+1`
     (current-state semantics — edits applied, tombstones drop deletions).
   - Focus protocol: sendFocus on every channel switch and on window
     blur/focus — the server suppresses push notifications for focused
     channels, so this must stay accurate. The last focus is re-sent after
     every (re)connect because focus is per-connection server state. */

import { useChannels } from "./stores/channels";
import { useMessages } from "./stores/messages";
import { usePresence } from "./stores/presence";
import type { ServerFrame, SettableStatus } from "./types";

const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;

type SocketState = "idle" | "connecting" | "open" | "ready";

export class DisjornSocket {
  private ws: WebSocket | null = null;
  private state: SocketState = "idle";
  private attempt = 0;
  private hadReady = false; // a ready has been seen at some point => next ready is a reconnect
  private stopped = true;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private focusedChannelId: number | null = null;

  /** Open the socket (and keep it open until disconnect()). Idempotent. */
  connect(): void {
    this.stopped = false;
    if (this.ws !== null || this.reconnectTimer !== null) return;
    this.open();
  }

  /** Close for good (logout). Resets reconnect state. */
  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.attempt = 0;
    this.hadReady = false;
    this.state = "idle";
    const ws = this.ws;
    this.ws = null;
    ws?.close();
  }

  /* ---- client ops ---- */

  sendTyping(channelId: number): void {
    this.send({ op: "typing", channel_id: channelId });
  }

  sendStatus(status: SettableStatus): void {
    this.send({ op: "status", status });
  }

  /**
   * Mark a channel as focused (null = nothing focused). Call on every channel
   * switch AND on window blur/focus — push suppression depends on it.
   */
  sendFocus(channelId: number | null): void {
    this.focusedChannelId = channelId;
    this.send({ op: "focus", channel_id: channelId });
  }

  /* ---- internals ---- */

  private send(frame: Record<string, unknown>): void {
    if (this.state === "ready" && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }

  private open(): void {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    this.ws = ws;
    this.state = "connecting";

    ws.onopen = () => {
      if (ws !== this.ws) return;
      this.state = "open"; // not usable until the server's ready frame
    };
    ws.onmessage = (event: MessageEvent<string>) => {
      if (ws !== this.ws) return;
      let frame: ServerFrame;
      try {
        frame = JSON.parse(event.data) as ServerFrame;
      } catch {
        return;
      }
      this.dispatch(frame);
    };
    ws.onclose = () => {
      if (ws !== this.ws) return;
      this.ws = null;
      this.state = "idle";
      this.scheduleReconnect();
    };
    // onerror always precedes onclose; close handling is enough.
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return;
    const base = Math.min(BACKOFF_MIN_MS * 2 ** this.attempt, BACKOFF_MAX_MS);
    const jitter = base * 0.25 * (Math.random() * 2 - 1); // +/- 25%
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.open();
    }, Math.round(base + jitter));
  }

  private dispatch(frame: ServerFrame): void {
    switch (frame.type) {
      case "ready": {
        this.state = "ready";
        this.attempt = 0;
        const isReconnect = this.hadReady;
        this.hadReady = true;
        // Focus is per-connection server state — restore it first so push
        // suppression is correct while we resync.
        if (this.focusedChannelId !== null) {
          this.send({ op: "focus", channel_id: this.focusedChannelId });
        }
        if (isReconnect) void this.resync();
        return;
      }
      case "message_create": {
        const { message } = frame;
        useMessages.getState().applyCreate(message);
        // Viewing the channel with the window focused => instantly read.
        const isRead =
          useChannels.getState().activeChannelId === message.channel_id &&
          document.hasFocus();
        useChannels.getState().onMessageCreate(message, isRead);
        if (isRead) {
          void useChannels.getState().markRead(message.channel_id, message.seq);
        }
        return;
      }
      case "message_edit":
        useMessages.getState().applyEdit(frame.message);
        return;
      case "message_delete":
        useMessages.getState().applyDelete(frame.channel_id, frame.id, frame.seq);
        return;
      case "typing_start":
        usePresence
          .getState()
          .typingStarted(frame.channel_id, frame.author_type, frame.author_id);
        return;
      case "presence":
        usePresence.getState().setStatus(frame.user_id, frame.status);
        return;
      case "channel_create":
        useChannels.getState().onChannelCreate(frame.channel);
        return;
    }
  }

  /** After a reconnect: refresh the sidebar and close local message gaps. */
  private async resync(): Promise<void> {
    try {
      await useChannels.getState().refresh();
      const messages = useMessages.getState();
      await Promise.all(
        messages.channelIdsWithMessages().map((id) => messages.backfill(id)),
      );
    } catch {
      // Resync failing usually means the server bounced again; the next
      // reconnect will retry. Local state stays consistent (just stale).
    }
  }
}

/** App-wide singleton. Views import this; stores never do (no import cycle). */
export const socket = new DisjornSocket();
