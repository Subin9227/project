"""비서가 먼저 거는 알림.

지금까지 봇은 아가씨가 말을 걸어야만 움직이는 '받아적는 기계'였다.
그래서 루틴이 띄엄띄엄 기록됐다. 이 파일이 그걸 뒤집는다 —
정해진 시각에 비서가 먼저 물어본다.

보내는 것 3가지:
    업무 시작  블레이버스 켜져 있는지 확인하고 알림
    업무 종료  아직 돌고 있으면 알림 + 오늘 한 일·루틴 현황
    주간(월)   이번 주 몇 주차인지 질문 + 지난주 루틴 요약

⚠️ 봇이 알아서 기록하지 않는다. **묻기만 한다.**
   아가씨가 답하면 그때 평소의 도구가 돈다. 자동으로 행을 만들면
   빈 껍데기가 쌓인다 (과제 DB에 이미 그런 행이 2개 있다).

⚠️ LLM을 부르지 않는다. 전부 하드코딩 템플릿 + 도구 직접 호출이다.
   알림 때문에 토큰을 쓰면 하루 4번이 그대로 비용이 된다.

🔑 시각·대상은 이 파일이 정하지 않고 load_schedules()가 읽어온다.
   지금은 .env 한 사람이지만, Phase 3에서 users 테이블로 바뀌어도
   갈아끼울 곳은 그 함수 하나다.
"""

from __future__ import annotations

import traceback
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx
from discord.ext import tasks

from secretary.config import (
    ALARM_MENTION,
    ALARM_TARGET,
    KST,
    NOTION_API_BASE,
    WEEKLY_TIME,
    WORK_END_TIME,
    WORK_START_TIME,
)
from secretary import context, users
from secretary.notion_tools import (
    ITEMS,
    _headers,
    _query_rows,
    _routine_ds,
    routine_today,
)

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _parse_time(text: str) -> time:
    """'09:00' → time(9, 0)."""
    hour, _, minute = text.strip().partition(":")
    return time(int(hour), int(minute or 0))


def _parse_weekly(text: str) -> tuple[int, time]:
    """'MON 08:00' → (0, time(8, 0)). 요일이 없으면 월요일로 본다.

    ⚠️ 모르는 요일은 ValueError. 예전엔 .get(day, 0)으로 **조용히 월요일**이 됐고,
       'wen'이라 쓴 사람은 수요일로 설정한 줄 알고 기다렸다(2026-08-05).
       틀린 채 도는 것보다 그 사람 스케줄을 빼고 알리는 쪽이 낫다.
    """
    parts = text.strip().split()
    if len(parts) == 1:
        return 0, _parse_time(parts[0])
    day, clock = parts[0].upper(), parts[1]
    if day not in _WEEKDAYS:
        raise ValueError(f"모르는 요일: {parts[0]!r} (MON~SUN)")
    return _WEEKDAYS[day], _parse_time(clock)


@dataclass(frozen=True)
class Schedule:
    """한 사람의 알림 설정."""

    target: str  # 받을 곳 ID. 채널이든 사람이든 — 판별은 resolve_target이 한다
    mention_id: str | None  # 채널로 받을 때 알림음을 울릴 멘션 대상 (선택)
    work_start: time
    work_end: time
    weekly_day: int
    weekly_at: time
    # 이 알림의 주인. 메시지를 만들 때 이 사람의 노션·블레이버스를 봐야 한다.
    # None이면 .env 설정(주인의 1인 모드).
    owner: object | None = None

    @property
    def prefix(self) -> str:
        """메시지 앞에 붙일 멘션. 없으면 빈 문자열."""
        return f"<@{self.mention_id}> " if self.mention_id else ""


