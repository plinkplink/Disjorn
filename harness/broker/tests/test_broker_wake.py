"""The wake verb (SPECS/2026-08-25-agentic-residents.md, confirmed seq 1913).

The wall this file exists to hold: **wake origin is enforced, not inferred.**
The caller arrives as an SO_PEERCRED uid, so these tests connect over a real
socket and let the kernel say who is calling — a test that passed the caller in
the request body would be testing the thing the design refuses to do.

Held down here:

  * a resident, build or adapter uid cannot wake anyone, and the refusal is
    audited;
  * a wake caller can call the wake verb and nothing else;
  * the kill switch still governs it, and the verb appears in no seat's
    allowlist or generated surface;
  * a wake lands as one 0644 record in the plink-owned spool — the broker
    launches nothing;
  * an unsafe wake surface refuses to start rather than come up looking armed;
  * a woken seat may not build a spec it would review itself.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker_testlib import (  # noqa: E402
    ALL_VERBS,
    PY,
    SPEC_BODY,
    FakeBuildProc,
    FakeBuildSpawn,
    build_out,
)
from brokerd import Broker, ConfigError, load_config  # noqa: E402

ME = os.getuid()
OTHER_UID = ME + 1          # never connects; only the uid MAP needs it
CAP_SEC = 120
GRACE_SEC = 30

SPEC_WITH_OWNER = textwrap.dedent("""\
    # Spec: test build

    ## Request
    - **Verbatim**: do the thing
    - **Requester**: plink

    ## Lane → Review owner (DETERMINISTIC)
    - **Lane**: gable.
    - **Review owner**: {owner}

    ## Confirm record
    - **Confirmed by**: plink
    - **#custodian seq**: 1913
    - **Confirmed at**: 2026-08-25

    ## Status
    `confirmed`
