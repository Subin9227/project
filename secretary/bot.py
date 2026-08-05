"""디스코드 게이트웨이 — 메시지 수신 → 에이전트 호출 → 답장.

기존 discord_claude_chat_app.py의 '배선' 부분(인텐트, on_ready, on_message,
답장)이 여기로 이사왔다. 다만 메시지를 Claude에 직접 던지는 대신,
agent.py가 만든 에이전트에게 넘긴다.

대화기억의 핵심:
    thread_id = "디스코드 채널 ID:KST 오늘 날짜".
    같은 채널이라도 날짜가 바뀌면 다른 thread_id가 되므로, 대화는 하루 단위로
    새로 시작한다. 공주비서가 하는 일(오늘 루틴 확인·체크, 블레이버스 토글)은
    전부 '오늘'로 끝나서, 어제 맥락을 끌고 오면 득보다 실이 크다.
    ("어제 뭐 했더라"는 대화기억이 아니라 노션을 조회해서 답할 일이다.)
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta

import discord
from discord import app_commands
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

from secretary import context, users
from secretary.agent import build_agent, build_tools
from secretary.alarms import (
    audit_users,
    build_alarm_loop,
    format_audit,
    load_schedules,
    resolve_target,
)
from secretary.commands import setup_commands
from secretary.config import (
    DISCORD_BOT_TOKEN,
    KST,
    MEMORY_DB_PATH,
    MEMORY_KEEP_DAYS,
    OWNER_ID,
)
from secretary.webserver import attach_client, build_health_server

# 디스코드 한 메시지의 최대 길이. 초과분은 잘라서 보낸다.
DISCORD_MAX_LEN = 2000

# --- 안전벨트 상수 (토큰 폭주 방지) ---
# 겹1: LangGraph 스텝 상한. react 한 바퀴(LLM→도구)가 ~2스텝이니 12 = 약 5~6왕복.
#      기본값 25보다 낮춰, 도구가 계속 실패해도 무한 재시도 전에 멈춘다.
RECURSION_LIMIT = 12
# 겹2: 한 메시지 처리의 벽시계 상한(초). 넘으면 중단하고 사과 답장.
AGENT_TIMEOUT_SEC = 90

# 대화기억을 며칠치 남길지. 오늘 포함이므로 7 = 오늘~6일 전. 0이면 안 지운다.
# 값은 .env의 MEMORY_KEEP_DAYS로 바꾼다 (config.py 참고).
KEEP_DAYS = MEMORY_KEEP_DAYS


def _today_kst() -> str:
    return f"{datetime.now(KST):%Y-%m-%d}"


async def _prune_old_threads(checkpointer: AsyncSqliteSaver) -> int:
    """낡은 날짜의 대화기억을 지운다. 봇을 켤 때 딱 한 번.

    thread_id가 '<채널ID>:<YYYY-MM-DD>' 형태라 끝 10자로 날짜를 판별한다.
    날짜가 안 붙은 옛 thread_id(이 규칙 이전에 쌓인 것)도 같은 조건에 걸려
    자동으로 지워진다 — 따로 마이그레이션할 필요가 없다.

    ⚠️ DELETE만으로는 파일 크기가 안 줄어든다(빈 페이지로 남는다). VACUUM까지 해야
       디스크로 반납된다. 그리고 VACUUM은 트랜잭션 안에서 못 도니 commit이 먼저다.
    ⚠️ 이 DB는 WAL 모드다. VACUUM 결과가 -wal에만 쌓여서, 연결이 살아있는 동안에는
       본 파일이 77MB 그대로로 보인다(껐다 켜면 그때 줄어든다). wal_checkpoint를
       TRUNCATE로 한 번 돌려야 그 자리에서 반납된다.

    ponytail: 시작할 때 1회. 프로세스가 몇 달씩 안 죽으면 그때 main()의
    asyncio.gather에 하루 한 번 도는 태스크로 올린다.

    KEEP_DAYS가 0이면 한 건도 안 지운다(테스트 기간용). 지워버리면 사용자들이
    무슨 말을 했고 봇이 어떻게 답했는지 되짚을 방법이 사라진다.
    """
    if KEEP_DAYS <= 0:
        return 0

    today = datetime.now(KST).date()
    keep = {(today - timedelta(days=d)).isoformat() for d in range(KEEP_DAYS)}

    # thread_id 목록만 필요하다. alist()로 훑으면 체크포인트를 전부 역직렬화하므로
    # (수십 MB) 목록은 SQL로 싸게 얻고, 삭제는 공개 API에 맡긴다.
    async with checkpointer.conn.execute(
        "SELECT DISTINCT thread_id FROM checkpoints"
    ) as cur:
        thread_ids = [row[0] for row in await cur.fetchall()]

    stale = [t for t in thread_ids if t[-10:] not in keep]
    for thread_id in stale:
        await checkpointer.adelete_thread(thread_id)

    if stale:
        await checkpointer.conn.commit()
        await checkpointer.conn.execute("VACUUM")
        await checkpointer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return len(stale)


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


def _allowed(discord_id: str) -> bool:
    """이 사람의 말을 받아줄까.

    주인은 언제나 통과한다 — 봇이 .env 자격증명으로 돌기 때문에 주인에게
    등록을 요구하면 자기 봇에서 자기가 쫓겨난다.
    등록 기능이 아예 꺼져 있으면(CRED_KEY 없음) 예전처럼 1인 모드로 다 받는다.
    """
    if OWNER_ID and discord_id == OWNER_ID:
        return True
    if not users.enabled():
        return True
    user = users.get(discord_id)
    return bool(user and user.registered)


async def _describe_target(client: discord.Client, target: str) -> tuple[str, bool]:
    """알림이 실제로 어디로 갈지 (사람이 읽을 이름, 닿는가).

    기동할 때 확인시켜 주는 게 핵심이다: 채널 ID를 잘못 넣으면 알림 시각이
    되어서야 실패를 알게 되는데, 그땐 이미 한 번 놓친 뒤다.
    ⚠️ '닿는가'는 채널이 살아있고 봇이 볼 수 있다는 뜻일 뿐이다. 사람이 다른
       채널로 옮겨간 경우는 여기서 못 잡는다 — 옛 채널로 잘 배달된다.
    """
    try:
        dest, kind = await resolve_target(client, target)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ {target} ({type(e).__name__})", False

    if kind == "user":
        return f"DM → {dest}", True

    name = getattr(dest, "name", None) or str(dest)
    guild = getattr(dest, "guild", None)
    return (f"{guild.name} / #{name}" if guild else f"#{name}"), True


async def _display_name(client: discord.Client, uid: str) -> str:
    """디스코드에서 보이는 이름. 서버 별명이 있으면 그게 우선.

    fetch_user는 계정 아이디(hwamgai)만 준다. 사람들이 서로를 알아보는 건
    서버 별명('linda.hwang(황수빈)/인공지능')이라, 서버 쪽을 먼저 뒤진다.
    ⚠️ members 인텐트가 없어 캐시가 비어 있으므로 fetch_member로 물어본다.
       서버 수만큼 호출될 수 있지만 기동할 때 한 번뿐이다.
    """
    for guild in client.guilds:
        member = guild.get_member(int(uid))  # 캐시에 있으면 공짜
        if member is not None:
            return member.display_name
    for guild in client.guilds:
        try:
            return (await guild.fetch_member(int(uid))).display_name
        except Exception:  # noqa: BLE001 - 그 서버에 없는 사람
            continue
    try:  # 어느 서버에서도 못 찾으면 계정 이름이라도
        user = await client.fetch_user(int(uid))
        return user.display_name
    except Exception:  # noqa: BLE001 - 탈퇴 등
        return "(이름 못 찾음)"


async def _alarm_table(client: discord.Client) -> str:
    """기동 로그에 찍을 알림 표. ID를 사람 이름·채널 이름으로 바꿔 넣는다.

    이름 조회는 등록자 수만큼 API를 부르므로 기동이 조금 느려진다. 봇은 자주 켜지
    않고, 이 표를 보는 목적이 '누가 언제 받나'라서 ID만으로는 쓸모가 없다.
    """
    rows = audit_users()
    names: dict[str, str] = {}
    for row in rows:
        uid = row["discord_id"]
        if uid and uid not in names:
            names[uid] = await _display_name(client, uid)
        # 받을 곳이 없는 사람은 물어볼 것도 없다 ('연결' 칸은 '—'로 남는다)
        if row["target"]:
            row["place"], row["reach"] = await _describe_target(client, row["target"])
    return format_audit(rows, names, OWNER_ID)


async def main() -> None:
    """봇을 기동한다. run.py가 이 함수를 asyncio.run으로 실행한다."""
    intents = discord.Intents.default()
    intents.message_content = True  # 메시지 본문 읽기 권한
    client = discord.Client(intents=intents)

    # 슬래시 커맨드(/register 등)를 붙인다. discord.Client에는 트리가 없어서 직접 만든다.
    tree = app_commands.CommandTree(client)
    setup_commands(tree)

    # 대화기억 저장소를 열고, 그 안에서 봇 생애 전체를 돈다.
    # (async with 블록이 유지되는 동안 SQLite 연결이 살아있다.)
    async with AsyncSqliteSaver.from_conn_string(str(MEMORY_DB_PATH)) as checkpointer:
        pruned = await _prune_old_threads(checkpointer)
        if KEEP_DAYS <= 0:
            # 꺼져 있다고 말해주지 않으면 '조용한 것'과 '지울 게 없는 것'을 구분할 수 없다
            size_mb = MEMORY_DB_PATH.stat().st_size / 1024 / 1024
            print(f"대화기억 정리: 꺼짐 (MEMORY_KEEP_DAYS=0, 현재 {size_mb:.1f}MB)")
        elif pruned:
            size_mb = MEMORY_DB_PATH.stat().st_size / 1024 / 1024
            print(f"낡은 대화기억 {pruned}개 정리 완료 (현재 {size_mb:.1f}MB)")

        # 주인 몫을 미리 하나 만들어 둔다(도구 개수를 기동 로그에 찍기 위해서도).
        # 등록한 사람의 에이전트는 그 사람이 말을 걸 때 만들어져 캐시된다.
        agent = await build_agent(checkpointer)
        print(f"   도구: {len(build_tools())}개")
        alarm_loop = build_alarm_loop(client)

        @client.event
        async def on_ready():
            from secretary.config import CLAUDE_MODEL, VLLM_BASE_URL, VLLM_MODEL
    
            backend = (
                f"로컬 vLLM — {VLLM_MODEL} @ {VLLM_BASE_URL}"
                if VLLM_BASE_URL
                else f"Claude API — {CLAUDE_MODEL}"
            )
            print(f"공주비서 로그인 완료: {client.user}")
            print(f"   뇌: {backend}")

            # 슬래시 커맨드를 디스코드에 알린다.
            # ⚠️ 전역 등록 하나만 한다. copy_global_to로 서버에도 복사하면 디스코드가
            #    '서버용 한 벌 + 전역 한 벌'을 각각 보여줘서 목록에 두 번씩 뜬다.
            #    (전역은 DM에서도 보이므로 서버 복사본은 필요 없다)
            # clear_commands+sync는 예전에 복사해둔 서버 사본을 지우는 청소다.
            try:
                for guild in client.guilds:
                    tree.clear_commands(guild=guild)
                    await tree.sync(guild=guild)
                synced = await tree.sync()
                print("   커맨드: " + " ".join(f"/{c.name}" for c in synced))
            except Exception as e:  # noqa: BLE001
                print(f"   ⚠️ 커맨드 등록 실패: {type(e).__name__}: {e}")

            if not users.enabled():
                print("   등록 기능: 꺼짐 (.env에 CRED_KEY 없음)")
            count = len(users.all_users()) if users.enabled() else 0
            print(f"   등록된 사용자: {count}명  (자세히는 /users)")

            # 등록자 전원을 표로. 알림이 안 가는 사람도 '왜 안 가는지'까지 보여준다 —
            # 예전엔 가는 것만 찍어서, 등록만 하고 /setup을 안 한 사람이 안 보였다.
            print(await _alarm_table(client))

            # 알림은 로그인 뒤에 켠다 (DM을 보내려면 게이트웨이가 붙어 있어야 한다).
            # on_ready는 재접속 때도 불리므로 이미 돌고 있으면 다시 켜지 않는다.
            if not load_schedules():
                print("   알림: 꺼짐 (ALARM_TARGET도 /setup도 없음)")
            elif not alarm_loop.is_running():
                alarm_loop.start()

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

            # 등록 관문. 공개배포라 아무나 말을 걸 수 있으므로, 등록한 사람만 받는다.
            # (주인은 .env로 도니까 등록 없이 통과 — 안 그러면 주인부터 막힌다)
            author_id = str(message.author.id)
            if not _allowed(author_id):
                await message.reply(
                    "처음 뵙네요, 아가씨. `/register`로 노션과 연결해 주시면 "
                    "그때부터 도와드릴게요."
                )
                return

            # 이 요청의 주인을 심는다. 여기서부터 도구들이 이 사람의 노션·블레이버스를
            # 쓴다. 등록 안 한 주인이면 None이라 .env 설정으로 돈다(1인 모드).
            me = users.get(author_id) if users.enabled() else None
            ctx_token = context.set_current(me)

            # thread_id = 사용자ID + KST 오늘 날짜.
            # ⚠️ 채널이 아니라 **사람** 기준이다. 채널로 묶으면 한 채널의 두 사람이
            #    서로의 대화기억(과 도구 결과)을 보게 된다.
            # 날짜가 바뀌면 대화가 새로 시작된다. 자정을 넘기면 맥락이 끊기지만 그게
            # 맞다: _prompt_with_today가 매번 오늘 날짜를 새로 주입하기 때문이다.
            # recursion_limit(겹1)로 스텝 상한을 걸어 무한 재시도를 막는다.
            config = {
                "configurable": {"thread_id": f"{author_id}:{_today_kst()}"},
                "recursion_limit": RECURSION_LIMIT,
            }

            # 안전벨트로 감싸 호출한다:
            #   겹2 = asyncio.wait_for 벽시계 타임아웃
            #   겹3 = 예외를 잡아 크래시/무한대기 대신 페르소나 사과 답장
            try:
                # 이 사람의 뇌로 도는 에이전트. 뇌가 같으면 같은 그래프를 재사용한다.
                mine = await build_agent(checkpointer, me)
                # 답하는 동안 디스코드에 '입력 중...' 표시
                async with message.channel.typing():
                    result = await asyncio.wait_for(
                        mine.ainvoke(
                            {"messages": [HumanMessage(content=user_text)]},
                            config,
                        ),
                        timeout=AGENT_TIMEOUT_SEC,
                    )
                # 에이전트가 돌려준 메시지 목록의 맨 마지막이 최종 답변
                reply_text = _extract_text(result["messages"][-1])
            except GraphRecursionError:
                # 겹1 상한 도달: 같은 작업을 너무 여러 번 반복하다 멈춤
                reply_text = (
                    "야레야레 아가씨, 같은 걸 너무 여러 번 시도하다 멈췄어요. "
                    "요청을 조금만 더 구체적으로 주시겠어요?"
                )
            except asyncio.TimeoutError:
                # 겹2 상한 도달: 처리가 너무 오래 걸림
                reply_text = (
                    "아가씨, 처리가 너무 오래 걸려 중단했어요. 잠시 후 다시 시도해 주세요."
                )
            except Exception as e:  # noqa: BLE001 - 무엇이 터지든 봇은 살아남아 다음 메시지를 받아야 함
                # 키가 틀린 건 사용자가 고칠 수 있는 문제다. 예외 이름만 보여주면
                # (AuthenticationError) 뭘 해야 할지 알 수 없다.
                name = type(e).__name__
                if "Authentication" in name or "PermissionDenied" in name:
                    reply_text = (
                        "아가씨, 등록하신 API 키가 거부됐어요. "
                        "`/register`로 키를 다시 넣어 주시겠어요?"
                    )
                elif "RateLimit" in name:
                    reply_text = (
                        "아가씨의 API 사용량 한도에 걸렸어요. 잠시 뒤에 다시 불러주세요."
                    )
                else:
                    reply_text = f"처리 중 문제가 생겼어요, 아가씨. ({name})"
            finally:
                # 심어둔 주인을 반드시 걷어낸다. 안 걷으면 이 Task가 재사용될 때
                # 남의 설정이 남아 있을 수 있다.
                context.reset(ctx_token)

            await message.reply(reply_text)

        # 봇(디스코드 게이트웨이)과 상태 서버(/health)를 같은 asyncio 루프에서
        # 나란히 실행한다. 한 프로세스 안에서 둘 다 돌아가므로 Phase 2 관측
        # 스크립트를 그대로 재사용할 수 있다. 둘 중 하나라도 끝나면 gather가 반환된다.
        @client.event
        async def on_guild_join(guild: discord.Guild):
            """새 서버에 초대되면 주인에게 알린다.

            로컬이든 EC2든 봇은 하나뿐이라, 누가 데려갔는지 모르면 사용량을 가늠할 수
            없다. 초대 순간이 유일하게 확실한 신호다.
            """
            print(f"[초대] {guild.name} (총 {len(client.guilds)}개 서버)")
            if not OWNER_ID:
                return
            try:
                owner = await client.fetch_user(int(OWNER_ID))
                await owner.send(
                    f"🎀 '{guild.name}' 서버에 초대됐어요. "
                    f"지금 총 {len(client.guilds)}개 서버 / "
                    f"등록 {len(users.all_users()) if users.enabled() else 0}명이에요."
                )
            except Exception:  # noqa: BLE001 - 알림 실패로 봇이 죽으면 안 된다
                traceback.print_exc()

        health_server = build_health_server()
        attach_client(client)  # /health가 서버 수를 셀 수 있게
        await asyncio.gather(
            client.start(DISCORD_BOT_TOKEN),
            health_server.serve(),
        )