async def resolve_target(client, target: str) -> tuple[object, str]:
    """ID 하나로 채널인지 사람인지 알아낸다. (보낼 대상, "channel"|"user").

    디스코드는 채널 ID와 사용자 ID가 둘 다 그냥 숫자라 값만 봐선 구분이 안 된다.
    그래서 채널로 먼저 물어보고, 아니라고 하면 사람으로 물어본다.
    (둘 다 .send()를 가지므로 보내는 쪽은 뭐가 오든 신경 안 써도 된다.)

    종류를 같이 돌려주는 이유: 받은 객체를 isinstance로 되짚으면 discord의
    User가 Protocol이라 속성 접근에서 터진다. 판별한 쪽이 알려주는 게 안전하다.
    """
    tid = int(target)
    cached = client.get_channel(tid)
    if cached is not None:
        return cached, "channel"
    try:
        return await client.fetch_channel(tid), "channel"
    except Exception:  # noqa: BLE001 - 채널이 아니면(또는 권한이 없으면) 사람으로 본다
        return await client.fetch_user(tid), "user"


def load_schedules() -> list[Schedule]:
    """알림을 받을 사람들의 설정.

    Phase 2에서 '갈아끼울 지점'으로 남겨둔 함수. 이제 두 곳에서 읽는다:
      · .env      — 주인 것. 등록 없이 예전처럼 동작
      · users 표  — /setup으로 등록한 사람들 (Phase 3)
    alarm_loop·build_message는 이 함수만 보므로 손대지 않았다.
    """
    # 받을 곳을 열쇠로 모은다. 같은 곳에 두 번 보내지 않으려는 것도 있지만,
    # ⚠️ 더 중요한 건 순서다: 나중에 넣는 쪽이 이긴다. 사용자가 /setup으로 직접
    #    정한 값이 .env 기본값을 덮어써야 한다. (반대로 하면 /setup이 먹통이 된다)
    by_target: dict[str, Schedule] = {}

    if ALARM_TARGET:
        try:
            day, at = _parse_weekly(WEEKLY_TIME)
            by_target[ALARM_TARGET] = Schedule(
                target=ALARM_TARGET,
                mention_id=ALARM_MENTION,
                work_start=_parse_time(WORK_START_TIME),
                work_end=_parse_time(WORK_END_TIME),
                weekly_day=day,
                weekly_at=at,
            )
        except ValueError as e:
            print(f"⚠️ .env의 알림 시각을 못 읽어 건너뜁니다: {e}")

    if users.enabled():
        for u in users.all_users():
            # 받을 곳을 안 정했거나(/setup 전) 꺼둔 사람은 건너뛴다.
            # 꺼둬도 alarm_target은 남아 있어서 '/setup target:여기'로 바로 되살아난다.
            if not u.alarm_target or u.alarm_off:
                continue
            # ⚠️ 한 사람의 값이 깨졌다고 여기서 예외가 새면 alarm_loop 자체가 멈춰
            #    **전원의 알림이 조용히 끊긴다**(tasks.loop은 예외를 만나면 선다).
            #    못 읽는 사람만 빼고 나머지는 살린다 — users._to_user와 같은 원칙.
            try:
                day, at = _parse_weekly(u.weekly_at or WEEKLY_TIME)
                by_target[u.alarm_target] = Schedule(
                    target=u.alarm_target,
                    mention_id=u.alarm_mention,
                    work_start=_parse_time(u.work_start or WORK_START_TIME),
                    work_end=_parse_time(u.work_end or WORK_END_TIME),
                    weekly_day=day,
                    weekly_at=at,
                    owner=u,
                )
            except ValueError as e:
                print(f"⚠️ {u.discord_id}의 알림 설정을 못 읽어 건너뜁니다: {e} "
                      "— /setup으로 다시 넣어야 알림이 갑니다")
    return list(by_target.values())


