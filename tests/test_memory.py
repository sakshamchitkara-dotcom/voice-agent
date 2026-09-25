from app import memory
from tests.conftest import load_fixture

CALLER = "+14155550100"


def test_user_lines_prefers_messages_then_flat_transcript():
    msg = load_fixture("end_of_call_report.json")["message"]
    assert memory.user_lines(msg) == ["What's the weather in San Francisco?"]
    flat = {"artifact": {"transcript": "AI: Hi there. User: My name is dana lee. AI: Nice! User: Bye"}}
    assert memory.user_lines(flat) == ["My name is dana lee.", "Bye"]
    assert memory.user_lines({}) == []


def test_rule_facts():
    lines = [
        "Hi, my name is dana lee and I live in Oakland, near the lake.",
        "My dog's name is biscuit. My favourite coffee is a flat white!",
        "Remember my sister is visiting me next week.",
        "I'm allergic to peanuts; remember that I have a dentist appointment on Friday.",
        "What's the weather like?",
    ]
    assert memory.rule_facts(lines) == [
        "Their name is Dana Lee.",
        "They live in Oakland, near the lake.",
        "Their dog is Biscuit.",
        "Their favourite coffee is a flat white.",
        "They asked you to remember: their sister is visiting them next week.",
        "They are allergic to peanuts.",
        "They asked you to remember: they have a dentist appointment on Friday.",
    ]


def test_save_dedupes_filters_sensitive_and_caps(monkeypatch):
    assert memory.save(CALLER, ["They live in Oakland.", "they live in oakland.",
                                "They asked you to remember: my card is 4111 1111 1111 1111.",
                                "They asked you to remember: my password is hunter2."], "c1", "rules") == 1
    assert memory.facts_for(CALLER) == ["They live in Oakland."]
    assert memory.facts_for("+19995550199") == [] and memory.facts_for(None) == []
    monkeypatch.setattr(memory, "MAX_FACTS_PER_CALLER", 3)
    memory.save(CALLER, [f"Fact number {c}." for c in "abcd"], "c2", "rules")
    assert memory.facts_for(CALLER) == ["Fact number b.", "Fact number c.", "Fact number d."]
    assert memory.forget(CALLER) == 3 and memory.facts_for(CALLER) == []


async def test_remember_call_uses_claude_json(monkeypatch):
    seen = {}

    async def fake(prompt, system, **kw):
        seen["prompt"] = prompt
        return 'Sure:\n["Their daughter Mia starts school in October.", 42]'
    monkeypatch.setattr(memory.llm, "complete", fake)
    memory.save(CALLER, ["They live in Oakland."], "c0", "rules")
    msg = {"artifact": {"messages": [{"role": "user", "message": "Mia starts school in October"}]}}
    assert await memory.remember_call(CALLER, "c1", msg) == 1
    assert "- They live in Oakland." in seen["prompt"] and "- Mia starts school" in seen["prompt"]
    assert memory.facts_for(CALLER)[-1] == "Their daughter Mia starts school in October."


async def test_remember_call_falls_back_to_rules(monkeypatch):
    for reply in (None, "I can't produce JSON today"):
        async def fake(prompt, system, **kw):
            return reply
        monkeypatch.setattr(memory.llm, "complete", fake)
        msg = {"artifact": {"transcript": "AI: Hi. User: Please call me sam, I prefer texts."}}
        await memory.remember_call(CALLER, "c1", msg)
    assert memory.facts_for(CALLER) == ["They like to be called Sam.", "They prefer texts."]
    assert await memory.remember_call(CALLER, "c2", {}) == 0
