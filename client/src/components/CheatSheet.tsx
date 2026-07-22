/* Admin-only command cheat sheet (header button, gated on user.is_admin).
   A quick-reference popup of the commands plink reaches for when running the
   platform — chat slash-commands plus host-side ops. Each row has a copy
   button. Content is curated from real operational use; edit COMMANDS to taste.
   Esc or backdrop click closes. */

import { useEffect, useState } from "react";

interface Cmd {
  label: string;
  cmd: string;
}
interface Section {
  title: string;
  items: Cmd[];
}

const RES_GABLE = "sudo -u res-gable XDG_RUNTIME_DIR=/run/user/996 systemctl --user";
const RES_CLAUDETTE =
  "sudo -u res-claudette XDG_RUNTIME_DIR=/run/user/997 systemctl --user";
const REPO = "/home/plink/Disjorn/Disjorn";

const COMMANDS: Section[] = [
  {
    title: "Chat commands",
    items: [
      { label: "List the backlog", cmd: "/backlog" },
      { label: "File a backlog item", cmd: "/backlog <your request>" },
      { label: "Un-brick a resident (Claude Code)", cmd: "/unbrick-resident" },
    ],
  },
  {
    title: "Services",
    items: [
      { label: "Restart Disjorn", cmd: "sudo systemctl restart disjorn" },
      { label: "Tail Disjorn logs", cmd: "journalctl -u disjorn -f" },
      { label: "Restart the broker", cmd: "sudo systemctl restart disjorn-broker" },
      { label: "Edit kill-switches (verbs)", cmd: "sudoedit /etc/disjorn-broker/verbs.toml" },
    ],
  },
  {
    title: "Residents",
    items: [
      { label: "Stop Claudette's adapter", cmd: `${RES_CLAUDETTE} stop resident-cc.service` },
      { label: "Start Claudette's adapter", cmd: `${RES_CLAUDETTE} start resident-cc.service` },
      { label: "Restart Gable's summon daemon", cmd: `${RES_GABLE} restart gable-summon.service` },
      {
        label: "Reset Gable's daily summon budget (set count: 0)",
        cmd: "sudoedit /home/res-gable/resident-home/.summon-budget.json",
      },
      {
        label: "Check Gable's model pin",
        cmd: "sudo grep '^model' /srv/disjorn-resident-config/res-gable/summon.toml",
      },
    ],
  },
  {
    title: "Incident / un-brick",
    items: [
      {
        label: "Scan a channel for poison (dry-run)",
        cmd: `python3 ${REPO}/harness/keyboard/scrub_channel.py --channel 4 --contains 'FRAGMENT'`,
      },
      {
        label: "Scrub a channel (apply — backs up DB first)",
        cmd: `python3 ${REPO}/harness/keyboard/scrub_channel.py --channel 4 --contains 'FRAGMENT' --apply`,
      },
      { label: "Refresh Claudette's read-only mirror", cmd: "git -C /srv/disjorn-ro pull --ff-only" },
    ],
  },
  {
    title: "Deploy / repo",
    items: [
      {
        label: "Deploy residency code (troubleshooting)",
        cmd: `sudo cp ${REPO}/harness/residency/*.py /usr/local/lib/disjorn/residency/`,
      },
      { label: "Pull main", cmd: `git -C ${REPO} pull --ff-only origin main` },
      { label: "Push main", cmd: `git -C ${REPO} push origin main` },
      { label: "Run server tests", cmd: `cd ${REPO}/server && .venv/bin/python -m pytest -q` },
      { label: "Back up the DB", cmd: `cp ${REPO}/server/data/disjorn.db{,.bak}` },
    ],
  },
];

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };
  return (
    <button
      className={`cheat-copy${copied ? " copied" : ""}`}
      title="Copy to clipboard"
      aria-label={copied ? "Copied" : "Copy command"}
      onClick={copy}
    >
      {copied ? "✓" : "⧉"}
    </button>
  );
}

export function CheatSheet({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="cheatsheet"
        role="dialog"
        aria-label="Command cheat sheet"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="cheatsheet-head">
          <span className="cheatsheet-title">Command cheat sheet</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="cheatsheet-body">
          {COMMANDS.map((section) => (
            <div className="cheat-section" key={section.title}>
              <div className="cheat-section-title">{section.title}</div>
              {section.items.map((it) => (
                <div className="cheat-row" key={it.cmd}>
                  <div className="cheat-text">
                    <div className="cheat-label">{it.label}</div>
                    <code className="cheat-cmd">{it.cmd}</code>
                  </div>
                  <CopyButton text={it.cmd} />
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
