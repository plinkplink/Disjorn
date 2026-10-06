/* The steps a bot reported behind one of its messages: what it read,
   searched, remembered or changed, never what came back. The adapter wrote
   it; the server only checked its shape. */

import type { Message, MessageTrace } from "../types";

/** The trace's true step count, or 0 when the message carries none. */
export function traceTotal(message: Message): number {
  const t = message.trace;
  return t !== undefined && t !== null && t.total > 0 ? t.total : 0;
}

function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function TracePanel({
  id,
  trace,
  model,
}: {
  id: string;
  trace: MessageTrace;
  /** The attribution label, when the message names a model. */
  model: string | null;
}) {
  const more = trace.total - trace.steps.length;
  const listedMs = trace.steps.reduce((sum, s) => sum + s.ms, 0);
  return (
    <div className="msg-trace" id={id}>
      <ol className="msg-trace-steps">
        {trace.steps.map((step, i) => (
          <li key={i} className={`msg-trace-step trace-${step.outcome}`}>
            <span className={`trace-kind trace-kind-${step.kind}`}>{step.kind}</span>
            <code className="trace-label">{step.label}</code>
            <span className="trace-outcome">
              {step.outcome}
              {typeof step.reason === "string" && step.reason !== "" && (
                <span className="trace-reason"> · {step.reason}</span>
              )}
            </span>
            <span className="trace-ms">{formatMs(step.ms)}</span>
          </li>
        ))}
      </ol>
      {more > 0 && <div className="msg-trace-more">…{more} more</div>}
      <div className="msg-trace-foot">
        <span>
          {trace.steps.length > 0 && `listed steps ${formatMs(listedMs)}`}
          {trace.steps.length > 0 && model !== null && " · "}
          {model}
        </span>
        <span className="msg-trace-note">
          This is the bot's own account, written by its adapter; the server
          does not verify it.
        </span>
      </div>
    </div>
  );
}
