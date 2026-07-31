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

도구 3개:
    blaybus_status()          지금 뭐가 돌고 있나
    blaybus_start(task_title) 이름으로 태스크를 찾아 시간기록 시작
    blaybus_stop()            돌고 있는 것을 멈춤
"""

from __future__ import annotations

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


# agent.py가 가져다 쓰는 도구 목록
BLAYBUS_TOOLS = [blaybus_status, blaybus_start, blaybus_stop]


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
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