""")


class WakeHarness:
    """A broker on a scratch socket, with the CURRENT uid mapped to `me`."""

    def __init__(self, broker: Broker, verbs_path: Path, spool: Path,
                 specs_dir: Path) -> None:
        self.broker = broker
        self.verbs_path = verbs_path
        self.spool = spool
        self.specs_dir = specs_dir

    def call(self, verb: str, args: dict | None = None) -> dict:
        deadline = time.time() + 5
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            try:
                s.connect(self.broker.socket_path)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                s.close()
                if time.time() > deadline:
                    raise
                time.sleep(0.02)
        with s:
            req = {"verb": verb, "args": args if args is not None else {}}
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])

    def set_verbs(self, sections: dict[str, dict]) -> None:
        lines = []
        for section, flags in sections.items():
            lines.append(f"[{section}]")
            for verb, on in flags.items():
                lines.append(f'"{verb}" = {str(bool(on)).lower()}')
        self.verbs_path.write_text("\n".join(lines) + "\n")

    def audit_lines(self) -> list[dict]:
        path = Path(self.broker.audit_path)
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def records(self) -> list[dict]:
        return [json.loads(p.read_text())
                for p in sorted(self.spool.glob("*.wake.json"))]

    def write_spec(self, filename: str, *, owner: str | None = "Claudette") -> str:
        if owner is None:
            text = SPEC_BODY.format(status="confirmed", confirmed_by="plink",
                                    seq="1913")
        else:
            text = SPEC_WITH_OWNER.format(owner=owner)
        (self.specs_dir / filename).write_text(text)
        return filename

    def write_wake_record(self, wake_id: str, *, resident: str = "res-gable",
                          woken_by: str = "plink", age_sec: float = 5.0,
                          cap: int = CAP_SEC, grace: int = GRACE_SEC) -> None:
        """A wake in flight (or, with a big `age_sec`, one whose window has
        passed) — written by hand so the woken-build rule can be exercised
        without racing a real session."""
        import datetime as dt

        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_sec)
        (self.spool / f"{wake_id}.wake.json").write_text(json.dumps({
            "schema": 1, "wake_id": wake_id, "resident": resident,
            "woken_by": woken_by, "requested_at": when.isoformat(),
            "session_cap_sec": cap, "grace_sec": grace, "task": "do the thing"}))


def build_broker(tmp_path: Path, *, me: str = "plink", wake: bool = True,
                 callers: list[str] | None = None,
                 seats: list[str] | None = None,
                 spool_dir: Path | None = None,
                 extra_uids: dict[int, str] | None = None) -> WakeHarness:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    build_stub = stub_dir / "build.py"
    build_stub.write_text("#!/usr/bin/env python3\nprint('stub')\n")
    build_stub.chmod(0o755)

    specs_dir = tmp_path / "SPECS"
    specs_dir.mkdir(exist_ok=True)
    spool = spool_dir if spool_dir is not None else tmp_path / "wake-spool"
    spool.mkdir(exist_ok=True)

    uids = {ME: me}
    uids.update(extra_uids or {})
    uid_lines = "\n".join(f'"{uid}" = "{name}"' for uid, name in uids.items())
    wake_block = ""
    if wake:
        wake_block = textwrap.dedent(f"""\
            [wake]
            callers = {json.dumps(callers if callers is not None else ["plink"])}
            residents = {json.dumps(seats if seats is not None else ["res-gable"])}
            spool_dir = "{spool}"
            session_cap_sec = {CAP_SEC}
            grace_sec = {GRACE_SEC}
        """)

    broker_toml = tmp_path / "broker.toml"
    broker_toml.write_text(textwrap.dedent(f"""\
        [broker]
        socket_path = "{tmp_path / 'b.sock'}"
        audit_log = "{tmp_path / 'audit.jsonl'}"
        build_log_dir = "{tmp_path / 'build-logs'}"

        [uids]
        {uid_lines}

        [residents.res-gable]
        log_path = "{tmp_path / 'gable.log'}"

        [residents.res-claudette]
        log_path = "{tmp_path / 'claudette.log'}"

        [start_build]
        command = ["{PY}", "{build_stub}"]
        session_argv = ["--output-format", "json"]
        model = "claude-opus-4-8"
        specs_dir = "{specs_dir}"
        timeout_sec = 30
        daily_build_cap = 5

        [paths]
        metrics_json = "{tmp_path / 'metrics.json'}"

        [disjorn]
        url = "http://127.0.0.1:1"
        api_key_path = "{tmp_path / 'no-key'}"
        custodian_channel_id = 3
    """) + wake_block)
    (tmp_path / "build-logs").mkdir(exist_ok=True)

    def stub_transport(disjorn_cfg: dict, body: str) -> dict:
        return {"seq": 1, "message_id": 1}

    verbs_path = tmp_path / "verbs.toml"
    verbs_path.write_text("[plink]\n\"wake\" = true\n")
    broker = Broker(load_config(str(broker_toml)), str(verbs_path),
                    transport=stub_transport)
    h = WakeHarness(broker, verbs_path, spool, specs_dir)
    t = threading.Thread(target=broker.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(broker.socket_path):
        if time.time() > deadline:
            raise RuntimeError("broker socket never appeared")
        time.sleep(0.01)
    h._thread = t
    return h


@pytest.fixture()
def plink(tmp_path):
    """This uid IS the wake caller — the allowed path."""
    h = build_broker(tmp_path, me="plink", extra_uids={OTHER_UID: "res-gable"})
    yield h
    h.broker.shutdown()
    h._thread.join(timeout=5)


@pytest.fixture()
def seat(tmp_path):
    """This uid is a RESIDENT SEAT, and plink is someone else — the refused
    path, refused by the kernel's word about who connected."""
    h = build_broker(tmp_path, me="res-gable", callers=["plink"],
                     extra_uids={OTHER_UID: "plink"})
    h.set_verbs({"res-gable": {v: True for v in ALL_VERBS},
                 "plink": {"wake": True}})
    yield h
    h.broker.shutdown()
    h._thread.join(timeout=5)


