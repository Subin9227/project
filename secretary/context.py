"""'지금 이 요청은 누구 것인가'를 도구들에게 전달하는 얇은 층.

문제:
    도구(notion_tools, blaybus_tools)는 LangGraph가 부르기 때문에, bot.py가
    "이건 철수 요청이야"라고 알려줄 통로가 없다. 도구 시그니처에 user를 넣으면
    17개를 전부 고쳐야 하고, 모델에게도 그 인자가 보여서 헛소리를 채워 넣는다.

해법:
    contextvars. on_message에서 한 번 심어두면 그 요청 안에서만 보인다.
    discord.py는 이벤트마다 asyncio Task를 새로 만들고, Task는 컨텍스트를
    복사해 가므로 동시에 온 두 사람의 요청이 섞이지 않는다.
    (notion_tools가 쓰는 asyncio.to_thread도 컨텍스트를 복사한다)

    ponytail: 이 방식은 봇이 한 프로세스·한 이벤트루프일 때만 맞다.
    스레드풀이나 여러 프로세스로 가면 깨진다 — 그때는 도구마다
    `config: RunnableConfig`를 받아 LangGraph의 configurable에서 꺼내야 한다.

한 가지 더:
    등록하지 않은 주인은 users 표에 행이 없다. 그래서 '아무도 안 심었으면
    .env 값으로 만든 User를 준다'. 덕분에 1인 모드가 그대로 동작하고,
    도구들은 등록/미등록을 구분할 필요가 없다.
"""

from __future__ import annotations

from contextvars import ContextVar

from secretary import config, users

_current: ContextVar[users.User | None] = ContextVar("current_user", default=None)


class NotConfigured(RuntimeError):
    """아직 연결하지 않은 서비스를 쓰려 할 때.

    ⚠️ 이 예외가 있는 이유: 예전엔 값이 없으면 `or .env값`으로 폴백했다. 그런데
       그러면 블레이버스를 등록하지 않은 사람이 물었을 때 **주인 계정의 내역이
       그대로 나온다**(2계정 테스트에서 실제로 발생). 없으면 없다고 해야 한다.
    """


def _subject_josa(word: str) -> str:
    """받침에 맞는 주격조사. '블레이버스가' / '노션이'.

    한글이 아니면(영문·숫자로 끝나면) 판단할 수 없으니 '이(가)'로 둔다.
    """
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return "이(가)"
    has_batchim = (ord(last) - 0xAC00) % 28
    return "이" if has_batchim else "가"


def require(value, what: str):
    """값이 없으면 남의 것으로 때우지 말고, 사용자에게 안내를 낸다."""
    if not value:
        raise NotConfigured(
            f"{what}{_subject_josa(what)} 아직 연결 안 됐어요. `/register`로 등록해 주세요."
        )
    return value


def as_message(e: Exception, what: str) -> str:
    """도구가 사용자에게 돌려줄 오류 문장.

    NotConfigured는 이미 사람에게 할 말이므로 그대로 낸다. 안 그러면
    "블레이버스 목록을 못 봤어요: NotConfigured: 블레이버스가…"처럼 겹친다.
    그 밖의 예외는 진단이 되도록 타입을 붙여 둔다.
    """
    if isinstance(e, NotConfigured):
        return str(e)
    return f"{what}: {type(e).__name__}: {e}"


def set_current(user: users.User | None):
    """이 요청의 주인을 심는다. 돌려받은 토큰으로 reset할 수 있다."""
    return _current.set(user)


def reset(token) -> None:
    _current.reset(token)


def env_user() -> users.User:
    """.env 값으로 만든 가상의 사용자. 주인이 등록 없이 쓸 때의 설정."""
    return users.User(
        discord_id=config.OWNER_ID or "env",
        llm_provider="vllm" if config.VLLM_BASE_URL else "anthropic",
        llm_key=config.ANTHROPIC_API_KEY,
        llm_model=config.VLLM_MODEL if config.VLLM_BASE_URL else config.CLAUDE_MODEL,
        vllm_base_url=config.VLLM_BASE_URL,
        notion_token=config.NOTION_TOKEN,
        routine_ds_id=config.NOTION_ROUTINE_DS_ID,
        homework_ds_id=config.NOTION_HOMEWORK_DS_ID,
        blaybus_id=config.BLAYBUS_LOGIN_ID,
        blaybus_pw=config.BLAYBUS_PASSWORD,
        blaybus_pid=config.BLAYBUS_PROJECT_ID,
    )


def active() -> users.User:
    """지금 요청에 쓸 설정. 심어둔 사람이 있으면 그 사람, 없으면 .env."""
    return _current.get() or env_user()


def _selftest() -> None:
    import asyncio

    # 아무도 안 심으면 .env 사용자
    assert active().discord_id == env_user().discord_id
    assert isinstance(active(), users.User)
    assert active().notion_token == config.NOTION_TOKEN

    # 조사가 받침을 따라가야 한다 ("블레이버스이(가)"는 어색하다)
    assert _subject_josa("블레이버스") == "가"
    assert _subject_josa("노션") == "이"
    assert _subject_josa("과제 노션") == "이"
    assert _subject_josa("DB") == "이(가)"  # 한글이 아니면 판단 불가
    assert isinstance(_subject_josa("노션"), str)

    # 없으면 안내를 내고, 있으면 그 값을 그대로 준다
    try:
        require(None, "블레이버스")
        raise AssertionError("빈 값인데 통과했다")
    except NotConfigured as e:
        assert "블레이버스가 아직" in str(e), str(e)
    assert require("x", "노션") == "x"
    # 예외 이름이 사용자에게 새면 안 된다
    msg = as_message(NotConfigured("블레이버스가 아직 연결 안 됐어요."), "못 봤어요")
    assert msg == "블레이버스가 아직 연결 안 됐어요." and "NotConfigured" not in msg
    # 그 밖의 예외는 진단이 되게 타입을 남긴다
    assert "ValueError" in as_message(ValueError("boom"), "못 봤어요")

    fake = users.User(discord_id="111", notion_token="ntn_fake", routine_ds_id="rt")
    token = set_current(fake)
    assert active().discord_id == "111" and active().notion_token == "ntn_fake"
    reset(token)
    assert active().discord_id != "111", "reset이 안 먹었다"

    # 동시에 도는 두 Task가 서로를 덮어쓰지 않아야 한다 (이게 이 파일의 존재 이유)
    async def who(name: str, delay: float) -> str:
        set_current(users.User(discord_id=name))
        await asyncio.sleep(delay)  # 그 사이 다른 Task가 끼어든다
        return active().discord_id

    async def race():
        return await asyncio.gather(who("A", 0.02), who("B", 0.01))

    got = asyncio.run(race())
    assert got == ["A", "B"], f"요청끼리 섞였다: {got}"
    # 바깥 컨텍스트는 Task 안의 set에 오염되지 않아야 한다
    assert active().discord_id == env_user().discord_id
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