def audit_users() -> list[dict]:
    """등록자 전원 + .env를 '알림이 갈까 안 갈까'로 한 줄씩. 기동 로그 표에 쓴다.

    load_schedules()와 따로 두는 이유:
        그쪽은 알림 루프가 1분마다 부르는 실행 경로라 **가는 것만** 돌려준다.
        그래서 안 가는 사람이 조용히 빠지고, 왜 안 가는지 알 방법이 없다.
        여기는 안 가는 사람도 이유를 달아 내놓는다.

    status: 'on'(감) | 'unset'(/setup 전) | 'off'(꺼둠) | 'error'(값이 깨짐)
    """
    rows: list[dict] = []

    def row(owner, target, work_start, work_end, weekly, off) -> dict:
        out = {
            "owner": owner,  # None이면 .env 몫
            "discord_id": getattr(owner, "discord_id", None),
            "target": target,
            "work": None,
            "weekly": None,
            "status": "on",
            "reason": None,
        }
        if not target:
            out["status"] = "unset"
            return out
        if off:
            out["status"] = "off"
        try:
            day, at = _parse_weekly(weekly)
            out["work"] = f"{_parse_time(work_start):%H:%M}~{_parse_time(work_end):%H:%M}"
            out["weekly"] = f"{_WEEKDAY_KR[day]} {at:%H:%M}"
        except ValueError as e:
            out["status"] = "error"
            out["reason"] = str(e)
        return out

    if ALARM_TARGET:
        rows.append(row(None, ALARM_TARGET, WORK_START_TIME, WORK_END_TIME, WEEKLY_TIME, False))
    if users.enabled():
        for u in users.all_users():
            rows.append(row(
                u, u.alarm_target,
                u.work_start or WORK_START_TIME, u.work_end or WORK_END_TIME,
                u.weekly_at or WEEKLY_TIME, bool(u.alarm_off),
            ))

    # ⚠️ 받을 곳이 겹치면 load_schedules()의 by_target에서 나중 것이 이겨 하나만 남는다.
    #    표가 '켜짐'이라고 해놓고 실제로 안 가면 거짓말이 되므로, 실제로 살아남은
    #    줄인지 대조해 진 쪽은 'shadowed'로 표시한다.
    live = {s.target: getattr(s.owner, "discord_id", None) for s in load_schedules()}
    for r in rows:
        if r["status"] == "on" and live.get(r["target"]) != r["discord_id"]:
            r["status"] = "shadowed"
    return rows


_STATUS_KR = {
    "on": "✅ 켜짐",
    "unset": "➖ 미설정",
    "off": "⏸ 꺼둠",
    "error": "⚠️ 오류",
    "shadowed": "🔁 덮임",
}


def describe_owner(owner) -> str:
    """발송 로그에 쓸 짧은 주인 표기. .env 몫이면 '(.env)'."""
    return "(.env)" if owner is None else f"…{owner.discord_id[-4:]}"


