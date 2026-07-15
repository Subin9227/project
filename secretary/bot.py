"""디스코드 게이트웨이 — 메시지 수신 → 에이전트 호출 → 답장.

기존 discord_claude_chat_app.py의 '배선' 부분(인텐트, on_ready, on_message,
답장)이 여기로 이사왔다. 다만 메시지를 Claude에 직접 던지는 대신,
agent.py가 만든 에이전트에게 넘긴다.

대화기억의 핵심:
    thread_id = 디스코드 채널 ID.
    같은 채널에서 온 메시지는 같은 thread_id를 쓰므로, 에이전트가
    이전 대화를 이어서 기억한다. (채널이 다르면 기억도 분리된다.)
"""

from __future__ import annotations

import discord
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from secretary.agent import build_agent
from secretary.config import DISCORD_BOT_TOKEN, MEMORY_DB_PATH

# 디스코드 한 메시지의 최대 길이. 초과분은 잘라서 보낸다.
DISCORD_MAX_LEN = 2000


def _extract_text(message) -> str:
    """에이전트 응답(AIMessage)에서 사람에게 보여줄 텍스트만 뽑아낸다.

    Claude 응답의 content는 문자열일 수도, 블록 리스트일 수도 있어서 둘 다 처리한다.
    """
    content = message.content
    if isinstance(content, str):
        text = content
    else:
        # content가 [{"type": "text", "text": "..."}, ...] 형태인 경우
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts)
    text = text.strip() or "야레야레... 아가씨, 지금은 드릴 말씀이 마땅치 않네요."
    return text[:DISCORD_MAX_LEN]


async def main() -> None:
    """봇을 기동한다. run.py가 이 함수를 asyncio.run으로 실행한다."""
    intents = discord.Intents.default()
    intents.message_content = True  # 메시지 본문 읽기 권한
    client = discord.Client(intents=intents)

    # 대화기억 저장소를 열고, 그 안에서 봇 생애 전체를 돈다.
    # (async with 블록이 유지되는 동안 SQLite 연결이 살아있다.)
    async with AsyncSqliteSaver.from_conn_string(str(MEMORY_DB_PATH)) as checkpointer:
        agent = await build_agent(checkpointer)

        @client.event
        async def on_ready():
            print(f"공주비서 로그인 완료: {client.user}")

        @client.event
        async def on_message(message: discord.Message):
            # 봇이 보낸 메시지는 무시 (무한루프 방지)
            if message.author.bot:
                return
            user_text = message.content.strip()

            # 첨부된 이미지 URL을 뽑아 본문에 덧붙인다.
            # → 에이전트가 이 URL을 보고 attach_routine_photo 도구에 넘긴다.
            #   (도구가 즉시 다운로드하므로 디스코드 URL 만료는 문제 안 됨)
            image_urls = [
                a.url
                for a in message.attachments
                if (a.content_type or "").startswith("image/")
            ]
            if image_urls:
                user_text += "\n\n[첨부 이미지 URL]\n" + "\n".join(image_urls)

            # 글자도 첨부도 없으면 무시
            if not user_text:
                return

            # 채널 ID를 thread_id로 사용 → 채널별 대화 맥락 유지
            config = {"configurable": {"thread_id": str(message.channel.id)}}

            # 답하는 동안 디스코드에 '입력 중...' 표시
            async with message.channel.typing():
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=user_text)]},
                    config,
                )

            # 에이전트가 돌려준 메시지 목록의 맨 마지막이 최종 답변
            reply_text = _extract_text(result["messages"][-1])
            await message.reply(reply_text)

        # 여기서 블로킹: 봇이 종료될 때까지 실행을 유지한다.
        await client.start(DISCORD_BOT_TOKEN)
