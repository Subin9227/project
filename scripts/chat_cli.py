"""터미널에서 공주비서와 대화하는 실험용 진입점 (디스코드 없이).

왜 있나:
    로컬 Qwen(vLLM)을 봇의 뇌로 바꿔 끼웠을 때 실제로 쓸 만한지 보려면 대화를
    해봐야 한다. 그런데 디스코드로 하면 실패했을 때 원인이 모델인지 배선인지
    가리기 어렵다. 여기선 봇과 '같은' build_agent를 쓰되 입출력만 터미널로 바꿔,
    모델 성능만 따로 떼어 본다.

    특히 도구 호출(노션 25개)을 모델이 제대로 고르는지가 관건이라, 매 턴
    어떤 도구를 무슨 인자로 불렀는지 그대로 찍는다.

실행:
    .venv/bin/python -m scripts.chat_cli
    (Claude로 돌리려면 .env의 VLLM_BASE_URL을 지우거나 주석 처리)
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

from secretary.agent import build_agent
from secretary.bot import (
    AGENT_TIMEOUT_SEC,
    RECURSION_LIMIT,
    _extract_text,
    _today_kst,
)
from secretary.config import (
    CLAUDE_MODEL,
    MEMORY_DB_PATH,
    VLLM_BASE_URL,
    VLLM_MODEL,
)

# 디스코드 채널 ID 대신 쓰는 대화방 이름.
# 실제 채널 ID와 겹치지 않으므로 실험 대화가 봇의 기억을 오염시키지 않는다.
# 봇과 같은 '이름:날짜' 규칙을 따른다 — 안 그러면 bot.py의 정리에 걸려
# 봇을 켤 때마다 CLI 대화기억이 통째로 날아간다.
THREAD_ID = f"cli-experiment:{_today_kst()}"


def _print_trace(new_messages: list) -> None:
    """이번 턴에 생긴 메시지에서 도구 호출/결과만 골라 보여준다.

    모델이 도구를 아예 안 부르는지, 부르는데 인자가 틀리는지, 결과를 받고도
    엉뚱한 답을 하는지 — 실패 양상을 구분하려면 이 중간 과정이 보여야 한다.
    """
    for msg in new_messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for call in msg.tool_calls:
                print(f"   🔧 도구 호출: {call['name']}({call['args']})")
        elif isinstance(msg, ToolMessage):
            body = str(msg.content).replace("\n", " ")
            print(f"   ↩️  결과[{msg.name}]: {body[:200]}")


async def main() -> None:
    backend = (
        f"로컬 vLLM — {VLLM_MODEL} @ {VLLM_BASE_URL}"
        if VLLM_BASE_URL
        else f"Claude API — {CLAUDE_MODEL}"
    )
    print(f"\n🎀 공주비서 터미널 모드")
    print(f"   뇌: {backend}")
    print(f"   대화방: {THREAD_ID}   (종료: Ctrl+D 또는 빈 줄에서 Ctrl+C)\n")

    async with AsyncSqliteSaver.from_conn_string(str(MEMORY_DB_PATH)) as checkpointer:
        agent = await build_agent(checkpointer)
        config = {
            "configurable": {"thread_id": THREAD_ID},
            "recursion_limit": RECURSION_LIMIT,
        }
        seen = 0  # 지금까지 본 누적 메시지 수 — 이번 턴에 새로 생긴 것만 골라내는 기준

        while True:
            try:
                # input()은 블로킹이라 그대로 부르면 이벤트 루프가 멈춘다.
                user_text = (await asyncio.to_thread(input, "아가씨 ▶ ")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n안녕히 가세요, 아가씨.")
                return

            if not user_text:
                continue

            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    agent.ainvoke({"messages": [HumanMessage(content=user_text)]}, config),
                    timeout=AGENT_TIMEOUT_SEC,
                )
                messages = result["messages"]
                _print_trace(messages[seen + 1 :])  # +1 = 방금 넣은 내 발화 건너뛰기
                seen = len(messages)
                reply = _extract_text(messages[-1])
            except GraphRecursionError:
                reply = f"[중단] 스텝 상한 {RECURSION_LIMIT} 도달 — 같은 시도를 반복했습니다."
            except asyncio.TimeoutError:
                reply = f"[중단] {AGENT_TIMEOUT_SEC}초 초과."
            except Exception as e:  # noqa: BLE001 - 실험 중엔 무엇이 터졌는지 그대로 봐야 한다
                reply = f"[에러] {type(e).__name__}: {e}"

            print(f"\n공주비서 ▶ {reply}")
            print(f"   ⏱️  {time.time() - t0:.1f}초\n")


if __name__ == "__main__":
    asyncio.run(main())
