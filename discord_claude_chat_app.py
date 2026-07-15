from __future__ import annotations

import os

import discord
from anthropic import Anthropic
from dotenv import load_dotenv


load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

discord_client = discord.Client(intents=intents)


@discord_client.event
async def on_ready():
    print(f"Discord bot logged in as {discord_client.user}")


@discord_client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    user_text = message.content.strip()
    if not user_text:
        return

    response = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=800,
        system=(
            "너는 사용자를 '아가씨'라고 부르며 극진히 챙기는 집사/메이드 페르소나야. "
            "닛몰캐쉬 '잘자요 아가씨'의 말투를 따라해. 특징:\n"
            "- 사용자를 항상 '아가씨'라고 부른다\n"
            "- 정중한 존댓말('~에요', '~이라구요', '~데스')을 쓰되 은근히 잔소리하듯 챙긴다\n"
            "- 가끔 '야레야레', '못 말리는 아가씨' 같은 추임새를 섞는다\n"
            "- 일본어 감탄사(お嬢様, やれやれ 등)를 자연스럽게 살짝 섞어도 좋다\n"
            "- 걱정과 애정이 담긴 츤데레 톤을 유지한다\n"
            "답변은 한국어로, 이 페르소나를 유지하면서 사용자의 요청을 처리해."
        ),
        messages=[
            {
                "role": "user",
                "content": user_text,
            }
        ],
    )

    reply_text = response.content[0].text
    await message.reply(reply_text)


if __name__ == "__main__":
    discord_client.run(DISCORD_BOT_TOKEN)