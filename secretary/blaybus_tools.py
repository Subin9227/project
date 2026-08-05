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

오류를 다루는 방식 (2026-08-04):
    서버는 거절할 때 **한국어로 이유를 정확히** 준다. raise_for_status()는 그 본문을
    버리므로, _request()가 통과 지점 한 곳에서 BlaybusError(message, code, data)로
    바꿔 던진다. 부르는 쪽 13곳에서 각자 버리던 것을 한 곳으로 모은 것이다.
    ⚠️ 태스크당 4시간(240분) 상한을 넘기면 그냥 거절하지 않고 STOP_SPLIT_REQUIRED와
       함께 **분할안(parts)까지 계산해서** 준다. 그 index를 {"selectedParts":[1,2]}로
       되돌려주면 저장된다. 조각 시간을 우리가 계산하면 서버와 어긋나므로 하지 않는다.
       분할하면 태스크가 '오전 (1/2)', '오전 (2/2)'로 쪼개진다 — 이름이 겹치는 원인.

도구 9개:
    blaybus_status()            지금 뭐가 돌고 있나
    blaybus_start(...)          이름으로 태스크를 찾아 시간기록 시작
    blaybus_stop()              돌고 있는 것을 멈춤 (= 태스크 완료)
    blaybus_list()              아젠다>워크>태스크 트리 보기
    blaybus_add_agenda(...)     아젠다 만들기
    blaybus_add_work(...)       워크 만들기
    blaybus_add_task(...)       태스크 만들기
    blaybus_rename(...)         이름 바꾸기 (3계층 공통)
    blaybus_today_tasks(...)    그날 잰 시간을 아젠다>워크>태스크>분으로

설계 원칙 3가지 (웹에서 사용자가 직접 고친 것과 어긋나지 않으려면):
    1) 트리를 캐시하지 않는다. 도구를 부를 때마다 서버에서 새로 읽는다.
    2) 도구가 id를 인자로 받지 않는다. 이름만 받아 그 자리에서 id로 푼다.
       id를 열어두면 몇 턴 전 대화에 남은 낡은 id를 그대로 쓰게 된다.
    3) ⚠️ **이름은 유일하지 않다.** 이 프로젝트는 '주차 아젠다 > 요일 워크 >
       오전/오후 태스크' 구조라 '수요일' 워크가 5개, '오전' 태스크가 11개다.
       그래서 대상을 찾는 도구는 전부 **경로(agenda_title·work_title)를 받아**
       범위를 좁힐 수 있어야 한다. 2026-08-04에 이게 없어서, 봇이 특정을 못 하자
       rename 대신 add_work로 우회해 쓰레기 워크를 만들었다.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.tools import tool

from secretary import context
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

# 사람마다 쿠키함을 따로 둔다.
# ⚠️ 예전엔 _client 하나를 전원이 공유했다. 그러면 철수의 401 재로그인이 아가씨의
#    쿠키를 덮어써서, 두 사람 요청이 서로를 로그아웃시킨다.
_clients: dict[str, httpx.AsyncClient] = {}
# ⚠️ 잠금이 없으면 같은 사람의 요청 둘이 동시에 '아직 클라이언트가 없네'를 보고
#    둘 다 로그인한다(check-then-await-then-assign 경쟁). 1인일 땐 안 보이지만
#    사람이 늘면 확정적으로 터진다. 단일 이벤트루프라 defaultdict로 충분하다.
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _creds() -> tuple[str, str | None, str | None]:
    """(쿠키함 열쇠, 아이디, 비밀번호). 열쇠는 사람마다 달라야 한다.

    ⚠️ 예전엔 `or BLAYBUS_LOGIN_ID`로 폴백했다. 그래서 블레이버스를 등록하지 않은
       사람이 "내 블레이버스 뭐야?"라고 묻자 **주인 계정의 아젠다가 그대로 나왔다**
       (2계정 테스트에서 실제 발생). 조회라 다행이었지 '시작해줘'였으면 주인
       계정에 남의 시간이 기록됐다. 1인 모드는 active()가 env_user()를 주므로
       여기서 때울 필요가 없다.
    """
    me = context.active()
    return me.discord_id, me.blaybus_id, me.blaybus_pw


def _project_id() -> str:
    return context.require(context.active().blaybus_pid, "블레이버스")


class BlaybusError(context.UserFacing):
    """서버가 요청을 거절했다. 메시지는 서버가 준 한국어 사유 그대로.

    ⚠️ 이 클래스가 생긴 이유: 예전엔 raise_for_status()가 응답 **본문을 버렸다.**
       그래서 아가씨는 "400 에러를 내면서 거부하고 있어요"만 보고 뭘 해야 할지
       알 수 없었다. 정작 서버는 "태스크 최대 기록 시간(240분)을 초과합니다.
       분할 선택이 필요합니다"라고 한국어로 정확히 알려주고 있었다.
    """

    def __init__(self, message: str, code: str | None = None, data=None):
        super().__init__(message)
        self.code = code  # 'STOP_SPLIT_REQUIRED'처럼 분기가 필요한 경우를 위해
        # 거절과 함께 오는 부가 정보. STOP_SPLIT_REQUIRED는 여기에 분할안(parts)을
        # 담아준다 — 우리가 시간을 계산하지 않고 서버가 준 것을 그대로 되돌려준다.
        self.data = data


