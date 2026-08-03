"""등록할 때 쓰는 검증·탐색 도구.

왜 따로 두나:
    등록은 **아직 저장되지 않은** 자격증명으로 남의 워크스페이스를 두드려야 한다.
    notion_tools/blaybus_tools의 함수들은 전역 설정(.env)을 쓰도록 되어 있어서
    그대로는 못 쓴다. 여기 있는 것들은 전부 토큰·비밀번호를 **인자로** 받는다.
    (Phase 4에서 도구들도 이 모양으로 바뀐다)

왜 등록할 때 검증까지 하나:
    안 하면 아가씨가 오타를 내도 조용히 저장되고, 다음 알림 시각이 되어서야
    실패를 안다. 그땐 이미 한 번 놓친 뒤다. 등록 순간에 알려주는 게 낫다.
"""

from __future__ import annotations

import re

import httpx

from secretary.config import BLAYBUS_API_BASE, NOTION_API_BASE, NOTION_VERSION

# 노션 URL 어디에 있든 32자리 hex(대시 없는 UUID)를 집어낸다.
_UUID32 = re.compile(r"[0-9a-fA-F]{32}")


def extract_notion_id(url_or_id: str) -> str | None:
    """노션 URL/ID에서 32자리 id를 뽑아 대시 있는 UUID로. 못 찾으면 None.

    붙여넣는 값이 제각각이라(전체 URL, ?v=... 붙은 것, 이미 UUID인 것)
    형태를 따지지 않고 hex 덩어리만 집는다.
    """
    text = (url_or_id or "").replace("-", "")
    found = _UUID32.search(text)
    if not found:
        return None
    u = found.group(0).lower()
    return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def discover_databases(token: str, page_url: str) -> tuple[list[dict], str | None]:
    """페이지 안의 DB들을 찾아 [{title, ds_id}] 로. 실패하면 (,, 에러문장).

    아가씨의 노션은 '비서 전용 페이지' 하나에 루틴 DB와 과제 DB가 나란히 들어있다.
    그래서 **페이지 URL 하나만** 받으면 둘 다 찾을 수 있다. 사람에게
    'data source id를 알아오세요'라고 하는 건 무리다.

    ⚠️ 페이지 URL의 id는 database_id이고, 우리가 쓰는 건 data_source_id다.
       둘은 다른 값이라 한 단계 더 물어봐야 한다.
    """
    page_id = extract_notion_id(page_url)
    if not page_id:
        return [], "노션 페이지 주소에서 ID를 못 찾았어요. 페이지 링크를 복사해 주세요."

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{NOTION_API_BASE}/blocks/{page_id}/children?page_size=100",
                headers=_headers(token),
            )
            if resp.status_code == 401:
                return [], "노션 토큰이 거부됐어요(401). 토큰을 다시 확인해 주세요."
            if resp.status_code == 404:
                return [], (
                    "그 페이지를 못 봤어요(404). 노션에서 페이지 우측 상단 '...' → "
                    "'연결' 에서 통합을 초대했는지 확인해 주세요."
                )
            resp.raise_for_status()

            found = []
            for block in resp.json().get("results", []):
                if block.get("type") != "child_database":
                    continue
                title = block["child_database"].get("title") or "(제목 없음)"
                db = await client.get(
                    f"{NOTION_API_BASE}/databases/{block['id']}",
                    headers=_headers(token),
                )
                if db.status_code != 200:
                    continue
                sources = db.json().get("data_sources") or []
                if sources:
                    found.append({"title": title, "ds_id": sources[0]["id"]})
    except Exception as e:  # noqa: BLE001
        return [], f"노션을 못 봤어요: {type(e).__name__}: {e}"

    if not found:
        return [], "그 페이지 안에서 데이터베이스를 못 찾았어요."
    return found, None


def pick_databases(found: list[dict]) -> tuple[str | None, str | None]:
    """찾은 DB들에서 (루틴, 과제)를 고른다. 이름으로 짐작하고, 없으면 순서대로.

    사람마다 DB 이름이 달라서 확신할 수 없다. 그래서 고른 결과를 등록 응답에
    그대로 보여주고, 틀렸으면 /setup으로 고치게 한다.
    """
    routine = next((d["ds_id"] for d in found if "루틴" in d["title"]), None)
    homework = next((d["ds_id"] for d in found if "과제" in d["title"]), None)
    rest = [d["ds_id"] for d in found if d["ds_id"] not in (routine, homework)]
    if routine is None and rest:
        routine = rest.pop(0)
    if homework is None and rest:
        homework = rest.pop(0)
    return routine, homework


async def verify_blaybus(login_id: str, password: str) -> tuple[list[dict], str | None]:
    """블레이버스 로그인 + 프로젝트 목록. 실패하면 (,, 에러문장).

    ⚠️ 프로젝트 목록은 응답 키가 data.list가 아니라 **data.message**다.
       다른 엔드포인트와 달라서 추측하면 틀린다.
    ⚠️ 재시도하지 않는다. 비밀번호가 틀려도 401이 오는데, 여기서 되풀이하면
       남의 계정을 잠글 수 있다.
    """
    origin = {"Origin": "https://www.blaybus.com", "Referer": "https://www.blaybus.com/"}
    try:
        async with httpx.AsyncClient(base_url=BLAYBUS_API_BASE, timeout=20.0) as client:
            login = await client.post(
                "/auth/user/sign-in",
                json={"loginId": login_id, "password": password},
                headers=origin,
            )
            if login.status_code == 401:
                return [], "블레이버스 로그인이 거부됐어요. 아이디/비밀번호를 확인해 주세요."
            login.raise_for_status()

            resp = await client.get(
                "/legacy-project/my-project",
                headers={**origin, "x-csrf-token": client.cookies.get("cs_token", "")},
            )
            resp.raise_for_status()
            projects = resp.json()["data"]["message"]
    except Exception as e:  # noqa: BLE001
        return [], f"블레이버스에 못 붙었어요: {type(e).__name__}: {e}"

    if not projects:
        return [], "블레이버스에 참여 중인 프로젝트가 없어요."
    return projects, None


def _selftest() -> None:
    # 어떤 형태로 붙여넣어도 id를 집어낸다
    want = "39f0ffe9-306d-80c0-b4c7-c921a99c7a21"
    assert extract_notion_id("https://app.notion.com/p/39f0ffe9306d80c0b4c7c921a99c7a21") == want
    assert extract_notion_id("https://notion.so/x/39f0ffe9306d80c0b4c7c921a99c7a21?v=1") == want
    assert extract_notion_id(want) == want  # 이미 UUID인 것도 그대로
    assert extract_notion_id("링크가 아님") is None
    assert extract_notion_id("") is None
    assert isinstance(extract_notion_id(want), str)

    # 이름으로 짐작
    found = [
        {"title": "과제 제출 (2)", "ds_id": "hw"},
        {"title": "오프라인 데일리 루틴 (2)", "ds_id": "rt"},
    ]
    assert pick_databases(found) == ("rt", "hw")
    # 이름이 안 맞으면 순서대로 (루틴 먼저)
    plain = [{"title": "A", "ds_id": "a"}, {"title": "B", "ds_id": "b"}]
    assert pick_databases(plain) == ("a", "b")
    # 하나뿐이면 루틴만 채우고 과제는 비운다
    assert pick_databases([{"title": "A", "ds_id": "a"}]) == ("a", None)
    assert pick_databases([]) == (None, None)
    # 반환이 tuple인지까지 본다 — len()만 세면 틀린 이유로 통과한다 (#8-2 함정)
    assert isinstance(pick_databases(found), tuple)
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