# ---------------------------------------------------------------- the wall


def test_a_seat_may_not_wake_anyone_at_the_socket(seat):
    """The spec's normative sentence, tested where it is enforced. Every verb
    is switched ON for this seat, including a hand-added `wake` line — and the
    refusal still comes, because it does not come from verbs.toml."""
    seat.set_verbs({"res-gable": {**{v: True for v in ALL_VERBS}, "wake": True}})
    resp = seat.call("wake", {"resident": "res-gable", "task": "wake myself"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "verb-disabled"
    assert "may not wake anyone" in resp["error"]["message"]
    assert seat.records() == []


def test_the_refusal_is_audit_logged_against_the_kernels_caller(seat):
    seat.call("wake", {"resident": "res-gable", "task": "x"})
    line = seat.audit_lines()[-1]
    assert line["verb"] == "wake"
    assert line["resident"] == "res-gable"
    assert line["allowed"] is False
    assert "may not wake anyone" in line["result_summary"]


def test_an_unconfigured_wake_surface_refuses_everyone(harness):
    """The shared fixture has no [wake] section: the verb is present and inert,
    which is the same fail-closed shape as an unflipped kill switch."""
    harness.enable_all()
    resp = harness.call("wake", {"resident": "res-gable", "task": "x"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "verb-disabled"


def test_a_wake_caller_may_call_nothing_else(plink):
    plink.set_verbs({"plink": {"wake": True, "read-metrics": True}})
    resp = plink.call("read-metrics")
    assert resp["ok"] is False
    assert "may call only" in resp["error"]["message"]


def test_the_kill_switch_still_governs_the_wake(plink):
    plink.set_verbs({"plink": {"wake": False}})
    resp = plink.call("wake", {"resident": "res-gable", "task": "x"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "verb-disabled"
    assert plink.records() == []
    plink.set_verbs({"plink": {"wake": True}})
    assert plink.call("wake", {"resident": "res-gable", "task": "x"})["ok"] is True


# --------------------------------------------------------------- the record


def test_a_wake_lands_as_one_readable_unwritable_record(plink):
    resp = plink.call("wake", {"resident": "res-gable",
                               "task": "read the digest and open a card"})
    assert resp["ok"] is True
    result = resp["result"]
    assert result["resident"] == "res-gable"
    assert result["session_cap_sec"] == CAP_SEC
    assert result["wake_id"].startswith("wake-")

    (record,) = plink.records()
    assert record["task"] == "read the digest and open a card"
    assert record["woken_by"] == "plink"
    assert record["session_cap_sec"] == CAP_SEC
    assert record["grace_sec"] == GRACE_SEC

    path = plink.spool / f"{result['wake_id']}.wake.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o644, "the seat's runner must read it and must not write it"


def test_the_wake_id_is_a_fact_on_the_audit_line(plink):
    """The join key: this line, the seat's action-log start/end pair, and the
    #custodian post all carry the same id."""
    wake_id = plink.call("wake", {"resident": "res-gable",
                                  "task": "x"})["result"]["wake_id"]
    assert plink.audit_lines()[-1]["wake_id"] == wake_id


def test_the_broker_launches_nothing(plink):
    """A wake is a record, not a session. The broker has no build thread, no
    subprocess and nothing detached to reap."""
    plink.call("wake", {"resident": "res-gable", "task": "x"})
    assert plink.broker._build_threads == []
    assert plink.broker._active_builds == set()


def test_a_wake_names_a_seat_from_config_never_from_the_caller(plink):
    resp = plink.call("wake", {"resident": "res-claudette", "task": "x"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "not a wakeable seat" in resp["error"]["message"]
    assert plink.records() == []


def test_a_wake_must_name_the_work(plink):
    assert plink.call("wake", {"resident": "res-gable",
                               "task": "   "})["error"]["code"] == "bad-args"
    assert plink.call("wake", {"resident": "res-gable"})["error"]["code"] == "bad-args"
    assert plink.call("wake", {"resident": "res-gable", "task": "x",
                               "cap": 99})["error"]["code"] == "bad-args"


def test_the_spool_is_pruned_on_a_retention_horizon_not_on_the_window(plink):
    """A record whose WINDOW has closed is exactly what lets a runner that was
    down come back and post that the wake was missed. Pruning on the window
    would delete that evidence; pruning on a week keeps the spool bounded."""
    plink.write_wake_record("wake-20260101T000000Z-000001", age_sec=99999)
    plink.write_wake_record("wake-20250101T000000Z-000002", age_sec=30 * 86400)
    live = plink.call("wake", {"resident": "res-gable",
                               "task": "x"})["result"]["wake_id"]
    ids = {r["wake_id"] for r in plink.records()}
    assert ids == {live, "wake-20260101T000000Z-000001"}


# ------------------------------------------------------- refusing to start


def test_a_resident_writable_spool_refuses_to_start(tmp_path):
    """Same wall as start_build.specs_dir, for the same reason: a resident that
    can write the spool can write itself a wake."""
    spool = tmp_path / "open-spool"
    spool.mkdir()
    spool.chmod(0o777)
    with pytest.raises(ConfigError) as ei:
        build_broker(tmp_path, spool_dir=spool)
    assert "wake.spool_dir is resident-writable" in str(ei.value)
    assert "nothing self-wakes" in str(ei.value)


def test_a_spool_inside_a_resident_home_refuses_to_start(tmp_path, monkeypatch):
    spool = tmp_path / "home-spool"
    spool.mkdir()
    monkeypatch.setattr(os.path, "realpath",
                        lambda p: "/home/res-gable/spool"
                        if str(p) == str(spool) else os.path.abspath(p))
    with pytest.raises(ConfigError) as ei:
        build_broker(tmp_path, spool_dir=spool)
    assert "wake.spool_dir is resident-writable" in str(ei.value)
    assert "/home/res-gable" in str(ei.value)


def test_a_seat_listed_as_a_wake_caller_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as ei:
        build_broker(tmp_path, callers=["res-gable"])
    assert "nothing self-wakes" in str(ei.value)


def test_a_wake_caller_with_no_uid_refuses_to_start(tmp_path):
    """A caller that could never be authenticated would read as a grant while
    refusing every call."""
    with pytest.raises(ConfigError) as ei:
        build_broker(tmp_path, me="res-gable", callers=["plink"])
    assert "no uid in [uids]" in str(ei.value)


def test_an_unknown_wakeable_seat_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as ei:
        build_broker(tmp_path, seats=["res-nobody"])
    assert "not a resident of this house" in str(ei.value)


def test_a_wake_section_without_a_spool_refuses_to_start(tmp_path):
    """A wake with nowhere to land is a wake the seat never hears about."""
    broker_toml = tmp_path / "broker.toml"
    broker_toml.write_text(textwrap.dedent(f"""\
        [broker]
        socket_path = "{tmp_path / 'b.sock'}"
        audit_log = "{tmp_path / 'audit.jsonl'}"

        [uids]
        "{ME}" = "plink"
        "{OTHER_UID}" = "res-gable"

        [residents.res-gable]
        log_path = "{tmp_path / 'gable.log'}"

        [wake]
        callers = ["plink"]
        residents = ["res-gable"]
    """))
    with pytest.raises(ConfigError) as ei:
        Broker(load_config(str(broker_toml)), str(tmp_path / "verbs.toml"))
    assert "wake.spool_dir is missing" in str(ei.value)


# ------------------------------------------------ the woken build's extra rule


def _allow_builds(h: WakeHarness) -> FakeBuildSpawn:
    h.set_verbs({"res-gable": {"start-build": True}, "plink": {"wake": True}})
    spawn = FakeBuildSpawn(lambda: FakeBuildProc(out=build_out()))
    h.broker._build_spawn = spawn
    return spawn


def test_a_woken_seat_may_not_build_what_it_would_review(plink):
    """The no-self-review rule, one level down. `start-build` is inherited by a
    woken session exactly as the spec says it is — and this is the one thing
    the inheritance adds."""
    _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-aaaaaa")
    spec = plink.write_spec("2026-08-25-thing.md", owner="Gable")

    resp = plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})

    assert resp["ok"] is False
    assert "would then review" in resp["error"]["message"]
    assert "wake-20260825T120000Z-aaaaaa" in resp["error"]["message"]


def test_a_woken_seat_may_build_what_someone_else_reviews(plink):
    spawn = _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-bbbbbb")
    spec = plink.write_spec("2026-08-25-thing.md", owner="Claudette")

    resp = plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})

    assert resp["ok"] is True, resp
    assert spawn.calls
    plink.broker.join_builds()


def test_a_woken_seat_may_not_build_a_spec_with_no_review_owner(plink):
    """A comparison with nothing to compare cannot be satisfied — and a spec
    that names nobody is exactly the spec whose review lands nowhere."""
    _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-cccccc")
    spec = plink.write_spec("2026-08-25-thing.md", owner=None)

    resp = plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})

    assert resp["ok"] is False
    assert "states no review owner" in resp["error"]["message"]


def test_a_human_review_owner_refuses_nothing(plink):
    """`plink` resolves to no seat, and correctly so: a human review owner is
    the case the rule protects, not one it should refuse."""
    _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-dddddd")
    spec = plink.write_spec("2026-08-25-thing.md", owner="plink")

    resp = plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})

    assert resp["ok"] is True, resp
    plink.broker.join_builds()


