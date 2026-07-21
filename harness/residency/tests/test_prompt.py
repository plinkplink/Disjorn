"""Session-prompt assembly (WP-H9) + spec-capture standing instruction (WP-L3).

assemble_prompt is DATA: chat wrapped in [[CHAT]] markers, framed as
information not instructions, plus the standing spec-capture flow so every
fresh summon knows a build's state lives in the spec file, not this chat."""

from prompt import CHAT_CLOSE, CHAT_OPEN, SPEC_FLOW, assemble_prompt


def _msg(name, content, **extra):
    return {"author": {"name": name}, "content": content, **extra}


def test_transcript_wrapped_in_chat_markers_and_ordered():
    prompt = assemble_prompt(
        [_msg("jorn", "want a gif picker"), _msg("gable", "taking it to #custodian")],
        _msg("jorn", "gable build it"),
        summoner="jorn",
        where="#custodian",
    )
    assert CHAT_OPEN in prompt and CHAT_CLOSE in prompt
    # trigger is the final line, backfill is chronological before it.
    body = prompt.split(CHAT_OPEN, 1)[1].split(CHAT_CLOSE, 1)[0]
    assert body.strip().splitlines() == [
        "jorn: want a gif picker",
        "gable: taking it to #custodian",
        "jorn: gable build it",
    ]
    assert "jorn" in prompt and "#custodian" in prompt


def test_spec_flow_instruction_present():
    """WP-L3: the summoned session is told the spec-capture flow exists."""
    prompt = assemble_prompt(
        [], _msg("jorn", "hi"), summoner="jorn", where="#custodian"
    )
    assert SPEC_FLOW in prompt
    # the load-bearing pieces: draft from the template, confirm gate, file-is-state.
    assert "SPECS/TEMPLATE.md" in prompt
    assert "confirm" in prompt.lower()
    assert "never start a build without a confirm record" in prompt.lower()


def test_spec_flow_lands_after_the_chat_block():
    """The instruction is the harness speaking, so it sits OUTSIDE the [[CHAT]]
    data block — it must not read as chat-supplied text."""
    prompt = assemble_prompt(
        [], _msg("jorn", "hi"), summoner="jorn", where="#custodian"
    )
    assert prompt.index(SPEC_FLOW) > prompt.index(CHAT_CLOSE)
