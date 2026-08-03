"""블레이버스 시간기록 커스텀 도구.

왜 커스텀인가:
    블레이버스는 공식 API도 MCP 서버도 없다. 그래서 웹앱이 실제로 쓰는 엔드포인트를
    그대로 호출한다(개발자도구로 확보). notion_tools.py가 "MCP가 못 하는 것만 REST
    직접"이었다면, 여기는 처음부터 끝까지 REST 직접이다.

인증이 노션과 다른 점 (여기가 이 파일의 핵심):
    노션은 안 죽는 토큰 하나면 끝이다. 블레이버스는 access_token이 1시간짜리라
    손으로 갈아끼울 수가 없다. 그래서 아이디/비밀번호로 직접 로그인한다.
      - 쿠키 3종(access/refresh/cs)이 Set-Cookie로 내려와 클라이언트 쿠키함에 담긴다
      - cs_token만 HttpOnly가 아니다. 브라우저에서도 JS가 그 값을 읽어 x-csrf-token
        헤더에 복사해 보낸다(double-submit). 서버가 쿠키와 헤더를 대조하므로 우리도
        같은 값을 헤더에 실어야 한다
      - ⚠️ 쿠키 수명(3일)과 그 안에 든 JWT 수명(1시간)이 다르다. 쿠키가 있어도 이미
        죽은 토큰일 수 있으므로, 만료는 미리 계산하지 않고 401을 받고 나서 처리한다

아젠다 > 워크 > 태스크 3계층이고, 타이머는 태스크에만 있다.

도구 8개:
    blaybus_status()            지금 뭐가 돌고 있나
    blaybus_start(task_title)   이름으로 태스크를 찾아 시간기록 시작
    blaybus_stop()              돌고 있는 것을 멈춤 (= 태스크 완료)
    blaybus_list()              아젠다>워크>태스크 트리 보기
    blaybus_add_agenda(...)     아젠다 만들기
    blaybus_add_work(...)       워크 만들기
    blaybus_add_task(...)       태스크 만들기
    blaybus_rename(...)         이름 바꾸기 (3계층 공통)

설계 원칙 2가지 (웹에서 사용자가 직접 고친 것과 어긋나지 않으려면):
    1) 트리를 캐시하지 않는다. 도구를 부를 때마다 서버에서 새로 읽는다.
    2) 도구가 id를 인자로 받지 않는다. 이름만 받아 그 자리에서 id로 푼다.
       id를 열어두면 몇 턴 전 대화에 남은 낡은 id를 그대로 쓰게 된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.tools import tool

from secretary.config import (
    BLAYBUS_API_BASE,
    BLAYBUS_LOGIN_ID,
    BLAYBUS_PASSWORD,
    BLAYBUS_PROJECT_ID,
)

_ORIGIN_HEADERS = {
    "Origin": "https://www.blaybus.com",
    "Referer": "https://www.blaybus.com/",
    "x-client-platform": "web",
}

_client: httpx.AsyncClient | None = None


async def _login(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/auth/user/sign-in",
        json={"loginId": BLAYBUS_LOGIN_ID, "password": BLAYBUS_PASSWORD},
        headers=_ORIGIN_HEADERS,
    )
    resp.raise_for_status()  # 쿠키 3종은 client.cookies에 자동 저장된다


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """로그인 상태를 유지하며 요청한다. 토큰이 죽었으면 재로그인 후 딱 한 번 재시도.

    재시도를 1회로 못 박은 이유: 비밀번호가 틀렸거나 계정이 잠긴 경우에도 401이
    오는데, 그때 무한 재로그인하면 계정 잠금을 스스로 부른다.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BLAYBUS_API_BASE, timeout=20.0)
        await _login(_client)

    def _headers() -> dict[str, str]:
        return {**_ORIGIN_HEADERS, "x-csrf-token": _client.cookies.get("cs_token", "")}

    resp = await _client.request(method, path, headers=_headers(), **kwargs)
    if resp.status_code == 401:
        await _login(_client)
        resp = await _client.request(method, path, headers=_headers(), **kwargs)
    return resp


def _norm(text: str) -> str:
    """공백을 없애고 소문자로. ('오후 (2/2)' 를 '오후(2/2)' 로도 찾게 한다)"""
    return "".join(text.split()).lower()


