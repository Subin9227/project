"""사용자별 설정·자격증명 저장소 (data/users.sqlite).

이 프로젝트가 처음으로 갖는 '우리가 스키마를 소유하는' 저장소다.
(memory.sqlite는 LangGraph 것이고, bot.py가 시작할 때 통째로 지운다.)

⚠️ 정직하게 적어둘 것 — 블레이버스는 OAuth가 없다.
   토큰이 1시간짜리라 봇이 무인으로 돌려면 **재사용 가능한 비밀번호를 들고 있어야**
   한다. Fernet 암호화는 공격자를 막는 게 아니라 `cat users.sqlite`를 막는 것이다.
   이 서버에 셸이 뚫리면 .env의 CRED_KEY도 같이 털리고, 그러면 전원의 비밀번호가
   평문으로 복구된다. 사람들은 비밀번호를 재사용한다.
   → 그래서 등록 화면에 이 사실을 한국어 한 줄로 고지하고, /forget을 반드시 둔다.
     그 고지 문장이 이 단계의 진짜 산출물이다.

⚠️ CRED_KEY가 없으면 저장 기능을 통째로 끈다. 평문 저장은 하지 않는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from secretary import config
from secretary.config import CRED_KEY, KST, USERS_DB_PATH

# 비밀로 다뤄 저장 시 암호화하는 칸.
SECRET_COLUMNS = ("llm_key", "notion_token", "blaybus_pw")

_COLUMNS = (
    "discord_id",
    # 뇌 — 각자 자기 키를 낸다 (공개배포라 주인 지갑을 열어두지 않는다)
    "llm_provider",  # anthropic | openai | vllm
    "llm_key",
    "llm_model",
    "vllm_base_url",
    # 노션
    "notion_token",
    "routine_ds_id",
    "homework_ds_id",
    # 블레이버스
    "blaybus_id",
    "blaybus_pw",
    "blaybus_pid",
    # 알림 (Phase 2의 .env 설정이 여기로 옮겨온다)
    "alarm_target",
    "alarm_mention",
    # 알림을 잠시 멈춤('1'). 받을 곳은 그대로 두고 발송만 막는다 —
    # 끌 때 alarm_target을 지우면 다시 켤 때 채널 ID를 또 찾아야 한다.
    "alarm_off",
    "work_start",
    "work_end",
    "weekly_at",
    # 남용 방지
    "daily_count",
    "count_date",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    {_COLUMNS[0]} TEXT PRIMARY KEY,
    {" TEXT, ".join(_COLUMNS[1:-2])} TEXT,
    daily_count INTEGER DEFAULT 0,
    count_date  TEXT
);
"""


class CredentialsDisabled(RuntimeError):
    """CRED_KEY가 없어 저장 기능을 못 쓰는 상태."""


def enabled() -> bool:
    """등록 기능을 쓸 수 있는가. .env에 CRED_KEY가 있어야 한다."""
    return bool(CRED_KEY)


def _fernet():
    if not CRED_KEY:
        raise CredentialsDisabled(
            "CRED_KEY가 없어서 자격증명을 저장할 수 없어요. "
            "평문으로 남의 비밀번호를 두는 일은 하지 않습니다."
        )
    from cryptography.fernet import Fernet

    return Fernet(CRED_KEY.encode())


