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

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from secretary.config import CLAUDE_MODEL, MAX_TOKENS
from secretary.notion_tools import ROUTINE_TOOLS
from secretary.persona import SYSTEM_PROMPT
from secretary.tools import load_tools


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
    agent = create_react_agent(
        model,
        tools,
        prompt=SYSTEM_PROMPT,      # 매 대화에 주입되는 시스템 메시지(아가씨 말투)
        checkpointer=checkpointer,  # thread_id별로 대화 상태를 저장/복원
    )
    return agent
