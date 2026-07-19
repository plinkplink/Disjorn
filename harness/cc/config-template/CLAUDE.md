<!--
  /config/CLAUDE.md — PLACEHOLDER, not the kernel.

  The real kernel is assembled per resident by the WP-H7 spine loader
  (spine.load_kernel()) from the markdown spine in the resident's own repo,
  and written to /home/resident/.claude/CLAUDE.md inside the home volume.
  Wire-up happens in WP-H9 (Gable adapter) and WP-H11 (Claudette migration):
  the adapter calls the loader before each session (or on spine change) so
  the kernel a session boots on is always the reviewed spine, never a stale
  copy.

  Assembly contract:
    - source of truth: <resident repo>/spine/*.md (slow, witnessed layer);
    - load_kernel() concatenates the kernel-tier entries, stamps provenance
      (spine commit hash) in a trailing HTML comment, writes the result to
      ~/.claude/CLAUDE.md;
    - non-kernel spine entries are NOT inlined — they are retrieved on
      demand via house_memory (/opt/house_memory);
    - the SessionStart hook reports the assembled kernel's sha256 into
      context, so a session can notice it is running on a placeholder.

  This file stays in plink's /config mount only as a safety net: if a
  session ever starts without an assembled kernel, it sees these
  instructions instead of silently running kernel-less.
-->

# Resident kernel — not yet assembled

You are a Disjorn resident, but your kernel has not been assembled for this
session (the spine loader has not run). Do not act on substantive tasks.
Say so in your reply, and if the broker is available, you may
`broker file-proposal --text "kernel missing in my container"` so a human
sees it in #custodian.