def encrypt(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str | None:
    return _fernet().decrypt(value.encode()).decode() if value else None


@dataclass
class User:
    """한 사람의 설정. 비밀 칸은 **복호화된 상태**로 들고 있는다."""

    discord_id: str
    llm_provider: str | None = None
    llm_key: str | None = None
    llm_model: str | None = None
    vllm_base_url: str | None = None
    notion_token: str | None = None
    routine_ds_id: str | None = None
    homework_ds_id: str | None = None
    blaybus_id: str | None = None
    blaybus_pw: str | None = None
    blaybus_pid: str | None = None
    alarm_target: str | None = None
    alarm_mention: str | None = None
    alarm_off: str | None = None
    work_start: str | None = None
    work_end: str | None = None
    weekly_at: str | None = None
    daily_count: int = 0
    count_date: str | None = None

    @property
    def registered(self) -> bool:
        """봇을 쓸 수 있는 상태인가. 뇌가 없으면 아무것도 못 한다.

        ⚠️ vllm은 주소를 비워도 등록으로 본다 — 봇이 쓰는 기본 서버(.env)를 따라가기
           때문이다. 다만 **provider가 vllm일 때만** 연다. 조건을 넓게 풀면 아무것도
           안 넣은 사람이 관문을 통과한다.
        """
        if self.llm_provider == "vllm":
            return bool(self.vllm_base_url or config.VLLM_BASE_URL)
        return bool(self.llm_provider and self.llm_key)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # ⚠️ CREATE TABLE IF NOT EXISTS는 이미 있는 표에 칸을 **안 늘린다.** 칸을 더하면
    #    기존 사용자들의 행은 그 칸 없이 남아 SELECT * 결과가 User(**data)에서 터진다.
    #    그래서 열 때마다 빠진 칸을 채운다 (있으면 아무 일도 안 한다).
    have = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for col in _COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    return conn


def _to_user(row: sqlite3.Row) -> User | None:
    """행을 복호화해 User로. 열쇠가 안 맞으면 None.

    ⚠️ 여기서 예외를 흘리면 안 된다. CRED_KEY를 바꾸거나 잃어버리면 옛 암호문이
       전부 안 풀리는데, 그게 그대로 터지면 all_users()가 죽고 → 알림 루프가 죽고
       → 봇이 아예 못 뜬다. 못 읽는 사람은 건너뛰고 봇은 살아 있어야 한다.
       (그 사람은 /register를 다시 하면 된다)
    """
    from cryptography.fernet import InvalidToken

    data = dict(row)
    try:
        for col in SECRET_COLUMNS:
            data[col] = decrypt(data.get(col))
    except (InvalidToken, CredentialsDisabled):
        print(f"⚠️ {data['discord_id']}의 자격증명을 못 읽었어요 (CRED_KEY 불일치)")
        return None
    return User(**data)


def get(discord_id: str) -> User | None:
    """한 사람의 설정. 비밀 칸은 복호화해서 준다. 없거나 못 풀면 None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
    return _to_user(row) if row is not None else None


def all_users() -> list[User]:
    """등록된 사람 전부. 알림 스케줄을 만들 때 쓴다. 못 읽는 사람은 빠진다."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
    return [u for u in map(_to_user, rows) if u is not None]


def save(discord_id: str, **fields) -> None:
    """준 칸만 갱신한다. 행이 없으면 만든다.

    비밀 칸은 여기서 암호화한다 — 부르는 쪽이 암호화를 잊는 실수를 원천 차단한다.
    """
    unknown = set(fields) - set(_COLUMNS)
    if unknown:
        raise ValueError(f"모르는 칸: {sorted(unknown)}")

    values = {
        col: (encrypt(val) if col in SECRET_COLUMNS else val)
        for col, val in fields.items()
    }
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (discord_id) VALUES (?)", (str(discord_id),)
        )
        if values:
            assigns = ", ".join(f"{col} = ?" for col in values)
            conn.execute(
                f"UPDATE users SET {assigns} WHERE discord_id = ?",
                (*values.values(), str(discord_id)),
            )


def forget(discord_id: str) -> bool:
    """그 사람 기록을 통째로 지운다. 지웠으면 True.

    남의 비밀번호를 들고 있는 이상 이 기능은 타협 대상이 아니다.
    """
    with _connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE discord_id = ?", (str(discord_id),))
    return cur.rowcount > 0


def bump_daily(discord_id: str, limit: int) -> bool:
    """오늘 몫을 하나 쓴다. 한도를 넘었으면 False.

    본인 키를 쓰더라도 폭주는 막는다 (recursion_limit이 한 메시지를 막고,
    이건 하루치를 막는다).
    """
    today = datetime.now(KST).date().isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT daily_count, count_date FROM users WHERE discord_id = ?",
            (str(discord_id),),
        ).fetchone()
        count = (row["daily_count"] or 0) if row and row["count_date"] == today else 0
        if count >= limit:
            return False
        conn.execute(
            "UPDATE users SET daily_count = ?, count_date = ? WHERE discord_id = ?",
            (count + 1, today, str(discord_id)),
        )
    return True


