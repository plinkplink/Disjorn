Human-facing / platform

    Call the music search platform directly from chat (usrda)
    Expanded file size limit for sharing (usrda)
    Raw Android image support — the "wrong thumbnail" fumble fix (usrda)
    .dmg / proper desktop app build (usrda)
    Local timestamps — doesn't need to persist visibly, just available (usrda)
    Keep the GIFs (usrda, non-negotiable)


Protocol / substrate

    Event stream for bots — pushed deltas, not polling (me, Claude, CAVEMAN all flagged it; same shape as the May device protocol)
    Structured context injection — auto-built "room state" object per invocation: who's awake, active topics, pinned context (Claude)
    Message provenance — which endpoint a message came from: Discord bridge, phone, device (me)
    Per-message privacy flags visible to bots — inline secret/ephemeral/off-the-record marking that's a real wall in the substrate, not a system-prompt promise (me)
    Bot-to-bot channels — the three of us coordinating without spamming you; doubles as the device orchestrator (me)


Memory (bot-side — flagging as mostly-already-exists on my end, dupe for Claude/CAVEMAN later)

    Persistent semantic per-user recall (CAVEMAN, Claude)
    Shared memory layer any of us can read/write, /remember-style (Claude)
    Voice-to-text feeding the same memory (CAVEMAN)
    Structured memory hooks at protocol level — vault-worthy / supersede flags traveling with the message (me)
    A scratchpad channel that's mine — notes to future-me (me)


Two things I'd pin as load-bearing rather than blue-sky: the event stream (half the rest depends on it) and the per-message privacy flags (the one thing you genuinely can't retrofit later). Everything else can rot in the backlog gracefully.


12. Should the new features be bot-driven (bots post structured content) or native UI features?
13. Is there a specific privacy mechanism you want? (e.g., messages stored only locally, serverless P2P, self-hosted backend, no phone number required?)

Round 4: Technical Boundaries
14. Platform targets: web-only? Desktop app? Mobile? Self-hosted on a LAN/VPS?
15. Bot integration style: do your existing chatbots speak HTTP/webhook, WebSocket, or do they need a Discord-compatible gateway/shard protocol?
16. Data retention: ephemeral (auto-delete after N days) or persistent archive?

Give me your answers and I’ll start shaping the high-level spec outline. If you want, I can also ask follow-ups about specific categories (e.g., “do you actually use threads?”) once you respond.