def _pick_task(tasks: list[dict], title: str) -> dict | list[dict]:
    """제목으로 태스크 하나를 고른다. 후보가 여럿이면 후보 목록을 그대로 돌려준다.

    완전일치를 부분일치보다 먼저 보는 이유: '오후'라고 말했을 때 '오후 (2/2)',
    '오후 (1/2)'까지 걸려 되묻게 되면, 정확히 '오후'라는 태스크가 있는데도
    사용자를 붙잡는 꼴이 된다.
    """
    want = _norm(title)
    exact = [t for t in tasks if _norm(t["title"]) == want]
    if exact:
        return exact[0] if len(exact) == 1 else exact
    partial = [t for t in tasks if want in _norm(t["title"])]
    if len(partial) == 1:
        return partial[0]
    return partial  # 0개(못 찾음)거나 2개 이상(모호함)


def _duration(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


# ── 3계층 공통 ────────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))

_KIND_KR = {"agenda": "아젠다", "work": "워크", "task": "태스크"}

# 이름변경 경로. 셋의 생김새가 제각각이라 규칙으로 만들 수 없어 그대로 적어둔다.
# ⚠️ 'patch'는 경로 조각이지 HTTP 메서드가 아니다 — 요청은 전부 POST.
# ⚠️ 태스크만 'legacy-' 접두어가 붙고 project 순서까지 뒤바뀐다. 추론하면 404.
_RENAME_PATHS = {
    "agenda": "/project/patch/{pid}/agenda/{id}",
    "work": "/project/patch/{pid}/work/{id}",
    "task": "/legacy-project/patch/project/{pid}/task/{id}",
}


async def _tree() -> list[dict]:
    """아젠다>워크>태스크를 통째로. 이 주소 하나가 3계층을 다 준다.

    ⚠️ completed=true는 '완료된 것만'이 아니라 '완료된 것도 포함'이다.
    빼면 기본값이 completed=false라 끝난 아젠다가 통째로 안 보인다(9개→4개).
    """
    resp = await _request(
        "GET", f"/project/{BLAYBUS_PROJECT_ID}/agenda?completed=true&page=1&pageSize=100"
    )
    resp.raise_for_status()
    return resp.json()["data"]["list"]


async def _my_id() -> int:
    """태스크 생성에 필요한 assignee. 계정이 바뀌면 조용히 남의 태스크를 만들게 되므로 박아두지 않는다."""
    resp = await _request("GET", "/auth/user/info")
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def _flatten(tree: list[dict]) -> list[tuple[str, dict]]:
    """트리를 (계층, 항목) 한 줄짜리 목록으로 편다."""
    out: list[tuple[str, dict]] = []
    for agenda in tree:
        out.append(("agenda", agenda))
        for work in agenda.get("works") or []:
            out.append(("work", work))
            for task in work.get("tasks") or []:
                out.append(("task", task))
    return out


def _match(pairs: list[tuple[str, dict]], title: str) -> list[tuple[str, dict]]:
    """계층을 가리지 않고 이름으로 찾는다. 완전일치가 있으면 부분일치는 버린다."""
    want = _norm(title)
    exact = [p for p in pairs if _norm(p[1]["title"]) == want]
    return exact or [p for p in pairs if want in _norm(p[1]["title"])]


def _resolve(items: list[dict], title: str, label: str) -> tuple[dict | None, str | None]:
    """이름 하나를 고른다. 못 고르면 사용자에게 그대로 돌려줄 문장을 만들어 준다."""
    picked = _pick_task(items, title)
    if isinstance(picked, dict):
        return picked, None
    if not picked:
        names = ", ".join(f"'{i['title']}'" for i in items[:10]) or "(하나도 없어요)"
        return None, f"'{title}' {label}를 못 찾았어요. 있는 것: {names}"
    names = ", ".join(f"'{i['title']}'" for i in picked)
    return None, f"'{title}'에 해당하는 {label}가 여럿이에요: {names}. 어느 걸로 할까요?"


def _due(date: str | None) -> str:
    """목표일자. 웹앱은 '고른 날짜 + 지금 시각'을 보내고 서버가 그날 끝으로 정규화한다."""
    day = date or datetime.now(KST).strftime("%Y-%m-%d")
    return f"{day}T{datetime.now(timezone.utc):%H:%M:%S}.000Z"


@tool
async def blaybus_status() -> str:
    """블레이버스에서 지금 시간기록이 돌아가고 있는지 확인한다.

    "블레이버스 켜져 있어?", "지금 뭐 하고 있지?", "타이머 돌고 있어?" 같은 물음에 쓴다.

    Returns:
        진행 중인 태스크 제목과 경과 시간. 없으면 없다고 알려준다.
    """
    try:
        resp = await _request("GET", "/task-session/active")
        resp.raise_for_status()
        sessions = resp.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001 - 도구는 예외를 문자열로 돌려줘야 전달된다
        return f"블레이버스 상태를 못 봤어요: {type(e).__name__}: {e}"

    if not sessions:
        return "지금 블레이버스에 돌아가는 시간기록이 없어요."
    return " / ".join(
        f"'{s['taskTitle']}' ({s['workTitle']}) {_duration(s['elapsedSeconds'])}째 기록 중"
        for s in sessions
    )


@tool
async def blaybus_start(task_title: str) -> str:
    """블레이버스에서 태스크 이름으로 시간기록을 시작한다.

    "블레이버스 오후 시작해줘", "화요일 오전 켜줘" 같은 요청에 쓴다.
    이름이 애매하면 후보를 돌려주니, 사용자에게 어느 것인지 되물어라.

    Args:
        task_title: 시작할 태스크 제목. 워크나 아젠다 이름이 아니라 태스크 이름이다.

    Returns:
        처리 결과 문자열.
    """
    try:
        resp = await _request("GET", f"/project/{BLAYBUS_PROJECT_ID}/task")
        resp.raise_for_status()
        tasks = resp.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 태스크 목록을 못 봤어요: {type(e).__name__}: {e}"

    picked = _pick_task(tasks, task_title)
    if isinstance(picked, list):
        if not picked:
            titles = ", ".join(f"'{t['title']}'" for t in tasks[:10])
            return f"'{task_title}' 태스크를 못 찾았어요. 있는 것: {titles}"
        titles = ", ".join(f"'{t['title']}'" for t in picked)
        return f"'{task_title}'에 해당하는 게 여럿이에요: {titles}. 어느 걸로 할까요?"

    try:
        resp = await _request("POST", f"/task/{picked['id']}/session/start")
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"'{picked['title']}' 시작에 실패했어요: {type(e).__name__}: {e}"
    return f"블레이버스에서 '{picked['title']}' 시간기록을 시작했어요."


@tool
async def blaybus_stop() -> str:
    """블레이버스에서 돌아가고 있는 시간기록을 멈춘다.

    "블레이버스 꺼줘", "퇴근", "그만 기록해" 같은 요청에 쓴다.
    무엇이 돌고 있는지는 알아서 찾으므로 인자가 필요 없다.

    Returns:
        처리 결과 문자열.
    """
    try:
        resp = await _request("GET", "/task-session/active")
        resp.raise_for_status()
        sessions = resp.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 상태를 못 봤어요: {type(e).__name__}: {e}"

    if not sessions:
        return "지금 돌아가는 시간기록이 없어요. 멈출 게 없네요."

    stopped = []
    for s in sessions:
        try:
            resp = await _request("POST", f"/task/{s['taskId']}/session/stop")
            resp.raise_for_status()
            stopped.append(f"'{s['taskTitle']}' ({_duration(s['elapsedSeconds'])})")
        except Exception as e:  # noqa: BLE001
            return f"'{s['taskTitle']}' 중지에 실패했어요: {type(e).__name__}: {e}"
    return f"블레이버스 시간기록을 멈췄어요: {', '.join(stopped)}"


@tool
async def blaybus_list(agenda_title: str | None = None) -> str:
    """블레이버스 워크스페이스에 뭐가 있는지 아젠다>워크>태스크 구조로 보여준다.

    "블레이버스에 뭐뭐 있어?", "이번 주 아젠다 뭐야?" 같은 물음에 쓴다.
    어느 계층의 이름인지 헷갈릴 때 먼저 이걸로 확인해도 된다.

    Args:
        agenda_title: 특정 아젠다 이름. 주면 그 아젠다의 태스크까지 펼친다.
            생략하면 전체를 아젠다>워크까지만 (태스크는 개수로) 보여준다.

    Returns:
        아젠다 > 워크 > 태스크 트리.
    """
    try:
        tree = await _tree()
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 목록을 못 봤어요: {type(e).__name__}: {e}"

    if not tree:
        return "블레이버스에 아젠다가 하나도 없어요."

    if agenda_title:
        agenda, err = _resolve(tree, agenda_title, "아젠다")
        if err:
            return err
        tree = [agenda]

    lines = []
    for agenda in tree:
        lines.append(f"■ {agenda['title']}")
        for work in agenda.get("works") or []:
            done = " ✓" if work.get("status") == "Done" else ""
            tasks = work.get("tasks") or []
            # 태스크까지 다 펼치면 주차가 쌓일수록 디스코드 2000자를 넘는다.
            # 전체 보기에선 개수만, 아젠다를 콕 집었을 때만 이름을 편다.
            if agenda_title:
                lines.append(f"  └ {work['title']}{done}")
                lines += [f"      · {t['title']}" for t in tasks]
            else:
                lines.append(f"  └ {work['title']}{done} (태스크 {len(tasks)}개)")
    return "\n".join(lines)


@tool
async def blaybus_add_agenda(title: str) -> str:
    """블레이버스에 아젠다를 만든다. 아젠다는 워크와 태스크를 담는 가장 큰 덩어리다.

    "2주차 아젠다 만들어줘" 처럼 주 단위 묶음을 만들 때 쓴다.
    워크나 태스크를 만드는 도구가 아니다.

    Args:
        title: 아젠다 제목.

    Returns:
        처리 결과 문자열.
    """
    try:
        resp = await _request(
            "POST", f"/project/{BLAYBUS_PROJECT_ID}/agenda", json={"title": title}
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"아젠다 '{title}' 생성에 실패했어요: {type(e).__name__}: {e}"
    return f"블레이버스에 아젠다 '{title}'를 만들었어요."


@tool
async def blaybus_add_work(work_title: str, agenda_title: str, date: str | None = None) -> str:
    """블레이버스 아젠다 안에 워크를 만든다. 워크는 태스크를 묶는 중간 단위다.

    "1주차 아젠다에 'API 연결' 워크 추가해줘" 처럼 쓴다.

    Args:
        work_title: 만들 워크 제목.
        agenda_title: 이 워크를 넣을 아젠다 이름.
        date: 목표일자 'YYYY-MM-DD'. 생략하면 오늘.

    Returns:
        처리 결과 문자열.
    """
    try:
        tree = await _tree()
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 목록을 못 봤어요: {type(e).__name__}: {e}"

    agenda, err = _resolve(tree, agenda_title, "아젠다")
    if err:
        return err

    try:
        resp = await _request(
            "POST",
            f"/project/{BLAYBUS_PROJECT_ID}/agenda/{agenda['id']}/work",
            json={"title": work_title, "date": _due(date)},
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"워크 '{work_title}' 생성에 실패했어요: {type(e).__name__}: {e}"
    return f"아젠다 '{agenda['title']}'에 워크 '{work_title}'를 만들었어요."


@tool
async def blaybus_add_task(task_title: str, work_title: str | None = None) -> str:
    """블레이버스 워크 안에 태스크를 만든다. 태스크는 시간기록(타이머)을 다는 최소 단위다.

    "'로그인 붙이기' 태스크 만들어줘" 처럼 쓴다. 만들기만 하고 시작하지는 않는다
    (시작은 blaybus_start).

    Args:
        task_title: 만들 태스크 제목.
        work_title: 이 태스크를 넣을 워크 이름. 생략하면 워크가 하나뿐일 때만 거기에 넣고,
            여럿이면 어느 워크인지 되묻는다.

    Returns:
        처리 결과 문자열.
    """
    try:
        tree = await _tree()
        assignee = await _my_id()
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 정보를 못 봤어요: {type(e).__name__}: {e}"

    works = [(a, w) for a in tree for w in (a.get("works") or [])]
    if work_title:
        picked, err = _resolve([w for _, w in works], work_title, "워크")
        if err:
            return err
        agenda, work = next((a, w) for a, w in works if w["id"] == picked["id"])
    elif len(works) == 1:
        agenda, work = works[0]
    else:
        # 워크 이름은 프로젝트 안에서 유일하지 않다('금요일'이 여러 아젠다에 있다).
        # 그래서 소속 아젠다를 같이 보여줘야 사용자가 고를 수 있다.
        names = ", ".join(f"'{a['title']} > {w['title']}'" for a, w in works[:10]) or "(워크가 없어요)"
        return f"어느 워크에 넣을까요? 있는 워크: {names}"

    try:
        resp = await _request(
            "POST",
            f"/project/{BLAYBUS_PROJECT_ID}/task",
            json={
                "title": task_title,
                "assignee": assignee,
                "agendaId": agenda["id"],
                "workId": work["id"],
                "position": None,
            },
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"태스크 '{task_title}' 생성에 실패했어요: {type(e).__name__}: {e}"
    return f"워크 '{work['title']}'에 태스크 '{task_title}'를 만들었어요."


@tool
async def blaybus_rename(old_title: str, new_title: str, kind: str | None = None) -> str:
    """블레이버스 아젠다/워크/태스크의 이름을 바꾼다.

    "'API연결' 이름 바꿔줘" 처럼 쓴다. 사용자가 계층을 말하지 않아도 되도록
    kind 없이도 찾지만, 같은 이름이 여러 계층에 있으면 되묻는다.

    Args:
        old_title: 지금 이름.
        new_title: 바꿀 이름.
        kind: 'agenda' | 'work' | 'task'. 어느 계층인지 확실할 때만 준다.

    Returns:
        처리 결과 문자열.
    """
    if kind and kind not in _RENAME_PATHS:
        return f"kind는 agenda, work, task 중 하나여야 해요 (받은 값: '{kind}')."

    try:
        pairs = _flatten(await _tree())
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 목록을 못 봤어요: {type(e).__name__}: {e}"

    if kind:
        pairs = [p for p in pairs if p[0] == kind]
    hits = _match(pairs, old_title)

    if not hits:
        where = f"{_KIND_KR[kind]} 중에" if kind else "블레이버스에"
        return f"{where} '{old_title}'가 없어요. 이름을 다시 확인해 주세요."
    if len(hits) > 1:
        names = ", ".join(f"{_KIND_KR[k]} '{i['title']}'" for k, i in hits[:10])
        return f"'{old_title}'에 해당하는 게 여럿이에요: {names}. 어느 걸로 할까요?"

    found_kind, item = hits[0]
    path = _RENAME_PATHS[found_kind].format(pid=BLAYBUS_PROJECT_ID, id=item["id"])
    try:
        resp = await _request("POST", path, json={"title": new_title})
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"이름 변경에 실패했어요: {type(e).__name__}: {e}"
    return f"{_KIND_KR[found_kind]} '{item['title']}'를 '{new_title}'로 바꿨어요."


def _group_by_parent(items: list[dict]) -> dict[tuple[str, str], list[tuple[str, int]]]:
    """(아젠다, 워크) → [(태스크, 분), ...] 로 묶는다."""
    out: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for t in items:
        key = (
            (t.get("agenda") or {}).get("title") or "(아젠다 없음)",
            (t.get("work") or {}).get("title") or "(워크 없음)",
        )
        out.setdefault(key, []).append((t["title"], int(t.get("duration") or 0)))
    return out


@tool
async def blaybus_today_tasks(date: str = "today") -> str:
    """그날 블레이버스에서 시간을 잰 태스크를 아젠다 > 워크 > 태스크 > 시간으로 뽑는다.

    데일리루틴 '오늘 한 일' 칸을 채울 때 이걸로 먼저 뽑아서 그대로 옮겨 적으면 된다.
    "오늘 뭐 했지?", "오늘 몇 시간 일했어?" 같은 물음에도 쓴다.

    Args:
        date: 대상 날짜 YYYY-MM-DD. 기본 'today' = 오늘.

    Returns:
        아젠다 > 워크별로 묶은 태스크와 시간, 그리고 합계.
    """
    today = datetime.now(KST).strftime("%Y-%m-%d")
    day = today if date in ("", "today", None) else date
    try:
        uid = await _my_id()
        resp = await _request(
            "GET", f"/task/calendar/user?from={day}&to={day}&userId={uid}"
        )
        resp.raise_for_status()
        # ⚠️ source로 반드시 거른다. 'upcoming'(미완료)은 duration이 없는 채로
        #    조회 날짜와 무관하게 딸려 나온다 — 안 거르면 남의 날 일이 섞인다.
        done = [t for t in resp.json()["data"]["list"] if t.get("source") == "completed"]

        # 지금 돌고 있는 태스크는 아직 completed가 아니라 따로 붙여준다.
        # ⚠️ 오늘을 물었을 때만. 과거 날짜에 붙이면 그날 안 한 일이 섞이고
        #    합계까지 틀어진다.
        running = []
        if day == today:
            active = await _request("GET", "/task-session/active")
            active.raise_for_status()
            running = active.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        return f"블레이버스 조회 중 오류: {type(e).__name__}: {e}"

    if not done and not running:
        return f"{day}에 블레이버스로 시간을 잰 태스크가 없어요."

    lines: list[str] = []
    total = 0
    for (agenda, work), tasks in _group_by_parent(done).items():
        lines.append(f"{agenda} > {work}")
        for title, minutes in tasks:
            total += minutes
            lines.append(f"  · {title}  {_duration(minutes * 60)}")

    for s in running:
        secs = int(s.get("elapsedSeconds") or 0)
        total += secs // 60
        lines.append(f"  · {s['taskTitle']}  {_duration(secs)} (진행 중)")

    lines.append(f"합계 {_duration(total * 60)}")
    return "\n".join(lines)


# agent.py가 가져다 쓰는 도구 목록
BLAYBUS_TOOLS = [
    blaybus_status,
    blaybus_start,
    blaybus_stop,
    blaybus_list,
    blaybus_add_agenda,
    blaybus_add_work,
    blaybus_add_task,
    blaybus_rename,
    blaybus_today_tasks,
]


def _selftest() -> None:
    """제목 매칭이 이 파일의 유일한 판단 로직이라 여기만 점검한다."""
    tasks = [
        {"id": 1, "title": "오후"},
        {"id": 2, "title": "오후 (2/2)"},
        {"id": 3, "title": "오전"},
    ]
    assert _pick_task(tasks, "오후")["id"] == 1  # 완전일치가 부분일치를 이긴다
    assert _pick_task(tasks, "오전")["id"] == 3
    assert _pick_task(tasks, "오후(2/2)")["id"] == 2  # 공백 무시
    assert _pick_task(tasks, "없는거") == []
    # 완전일치가 없고 부분일치만 여럿이면 되물어야 한다 (dict가 아니라 list를 돌려줌)
    ambiguous = _pick_task([{"id": 4, "title": "회의 준비"}, {"id": 5, "title": "회의록"}], "회의")
    assert isinstance(ambiguous, list) and len(ambiguous) == 2

    # 3계층을 이름만으로 찾는 부분. 계층 판별을 사용자에게 안 묻기로 했으니 여기가 핵심이다.
    tree = [
        {
            "id": 100,
            "title": "1주차",
            "works": [
                {"id": 200, "title": "API 연결", "tasks": [{"id": 300, "title": "로그인 붙이기"}]},
                {"id": 201, "title": "정리", "tasks": []},
            ],
        }
    ]
    pairs = _flatten(tree)
    assert len(pairs) == 4  # 아젠다1 + 워크2 + 태스크1
    assert [k for k, _ in pairs] == ["agenda", "work", "task", "work"]

    assert _match(pairs, "1주차") == [("agenda", tree[0])]
    assert _match(pairs, "로그인 붙이기")[0][0] == "task"
    assert _match(pairs, "없는거") == []
    # 완전일치가 있으면 부분일치('API 연결하기' 같은 것)에 밀리지 않는다
    assert len(_match(pairs, "API 연결")) == 1

    # _resolve는 dict가 아니라 (항목, 에러문장) 짝을 준다. len()으로 세면 통과해버리므로 타입까지 본다.
    picked, err = _resolve([w for w in tree[0]["works"]], "정리", "워크")
    assert isinstance(picked, dict) and picked["id"] == 201 and err is None
    picked, err = _resolve(tree[0]["works"], "없는워크", "워크")
    assert picked is None and isinstance(err, str) and "못 찾았어요" in err

    assert _due("2026-08-07").startswith("2026-08-07T") and _due(None).endswith("Z")
    assert set(_RENAME_PATHS) == {"agenda", "work", "task"}

    print("selftest OK")


if __name__ == "__main__":
    _selftest()
