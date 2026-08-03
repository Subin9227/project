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
from secretary.homework_tools import HOMEWORK_TOOLS
from secretary.notion_tools import ROUTINE_TOOLS
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

    return [SystemMessage(content=SYSTEM_PROMPT + today_line), *recent]


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


def build_tools() -> list:
    """도구 목록. 누구에게나 똑같다.

    ⚠️ 예전엔 블레이버스 자격증명이 있을 때만 붙였는데, 사람마다 있고 없고가
       다르면 그래프 모양까지 사람마다 달라진다. 그래서 **항상 다 붙이고**,
       계정이 없으면 도구가 "등록해 주세요"라고 답하게 한다.
       덕분에 그래프는 모델만 다르면 되고, 캐시가 훨씬 잘 듣는다.
    """
    from secretary.blaybus_tools import BLAYBUS_TOOLS

    return ROUTINE_TOOLS + HOMEWORK_TOOLS + BLAYBUS_TOOLS


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
