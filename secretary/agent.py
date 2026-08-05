"""Claude + 도구 + 대화기억을 하나로 묶는 에이전트 조립 모듈.

이 파일이 '뇌'다. 기존 봇의 anthropic_client.messages.create(...) 한 방 호출이,
여기서 create_react_agent가 만드는 '그래프'로 대체된다.

create_react_agent가 내부적으로 대신 해주는 것:
    1) Claude 호출
    2) Claude가 "노션 도구 써야겠다"(tool call)고 하면 그 도구를 실행
    3) 도구 결과를 다시 Claude에 먹여 최종 답을 받음  (이 반복 루프)
    4) checkpointer가 있으면 대화 상태를 자동 저장/복원
우리가 이 루프를 손으로 짜지 않아도 되는 이유다.
"""

from __future__ import annotations

from datetime import datetime

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from secretary.config import (
    CLAUDE_MODEL,
    KST,
    MAX_TOKENS,
    VLLM_API_KEY,
    VLLM_BASE_URL,
    VLLM_MODEL,
)
from secretary.blaybus_tools import BLAYBUS_TOOLS
from secretary.homework_tools import HOMEWORK_TOOLS
from secretary.notion_tools import ROUTINE_TOOLS, item_guide
from secretary.onboarding import DEFAULT_MODEL
from secretary.persona import SYSTEM_PROMPT

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 모델에 실어 보낼 최근 '대화 턴' 수. (메시지 개수가 아니다 — #4-2에서 바뀐 단위)
# 1턴 = 사람이 말한 시점부터 다음 사람 발화 직전까지. 그 안의 AI 답변·도구 호출·
# 도구 결과는 몇 개가 되든 통째로 한 턴에 속한다.
#
# 공주비서는 '한 방 명령 실행기'라 긴 대화기억이 필요 없다. 다만 되묻기 한 바퀴
# (사진 → "어느 항목?" → "운동")는 이어져야 하므로 2턴이면 충분하다.
# (무한 누적 → 토큰 폭증을 막는 핵심 장치)
RECENT_WINDOW = 2


# 사용자가 기록처를 콕 집어 말했는지 알아볼 낱말.
_NOTION_WORDS = ("노션", "notion")
_BLAYBUS_WORDS = ("블레이버스", "blaybus")


def _pinned_place(text: str) -> str | None:
    """마지막 발화에서 기록처를 집어냈으면 'notion' | 'blaybus'. 아니면 None.

    둘 다 말했으면 None — 어느 쪽도 막지 않는다("노션이랑 블레이버스 둘 다 보여줘").
    """
    low = (text or "").lower()
    notion = any(w in low for w in _NOTION_WORDS)
    blaybus = any(w in low for w in _BLAYBUS_WORDS)
    if notion == blaybus:  # 둘 다거나 둘 다 아니면 고정하지 않는다
        return None
    return "notion" if notion else "blaybus"


_PIN_LINE = {
    "notion": (
        "\n\n[⚠️ 지금 아가씨는 **노션**이라고 하셨다. 앞 대화에서 블레이버스 작업을 하던 "
        "중이라도 그건 접어두고 노션 도구(routine_*·homework_*)만 써라. "
        "blaybus_* 는 부르지 마라 — 아가씨가 기록처를 바로잡으신 것이다.]"
    ),
    "blaybus": (
        "\n\n[⚠️ 지금 아가씨는 **블레이버스**라고 하셨다. 앞 대화에서 노션 작업을 하던 "
        "중이라도 그건 접어두고 블레이버스 도구(blaybus_*)만 써라. "
        "routine_*·homework_* 는 부르지 마라 — 아가씨가 기록처를 바로잡으신 것이다.]"
    ),
}


# 데일리루틴 항목과 아가씨가 부르실 만한 다른 이름. notion_tools가 유일한 출처다.
# 모델이 도구를 부르기 전에 판단하므로 프롬프트에 실어야 한다 — 자세한 이유는 item_guide().
_ITEM_LINE = (
    "\n\n[노션 데일리루틴 항목은 여섯 개뿐이고, 괄호 안 이름으로 말씀하셔도 같은 항목이다: "
    f"{item_guide()}] 괄호 안 이름을 들으시면 되묻지 말고 그대로 도구에 넣어라."
)


def _prompt_with_today(state):
    """매 메시지마다 실행되어 '오늘 날짜'를 시스템 메시지에 새로 주입한다.

    Claude는 스스로 오늘 날짜를 알 수 없다(학습 시점에 멈춰있음). 그래서 매 호출마다
    지금 이 순간의 KST 날짜를 시계에서 읽어 시스템 메시지로 먹인다. 고정 문자열에 한 번
    박아두면 자정을 넘겨도 갱신되지 않으므로, 반드시 '함수' 형태로 매번 계산한다.

    이 시스템 메시지는 checkpointer에 저장되지 않으므로, 어제 대화에 남은 날짜에
    Claude가 이끌리는 문제(잔류 앵커링)도 덮어써서 바로잡는다.
    """
    now = datetime.now(KST)
    today_line = (
        f"\n\n[오늘은 {now:%Y-%m-%d} ({_WEEKDAY_KR[now.weekday()]})이에요.] "
        "사용자가 날짜를 따로 말하지 않으면, 노션 도구를 호출할 때 이 오늘 날짜를 기준으로 삼아. "
        "예전 대화에 나온 날짜에 이끌리지 말 것."
    )
    # 체크포인터엔 전체 대화가 쌓이지만, 모델에는 최근 RECENT_WINDOW '턴'만 실어 보낸다.
    #
    # 자르는 단위가 왜 '메시지'가 아니라 '턴'인가 (#4-2에서 고친 버그):
    #   예전엔 trim_messages(max_tokens=3, start_on="human")으로 낱개 메시지 3개를
    #   셌다. 그런데 도구를 n번 쓰면 그 턴의 메시지는 2n+1개로 불어난다. 도구를 두 번만
    #   써도 마지막 3개가 [Tool, AI, Tool]이 되어 사람 메시지가 창 밖으로 밀려나고,
    #   start_on="human"이 시작점을 못 찾아 '전부 버리고 빈 목록'을 돌려줬다.
    #   → 시스템 메시지만 남은 채 API 호출 → BadRequestError로 봇이 답을 못 했다.
    #
    # 그래서 턴 경계(=사람이 말한 지점)에서만 자른다. 진행 중인 턴은 도구를 몇 번
    # 쓰든 통째로 남으므로 빈 목록이 구조적으로 나올 수 없고, 연속 구간을 그대로
    # 뜨기 때문에 tool_use ↔ tool_result 짝도 저절로 보존된다.
    msgs = state["messages"]
    human_at = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
    # 뒤에서 RECENT_WINDOW번째 사람 메시지부터 끝까지. 대화가 그보다 짧으면 처음부터.
    start = human_at[-RECENT_WINDOW] if len(human_at) >= RECENT_WINDOW else 0
    recent = msgs[start:]

    # ⚠️ 마지막 발화가 앞 대화에 눌리는 문제(잔류 앵커링). 날짜와 같은 병이라 같은
    #    약을 쓴다 — 시스템 메시지로 격상시킨다.
    #    2026-08-05: "노션에 태스크 만들어줘"로 블레이버스 되묻기가 시작되자, 그 뒤
    #    "노션에 추가해줘"(11자)가 직전 도구 결과의 블레이버스 낱말 덩어리에 눌려
    #    세 번을 말해도 계속 blaybus_*로 갔다.
    #    ⚠️ 마지막 '메시지'가 아니라 마지막 '사람 메시지'를 본다. 이 함수는 도구를
    #       부를 때마다 다시 실행되는데, 그때 msgs[-1]은 도구 결과라 고정이 풀린다.
    last_said = msgs[human_at[-1]].content if human_at else ""
    pin = _pinned_place(last_said) if isinstance(last_said, str) else None

    prompt = SYSTEM_PROMPT + _ITEM_LINE + today_line + _PIN_LINE.get(pin, "")
    return [SystemMessage(content=prompt), *recent]


def _build_model(user):
    """그 사람의 뇌를 만든다. 등록 안 한 주인이면 .env 값이 들어온다.

    vLLM·OpenAI 둘 다 OpenAI 호환이라 base_url만 갈아끼우면 붙는다 — 그래서
    아래 그래프 조립부는 한 줄도 바뀌지 않는다.
    """
    if user.llm_provider == "anthropic":
        return ChatAnthropic(
            model=user.llm_model or CLAUDE_MODEL,
            api_key=user.llm_key,
            max_tokens=MAX_TOKENS,
        )

    # OpenAI/vLLM 쪽만 필요한 의존성이라 여기서 늦게 import한다.
    from langchain_openai import ChatOpenAI

    if user.llm_provider == "openai":
        return ChatOpenAI(
            model=user.llm_model or DEFAULT_MODEL["openai"],
            api_key=user.llm_key,
            max_tokens=MAX_TOKENS,
        )

    return ChatOpenAI(
        model=user.llm_model or VLLM_MODEL,
        base_url=user.vllm_base_url or VLLM_BASE_URL,
        api_key=user.llm_key or VLLM_API_KEY,
        max_tokens=MAX_TOKENS,
        temperature=0.7,
        # Qwen3.5는 thinking이 기본 ON인데, 켜두면 짧은 질문에도 2048토큰·72초를
        # 쓰고 답을 못 낸다(12주차 측정). 도구 호출 루프에선 치명적이라 끈다.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


def _tag_of(name: str) -> str:
    return "[블레이버스] " if name.startswith("blaybus_") else "[노션] "


def _tagged(tool):
    """도구 답을 '[노션] '/'[블레이버스] '로 시작하게 감싼다.

    도구 반환 문구는 아가씨께 하는 말이자 **모델에게 주는 다음 지시**다. 그런데
    노션 도구의 답에는 '노션'이라는 낱말이 안 들어가고 블레이버스 도구의 답에는
    '태스크·워크·아젠다'가 잔뜩 들어간다. 그 비대칭 때문에 되묻기 한 바퀴만 돌면
    모델이 기록처를 블레이버스로 착각했다(2026-08-05).

    도구 문구를 하나하나 고치는 대신 여기서 한 번에 붙인다 — 새 도구가 생겨도
    이름만 blaybus_* 규칙을 따르면 저절로 붙는다.
    """
    inner = tool.coroutine
    tag = _tag_of(tool.name)

    async def wrapped(*args, **kwargs):
        out = await inner(*args, **kwargs)
        return tag + out if isinstance(out, str) else out

    tool.coroutine = wrapped
    return tool


def build_tools() -> list:
    """도구 목록. 누구에게나 똑같다.

    ⚠️ 예전엔 블레이버스 자격증명이 있을 때만 붙였는데, 사람마다 있고 없고가
       다르면 그래프 모양까지 사람마다 달라진다. 그래서 **항상 다 붙이고**,
       계정이 없으면 도구가 "등록해 주세요"라고 답하게 한다.
       덕분에 그래프는 모델만 다르면 되고, 캐시가 훨씬 잘 듣는다.
    """
    return _TOOLS


# ⚠️ 감싸기는 import 시점에 딱 한 번. build_tools()가 불릴 때마다 감싸면 사람이
#    늘수록 태그가 '[노션] [노션] …'로 겹친다(도구 객체가 모듈 전역이라 공유된다).
_TOOLS = [_tagged(t) for t in ROUTINE_TOOLS + HOMEWORK_TOOLS + BLAYBUS_TOOLS]


# 모델이 같으면 그래프도 같으니 재사용한다. 열쇠는 '뇌의 생김새'다.
# MCP를 걷어낸 덕분에 그래프 하나에 node 프로세스가 딸려오지 않아 가볍다.
# ponytail: 상한 없음. 등록자가 수백 명이 되면 LRU로 바꾼다.
_agents: dict[tuple, object] = {}


async def build_agent(checkpointer, user=None):
    """그 사람의 에이전트(컴파일된 LangGraph)를 돌려준다. 같은 뇌면 재사용.

    Args:
        checkpointer: 대화기억 저장소(SqliteSaver). bot.py에서 열어서 넘겨준다.
        user: 이 요청의 주인. 없으면 .env 설정(1인 모드).

    Returns:
        agent: .ainvoke({"messages": [...]}, config)로 호출하는 실행 가능한 그래프.
    """
    from secretary import context

    user = user or context.env_user()
    # ⚠️ llm_key도 열쇠에 넣어야 한다. 빼면 provider·model이 같은 두 사람이 그래프를
    #    공유하게 되고, 먼저 만든 사람의 키로 남의 요청이 나간다.
    key = (user.llm_provider, user.llm_model, user.vllm_base_url, user.llm_key)
    cached = _agents.get(key)
    if cached is not None:
        return cached

    agent = create_react_agent(
        _build_model(user),
        build_tools(),
        prompt=_prompt_with_today,  # 페르소나 + 그 순간의 오늘 날짜
        checkpointer=checkpointer,  # thread_id별로 대화 상태를 저장/복원
    )
    _agents[key] = agent
    return agent


def _selftest() -> None:
    import asyncio

    # 기록처를 하나만 집어 말했을 때만 고정한다
    assert _pinned_place("노션에 추가해줘") == "notion"
    assert _pinned_place("블레이버스 태스크 만들어줘") == "blaybus"
    assert _pinned_place("오늘 뭐 했지") is None
    assert _pinned_place("노션이랑 블레이버스 둘 다 보여줘") is None
    assert _pinned_place("") is None

    # ⚠️ 도구 결과가 뒤에 붙어도 고정이 풀리면 안 된다(이 버그를 잡으려고 만든 것).
    #    _prompt_with_today는 도구를 부를 때마다 다시 실행되므로 마지막 '메시지'가
    #    아니라 마지막 '사람 메시지'를 봐야 한다.
    from langchain_core.messages import AIMessage, ToolMessage

    msgs = [
        HumanMessage(content="노션에 추가해줘"),
        AIMessage(content="", tool_calls=[{"name": "blaybus_list", "args": {}, "id": "t1"}]),
        ToolMessage(content="[블레이버스] 아젠다 '13주차' > 워크 '수요일'", tool_call_id="t1"),
    ]
    prompt = _prompt_with_today({"messages": msgs})[0].content
    assert "노션" in prompt and "blaybus_* 는 부르지 마라" in prompt
    assert _PIN_LINE["blaybus"] not in prompt

    # 항목 별칭이 실제로 프롬프트에 실려야 한다. 모델은 도구를 부르기 전에 판단한다.
    assert "미라클모닝" in prompt and "헬스" in prompt and "야자" in prompt
    # ⚠️ 목록의 출처는 notion_tools 하나. persona가 사본을 들면 노션에서 이름을 바꿀 때
    #    한쪽만 고쳐져 어긋난다 — 오늘 사고가 정확히 그 어긋남이었다.
    assert "코테/도착 8시/운동" not in SYSTEM_PROMPT

    # 도구 답에는 어느 기록처인지 표가 붙는다. 이름 규칙만으로 갈린다.
    assert _tag_of("routine_check") == "[노션] "
    assert _tag_of("homework_write") == "[노션] "
    assert _tag_of("blaybus_stop") == "[블레이버스] "
    names = {t.name for t in build_tools()}
    assert len(names) == 17, names

    # ⚠️ 감싸기가 두 번 걸리면 '[노션] [노션] …'이 된다.
    #    자격증명이 빈 사람으로 불러 안내문만 받는다 — 셀프테스트가 망을 타면 안 된다.
    from secretary import context, users

    token = context.set_current(users.User(discord_id="selftest"))
    try:
        status = next(t for t in build_tools() if t.name == "blaybus_status")
        out = asyncio.run(status.coroutine())
    finally:
        context.reset(token)
    assert out.startswith("[블레이버스] "), out
    assert not out.startswith("[블레이버스] [블레이버스] "), out

    print("selftest OK")


if __name__ == "__main__":
    _selftest()
