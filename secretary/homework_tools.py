"""노션 '과제 제출' DB 도구.

루틴 DB와 같은 페이지에 있는 별개 DB다. 주차별 1행.

역할 분담이 핵심:
    봇은 **틀을 깔고 상태를 옮기는 것**까지만 한다.
    '과제 결과'·'회고' 같은 서술 칸은 아가씨가 준 글을 그대로 옮겨 적을 뿐,
    내용을 지어내지 않는다. '승인'·'반려'는 강사가 정하는 값이라 아예 안 건드린다.

도구 4개:
    homework_status(week)              어떤 주차가 어떤 상태인지
    homework_create(week)              템플릿 행 만들고 '진행중'
    homework_write(week, section, ...)  '과제 결과'·'회고' 칸 채우기
    homework_complete(week)            '진행완료'로 (되물어 확인한 뒤에만)
"""

from __future__ import annotations

import httpx
from langchain_core.tools import tool

from secretary import context
from secretary.config import KST, NOTION_API_BASE, NOTION_HOMEWORK_DS_ID
from secretary.notion_tools import (
    _copy_template,
    _find_heading_block,
    _headers,
    _list_headings,
    _query_rows,
)
from datetime import datetime

# '과제 상황' select 값. 이 DB에 실제로 있는 5개.
# ⚠️ '승인'·'반려'는 강사가 정한다 — 봇이 쓰면 사실과 달라지므로 도구로 열지 않는다.
STATUS_DOING = "진행중"
STATUS_DONE = "진행완료"


def _homework_ds() -> str:
    """지금 요청 주인의 과제 DB. 없으면 안내(남의 DB로 때우지 않는다)."""
    return context.require(context.active().homework_ds_id, "과제 노션")


def _week_str(week: str | int) -> str:
    """'12주차'·'12'·12 → '12'. 노션 select 옵션이 숫자 문자열이라 맞춘다."""
    text = str(week).strip()
    digits = "".join(c for c in text if c.isdigit())
    return digits or text


def _nice_error(e: httpx.HTTPStatusError) -> str:
    """노션 400을 사람 말로 바꾼다.

    '주차'는 미리 만들어둔 select 옵션만 받는다. 없는 값을 주면 조회조차 400이라,
    원시 JSON을 그대로 보여주면 아가씨가 뭘 고쳐야 할지 알 수 없다.
    """
    # 응답 본문을 문자열로 자르면 escape(\")가 남아 지저분해진다. JSON으로 꺼낸다.
    try:
        message = e.response.json().get("message", "")
    except ValueError:
        message = e.response.text
    if "not found for property" in message:
        opts = message.split("Available options:")[-1].strip().rstrip(".")
        return f"노션 '주차' 선택지에 없는 값이에요. 가능한 값: {opts}"
    return f"노션 처리 중 오류 ({e.response.status_code}): {message[:200]}"


async def _find_week_row(client: httpx.AsyncClient, week: str) -> dict | None:
    rows = await _query_rows(
        client, _homework_ds(), {"property": "주차", "select": {"equals": week}}
    )
    return rows[0] if rows else None


def _row_summary(row: dict) -> str:
    props = row["properties"]
    week = (props.get("주차", {}).get("select") or {}).get("name") or "?"
    status = (props.get("과제 상황", {}).get("select") or {}).get("name") or "상태 없음"
    title_rt = props.get("제목", {}).get("title") or []
    title = "".join(rt.get("plain_text", "") for rt in title_rt) or "(제목 없음)"
    return f"{week}주차 — {status} — {title}"