def _width(text: str) -> int:
    """한글은 두 칸을 먹는다. len()으로 맞추면 표가 어긋난다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def format_audit(rows: list[dict], names: dict[str, str], owner_id: str | None) -> str:
    """audit_users() 결과를 기동 로그에 찍을 표로. names는 {discord_id: 사람 이름}.

    '설정'과 '연결'을 따로 세우는 이유: 앞은 DB만 보면 알고, 뒤는 디스코드에
    물어봐야 안다. 한 칸에 뭉치면 '받겠다고 해놓고 못 받는 중'이 안 보인다.
    """
    cells = []
    for r in rows:
        uid = r["discord_id"]
        who = "(.env)" if uid is None else f"{names.get(uid, '(이름 못 찾음)')} ({uid})"
        reach = r.get("reach")
        cells.append([
            "👑" if uid and uid == owner_id else "  ",
            who,
            r.get("place") or (r["target"] or "—"),
            r["work"] or "—",
            r["weekly"] or "—",
            _STATUS_KR.get(r["status"], r["status"]) + (f" — {r['reason']}" if r["reason"] else ""),
            "—" if reach is None else ("✅ 닿음" if reach else "❌ 못 닿음"),
        ])

    head = ["", "사용자", "받을 곳", "업무", "주간", "설정", "연결"]
    widths = [max(_width(row[i]) for row in [head, *cells]) for i in range(len(head))]
    lines = ["  " + "  ".join(_pad(h, w) for h, w in zip(head, widths)).rstrip()]
    lines.append("  " + "─" * (sum(widths) + 2 * (len(widths) - 1)))
    lines += ["  " + "  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip() for row in cells]

    on = sum(1 for r in rows if r["status"] == "on")
    people = sum(1 for r in rows if r["discord_id"])
    header = f"알림 — 등록 {people}명" + (" + .env" if len(rows) > people else "") + f" · 켜짐 {on}곳"
    footer = (
        f"  .env 기본값 (시각을 안 정한 사람이 물려받음): "
        f"업무 {WORK_START_TIME}~{WORK_END_TIME} · 주간 {WEEKLY_TIME}"
    )
    return "\n".join([f"   {header}", *lines, footer])


def due_kinds(schedule: Schedule, now: datetime) -> list[str]:
    """지금 이 순간 보내야 할 알림 종류. 분 단위로 정확히 일치할 때만.

    분까지만 비교하는 이유: 루프가 1분마다 도는데 초까지 맞추면 영영 안 걸린다.
    """
    hm = (now.hour, now.minute)
    out = []
    if hm == (schedule.work_start.hour, schedule.work_start.minute):
        out.append("work_start")
    if hm == (schedule.work_end.hour, schedule.work_end.minute):
        out.append("work_end")
    if now.weekday() == schedule.weekly_day and hm == (
        schedule.weekly_at.hour,
        schedule.weekly_at.minute,
    ):
        out.append("weekly")
    return out


# --- 메시지 만들기 ---------------------------------------------------------
# 전부 하드코딩 템플릿. 도구는 부르지만 LLM은 안 부른다.


async def _blaybus_state() -> tuple[str, bool]:
    """(상태 한 줄, 지금 돌고 있나). 블레이버스를 안 쓰면 ("", False).

    blaybus_status 도구의 문장을 받아 '기록 중'을 문자열로 찾는 방법도 있지만,
    문구를 다듬는 순간 조용히 깨진다. 세션 목록이 비었는지로 직접 판단한다.
    """
    try:
        from secretary.blaybus_tools import _duration, _request
    except Exception:  # noqa: BLE001
        return "", False
    # ⚠️ 전역이 아니라 '이 알림의 주인'에게 블레이버스 계정이 있는지를 본다.
    if not context.active().blaybus_id:
        return "", False
    try:
        resp = await _request("GET", "/task-session/active")
        resp.raise_for_status()
        sessions = resp.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 상태를 못 봤어요 ({type(e).__name__})", False

    if not sessions:
        return "블레이버스는 지금 꺼져 있어요.", False
    return (
        " / ".join(
            f"'{s['taskTitle']}' ({s['workTitle']}) "
            f"{_duration(s['elapsedSeconds'])}째 기록 중"
            for s in sessions
        ),
        True,
    )


def _last_week_range(today: date) -> tuple[str, str]:
    """지난주(월~일)의 [시작, 끝) 날짜. 끝은 이번 주 월요일이라 포함하지 않는다.

    '오늘부터 7일 전'이 아니라 **주 단위**로 자른다. 월요일에 돌리면 둘이 같지만,
    WEEKLY_TIME을 금요일로 바꾸는 순간 7일 전 방식은 '지난주 금 ~ 이번주 목'이
    되어 문구와 어긋난다. 어느 요일에 돌려도 같은 지난주를 가리켜야 한다.
    """
    monday = today - timedelta(days=today.weekday())
    return (monday - timedelta(days=7)).isoformat(), monday.isoformat()


async def _last_week_summary(now: datetime) -> str:
    """지난주(월~일) 루틴 달성 요약. 조회 1회로 끝낸다."""
    start, end = _last_week_range(now.date())
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            rows = await _query_rows(
                client,
                _routine_ds(),  # ⚠️ 전역이 아니라 이 알림 주인의 루틴 DB
                {
                    "and": [
                        {"property": "날짜", "date": {"on_or_after": start}},
                        {"property": "날짜", "date": {"before": end}},
                    ]
                },
            )
    except Exception as e:  # noqa: BLE001
        return f"지난주 기록을 못 봤어요 ({type(e).__name__})"

    if not rows:
        return "지난주엔 기록된 날이 하루도 없었어요."

    total = 0
    missed = {name: 0 for name in ITEMS.values()}
    for row in rows:
        props = row["properties"]
        total += props.get("달성률", {}).get("formula", {}).get("number") or 0
        for name in ITEMS.values():
            prop = props.get(name)
            if prop and prop.get("type") == "checkbox" and not prop["checkbox"]:
                missed[name] += 1

    worst = sorted(missed.items(), key=lambda kv: -kv[1])[:3]
    worst_text = ", ".join(f"{k}({v}일 빠짐)" for k, v in worst if v)
    return (
        f"지난주는 {len(rows)}일 기록에 {total}칸 채우셨어요.\n"
        f"많이 놓친 것: {worst_text or '없어요, 훌륭하세요'}"
    )


async def build_message(kind: str, now: datetime) -> str:
    """알림 종류별 DM 본문."""
    if kind == "work_start":
        state, running = await _blaybus_state()
        nudge = (
            "이미 기록 중이시네요. 오늘도 잘 부탁드려요."
            if running
            else "시작하실 거면 '○○ 시작해줘'라고 말씀만 주세요."
        )
        return f"아가씨, {now:%H:%M} 업무 시작 시간이에요.\n{state}\n{nudge}"

    if kind == "work_end":
        state, running = await _blaybus_state()
        lines = [f"아가씨, {now:%H:%M} 업무 종료 시간이에요.", state]
        if running:
            lines.append("아직 돌고 있어요. 멈추시려면 '중지해줘'라고 하시면 돼요.")
        try:
            from secretary.blaybus_tools import blaybus_today_tasks

            if context.active().blaybus_id:
                lines.append("\n[오늘 한 일]\n" + await blaybus_today_tasks.ainvoke({}))
        except Exception:  # noqa: BLE001
            pass
        lines.append("\n" + await routine_today.ainvoke({}))
        lines.append("\n회고 남기실 거면 그냥 말씀해 주세요. 제가 적어둘게요.")
        return "\n".join(lines)

    # weekly
    return (
        f"아가씨, 새 주가 시작됐어요. ({now:%m월 %d일} "
        f"{_WEEKDAY_KR[now.weekday()]}요일)\n"
        "이번 주는 **몇 주차**예요? 알려주시면 과제 칸을 만들어 둘게요.\n\n"
        f"{await _last_week_summary(now)}"
    )


def build_alarm_loop(client):
    """1분마다 돌며 때가 된 알림을 보내는 태스크를 만들어 돌려준다.

    ⚠️ 같은 분에 두 번 보내지 않으려고 보낸 기록을 메모리에 둔다. 그래서 알림
       시각 직후에 봇을 재시작하면 한 번 더 갈 수 있다.
       드물고 피해도 DM 하나라 파일에 안 남긴다.
       사람이 늘어 성가셔지면 그때 users 테이블에 마지막 발송을 적는다.
    """
    sent: set[tuple[str, str, str]] = set()  # (날짜, 보낼 곳, 종류)

    @tasks.loop(minutes=1)
    async def alarm_loop():
        now = datetime.now(KST)
        today = now.date().isoformat()
        for schedule in load_schedules():
            for kind in due_kinds(schedule, now):
                key = (today, schedule.target, kind)
                if key in sent:
                    continue
                sent.add(key)
                # 알림 본문은 노션·블레이버스를 조회해서 만든다. 그 조회가 **이 알림의
                # 주인** 것이어야 한다 — 안 심으면 전원의 알림에 주인 데이터가 실린다.
                ctx_token = context.set_current(schedule.owner)
                who = describe_owner(schedule.owner)
                try:
                    dest, _ = await resolve_target(client, schedule.target)
                    await dest.send(schedule.prefix + await build_message(kind, now))
                    print(f"[알림 {now:%H:%M}] {kind} → {dest} ({who}) ✅")
                except Exception as e:  # noqa: BLE001 - 알림이 터져도 봇은 살아야 한다
                    # ⚠️ 예전엔 실패하면 traceback만 떠서 '안 갔다'를 알아채기 어려웠다.
                    #    성공과 같은 모양의 한 줄을 먼저 찍어 눈에 걸리게 한다.
                    print(f"[알림 {now:%H:%M}] {kind} → {schedule.target} ({who}) "
                          f"❌ {type(e).__name__}: {str(e)[:80]}")
                    traceback.print_exc()
                finally:
                    context.reset(ctx_token)
        # 어제 것까지만 들고 있으면 충분하다 (무한 증식 방지)
        for key in [k for k in sent if k[0] != today]:
            sent.discard(key)

    @alarm_loop.before_loop
    async def _wait():
        await client.wait_until_ready()

    return alarm_loop


def _selftest() -> None:
    assert _parse_time("09:00") == time(9, 0)
    assert _parse_time("7:5") == time(7, 5)
    assert _parse_weekly("MON 08:00") == (0, time(8, 0))
    assert _parse_weekly("fri 17:30") == (4, time(17, 30))
    assert _parse_weekly("08:00") == (0, time(8, 0))  # 요일 생략 → 월요일

    # 멘션 대상이 있으면 앞에 붙는다
    withm = Schedule("111", "222", time(9, 0), time(18, 0), 0, time(8, 0))
    assert withm.prefix == "<@222> "
    # 없으면 빈 문자열 (None이 아니라 — 문자열 이어붙이기에 바로 쓰인다)
    without = Schedule("111", None, time(9, 0), time(18, 0), 0, time(8, 0))
    assert without.prefix == "" and isinstance(without.prefix, str)

    s = withm
    mon = datetime(2026, 8, 3, 9, 0, tzinfo=KST)  # 월요일 09:00
    assert mon.weekday() == 0
    assert due_kinds(s, mon) == ["work_start"]
    assert due_kinds(s, mon.replace(hour=18)) == ["work_end"]
    assert due_kinds(s, mon.replace(hour=8)) == ["weekly"]
    # 1분만 어긋나도 안 걸려야 한다 (매분 중복 발송 방지의 근거)
    assert due_kinds(s, mon.replace(minute=1)) == []
    # 화요일 08:00은 주간 알림이 아니다
    assert due_kinds(s, mon.replace(day=4, hour=8)) == []
    # 반환이 list인지까지 본다 — len()만 세면 틀린 이유로 통과한다 (#8-2 함정)
    assert isinstance(due_kinds(s, mon), list)

    # --- 기동 표: 등록자 전원이 '왜 가고 왜 안 가는지'까지 보여야 한다 -----------
    # (2026-08-05: 가는 것만 찍어서 등록만 하고 /setup 안 한 사람이 안 보였다)
    import tempfile

    from cryptography.fernet import Fernet

    import secretary.users as U

    real_path, real_key, real_env = U.USERS_DB_PATH, U.CRED_KEY, ALARM_TARGET
    U.USERS_DB_PATH = tempfile.mktemp(suffix=".sqlite")
    U.CRED_KEY = Fernet.generate_key().decode()
    globals()["ALARM_TARGET"] = None
    try:
        U.save("on1", llm_provider="anthropic", llm_key="k",
               alarm_target="ch1", work_start="09:00", weekly_at="WED 07:40")
        U.save("unset", llm_provider="anthropic", llm_key="k")
        U.save("off1", llm_provider="anthropic", llm_key="k",
               alarm_target="ch2", alarm_off="1")
        U.save("bad", llm_provider="anthropic", llm_key="k",
               alarm_target="ch3", work_start="8시40분")
        rows = {r["discord_id"]: r for r in audit_users()}
        assert rows["on1"]["status"] == "on", rows["on1"]
        assert rows["on1"]["weekly"] == "수 07:40", rows["on1"]
        assert rows["unset"]["status"] == "unset" and rows["unset"]["work"] is None
        assert rows["off1"]["status"] == "off", rows["off1"]
        assert rows["bad"]["status"] == "error" and "8시40분" in rows["bad"]["reason"]
        # 안 가는 사람도 표에 남아야 한다 — load_schedules()는 이들을 버린다
        assert len(rows) == 4 and len(load_schedules()) == 1

        # 같은 곳을 두 사람이 가리키면 하나만 실제로 나간다 → 진 쪽은 '덮임'
        U.save("dup", llm_provider="anthropic", llm_key="k", alarm_target="ch1")
        got = {r["discord_id"]: r["status"] for r in audit_users()}
        assert sorted([got["on1"], got["dup"]]) == ["on", "shadowed"], got

        # 표가 그려지고 주인 줄에만 👑가 붙는다
        rows = audit_users()
        table = format_audit(rows, {"on1": "철수", "unset": "영희"}, "on1")
        assert "👑" in table and table.count("👑") == 1, table
        assert "철수 (on1)" in table and "영희 (unset)" in table, table
        assert "⚠️ 오류" in table and "➖ 미설정" in table and "⏸ 꺼둠" in table, table
        assert isinstance(table, str)

        # '설정'과 '연결'은 다른 질문이다 — 켜뒀는데 못 닿는 경우가 보여야 한다
        for r in rows:
            r["reach"] = {"on1": True, "off1": False}.get(r["discord_id"])
        table = format_audit(rows, {}, None)
        assert "✅ 닿음" in table and "❌ 못 닿음" in table, table
        # 받을 곳이 없는 사람은 물어볼 것도 없으니 '—'
        unset_line = next(ln for ln in table.splitlines() if "(unset)" in ln)
        assert unset_line.rstrip().endswith("—"), unset_line
        assert "👑" not in table  # owner_id가 None이면 아무 줄에도 안 붙는다
    finally:
        U.USERS_DB_PATH, U.CRED_KEY = real_path, real_key
        globals()["ALARM_TARGET"] = real_env

    # 한글 폭 계산 — len()으로 맞추면 표가 어긋난다
    assert _width("가나") == 4 and _width("ab") == 2 and _width("가b") == 3
    assert _pad("가", 4) == "가  " and _width(_pad("가", 4)) == 4

    # 지난주 범위는 요일과 무관하게 같아야 한다 (WEEKLY_TIME을 금요일로 바꿔도)
    assert _last_week_range(date(2026, 8, 3)) == ("2026-07-27", "2026-08-03")  # 월
    assert _last_week_range(date(2026, 8, 7)) == ("2026-07-27", "2026-08-03")  # 금
    assert _last_week_range(date(2026, 8, 9)) == ("2026-07-27", "2026-08-03")  # 일
    # 한 주 넘어가면 한 주 밀린다
    assert _last_week_range(date(2026, 8, 10)) == ("2026-08-03", "2026-08-10")
    assert isinstance(_last_week_range(date(2026, 8, 3)), tuple)

    # 시작·종료가 같은 시각이면 둘 다 걸린다 (설정 실수를 조용히 삼키지 않는다)
    same = Schedule("111", "222", time(9, 0), time(9, 0), 0, time(8, 0))
    assert due_kinds(same, mon) == ["work_start", "work_end"]
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