def _selftest() -> None:
    """임시 DB로 저장·복호화·삭제를 확인한다. 실제 users.sqlite는 안 건드린다."""
    import os
    import tempfile

    from cryptography.fernet import Fernet

    global USERS_DB_PATH, CRED_KEY
    tmp = tempfile.mktemp(suffix=".sqlite")
    USERS_DB_PATH = tmp  # type: ignore[assignment]
    CRED_KEY = Fernet.generate_key().decode()  # type: ignore[assignment]

    assert get("1") is None

    save("1", llm_provider="anthropic", llm_key="sk-secret", blaybus_pw="pw#1")
    u = get("1")
    assert isinstance(u, User) and u.discord_id == "1"
    # 복호화해서 돌려주는지 (원문과 같아야 한다)
    assert u.llm_key == "sk-secret" and u.blaybus_pw == "pw#1"
    assert u.registered is True

    # 파일에는 암호문으로 들어가야 한다 — 원문이 그대로 보이면 안 된다
    raw = open(tmp, "rb").read()
    assert b"sk-secret" not in raw and b"pw#1" not in raw, "평문이 파일에 남았다"

    # 준 칸만 갱신하고 나머지는 유지
    save("1", notion_token="ntn_x")
    u = get("1")
    assert u.notion_token == "ntn_x" and u.llm_key == "sk-secret"

    # 뇌가 없으면 등록으로 안 친다
    save("2", notion_token="ntn_y")
    assert get("2").registered is False
    # vLLM은 키 없이 URL만으로도 뇌가 된다
    save("2", llm_provider="vllm", vllm_base_url="http://x")
    assert get("2").registered is True

    # ⚠️ vLLM은 주소를 비워도 봇의 기본 서버(.env)를 따라간다. 다만 관문이 넓어지면
    #    안 되므로 provider가 vllm일 때만 열린다.
    save("2", vllm_base_url=None)
    assert get("2").registered is bool(config.VLLM_BASE_URL)
    save("2", llm_provider="openai")
    assert get("2").registered is False, "키 없는 openai가 관문을 통과했다"

    # ⚠️ provider를 바꾸면 반대편 칸이 비워져야 한다. 안 그러면 옛 OpenAI 키가 남아
    #    로컬 서버로 나가 401이 난다 (2026-08-05에 실제로 겪음).
    save("3", llm_provider="openai", llm_key="sk-old", llm_model="gpt-4o-mini")
    save("3", llm_provider="vllm", vllm_base_url="http://y", llm_key=None, llm_model=None)
    v = get("3")
    assert v.llm_key is None and v.llm_model is None, (v.llm_key, v.llm_model)
    assert v.registered is True

    assert len(all_users()) == 3
    assert isinstance(all_users()[0], User)

    # 알림 끄기는 받을 곳을 지우지 않는다 — 지우면 다시 켤 때 채널 ID를 또 찾아야 한다
    save("1", alarm_target="ch1", alarm_mention="me", work_start="08:40")
    save("1", alarm_off="1")
    u = get("1")
    assert u.alarm_off == "1" and u.alarm_target == "ch1" and u.work_start == "08:40"
    save("1", alarm_off=None)
    assert get("1").alarm_off is None and get("1").alarm_target == "ch1"

    # 칸을 나중에 더해도 옛 DB가 열려야 한다 (CREATE TABLE IF NOT EXISTS는 안 늘린다)
    with _connect() as conn:
        conn.execute("ALTER TABLE users DROP COLUMN alarm_off")
    assert get("1").alarm_off is None, "빠진 칸을 다시 채우지 못했다"

    # 하루 한도
    assert bump_daily("1", 2) and bump_daily("1", 2)
    assert bump_daily("1", 2) is False

    # 모르는 칸은 조용히 무시하지 말고 터뜨린다 (오타를 삼키면 설정이 안 먹는다)
    try:
        save("1", nope="x")
        raise AssertionError("모르는 칸인데 통과했다")
    except ValueError:
        pass

    # 열쇠가 바뀌면 옛 암호문을 못 푼다. 그때 봇이 죽으면 안 되고, 조용히 건너뛴다.
    old_key = CRED_KEY
    CRED_KEY = Fernet.generate_key().decode()  # type: ignore[assignment]
    assert get("1") is None, "못 푸는데 User를 돌려줬다"
    # 비밀이 든 행만 빠진다. "3"은 vLLM이라 암호화된 칸이 하나도 없어 그대로 읽힌다
    # — 키 없이 쓰는 사람은 CRED_KEY가 바뀌어도 영향을 안 받는다.
    assert [u.discord_id for u in all_users()] == ["3"], all_users()
    CRED_KEY = old_key  # type: ignore[assignment]
    assert get("1") is not None, "열쇠를 되돌렸는데 못 읽는다"

    assert forget("1") is True and get("1") is None
    assert forget("1") is False  # 두 번째는 지울 게 없다

    os.unlink(tmp)
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
