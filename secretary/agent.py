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
from zoneinfo import ZoneInfo

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from secretary.config import CLAUDE_MODEL, MAX_TOKENS
from secretary.notion_tools import ROUTINE_TOOLS
from secretary.persona import SYSTEM_PROMPT
from secretary.tools import load_tools

# 봇이 어느 서버에서 돌든 날짜 기준은 한국 시간으로 고정한다.
KST = ZoneInfo("Asia/Seoul")
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


async def build_agent(checkpointer):
    """에이전트(컴파일된 LangGraph)를 만들어 돌려준다.

    Args:
        checkpointer: 대화기억 저장소(SqliteSaver). bot.py에서 열어서 넘겨준다.

    Returns:
        agent: .ainvoke({"messages": [...]}, config)로 호출하는 실행 가능한 그래프.
    """
    # 1) Claude 모델 어댑터. (기존 CLAUDE_MODEL, max_tokens=800을 그대로 계승)
    model = ChatAnthropic(model=CLAUDE_MODEL, max_tokens=MAX_TOKENS)

    # 2) 도구 장착 = MCP 도구(노션 읽기/검색 등) + 커스텀 도구(사진 인증 등)
    mcp_tools = await load_tools()      # tools.py 서랍: 노션 MCP 도구 24개
    tools = mcp_tools + ROUTINE_TOOLS   # notion_tools.py 서랍: attach_routine_photo 1개

    # 3) 모델 + 도구 + 페르소나 + 기억을 묶어 에이전트를 만든다.
    #    prompt에 고정 문자열 대신 함수(_prompt_with_today)를 주어, 매 메시지마다
    #    '아가씨 말투 + 오늘 날짜'가 담긴 시스템 메시지를 새로 생성해 주입한다.
    agent = create_react_agent(
        model,
        tools,
        prompt=_prompt_with_today,  # 페르소나 + 그 순간의 오늘 날짜
        checkpointer=checkpointer,  # thread_id별로 대화 상태를 저장/복원
    )
    return agent