def _server_message(resp: httpx.Response) -> tuple[str, str | None, object]:
    """오류 응답에서 (사람이 읽을 사유, code, data)를 뽑는다. JSON이 아니면 본문 앞부분.

    ⚠️ message가 문자열이 아니라 리스트로 오는 경우가 있다
       (DTO 검증 실패: ["property parts should not exist"]).
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - HTML 오류 페이지 등
        return (resp.text or "").strip()[:200] or f"HTTP {resp.status_code}", None, None

    if not isinstance(body, dict):
        return f"HTTP {resp.status_code}", None, None
    message = body.get("message")
    if isinstance(message, list):
        message = ", ".join(str(m) for m in message)
    return str(message or f"HTTP {resp.status_code}"), body.get("code"), body.get("data")


def _check(resp: httpx.Response, prefix: str = "") -> httpx.Response:
    """오류면 서버가 준 사유를 실어 던진다. 성공이면 그대로 돌려준다."""
    if resp.is_error:
        message, code, data = _server_message(resp)
        raise BlaybusError(f"{prefix}{message}", code, data)
    return resp


async def _login(client: httpx.AsyncClient, login_id: str, password: str) -> None:
    resp = await client.post(
        "/auth/user/sign-in",
        json={"loginId": login_id, "password": password},
        headers=_ORIGIN_HEADERS,
    )
    _check(resp, "블레이버스 로그인이 거부됐어요: ")  # 쿠키 3종은 자동 저장된다


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """로그인 상태를 유지하며 요청한다. 토큰이 죽었으면 재로그인 후 딱 한 번 재시도.

    재시도를 1회로 못 박은 이유: 비밀번호가 틀렸거나 계정이 잠긴 경우에도 401이
    오는데, 그때 무한 재로그인하면 계정 잠금을 스스로 부른다.
    ⚠️ 이제 남의 계정도 다루므로 이 규칙이 더 중요하다 — 되풀이하면 남의 계정을
       잠근다.

    오류는 여기서 BlaybusError로 던진다. 부르는 쪽마다 raise_for_status()를 하면
    서버가 준 사유를 13곳에서 각자 버리게 되므로, 통과 지점 한 곳에서 처리한다.
    """
    key, login_id, password = _creds()
    context.require(login_id and password, "블레이버스")

    async with _locks[key]:
        client = _clients.get(key)
        if client is None:
            client = httpx.AsyncClient(base_url=BLAYBUS_API_BASE, timeout=20.0)
            await _login(client, login_id, password)
            _clients[key] = client

    def _headers() -> dict[str, str]:
        return {**_ORIGIN_HEADERS, "x-csrf-token": client.cookies.get("cs_token", "")}

    resp = await client.request(method, path, headers=_headers(), **kwargs)
    if resp.status_code == 401:
        # 재로그인도 잠금 안에서. 밖에서 하면 같은 사람의 다른 요청이 그 사이
        # 반쯤 갱신된 쿠키를 쓴다.
        async with _locks[key]:
            await _login(client, login_id, password)
        resp = await client.request(method, path, headers=_headers(), **kwargs)
    return _check(resp)


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
        "GET", f"/project/{_project_id()}/agenda?completed=true&page=1&pageSize=100"
    )
    return resp.json()["data"]["list"]


async def _my_id() -> int:
    """태스크 생성에 필요한 assignee. 계정이 바뀌면 조용히 남의 태스크를 만들게 되므로 박아두지 않는다."""
    resp = await _request("GET", "/auth/user/info")
    return resp.json()["data"]["id"]


def _walk(tree: list[dict]) -> list[tuple[dict, dict, dict]]:
    """(아젠다, 워크, 태스크) 3단 경로를 통째로 편다. 태스크가 없는 워크는 빠진다."""
    return [
        (a, w, t)
        for a in tree
        for w in (a.get("works") or [])
        for t in (w.get("tasks") or [])
    ]


def _narrow(tree: list[dict], agenda_title: str | None) -> tuple[list[dict], str | None]:
    """아젠다 이름으로 트리를 좁힌다. 이름을 안 주면 통째로.

    ⚠️ 이 함수가 있는 이유: 이 프로젝트는 '주차 아젠다 > 요일 워크 > 오전/오후 태스크'
       구조라 **이름이 유일하지 않다**('수요일' 워크 5개, '오전' 태스크 11개).
       아젠다로 먼저 좁히지 않으면 어느 것을 말하는지 영영 특정할 수 없다.
    """
    if not agenda_title:
        return tree, None
    picked, err = _resolve(tree, agenda_title, "아젠다")
    if err:
        return [], err
    return [picked], None


def _describe(agenda: dict, work: dict | None = None) -> str:
    """되물을 때 쓸 경로 표기. '13주차 > 수요일'"""
    return f"{agenda['title']} > {work['title']}" if work else agenda["title"]


def _path_of(tree: list[dict], kind: str, item: dict) -> str:
    """트리에서 그 항목이 어디 붙어 있는지 찾아 경로 문자열로.

    되물을 때 "워크 '수요일', 워크 '수요일', …"이라고만 하면 고를 수가 없다.
    '13주차 > 수요일'까지 보여줘야 사용자가 답할 수 있다.
    """
    if kind == "agenda":
        return item["title"]
    for agenda in tree:
        for work in agenda.get("works") or []:
            if kind == "work" and work["id"] == item["id"]:
                return _describe(agenda, work)
            if kind == "task":
                for task in work.get("tasks") or []:
                    if task["id"] == item["id"]:
                        return f"{_describe(agenda, work)} > {task['title']}"
    return item["title"]  # 못 찾으면 이름만 (있을 수 없지만 방어)


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
        sessions = resp.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001 - 도구는 예외를 문자열로 돌려줘야 전달된다
        return context.as_message(e, "블레이버스 상태를 못 봤어요")

    if not sessions:
        return "지금 블레이버스에 돌아가는 시간기록이 없어요."
    return " / ".join(
        f"'{s['taskTitle']}' ({s['workTitle']}) {_duration(s['elapsedSeconds'])}째 기록 중"
        for s in sessions
    )


@tool
async def blaybus_start(
    task_title: str,
    work_title: str | None = None,
    agenda_title: str | None = None,
) -> str:
    """블레이버스에서 태스크 이름으로 시간기록을 시작한다.

    "13주차 수요일 오전 시작해줘" 처럼 쓴다.

    ⚠️ 태스크 이름은 유일하지 않다 — '오전'만 열 개가 넘는다. 여럿이라고 답이 오면
    **work_title·agenda_title을 채워 다시 불러라.**

    Args:
        task_title: 시작할 태스크 제목 (예: '오전'). 워크·아젠다 이름이 아니다.
        work_title: 그 태스크가 속한 워크 (예: '수요일').
        agenda_title: 그 워크가 속한 아젠다 (예: '13주차').

    Returns:
        처리 결과 문자열.
    """
    try:
        tree = await _tree()
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "블레이버스 태스크 목록을 못 봤어요")

    tree, err = _narrow(tree, agenda_title)
    if err:
        return err

    # 트리를 쓰는 이유: 되물을 때 '13주차 > 수요일'을 보여주려면 경로가 있어야 한다.
    # (/project/{pid}/task?agendaId=&workId= 로 좁힐 수는 있지만, 그건 id를 이미
    #  알 때 얘기다. 우리는 이름만 받으므로 어차피 트리를 한 번 훑어야 한다.)
    triples = _walk(tree)
    if work_title:
        want = _norm(work_title)
        narrowed = [x for x in triples if _norm(x[1]["title"]) == want]
        triples = narrowed or [x for x in triples if want in _norm(x[1]["title"])]

    want = _norm(task_title)
    hits = [x for x in triples if _norm(x[2]["title"]) == want]
    if not hits:
        hits = [x for x in triples if want in _norm(x[2]["title"])]

    if not hits:
        names = ", ".join(f"'{_describe(a, w)} > {t['title']}'" for a, w, t in triples[:8])
        return f"'{task_title}' 태스크를 못 찾았어요. 있는 것: {names or '(없어요)'}"

    # ⚠️ 끝난 태스크는 시작 후보가 아니다. 같은 워크 안에 같은 이름이 여러 개
    #    있는 게 정상이라('수요일'에 '오전' 3개), 이걸 안 거르면 경로를 다 줘도
    #    특정이 안 된다. 블레이버스도 이어하기는 재시작이 아니라 복제로 한다
    #    (이용 가이드 p.29 — 태스크당 최대 4시간).
    running = [x for x in hits if not x[2].get("completedDate")]
    if not running:
        where = ", ".join(f"'{_describe(a, w)}'" for a, w, _ in hits[:5])
        return (
            f"'{task_title}'는 이미 다 끝난 것뿐이에요 ({where}). "
            "새로 만들어서 시작할까요?"
        )
    hits = running

    if len(hits) > 1:
        names = ", ".join(f"'{_describe(a, w)} > {t['title']}'" for a, w, t in hits[:10])
        return (
            f"'{task_title}'에 해당하는 게 여럿이에요: {names}. "
            "어느 아젠다·워크 것인지 알려주시면 그걸 시작할게요."
        )

    agenda, work, task = hits[0]
    try:
        resp = await _request("POST", f"/task/{task['id']}/session/start")
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, f"'{task['title']}' 시작에 실패했어요")
    return f"'{_describe(agenda, work)} > {task['title']}' 시간기록을 시작했어요."


def _unique_title(existing: list[str], title: str) -> str:
    """같은 이름이 이미 있으면 '오전 (2)', '오전 (3)'으로 비켜준다.

    ⚠️ 블레이버스는 멈춘 타이머를 이어서 못 켠다. 그래서 같은 일을 다시 하려면
       같은 이름의 태스크를 또 만들 수밖에 없는데, 그러면 나중에 '오전'이 열 개가
       되어 무엇을 시작/중지할지 특정할 수 없다(2026-08-04·05 실제 사고).
       서버가 분할하며 붙이는 '(1/3)'과는 모양이 달라 섞이지 않는다.
    """
    taken = {_norm(t) for t in existing}
    if _norm(title) not in taken:
        return title
    n = 2
    while _norm(f"{title} ({n})") in taken:
        n += 1
    return f"{title} ({n})"


def _split_parts(data) -> list[dict]:
    """거절 응답에서 서버가 계산해 준 분할안을 꺼낸다. 없으면 빈 목록."""
    parts = (data or {}).get("parts") if isinstance(data, dict) else None
    return [p for p in (parts or []) if isinstance(p, dict) and "index" in p]


def _describe_parts(parts: list[dict]) -> str:
    """'240분 + 62분' 처럼 사람이 읽을 분할 모양."""
    return " + ".join(_duration(int(p.get("durationMinutes") or 0) * 60) for p in parts)


def _session_path(s: dict) -> str:
    """되물을 때 보여줄 세션 경로. '수요일 > 오전'"""
    return f"{s.get('workTitle') or '(워크 없음)'} > {s['taskTitle']}"


def _pick_sessions(
    sessions: list[dict], task_title: str | None, work_title: str | None
) -> tuple[list[dict], str | None]:
    """멈출 세션을 고른다. 못 고르면 (,, 사용자에게 그대로 돌려줄 문장).

    ⚠️ 이름은 유일하지 않다. '오전'이 두 워크에서 동시에 돌 수 있으므로,
       하나로 못 좁히면 멈추지 말고 되묻는다 — 블레이버스엔 삭제가 없어서
       잘못 멈춘 건 되돌릴 수 없다(멈춤 = 완료 처리).
    """
    hits = sessions
    if work_title:
        want = _norm(work_title)
        exact = [s for s in hits if _norm(s.get("workTitle") or "") == want]
        hits = exact or [s for s in hits if want in _norm(s.get("workTitle") or "")]
    if task_title:
        want = _norm(task_title)
        exact = [s for s in hits if _norm(s["taskTitle"]) == want]
        hits = exact or [s for s in hits if want in _norm(s["taskTitle"])]

    running = ", ".join(f"'{_session_path(s)}'" for s in sessions)
    if not hits:
        return [], f"'{task_title or work_title}'는 지금 돌고 있지 않아요. 돌고 있는 것: {running}"
    if len(hits) > 1:
        names = ", ".join(f"'{_session_path(s)}' ({_duration(s['elapsedSeconds'])})" for s in hits)
        return [], (
            f"멈출 게 여럿이에요: {names}. 어느 것인지 알려주시면 그것만 멈출게요. "
            "전부 멈추려면 그렇게 말씀해 주세요."
        )
    return hits, None


@tool
async def blaybus_stop(
    task_title: str | None = None,
    work_title: str | None = None,
    all_running: bool = False,
    confirm_split: bool = False,
) -> str:
    """블레이버스에서 돌아가고 있는 시간기록을 멈춘다.

    "블레이버스 꺼줘", "퇴근", "그만 기록해" 같은 요청에 쓴다.

    ⚠️ 블레이버스는 타이머를 **동시에 여러 개** 켤 수 있다. 그래서 무엇을 멈출지가
    중요하다 — 멈춤은 곧 완료 처리이고, 블레이버스엔 삭제도 이어하기도 없다.
    아가씨가 특정 태스크를 지목하시면(예: "오전 꺼줘", 목록의 "첫번째") 반드시
    **task_title을 채워서 불러라.** 비워두고 부르면, 하나만 돌 때는 그것을 멈추지만
    여럿이 돌면 아무것도 안 멈추고 목록을 돌려준다.
    "다 꺼줘"처럼 **전부를 분명히 원하실 때만 all_running=true**로 불러라.

    ⚠️ 태스크당 4시간이 상한이라, 넘겨서 켜둔 경우엔 여러 조각으로 나눠 저장해야
    한다. 그때 이 도구가 "이렇게 나눠 저장할까요?"라고 되돌려주니, 아가씨께
    여쭙고 **그렇다고 하시면 confirm_split=true로 다시 불러라.**

    Args:
        task_title: 멈출 태스크 제목 (예: '오전'). 지목하셨으면 반드시 채운다.
        work_title: 그 태스크가 속한 워크 (예: '수요일'). 같은 이름이 여럿일 때 좁힌다.
        all_running: 돌고 있는 것을 **전부** 멈춘다. "다 꺼줘"라고 하셨을 때만 true.
        confirm_split: 4시간을 넘긴 기록을 나눠 저장해도 된다고 확인받았을 때만 true.

    Returns:
        처리 결과 문자열.
    """
    try:
        resp = await _request("GET", "/task-session/active")
        sessions = resp.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "블레이버스 상태를 못 봤어요")

    if not sessions:
        return "지금 돌아가는 시간기록이 없어요. 멈출 게 없네요."

    if not all_running:
        sessions, err = _pick_sessions(sessions, task_title, work_title)
        if err:
            return err

    stopped = []
    for s in sessions:
        path = f"/task/{s['taskId']}/session/stop"
        try:
            await _request("POST", path)
            stopped.append(f"'{s['taskTitle']}' ({_duration(s['elapsedSeconds'])})")
        except BlaybusError as e:
            # ⚠️ 태스크당 4시간이 상한이라, 넘기면 서버가 그냥 거절하지 않고
            #    "이렇게 나눠 저장할래?"라며 분할안(parts)까지 계산해 준다.
            #    조각 시간을 우리가 계산하지 않는다 — 서버가 준 index만 되돌려준다.
            if e.code != "STOP_SPLIT_REQUIRED":
                return context.as_message(e, f"'{s['taskTitle']}' 중지에 실패했어요")

            parts = _split_parts(e.data)
            if not parts:  # 분할안을 못 읽으면 지어내지 말고 사유를 그대로 전한다
                return context.as_message(e, f"'{s['taskTitle']}' 중지에 실패했어요")

            if not confirm_split:
                # ⚠️ 쪼개진 뒤 이름('오전 (1/2)')을 미리 보여줬더니, 모델이 그걸 할 일로
                #    읽고 그 이름의 태스크를 찾아 시작하려 들었다(2026-08-04 실사용자).
                #    없으니 새로 만들자고까지 했다 — 이름은 빼고 조각 수만 말한다.
                return (
                    # _duration은 항상 '분'으로 끝나 받침이 있으므로 '이라'로 고정
                    f"'{s['taskTitle']}' 태스크가 {_duration(s['elapsedSeconds'])}이라 "
                    f"블레이버스 상한(4시간)을 넘었어요. {_describe_parts(parts)}, "
                    f"{len(parts)}조각으로 나눠서 저장해야 해요. 그렇게 할까요? "
                    "(저장한 뒤엔 웹에서 손으로만 고칠 수 있어요)"
                )

            try:
                await _request(
                    "POST", path, json={"selectedParts": [p["index"] for p in parts]}
                )
            except Exception as e2:  # noqa: BLE001
                return context.as_message(e2, f"'{s['taskTitle']}' 분할 저장에 실패했어요")
            stopped.append(
                f"'{s['taskTitle']}' ({_describe_parts(parts)}, {len(parts)}조각으로 나눠 저장)"
            )
        except Exception as e:  # noqa: BLE001
            return context.as_message(e, f"'{s['taskTitle']}' 중지에 실패했어요")
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
        return context.as_message(e, "블레이버스 목록을 못 봤어요")

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
            "POST", f"/project/{_project_id()}/agenda", json={"title": title}
        )
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, f"아젠다 '{title}' 생성에 실패했어요")
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
        return context.as_message(e, "블레이버스 목록을 못 봤어요")

    agenda, err = _resolve(tree, agenda_title, "아젠다")
    if err:
        return err

    title = _unique_title([w["title"] for w in (agenda.get("works") or [])], work_title)
    try:
        resp = await _request(
            "POST",
            f"/project/{_project_id()}/agenda/{agenda['id']}/work",
            json={"title": title, "date": _due(date)},
        )
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, f"워크 '{work_title}' 생성에 실패했어요")
    renamed = f" (같은 이름이 있어서 '{title}'로 만들었어요)" if title != work_title else ""
    return f"아젠다 '{agenda['title']}'에 워크 '{title}'를 만들었어요.{renamed}"


@tool
async def blaybus_add_task(
    task_title: str,
    work_title: str | None = None,
    agenda_title: str | None = None,
) -> str:
    """블레이버스 워크 안에 태스크를 만든다. 태스크는 시간기록(타이머)을 다는 최소 단위다.

    "13주차 수요일에 '오전' 태스크 만들어줘" 처럼 쓴다. 만들기만 하고 시작하지는
    않는다 (시작은 blaybus_start).

    ⚠️ 워크 이름은 유일하지 않다 — '수요일' 워크가 주차마다 있다. 되물으면 사용자가
    알려준 아젠다를 **agenda_title에 넣어 다시 불러라.** 그래야 특정이 된다.

    Args:
        task_title: 만들 태스크 제목.
        work_title: 넣을 워크 이름 (예: '수요일').
        agenda_title: 그 워크가 속한 아젠다 (예: '13주차'). 같은 이름의 워크가
            여러 아젠다에 있을 때 이걸로 좁힌다.

    Returns:
        처리 결과 문자열.
    """
    try:
        tree = await _tree()
        assignee = await _my_id()
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "블레이버스 정보를 못 봤어요")

    tree, err = _narrow(tree, agenda_title)
    if err:
        return err

    works = [(a, w) for a in tree for w in (a.get("works") or [])]
    if work_title:
        matched = [(a, w) for a, w in works if _norm(w["title"]) == _norm(work_title)]
        if not matched:  # 완전일치가 없으면 부분일치까지
            matched = [(a, w) for a, w in works if _norm(work_title) in _norm(w["title"])]
        if not matched:
            names = ", ".join(f"'{_describe(a, w)}'" for a, w in works[:10]) or "(없어요)"
            return f"'{work_title}' 워크를 못 찾았어요. 있는 것: {names}"
        if len(matched) > 1:
            names = ", ".join(f"'{_describe(a, w)}'" for a, w in matched)
            return (
                f"'{work_title}' 워크가 여럿이에요: {names}. "
                "어느 아젠다 것인지 알려주시면 거기에 넣을게요."
            )
        agenda, work = matched[0]
    elif len(works) == 1:
        agenda, work = works[0]
    else:
        names = ", ".join(f"'{_describe(a, w)}'" for a, w in works[:10]) or "(워크가 없어요)"
        return f"어느 워크에 넣을까요? 있는 워크: {names}"

    # 같은 워크에 같은 이름이 있으면 '오전 (2)'로 비켜준다. 멈춘 타이머를 이어서
    # 못 켜는 구조라 같은 이름이 계속 쌓이는데, 그러면 무엇을 시작/중지할지 못 고른다.
    title = _unique_title([t["title"] for t in (work.get("tasks") or [])], task_title)
    try:
        resp = await _request(
            "POST",
            f"/project/{_project_id()}/task",
            json={
                "title": title,
                "assignee": assignee,
                "agendaId": agenda["id"],
                "workId": work["id"],
                "position": None,
            },
        )
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, f"태스크 '{task_title}' 생성에 실패했어요")
    renamed = f" (같은 이름이 있어서 '{title}'로 만들었어요)" if title != task_title else ""
    return f"'{_describe(agenda, work)}'에 태스크 '{title}'를 만들었어요.{renamed}"


@tool
async def blaybus_rename(
    old_title: str,
    new_title: str,
    kind: str | None = None,
    agenda_title: str | None = None,
    work_title: str | None = None,
) -> str:
    """블레이버스 아젠다/워크/태스크의 **이름만** 바꾼다. 새로 만들지 않는다.

    "13주차 수요일 워크를 화요일로 바꿔줘" 처럼 쓴다.

    ⚠️ 이름이 유일하지 않다 — '수요일' 워크가 주차마다 있다. 여럿이라고 답이 오면
    **agenda_title에 아젠다를 넣어 다시 불러라.** 절대 blaybus_add_work로
    새로 만들어 우회하지 마라 — 쓰레기 워크가 쌓인다.

    Args:
        old_title: 지금 이름.
        new_title: 바꿀 이름.
        kind: 'agenda' | 'work' | 'task'. 어느 계층인지 확실할 때만.
        agenda_title: 대상이 속한 아젠다 (예: '13주차'). 같은 이름이 여러 곳에
            있을 때 이걸로 좁힌다.
        work_title: 태스크 이름을 바꿀 때, 그 태스크가 속한 워크 (예: '수요일').

    Returns:
        처리 결과 문자열.
    """
    if kind and kind not in _RENAME_PATHS:
        return f"kind는 agenda, work, task 중 하나여야 해요 (받은 값: '{kind}')."

    try:
        tree = await _tree()
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "블레이버스 목록을 못 봤어요")

    tree, err = _narrow(tree, agenda_title)
    if err:
        return err

    pairs = _flatten(tree)
    # 워크를 지정하면 그 워크와 그 아래 태스크만 남긴다 (아젠다는 범위 밖).
    if work_title:
        want = _norm(work_title)
        pairs = [
            p
            for p in pairs
            if (p[0] == "work" and _norm(p[1]["title"]) == want)
            or (
                p[0] == "task"
                and any(
                    _norm(w["title"]) == want and p[1] in (w.get("tasks") or [])
                    for a in tree
                    for w in (a.get("works") or [])
                )
            )
        ]
    if kind:
        pairs = [p for p in pairs if p[0] == kind]
    hits = _match(pairs, old_title)

    if not hits:
        where = f"{_KIND_KR[kind]} 중에" if kind else "블레이버스에"
        scope = f" ('{agenda_title}' 안에서 찾았어요)" if agenda_title else ""
        return f"{where} '{old_title}'가 없어요{scope}. 이름을 다시 확인해 주세요."
    if len(hits) > 1:
        # ⚠️ 이름만 나열하면 "워크 '수요일', 워크 '수요일', …"이 되어 고를 수가 없다.
        #    어느 아젠다 밑인지 보여야 사용자가 답할 수 있다.
        names = ", ".join(f"{_KIND_KR[k]} '{_path_of(tree, k, i)}'" for k, i in hits[:10])
        return (
            f"'{old_title}'에 해당하는 게 여럿이에요: {names}. "
            "어느 아젠다 것인지 알려주시면 그것만 바꿀게요."
        )

    found_kind, item = hits[0]
    path = _RENAME_PATHS[found_kind].format(pid=_project_id(), id=item["id"])
    try:
        resp = await _request("POST", path, json={"title": new_title})
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "이름 변경에 실패했어요")
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
        # ⚠️ source로 반드시 거른다. 'upcoming'(미완료)은 duration이 없는 채로
        #    조회 날짜와 무관하게 딸려 나온다 — 안 거르면 남의 날 일이 섞인다.
        done = [t for t in resp.json()["data"]["list"] if t.get("source") == "completed"]

        # 지금 돌고 있는 태스크는 아직 completed가 아니라 따로 붙여준다.
        # ⚠️ 오늘을 물었을 때만. 과거 날짜에 붙이면 그날 안 한 일이 섞이고
        #    합계까지 틀어진다.
        running = []
        if day == today:
            active = await _request("GET", "/task-session/active")
            running = active.json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "블레이버스 조회 중 오류")

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

    # --- 이름이 겹치는 실제 구조 ---------------------------------------------
    # ⚠️ 위 트리는 이름이 전부 유일해서 2026-08-04에 터진 버그를 못 잡았다.
    #    실제 데이터는 '수요일' 워크 5개, '오전' 태스크 11개다. 그대로 흉내낸다.
    dup = [
        {
            "id": 1, "title": "12주차",
            "works": [{"id": 10, "title": "수요일",
                       "tasks": [{"id": 100, "title": "오전"}]}],
        },
        {
            "id": 2, "title": "13주차",
            "works": [{"id": 20, "title": "수요일",
                       "tasks": [{"id": 200, "title": "오전"}]}],
        },
    ]

    # 아젠다를 안 주면 트리 전체, 주면 그 아젠다만
    narrowed, err = _narrow(dup, None)
    assert narrowed == dup and err is None
    narrowed, err = _narrow(dup, "13주차")
    assert err is None and len(narrowed) == 1 and narrowed[0]["id"] == 2
    # 없는 아젠다는 빈 목록 + 안내문 (조용히 전체를 쓰면 남의 주차를 건드린다)
    narrowed, err = _narrow(dup, "99주차")
    assert narrowed == [] and isinstance(err, str) and "못 찾았어요" in err

    # 좁히기 전엔 '수요일'이 둘, 좁힌 뒤엔 하나 — 이게 되물음이 풀리는 지점이다
    def _works_named(t, name):
        return [w for a in t for w in a["works"] if w["title"] == name]

    assert len(_works_named(dup, "수요일")) == 2
    assert len(_works_named(_narrow(dup, "13주차")[0], "수요일")) == 1

    # 3단 경로가 태스크마다 온전히 나와야 되물을 때 '13주차 > 수요일 > 오전'을 보여준다
    triples = _walk(dup)
    assert len(triples) == 2
    assert all(len(x) == 3 for x in triples)
    assert {_describe(a, w) for a, w, _ in triples} == {"12주차 > 수요일", "13주차 > 수요일"}
    assert _describe(dup[1]) == "13주차"  # 워크 없이 아젠다만
    assert isinstance(_describe(dup[1], dup[1]["works"][0]), str)

    # 태스크가 없는 워크는 _walk에서 빠진다 (시작할 게 없으니 후보도 아니다)
    assert _walk([{"id": 3, "title": "빈주차", "works": [{"id": 30, "title": "월", "tasks": []}]}]) == []

    # 되물을 때 경로가 나와야 고를 수 있다 ("워크 '수요일'"만 반복되면 무의미)
    w13 = dup[1]["works"][0]
    assert _path_of(dup, "work", w13) == "13주차 > 수요일"
    assert _path_of(dup, "task", dup[1]["works"][0]["tasks"][0]) == "13주차 > 수요일 > 오전"
    assert _path_of(dup, "agenda", dup[1]) == "13주차"
    # 트리에 없는 것도 터지지 않고 이름만 돌려준다
    assert _path_of(dup, "work", {"id": 999, "title": "없는워크"}) == "없는워크"
    assert isinstance(_path_of(dup, "work", w13), str)

    assert _due("2026-08-07").startswith("2026-08-07T") and _due(None).endswith("Z")
    assert set(_RENAME_PATHS) == {"agenda", "work", "task"}

    # --- 서버가 준 사유를 버리지 않는가 (2026-08-04 실측 응답 그대로) -----------
    # 예전엔 raise_for_status()가 본문을 버려서 "400 에러"만 남았다.
    split = httpx.Response(
        400,
        json={
            "message": "프로젝트의 태스크 최대 기록 시간(240분)을 초과합니다. 분할 선택이 필요합니다.",
            "code": "STOP_SPLIT_REQUIRED",
            "data": {"capMinutes": 240, "currentMinutes": 302},
        },
    )
    message, code, data = _server_message(split)
    assert "240분" in message and code == "STOP_SPLIT_REQUIRED", (message, code)
    assert data["currentMinutes"] == 302

    # message가 리스트로 오는 경우(DTO 검증 실패)도 문장으로 합쳐야 한다
    listy = httpx.Response(400, json={"message": ["property parts should not exist"], "code": "002"})
    assert _server_message(listy)[0] == "property parts should not exist"

    # JSON이 아니면(HTML 오류 페이지 등) 터지지 말고 본문 앞부분을 준다
    assert "HTTP 502" in _server_message(httpx.Response(502, text=""))[0]
    assert _server_message(httpx.Response(500, text="<html>oops</html>"))[0] == "<html>oops</html>"

    # _check: 성공은 그대로 통과, 실패는 BlaybusError로 던진다
    ok = httpx.Response(200, json={"data": {}})
    assert _check(ok) is ok
    try:
        _check(split)
        raise AssertionError("400인데 통과했다")
    except BlaybusError as e:
        assert e.code == "STOP_SPLIT_REQUIRED"
        # ⚠️ 예외 타입 이름이 아가씨께 새면 안 된다 — UserFacing이라 그대로 나가야 한다
        shown = context.as_message(e, "중지에 실패했어요")
        assert shown.startswith("프로젝트의 태스크") and "BlaybusError" not in shown, shown
    # 로그인 실패는 접두사가 붙는다 (어느 단계에서 막혔는지 알아야 한다)
    try:
        _check(httpx.Response(401, json={"message": "비밀번호가 틀렸습니다"}), "블레이버스 로그인이 거부됐어요: ")
        raise AssertionError("401인데 통과했다")
    except BlaybusError as e:
        assert str(e) == "블레이버스 로그인이 거부됐어요: 비밀번호가 틀렸습니다"

    # --- 분할 저장 (2026-08-04 웹앱 cURL: {"selectedParts":[1,2]}) --------------
    # ⚠️ 조각 시간을 우리가 계산하면 서버와 어긋난다. index만 되돌려주는지 본다.
    real_parts = [
        {"index": 1, "startAt": "2026-08-04T00:15:26.000Z", "durationMinutes": 240},
        {"index": 2, "startAt": "2026-08-04T04:15:26.000Z", "durationMinutes": 62},
    ]
    parts = _split_parts({"capMinutes": 240, "currentMinutes": 302, "parts": real_parts})
    assert [p["index"] for p in parts] == [1, 2]
    assert _describe_parts(parts) == "4시간 0분 + 1시간 2분", _describe_parts(parts)

    # 분할안이 없거나 모양이 다르면 빈 목록 — 지어내지 않는다 (없으면 사유를 그대로 전한다)
    assert _split_parts(None) == [] and _split_parts({}) == []
    assert _split_parts({"parts": None}) == []
    assert _split_parts({"parts": [{"noIndex": 1}]}) == []
    assert isinstance(_split_parts({"parts": real_parts}), list)

    # 확인 전에는 저장하면 안 된다 — 모델이 보는 스키마에서 기본값이 False여야
    # 그냥 "멈춰줘"에 한 번 여쭙는 단계를 거친다.
    props = blaybus_stop.args_schema.model_json_schema()["properties"]
    assert props["confirm_split"].get("default") is False, props
    # 전부 끄기도 기본값이 False여야 한다. 되돌릴 수 없는 동작이 기본이면 안 된다.
    assert props["all_running"].get("default") is False, props

    # --- 같은 이름이 쌓이는 것 막기 ('오전' → '오전 (2)') ---------------------
    assert _unique_title([], "오전") == "오전"
    assert _unique_title(["오후"], "오전") == "오전"
    assert _unique_title(["오전"], "오전") == "오전 (2)"
    assert _unique_title(["오전", "오전 (2)"], "오전") == "오전 (3)"
    assert _unique_title(["오전", "오전 (3)"], "오전") == "오전 (2)"  # 빈 번호를 먼저 쓴다
    assert _unique_title(["오전(2)"], "오전 (2)") == "오전 (2) (2)"  # 공백 무시해 같은 이름으로 본다
    # 서버가 분할하며 붙이는 '(1/3)'과는 모양이 달라 섞이지 않는다
    assert _unique_title(["오전 (1/3)"], "오전") == "오전"
    assert isinstance(_unique_title(["오전"], "오전"), str)

    # --- 여러 개가 돌 때 무엇을 멈출지 (2026-08-05: 하나만 끄랬는데 둘 다 껐다) ---
    two = [
        {"taskId": 1, "taskTitle": "4시간 이상 테스트", "workTitle": "8월4일 테스트", "elapsedSeconds": 43020},
        {"taskId": 2, "taskTitle": "오전", "workTitle": "수요일", "elapsedSeconds": 60},
    ]
    # 지목 없이 여럿 → 아무것도 안 고르고 되묻는다
    picked, err = _pick_sessions(two, None, None)
    assert picked == [] and "여럿이에요" in err, (picked, err)
    assert "8월4일 테스트 > 4시간 이상 테스트" in err and "수요일 > 오전" in err, err
    # 이름을 주면 그것만
    picked, err = _pick_sessions(two, "오전", None)
    assert err is None and [s["taskId"] for s in picked] == [2], (picked, err)
    picked, err = _pick_sessions(two, "4시간 이상 테스트", None)
    assert err is None and [s["taskId"] for s in picked] == [1], (picked, err)
    # 워크로도 좁혀진다
    picked, err = _pick_sessions(two, None, "수요일")
    assert err is None and [s["taskId"] for s in picked] == [2], (picked, err)
    # 안 돌고 있는 이름 → 멈추지 말고 알린다
    picked, err = _pick_sessions(two, "없는거", None)
    assert picked == [] and "돌고 있지 않아요" in err, err
    # 하나만 돌면 지목 없이도 그것을 멈춘다 ('퇴근' 한 마디)
    picked, err = _pick_sessions(two[:1], None, None)
    assert err is None and len(picked) == 1
    # 같은 이름이 두 워크에서 동시에 돌면 되묻는다
    dup_run = [dict(two[1]), {**two[1], "taskId": 3, "workTitle": "목요일"}]
    picked, err = _pick_sessions(dup_run, "오전", None)
    assert picked == [] and "여럿이에요" in err, err
    picked, err = _pick_sessions(dup_run, "오전", "목요일")
    assert err is None and [s["taskId"] for s in picked] == [3], (picked, err)

    # blaybus_stop 흐름을 가짜 서버로 통째로 확인한다.
    # ⚠️ 실 계정으로 시험하면 4시간짜리 세션을 만들어야 하고, 블레이버스엔 삭제가
    #    없어서 잔여물이 영구히 남는다. 그래서 _request만 갈아끼워 확인한다.
    global _request

    real_request, sent = _request, []

    async def _fake(method, path, **kwargs):
        body = kwargs.get("json")
        sent.append((method, path, body))
        if path == "/task-session/active":
            # ⚠️ 두 개를 돌린다. 예전 셀프테스트는 하나만 써서 '하나만 끄랬는데 둘 다
            #    꺼지는' 버그를 구조적으로 못 잡았다(2026-08-05 실사용자).
            return httpx.Response(200, json={"data": {"list": [
                {"taskId": 1, "taskTitle": "오전", "workTitle": "수요일", "elapsedSeconds": 18130},
                {"taskId": 2, "taskTitle": "회의", "workTitle": "수요일", "elapsedSeconds": 60},
            ]}})
        if body is None:  # 확인 없는 stop → 서버가 분할을 요구한다
            return _check(httpx.Response(400, json={
                "message": "프로젝트의 태스크 최대 기록 시간(240분)을 초과합니다.",
                "code": "STOP_SPLIT_REQUIRED",
                "data": {"capMinutes": 240, "currentMinutes": 302, "parts": real_parts},
            }))
        return httpx.Response(200, json={"data": {}})

    _request = _fake
    try:
        # ① 둘이 도는데 지목이 없으면 **아무것도 멈추지 않는다**
        msg = asyncio.run(blaybus_stop.ainvoke({}))
        assert "여럿이에요" in msg, msg
        assert not [p for _, p, _ in sent if p.endswith("/stop")], "지목 없이 멈췄다"

        # ② 이름을 주면 그것만 (여기선 4시간 초과라 확인부터 받는다)
        sent.clear()
        msg = asyncio.run(blaybus_stop.ainvoke({"task_title": "오전"}))
        assert "나눠서 저장" in msg and "4시간 0분 + 1시간 2분" in msg, msg
        # 쪼개진 뒤 이름을 흘리면 모델이 그 이름의 태스크를 찾아 시작하려 든다
        assert "(1/2)" not in msg and "(2/2)" not in msg, msg
        assert all(body is None for _, _, body in sent), "확인 전에 뭔가를 저장했다"
        assert not any(p.endswith("/task/2/session/stop") for _, p, _ in sent), "'회의'를 건드렸다"

        # ③ 확인받은 뒤 → 서버가 준 index를 그대로 되돌려준다. 지목한 것만.
        sent.clear()
        msg = asyncio.run(blaybus_stop.ainvoke({"task_title": "오전", "confirm_split": True}))
        assert sent[-1] == ("POST", "/task/1/session/stop", {"selectedParts": [1, 2]}), sent[-1]
        assert "나눠 저장" in msg, msg
        assert not any(p.endswith("/task/2/session/stop") for _, p, _ in sent), "'회의'를 건드렸다"

        # ④ "다 꺼줘" → 둘 다 멈춘다
        sent.clear()
        msg = asyncio.run(blaybus_stop.ainvoke({"all_running": True, "confirm_split": True}))
        stopped_ids = {p for _, p, _ in sent if p.endswith("/stop")}
        assert stopped_ids == {"/task/1/session/stop", "/task/2/session/stop"}, stopped_ids
        assert "오전" in msg and "회의" in msg, msg
    finally:
        _request = real_request

    print("selftest OK")


if __name__ == "__main__":
    _selftest()