def test_the_rule_is_off_once_the_wake_window_has_passed(plink):
    """A summoned build of a spec this seat reviews is not this spec's business
    — the extra rule applies while a wake is in flight and not after."""
    _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-eeeeee", age_sec=99999)
    spec = plink.write_spec("2026-08-25-thing.md", owner="Gable")

    resp = plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})

    assert resp["ok"] is True, resp
    plink.broker.join_builds()


def test_a_build_refused_by_the_rule_burns_no_budget_slot(plink):
    _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-ffffff")
    spec = plink.write_spec("2026-08-25-thing.md", owner="Gable")
    plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})
    assert plink.broker._builds.get("res-gable", (None, 0))[1] == 0
    assert plink.broker._active_builds == set()


def test_a_woken_seat_may_not_build_what_the_waking_seat_reviews(plink):
    """The forward rule, inert while only humans wake — written now so it is
    already true on the day a non-human wake lands."""
    _allow_builds(plink)
    plink.write_wake_record("wake-20260825T120000Z-999999",
                            woken_by="res-claudette")
    spec = plink.write_spec("2026-08-25-thing.md", owner="Claudette")

    resp = plink.broker.dispatch(OTHER_UID, "start-build", {"spec": spec})

    assert resp["ok"] is False
    assert "the seat that woke this one" in resp["error"]["message"]


# ------------------------------------------------------- the surface a seat sees


def test_no_seat_surface_grows_a_wake_button():
    """A verb no seat may call must also be a verb no seat can see: the CLI
    table and the bot tool schemas are generated from the SEAT sections only."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import gen_verb_surface as gen

    assert "wake" not in gen.verb_names()
    assert gen.check() == []
    cli = (Path(gen.__file__).resolve().parent.parent.parent
           / "harness" / "cc" / "broker-cli" / "broker").read_text()
    assert '"wake"' not in cli


def test_the_shipped_verbs_template_keeps_the_wake_out_of_both_seats():
    import tomllib

    path = Path(__file__).resolve().parent.parent / "verbs.toml"
    with open(path, "rb") as fh:
        tmpl = tomllib.load(fh)
    assert "wake" not in tmpl["res-gable"] and "wake" not in tmpl["res-claudette"]
    assert tmpl["plink"] == {"wake": False}