@tool
async def homework_status(week: str = "") -> str:
    """**노션** 과제 제출 DB에서 제출 현황을 본다.

    "이번 주 과제 냈나?", "과제 어디까지 했지?" 같은 물음에 쓴다.

    Args:
        week: 주차. '12' 또는 '12주차'. 비우면 전체 목록.

    Returns:
        주차별 상태 요약.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if week:
                w = _week_str(week)
                row = await _find_week_row(client, w)
                if row is None:
                    return f"{w}주차 과제 행이 아직 없어요."
                return _row_summary(row)

            resp = await client.post(
                f"{NOTION_API_BASE}/data_sources/{_homework_ds()}/query",
                headers=_headers(),
                json={},
            )
            resp.raise_for_status()
            rows = resp.json().get("results", [])
            if not rows:
                return "과제 제출 DB가 비어 있어요."
            return "\n".join(_row_summary(r) for r in rows)
    except httpx.HTTPStatusError as e:
        return _nice_error(e)
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


@tool
async def homework_create(week: str, title: str = "") -> str:
    """**노션** 과제 제출 DB에 그 주차 행을 템플릿대로 만들고 '진행중'으로 표시한다.

    ⚠️ 같은 주차 행이 이미 있으면 만들지 않는다 (빈 껍데기가 쌓이는 걸 막는다).

    Args:
        week: 주차. '12' 또는 '12주차'.
        title: 제목. 비우면 'N주차 과제'.

    Returns:
        처리 결과 문자열.
    """
    w = _week_str(week)
    if not w.isdigit():
        return f"'{week}'에서 주차 숫자를 못 읽었어요. '12주차'처럼 알려주세요."

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            existing = await _find_week_row(client, w)
            if existing:
                return f"{w}주차 행은 이미 있어요 → {_row_summary(existing)}"

            create = await client.post(
                f"{NOTION_API_BASE}/pages",
                headers=_headers(),
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": _homework_ds(),
                    },
                    "properties": {
                        "제목": {"title": [{"text": {"content": title or f"{w}주차 과제"}}]},
                        "주차": {"select": {"name": w}},
                        "과제 상황": {"select": {"name": STATUS_DOING}},
                        "작성 시각": {"date": {"start": datetime.now(KST).isoformat()}},
                    },
                },
            )
            create.raise_for_status()
            page_id = create.json()["id"]
            blocks = await _copy_template(client, _homework_ds(), page_id)
        return (
            f"{w}주차 과제 행을 만들고 '{STATUS_DOING}'으로 뒀어요. "
            f"(템플릿 {blocks}칸)"
        )
    except httpx.HTTPStatusError as e:
        return _nice_error(e)
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


@tool
async def homework_write(week: str, section: str, text: str) -> str:
    """**노션** 과제 행의 특정 칸(헤딩) 아래에 아가씨가 준 글을 그대로 옮겨 적는다.

    ⚠️ 내용을 지어내지 마라. 아가씨가 준 글만 옮긴다.
    칸 이름은 템플릿에 있는 그대로 — 예: '과제 결과', '회고', '상황 한줄 정리'.
    이름을 모르면 틀린 이름으로 한 번 불러라. 있는 칸을 알려준다.

    Args:
        week: 주차. '12' 또는 '12주차'.
        section: 글을 넣을 칸(헤딩) 이름.
        text: 넣을 내용. 줄바꿈이 있으면 문단이 나뉜다.

    Returns:
        처리 결과 문자열.
    """
    w = _week_str(week)
    body = text.strip()
    if not body:
        return "적을 내용이 비어 있어요, 아가씨."

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            row = await _find_week_row(client, w)
            if row is None:
                return f"{w}주차 행이 없어요. homework_create로 먼저 만들어야 해요."
            page_id = row["id"]

            heading_id = await _find_heading_block(client, page_id, section)
            if heading_id is None:
                found = ", ".join(await _list_headings(client, page_id)) or "(하나도 없어요)"
                return f"'{section}' 칸을 못 찾았어요. 있는 칸: {found}"

            paragraphs = [
                {"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": ln}}]}}
                for ln in body.splitlines()
                if ln.strip()
            ]
            resp = await client.patch(
                f"{NOTION_API_BASE}/blocks/{page_id}/children",
                headers=_headers(),
                json={"after": heading_id, "children": paragraphs},
            )
            resp.raise_for_status()
        return f"{w}주차 과제 '{section}'에 적었어요."
    except httpx.HTTPStatusError as e:
        return _nice_error(e)
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


@tool
async def homework_complete(week: str) -> str:
    """**노션** 과제를 그 주차만 '진행완료'로 바꾼다.

    ⚠️ 반드시 아가씨에게 "정말 다 끝난 거예요?"라고 한 번 더 물어보고,
    그렇다는 답을 들은 뒤에만 호출해라. 되돌리려면 노션에서 손으로 고쳐야 한다.
    ⚠️ '승인'·'반려'는 강사가 정하는 값이라 이 도구로 못 바꾼다.

    Args:
        week: 주차. '12' 또는 '12주차'.

    Returns:
        처리 결과 문자열.
    """
    w = _week_str(week)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            row = await _find_week_row(client, w)
            if row is None:
                return f"{w}주차 행이 없어요."

            resp = await client.patch(
                f"{NOTION_API_BASE}/pages/{row['id']}",
                headers=_headers(),
                json={
                    "properties": {
                        "과제 상황": {"select": {"name": STATUS_DONE}},
                        "모든 과제 제출 시각": {
                            "date": {"start": datetime.now(KST).isoformat()}
                        },
                    }
                },
            )
            resp.raise_for_status()
        return f"{w}주차 과제를 '{STATUS_DONE}'으로 바꿨어요. 고생하셨어요, 아가씨."
    except httpx.HTTPStatusError as e:
        return _nice_error(e)
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


HOMEWORK_TOOLS = [
    homework_status,
    homework_create,
    homework_write,
    homework_complete,
]


def _selftest() -> None:
    assert _week_str("12주차") == "12"
    assert _week_str("12") == "12"
    assert _week_str(12) == "12"
    assert _week_str("방학") == "방학"  # 숫자가 없으면 원문 유지
    # 반환이 문자열인지까지 본다 — len()만 세면 틀린 이유로 통과한다 (#8-2 함정)
    assert isinstance(_week_str(7), str)
    row = {
        "properties": {
            "주차": {"select": {"name": "12"}},
            "과제 상황": {"select": None},
            "제목": {"title": [{"plain_text": "12주차 과제"}]},
        }
    }
    summary = _row_summary(row)
    assert isinstance(summary, str) and "상태 없음" in summary and "12주차" in summary
    assert HOMEWORK_TOOLS and len(HOMEWORK_TOOLS) == 4
    print("selftest OK")


if __name__ == "__main__":
    _selftest()
