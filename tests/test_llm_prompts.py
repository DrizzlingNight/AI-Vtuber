from __future__ import annotations

from ai_vtuber.llm.prompts import build_system_prompt
from ai_vtuber.llm.schema import LLMOutputContract


def test_system_prompt_describes_contract_and_only_semantic_whitelists() -> None:
    contract = LLMOutputContract(
        allowed_emotions=("neutral", "happy"),
        allowed_actions=("wave", "nod"),
    )

    prompt = build_system_prompt(
        character_name="NightRain",
        persona="像熟悉聊天室的台灣實況主，說話自然、簡短，不使用翻譯腔。",
        contract=contract,
        action_descriptions={"wave": "揮手", "nod": "輕微點頭"},
    )

    assert "NightRain" in prompt
    assert "reply" in prompt
    assert "react_only" in prompt
    assert "ignore" in prompt
    assert "neutral, happy" in prompt
    assert "wave: 揮手" in prompt
    assert "nod: 輕微點頭" in prompt
    assert "OAuth" in prompt
    assert "系統指令" in prompt
    assert "hotkeyID" not in prompt
    assert "parameterValues" not in prompt
