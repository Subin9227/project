"""노션 데일리루틴 사진 인증 커스텀 도구.

왜 커스텀이 필요한가:
    지금 붙은 노션 MCP 서버(@notionhq/notion-mcp-server)에는
    (1) 파일 업로드 도구가 없고, (2) 이미지 블록조차 못 만든다(문단/글머리기호만).
    그래서 노션 REST API(File Upload API 포함)를 httpx로 직접 호출한다.

핵심 도구: attach_routine_photo(item, image_url, date)
    "데일리 루틴에 운동 완료했어 (사진) 넣어줘" 를 처리한다.
    → 오늘 행의 '## 운동' 헤딩 아래에 사진을 넣고, 운동 체크박스를 켠다.

흐름:
    1) 대상 날짜의 행을 데이터소스에서 조회 (없으면 새로 만들고 6개 헤딩 scaffold)
    2) image_url에서 바이트 다운로드 (5MB 초과면 Pillow로 압축)
    3) 노션 File Upload: 생성 → 전송
    4) 행 본문에서 '## {item}' 헤딩을 찾아 그 뒤에 image 블록 삽입
    5) 해당 항목 체크박스 ON
"""

from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool
from PIL import Image

from secretary import context
from secretary.config import (
    KST,
    NOTION_API_BASE,
    NOTION_MAX_UPLOAD_BYTES,
    NOTION_ROUTINE_DS_ID,
    NOTION_TOKEN,
    NOTION_VERSION,
)

# --- 항목 매핑 -----------------------------------------------------------
# 사용자가 말하는 항목명 → 노션 체크박스 속성명.
# 헤딩명은 따로 안 들고 있는다: 체크박스 "어드민나잇"(붙임)과 헤딩 "어드민 나잇"(공백)이
# 달라 보이지만 _norm()이 공백을 지우므로 같은 값이 된다. 헤딩은 템플릿이 정하는 것이고,
# 코드가 사본을 들고 있으면 노션에서 템플릿을 고칠 때마다 어긋난다.
ITEMS: dict[str, str] = {
    "코테": "코테",
    "도착8시": "도착 8시",
    "운동": "운동",
    "영어스피킹": "영어 스피킹",
    "어드민나잇": "어드민나잇",
    "회고": "회고",
}

# 사용자가 다르게 말할 수 있는 표현 → 표준 키.
# ⚠️ 키는 **공백 없이** 적는다. _resolve_item이 _norm()으로 공백을 지운 뒤 찾기 때문에,
#    "미라클 모닝"이라 적어두면 영영 안 걸린다.
# 항목의 실제 뜻: 도착 8시=8시 전 출근(미라클 모닝) / 어드민나잇=야간자습 /
#                 영어 스피킹=회화 공부
ALIASES: dict[str, str] = {
    "코딩테스트": "코테",
    "코딩": "코테",
    "도착": "도착8시",
    "8시": "도착8시",
    "출근": "도착8시",
    "미라클모닝": "도착8시",
    "미라클": "도착8시",
    "헬스": "운동",
    "운동하기": "운동",
    "영어": "영어스피킹",
    "스피킹": "영어스피킹",
    "회화": "영어스피킹",
    "회화공부": "영어스피킹",
    "어드민": "어드민나잇",
    "어드민나이트": "어드민나잇",
    "야간자습": "어드민나잇",
    "야자": "어드민나잇",
}


def _norm(text: str) -> str:
    """공백을 없애 비교용으로 정규화한다. ('도착 8시' == '도착8시')"""
    return "".join(text.split())


def _day_or_today(date: str | None) -> str:
    """'today'/빈값이면 KST 오늘, 아니면 준 날짜 그대로.

    ⚠️ date.today()를 쓰면 시스템 로컬 날짜라 UTC 컨테이너에서 KST 0~9시에
       어제 행으로 간다. '오늘'은 언제나 KST 기준이어야 한다.
    """
    if date in ("", "today", None):
        return datetime.now(KST).strftime("%Y-%m-%d")
    return date


def _resolve_item(item: str) -> str | None:
    """사용자가 말한 항목명을 노션 체크박스 속성명으로 변환. 못 찾으면 None."""
    key = _norm(item)
    if key in ITEMS:
        return ITEMS[key]
    if key in ALIASES:
        return ITEMS[ALIASES[key]]
    return None


def item_guide() -> str:
    """모델에게 줄 '항목과 그 별칭' 한 줄. agent.py가 시스템 메시지에 싣는다.

    ⚠️ 별칭은 _resolve_item() 안에서만 도는데, 그건 **도구가 실행된 뒤**의 코드다.
       모델은 그 앞에서 판단하므로, 알려주지 않으면 '미라클모닝'을 없는 항목으로 보고
       도구를 아예 안 부른다(2026-08-05 실제 발생). ITEMS·ALIASES가 유일한 출처라,
       별칭을 늘리면 프롬프트가 저절로 따라온다.
    """
    said_as: dict[str, list[str]] = {key: [] for key in ITEMS}
    for said, key in ALIASES.items():
        said_as[key].append(said)
    return " / ".join(
        f"{prop}({'·'.join(said_as[key])})" if said_as[key] else prop
        for key, prop in ITEMS.items()
    )


# --- 노션 REST 헬퍼 -------------------------------------------------------
def _routine_ds() -> str:
    """지금 요청 주인의 루틴 DB.

    ⚠️ `or NOTION_ROUTINE_DS_ID`로 때우지 않는다. 등록했지만 노션을 안 넣은 사람이
       주인의 DB를 쓰게 된다. 1인 모드는 context.active()가 env_user()를 주므로
       여기서 폴백할 필요가 없다.
    """
    return context.require(context.active().routine_ds_id, "노션")


def _headers(json: bool = True) -> dict[str, str]:
    # ⚠️ 전역 토큰이 아니라 '지금 말 건 사람'의 토큰이다. 이걸 전역으로 두면
    #    철수가 시킨 일이 아가씨 노션에 기록된다.
    h = {
        "Authorization": f"Bearer {context.require(context.active().notion_token, '노션')}",
        "Notion-Version": NOTION_VERSION,
    }
    if json:
        h["Content-Type"] = "application/json"
    return h


# 템플릿에서 그대로 베낄 블록 종류. 여기 없는 종류(이미지·임베드 등)는 건너뛴다 —
# 파일이 딸린 블록은 통째로 복사하면 남의 파일 URL을 가리키게 되어 만료된다.
_COPYABLE = {
    "heading_1",
    "heading_2",
    "heading_3",
    "paragraph",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "divider",
    "table_of_contents",
}


async def _blocks_of(client: httpx.AsyncClient, block_id: str) -> list[dict]:
    """블록의 자식을 전부 읽는다 (100개 넘으면 이어서)."""
    out: list[dict] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = await client.get(
            f"{NOTION_API_BASE}/blocks/{block_id}/children",
            headers=_headers(json=False),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")


def _strip_block(block: dict) -> dict | None:
    """조회한 블록을 '만들 수 있는' 블록으로 깎는다.

    조회 응답에는 id·created_time 등 읽기 전용 필드가 잔뜩 붙어 있어서 그대로
    보내면 400이 난다. 타입과 그 본문만 남긴다.
    """
    kind = block.get("type")
    if kind not in _COPYABLE:
        return None
    # ⚠️ 값이 null인 키는 반드시 뺀다. 조회 응답의 paragraph엔 "icon": null이 붙어
    #    오는데, 그대로 되보내면 노션이 400을 낸다("should be an object or undefined").
    body = {k: v for k, v in (block.get(kind) or {}).items() if v is not None}
    # rich_text의 plain_text/href도 읽기 전용이라 text만 남겨 다시 만든다.
    if "rich_text" in body:
        body["rich_text"] = [
            {
                "type": "text",
                "text": {"content": rt.get("plain_text", "")},
                "annotations": rt.get("annotations", {}),
            }
            for rt in body["rich_text"]
        ]
    body.pop("is_toggleable", None)
    return {"type": kind, kind: body}


async def _copy_template(client: httpx.AsyncClient, ds_id: str, page_id: str) -> int:
    """데이터소스의 기본 템플릿 본문을 새 행에 그대로 깐다. 깐 블록 수를 돌려준다.

    왜 템플릿을 베끼나:
        헤딩 목록을 코드에 박아두면, 아가씨가 노션에서 템플릿을 고치는 순간 어긋난다.
        (실제로 이 코드를 쓰는 도중 '오늘 한 일' 헤딩이 하나 늘었다.)
        템플릿을 단일 진실로 삼으면 노션만 고치면 봇이 따라온다.

    비용은 요청 2회(읽기+붙이기)뿐이고, 하루 첫 요청에서 행을 만들 때만 돈다.
    ⚠️ 템플릿에 자식을 가진 블록(토글 등)이 생기면 그 자식은 안 따라온다.
       지금 템플릿은 전부 평면이라 문제없다.
    """
    resp = await client.get(
        f"{NOTION_API_BASE}/data_sources/{ds_id}/templates",
        headers=_headers(json=False),
    )
    resp.raise_for_status()
    templates = resp.json().get("templates", [])
    if not templates:
        return 0
    default = next((t for t in templates if t.get("is_default")), templates[0])

    children = [b for b in map(_strip_block, await _blocks_of(client, default["id"])) if b]
    if not children:
        return 0

    patch = await client.patch(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_headers(),
        json={"children": children},
    )
    patch.raise_for_status()
    return len(children)


async def _query_rows(client: httpx.AsyncClient, ds_id: str, filter_: dict) -> list[dict]:
    resp = await client.post(
        f"{NOTION_API_BASE}/data_sources/{ds_id}/query",
        headers=_headers(),
        json={"filter": filter_},
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


class ItemMissing(context.UserFacing):
    """노션 DB에 그 이름의 체크박스가 없다. 메시지는 그대로 아가씨께 나간다."""


async def _set_checkbox(
    client: httpx.AsyncClient, row_id: str, prop: str, checked: bool = True
) -> None:
    """행의 체크박스 하나를 켜거나 끈다. 없는 속성이면 ItemMissing.

    ⚠️ ITEMS는 아가씨 노션의 속성 이름을 **코드가 베껴 들고 있는 것**이다. 노션에서
       '운동'을 '헬스'로 바꾸면 여기서 400이 나는데, 그대로 흘리면
       "노션 처리 중 오류 (400): {...}" 라는 알아볼 수 없는 답이 나간다.
       실제로 있는 이름을 보여주는 편이 낫다.
       ⏭️ 근본 해결은 ITEMS를 버리고 DB 스키마에서 읽어오는 것 (CLAUDE.md 참고).
    """
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{row_id}",
        headers=_headers(),
        json={"properties": {prop: {"checkbox": checked}}},
    )
    if resp.status_code != 400:
        resp.raise_for_status()
        return

    page = await client.get(f"{NOTION_API_BASE}/pages/{row_id}", headers=_headers(json=False))
    page.raise_for_status()
    boxes = [
        name
        for name, val in page.json().get("properties", {}).items()
        if isinstance(val, dict) and val.get("type") == "checkbox"
    ]
    if prop in boxes:  # 400의 원인이 이름이 아니면 원래대로 터뜨린다
        resp.raise_for_status()
    raise ItemMissing(
        f"노션에 '{prop}' 체크박스가 없어요. 지금 있는 항목: {' / '.join(boxes) or '(하나도 없어요)'}. "
        "노션에서 이름을 바꾸셨다면 원래대로 되돌리거나 그 이름으로 말씀해 주세요."
    )


async def _find_row(client: httpx.AsyncClient, day: str) -> dict | None:
    """대상 날짜의 데일리루틴 행. 없으면 None (만들지 않는다)."""
    rows = await _query_rows(
        client, _routine_ds(), {"property": "날짜", "date": {"equals": day}}
    )
    return rows[0] if rows else None


async def _find_or_create_row(client: httpx.AsyncClient, day: str) -> str:
    """대상 날짜의 데일리루틴 행 page_id. 없으면 만들고 템플릿을 깐다."""
    row = await _find_row(client, day)
    if row:
        return row["id"]

    create = await client.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(),
        json={
            "parent": {"type": "data_source_id", "data_source_id": _routine_ds()},
            "properties": {
                "인증": {"title": [{"text": {"content": day}}]},
                "날짜": {"date": {"start": day}},
            },
        },
    )
    create.raise_for_status()
    page_id = create.json()["id"]
    await _copy_template(client, _routine_ds(), page_id)
    return page_id


_HEADINGS = ("heading_1", "heading_2", "heading_3")


def _heading_text(block: dict) -> str | None:
    """헤딩 블록이면 그 글자를, 아니면 None."""
    kind = block.get("type")
    if kind not in _HEADINGS:
        return None
    return "".join(rt.get("plain_text", "") for rt in block[kind].get("rich_text", []))


async def _find_heading_block(
    client: httpx.AsyncClient, page_id: str, heading_name: str
) -> str | None:
    """행 본문에서 헤딩 블록 id를 찾는다 (공백 무시).

    h2뿐 아니라 h3도 본다: 회고 아래의 '오늘 한 일'·'셀프 회고: 칭찬'처럼
    한 단계 들어간 칸에도 써야 하기 때문이다.
    """
    target = _norm(heading_name)
    for block in await _blocks_of(client, page_id):
        text = _heading_text(block)
        if text is not None and _norm(text) == target:
            return block["id"]
    return None


async def _list_headings(client: httpx.AsyncClient, page_id: str) -> list[str]:
    """행 본문의 헤딩 이름 목록. 못 찾았을 때 뭐가 있는지 알려주려고 쓴다."""
    return [t for t in map(_heading_text, await _blocks_of(client, page_id)) if t]


async def _locate_section(
    client: httpx.AsyncClient, page_id: str, section: str
) -> tuple[str | None, str | None]:
    """칸(헤딩)의 블록 id와, 그 칸이 속한 h2 이름을 함께 돌려준다.

    왜 소속을 알아야 하나:
        '셀프 회고: 칭찬'을 채우면 '회고' 체크박스가 켜져야 한다. 그런데 회고 아래
        어떤 h3들이 있는지는 템플릿이 정하는 것이라, 목록을 코드에 박으면 아가씨가
        노션에서 칸을 늘리는 순간 또 어긋난다. 그래서 목록 대신 **구조**를 읽는다:
        h3는 자기 앞에 나온 가장 가까운 h2에 속한다.

    h2 자신을 찾은 경우엔 자기가 주인이다. 앞에 h2가 없는 h3면 주인은 None.
    """
    target = _norm(section)
    current_h2: str | None = None
    for block in await _blocks_of(client, page_id):
        text = _heading_text(block)
        if text is None:
            continue
        if block["type"] == "heading_2":
            current_h2 = text
        if _norm(text) == target:
            return block["id"], current_h2
    return None, None


async def _section_blocks(
    client: httpx.AsyncClient, page_id: str, heading_id: str
) -> list[dict]:
    """그 칸(헤딩) 아래에 실제로 붙어 있는 블록들. 다음 헤딩을 만나면 멈춘다.

    노션은 헤딩 아래 내용을 '자식'으로 담지 않는다 — 전부 페이지의 형제 블록이고,
    다음 헤딩이 나올 때까지가 그 칸의 몫이다. 그래서 순서대로 훑어야 한다.
    """
    out: list[dict] = []
    collecting = False
    for block in await _blocks_of(client, page_id):
        if block["id"] == heading_id:
            collecting = True
            continue
        if not collecting:
            continue
        if block.get("type") in _HEADINGS:
            break
        out.append(block)
    return out


def _append_anchor(section: list[dict], heading_id: str) -> str:
    """덧붙일 때 어느 블록 뒤에 넣을지. 칸의 마지막 블록, 비었으면 헤딩.

    ⚠️ 항상 헤딩 뒤에 넣으면 새 글이 매번 맨 위에 꽂혀서, 회고를 위에서 아래로
       읽을 때 시간이 거꾸로 흐른다 (2026-08-04 실사용자 화면에서 확인).
    """
    return section[-1]["id"] if section else heading_id


def _paragraph_text(block: dict) -> str | None:
    """문단 블록이면 그 글자를, 아니면 None (사진·구분선 등)."""
    if block.get("type") != "paragraph":
        return None
    return "".join(
        rt.get("plain_text", "") for rt in block["paragraph"].get("rich_text", [])
    )


def _compress_image(data: bytes) -> tuple[bytes, str, str]:
    """5MB 초과 이미지를 JPEG로 압축/축소한다. (bytes, filename, content_type) 반환."""
    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    quality = 85
    scale = 1.0
    for _ in range(8):
        buf = io.BytesIO()
        w, h = img.size
        resized = (
            img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            if scale < 1.0
            else img
        )
        resized.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= NOTION_MAX_UPLOAD_BYTES:
            return out, "photo.jpg", "image/jpeg"
        # 아직 크면 품질을 낮추고, 더 낮출 수 없으면 크기를 줄인다
        if quality > 40:
            quality -= 15
        else:
            scale *= 0.8
    return out, "photo.jpg", "image/jpeg"  # 최선 결과라도 반환


async def _download_image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str, str]:
    """이미지 URL에서 바이트를 받고, 5MB 초과면 압축한다."""
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    data = resp.content
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    filename = os.path.basename(urlparse(url).path) or "photo"

    if len(data) > NOTION_MAX_UPLOAD_BYTES:
        return await asyncio.to_thread(_compress_image, data)
    if not filename or "." not in filename:
        filename = "photo.jpg"
    return data, filename, content_type


async def _upload_to_notion(
    client: httpx.AsyncClient, data: bytes, filename: str, content_type: str
) -> str:
    """노션 File Upload: 생성 → 전송. file_upload id를 돌려준다."""
    create = await client.post(
        f"{NOTION_API_BASE}/file_uploads",
        headers=_headers(),
        json={"filename": filename, "content_type": content_type},
    )
    create.raise_for_status()
    file_upload_id = create.json()["id"]

    # multipart 전송 (Content-Type 헤더는 httpx가 boundary와 함께 자동 설정)
    send = await client.post(
        f"{NOTION_API_BASE}/file_uploads/{file_upload_id}/send",
        headers=_headers(json=False),
        files={"file": (filename, data, content_type)},
    )
    send.raise_for_status()
    return file_upload_id


# --- 에이전트에 노출되는 도구 ---------------------------------------------
@tool
async def attach_routine_photo(
    item: str,
    image_url: str,
    date: str = "today",
    check: bool = True,
    note: str = "",
) -> str:
    """**노션** 데일리루틴 항목에 인증 사진(+선택 메모)을 넣고, 필요하면 체크박스를 켠다.

    '사진 = 증거'와 '체크박스 = 달성'은 별개다. 항목을 실제로 달성했으면 체크박스를
    켜고(check=True), 증거만 남기고 달성은 아닐 때는 사진만 넣는다(check=False).

    예)
        "운동 인증 사진 넣어줘"            → check=True (달성)
        "8시 34분 도착이라고 적고 사진 넣어줘, 체크박스는 하지마"
                                          → item="도착8시", note="8시 34분 도착", check=False

    Args:
        item: 인증 항목. 코테 / 도착 8시 / 운동 / 영어 스피킹 / 어드민나잇 / 회고 중 하나.
        image_url: 첨부된 이미지의 URL (디스코드 첨부 URL 등).
        date: 대상 날짜 YYYY-MM-DD. 기본 'today' = 오늘.
        check: True면 해당 항목 체크박스를 켠다. False면 체크박스를 건드리지 않는다
            (목표 미달성·증거만 남길 때). 사용자가 달리 말하지 않으면 True.
        note: 사진 위에 함께 남길 짧은 텍스트(예: "8시 34분 도착"). 비우면 사진만 넣는다.

    Returns:
        처리 결과 요약 문자열.
    """
    resolved = _resolve_item(item)
    if resolved is None:
        valid = " / ".join(ITEMS.keys())
        return f"'{item}'은(는) 모르는 항목이에요. 가능한 항목: {valid}"

    day = _day_or_today(date)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            row_id = await _find_or_create_row(client, day)

            heading_id = await _find_heading_block(client, row_id, resolved)
            if heading_id is None:
                found = ", ".join(await _list_headings(client, row_id)) or "(하나도 없어요)"
                return (
                    f"{day} 행에서 '{resolved}' 칸을 못 찾았어요. 있는 칸: {found}"
                )

            data, filename, content_type = await _download_image(client, image_url)
            file_upload_id = await _upload_to_notion(client, data, filename, content_type)

            # 헤딩 뒤에 넣을 블록을 순서대로 구성: (있으면)메모 문단 → 사진
            new_blocks: list[dict] = []
            note_text = note.strip()
            if note_text:
                new_blocks.append(
                    {
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": note_text}}]},
                    }
                )
            new_blocks.append(
                {
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": file_upload_id},
                    },
                }
            )
            insert = await client.patch(
                f"{NOTION_API_BASE}/blocks/{row_id}/children",
                headers=_headers(),
                json={"after": heading_id, "children": new_blocks},
            )
            insert.raise_for_status()

            # 체크박스: check=True일 때만 켠다. False면 건드리지 않는다(미달성·증거만).
            if check:
                await _set_checkbox(client, row_id, resolved)

        # 결과 문구를 실제 수행한 내용에 맞춰 조립
        did = f"메모('{note_text}')와 사진을" if note_text else "사진을"
        if check:
            return (
                f"{day} 데일리루틴 '{resolved}'에 {did} 넣고 "
                f"체크박스를 켰어요. (달성률 자동 갱신됨)"
            )
        return (
            f"{day} 데일리루틴 '{resolved}'에 {did} 넣었어요. 체크박스는 켜지 않았어요."
        )
    except httpx.HTTPStatusError as e:
        return f"노션 처리 중 오류가 났어요 ({e.response.status_code}): {e.response.text[:300]}"
    except Exception as e:  # noqa: BLE001 - 도구는 예외를 문자열로 돌려줘야 에이전트가 전달함
        return context.as_message(e, "처리 중 예상치 못한 오류")


@tool
async def routine_today(date: str = "today") -> str:
    """**노션** 데일리루틴에서 그날 뭘 했고 뭐가 남았는지 확인한다.

    "오늘 뭐 했지?", "오늘 루틴 어때?", "뭐 남았어?" 같은 물음에 이걸 쓴다.

    Args:
        date: 대상 날짜 YYYY-MM-DD. 기본 'today' = 오늘.

    Returns:
        항목별 체크 여부와 달성률. 행이 아직 없으면 그렇다고 알려준다.
    """
    day = _day_or_today(date)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            row = await _find_row(client, day)
            if row is None:
                return f"{day} 데일리루틴 행이 아직 없어요. (오늘 기록이 하나도 없다는 뜻)"

            props = row["properties"]
            done, todo = [], []
            for name in ITEMS.values():
                prop = props.get(name)
                if not prop or prop.get("type") != "checkbox":
                    continue
                (done if prop["checkbox"] else todo).append(name)

            rate = props.get("달성률", {}).get("formula", {}).get("number")
            lines = [f"{day} 데일리루틴 ({rate}/{len(ITEMS)} 달성)"]
            lines.append("  했음: " + (", ".join(done) or "아직 없음"))
            lines.append("  남음: " + (", ".join(todo) or "없음 — 다 하셨어요"))
            return "\n".join(lines)
    except httpx.HTTPStatusError as e:
        return f"노션 조회 중 오류 ({e.response.status_code}): {e.response.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


@tool
async def routine_check(item: str, checked: bool = True, date: str = "today") -> str:
    """**노션** 데일리루틴 항목의 체크박스만 켜거나 끈다 (사진 없이).

    "운동 다녀왔어", "코테 했어" 처럼 사진 없이 달성만 알릴 때 쓴다.
    사진도 같이 넣어야 하면 attach_routine_photo를 쓴다.

    Args:
        item: 코테 / 도착 8시 / 운동 / 영어 스피킹 / 어드민나잇 / 회고 중 하나.
        checked: True면 켜고, False면 끈다(잘못 켠 걸 되돌릴 때).
        date: 대상 날짜 YYYY-MM-DD. 기본 'today' = 오늘.

    Returns:
        처리 결과 문자열.
    """
    resolved = _resolve_item(item)
    if resolved is None:
        return f"'{item}'은(는) 모르는 항목이에요. 가능한 항목: {' / '.join(ITEMS.keys())}"

    day = _day_or_today(date)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            row_id = await _find_or_create_row(client, day)
            await _set_checkbox(client, row_id, resolved, checked)
        state = "켰어요" if checked else "껐어요"
        return f"{day} 데일리루틴 '{resolved}' 체크박스를 {state}."
    except httpx.HTTPStatusError as e:
        return f"노션 처리 중 오류 ({e.response.status_code}): {e.response.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


@tool
async def routine_write(
    section: str,
    text: str,
    date: str = "today",
    check: bool = True,
    mode: str = "append",
) -> str:
    """**노션** 데일리루틴 행의 특정 칸(헤딩) 아래에 글을 적고, 그 항목 체크박스를 켠다.

    회고를 받아적을 때 쓴다. 칸 이름은 노션 템플릿에 있는 그대로 넘긴다 —
    예: '오늘 한 일', '오늘의 특별한 점', '셀프 회고: 칭찬', '셀프 회고: 반성'.
    어떤 칸이 있는지 모르면 틀린 이름으로 한 번 불러라. 있는 칸 목록을 알려준다.

    회고 아래 어느 칸을 채우든 '회고' 체크박스가 켜진다 (한 칸만 채워도 켜진다).

    ⚠️ **이미 적혀 있던 내용을 결과로 함께 알려준다.** 아가씨께 답할 때 그걸 보고
    "이미 이렇게 적혀 있는데 더할까요, 갈아 끼울까요?"처럼 안내해라.
    같은 내용을 이미 적었는지 지레짐작하지 말 것 — 이 도구가 알려준다.

    Args:
        section: 글을 넣을 칸(헤딩) 이름.
        text: 넣을 내용. 줄바꿈이 있으면 문단이 나뉜다.
        date: 대상 날짜 YYYY-MM-DD. 기본 'today' = 오늘.
        check: True면 그 칸이 속한 항목 체크박스를 켠다. 아가씨가 "체크는 하지 마"
            라고 할 때만 False.
        mode: 'append'(기본)는 뒤에 덧붙이되 **이미 똑같이 적힌 줄은 건너뛴다**.
            'replace'는 그 칸의 기존 문단을 지우고 새로 쓴다 — 블레이버스 시간처럼
            **값이 갱신되는 내용을 다시 적을 때** 쓴다(덧붙이면 옛 숫자가 같이 남는다).
            ⚠️ replace는 아가씨가 노션에서 손으로 쓰신 글도 지운다. 갱신이 확실할
            때만 쓰고, 애매하면 append로 두고 여쭤라. (사진은 지우지 않는다)

    Returns:
        처리 결과 문자열. 원래 적혀 있던 내용이 있으면 함께 돌려준다.
    """
    body = text.strip()
    if not body:
        return "적을 내용이 비어 있어요, 아가씨."

    mode = (mode or "append").strip().lower()
    if mode not in ("append", "replace"):
        return f"mode는 'append'나 'replace'여야 해요 (받은 값: '{mode}')."

    day = _day_or_today(date)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            row_id = await _find_or_create_row(client, day)
            heading_id, owner = await _locate_section(client, row_id, section)
            if heading_id is None:
                found = ", ".join(await _list_headings(client, row_id)) or "(하나도 없어요)"
                return f"'{section}' 칸을 못 찾았어요. 있는 칸: {found}"

            # ⚠️ 예전엔 기존 내용을 보지도 않고 헤딩 뒤에 무조건 끼워 넣었다. 그래서
            #    블레이버스 시간을 두 번 적으면 옛 숫자와 새 숫자가 나란히 쌓였다.
            #    (2026-08-04 실사용자 제보: "노션 수정할때 자꾸 추가만 해줘요")
            before = await _section_blocks(client, row_id, heading_id)
            existing = [t for t in map(_paragraph_text, before) if t and t.strip()]

            lines = [ln for ln in body.splitlines() if ln.strip()]
            removed = skipped = 0
            # 새 글을 어느 블록 뒤에 끼울지. 헤딩 뒤에 넣으면 항상 맨 위에 꽂혀서
            # 덧붙일수록 시간이 거꾸로 흐른다(2026-08-04 실사용자 화면에서 확인).
            # 덧붙이기는 칸의 끝에, 갈아치우기는 칸의 앞에(템플릿의 '글 다음 사진' 순서 유지).
            anchor = heading_id
            if mode == "append":
                anchor = _append_anchor(before, heading_id)
                have = {_norm(t) for t in existing}
                kept = [ln for ln in lines if _norm(ln) not in have]
                skipped = len(lines) - len(kept)
                lines = kept
            else:  # replace — 글이 든 문단만 지운다.
                # ⚠️ 사진(attach_routine_photo가 넣은 image)과 템플릿이 칸 끝에 두는
                #    빈 문단은 건드리지 않는다. 지우면 인증 사진이 날아가고 칸 모양이
                #    무너지는데, 둘 다 아가씨가 손으로 되돌려야 하는 종류다.
                for block in before:
                    if not (_paragraph_text(block) or "").strip():
                        continue
                    gone = await client.delete(
                        f"{NOTION_API_BASE}/blocks/{block['id']}",
                        headers=_headers(json=False),
                    )
                    gone.raise_for_status()
                    removed += 1

            if lines:
                paragraphs = [
                    {
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": ln}}]},
                    }
                    for ln in lines
                ]
                resp = await client.patch(
                    f"{NOTION_API_BASE}/blocks/{row_id}/children",
                    headers=_headers(),
                    json={"after": anchor, "children": paragraphs},
                )
                resp.raise_for_status()

            # 글을 쓴 칸이 속한 항목의 체크박스를 켠다.
            # ('셀프 회고: 칭찬'에 쓰면 '회고'가 켜진다 — 소속은 _locate_section이 찾는다)
            checked = None
            if check and owner:
                prop = _resolve_item(owner)
                if prop:
                    await _set_checkbox(client, row_id, prop)
                    checked = prop

        # 무엇을 했는지 + **원래 뭐가 있었는지**를 함께 돌려준다.
        # 뒤엣것이 핵심이다: 칸 내용을 읽는 도구가 따로 없어서, 이걸 안 주면 모델이
        # "지금 비어 있어요" 같은 말을 지어낸다 (2026-08-04 실제로 그랬다).
        if mode == "replace":
            head = f"{day} '{section}'을 새로 썼어요 ({len(lines)}줄, 이전 {removed}줄은 지웠어요)."
        elif not lines:
            head = f"{day} '{section}'에 넣을 게 없었어요 — {skipped}줄 모두 이미 똑같이 적혀 있어요."
        elif skipped:
            head = f"{day} '{section}'에 {len(lines)}줄 덧붙였어요 (이미 있던 {skipped}줄은 건너뜀)."
        else:
            head = f"{day} '{section}'에 {len(lines)}줄 덧붙였어요."
        if checked:
            head += f" '{checked}' 체크박스도 켰어요."

        if existing:
            was = "\n".join(existing)
            if len(was) > 600:  # 모델에 실릴 양을 묶어둔다
                was = was[:600] + f"\n… (외 {len(existing)}줄 중 일부 생략)"
            head += f"\n\n[원래 이 칸에 있던 내용]\n{was}"
        return head
    except httpx.HTTPStatusError as e:
        return f"노션 처리 중 오류 ({e.response.status_code}): {e.response.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return context.as_message(e, "처리 중 예상치 못한 오류")


# agent.py가 가져다 쓰는 도구 목록
ROUTINE_TOOLS = [routine_today, routine_check, routine_write, attach_routine_photo]


def _selftest() -> None:
    """칸 경계 판정과 중복 제거만 점검한다. 여기가 이 파일의 판단 로직이다.

    네트워크는 타지 않는다 — _blocks_of만 갈아끼운다. 실 노션으로 시험하면
    남의 행에 쓰레기가 남고, 사진 블록을 지우는 실수는 되돌리기 어렵다.
    """
    import asyncio

    def heading(bid, level, text):
        return {"id": bid, "type": f"heading_{level}", f"heading_{level}": {
            "rich_text": [{"plain_text": text}]}}

    def para(bid, text):
        return {"id": bid, "type": "paragraph", "paragraph": {
            "rich_text": [{"plain_text": text}]}}

    page = [
        heading("h-회고", 2, "회고"),
        heading("h-한일", 3, "오늘 한 일"),
        para("p1", "임베딩 정리"),
        {"id": "img1", "type": "image", "image": {}},      # 사진 인증
        para("p2", "검증 데이터 정리"),
        heading("h-특별", 3, "오늘의 특별한 점"),           # ← 여기서 끊겨야 한다
        para("p3", "남의 칸 내용"),
    ]

    global _blocks_of
    real = _blocks_of

    async def fake(client, block_id):
        return page

    _blocks_of = fake
    try:
        got = asyncio.run(_section_blocks(None, "row", "h-한일"))
        ids = [b["id"] for b in got]
        # 다음 헤딩 전까지만. 헤딩 자신도, 다음 칸 내용도 안 들어온다
        assert ids == ["p1", "img1", "p2"], ids
        # 마지막 칸은 페이지 끝까지
        assert [b["id"] for b in asyncio.run(_section_blocks(None, "row", "h-특별"))] == ["p3"]
        # 없는 헤딩이면 빈 목록 (엉뚱한 걸 지우면 안 된다)
        assert asyncio.run(_section_blocks(None, "row", "h-없음")) == []
        assert isinstance(got, list)
    finally:
        _blocks_of = real

    # 사진은 문단이 아니다 → replace가 지우면 안 되고, 중복 비교에도 안 낀다
    assert _paragraph_text(para("x", "글")) == "글"
    assert _paragraph_text({"id": "i", "type": "image", "image": {}}) is None
    assert _paragraph_text(heading("h", 2, "회고")) is None
    assert _paragraph_text(para("y", "")) == ""

    # replace가 실제로 지우는 대상: 글이 든 문단만.
    # (템플릿이 칸 끝에 두는 빈 문단과 사진은 남아야 한다 — 실 노션에서 확인한 구조)
    section = [para("p1", "옛 기록"), {"id": "img", "type": "image", "image": {}},
               para("p2", "   "), para("p3", "합계 5시간")]
    doomed = [b["id"] for b in section if (_paragraph_text(b) or "").strip()]
    assert doomed == ["p1", "p3"], doomed

    # --- 노션에서 체크박스 이름을 바꾸면 (2026-08-05 논의) ---------------------
    # ITEMS는 아가씨 노션 속성 이름의 사본이다. 노션에서 '운동'을 '헬스'로 바꾸면
    # 400이 나는데, 그대로 흘리면 알아볼 수 없는 답이 나간다.
    class _FakeResp:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body or {}

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

    class _FakeClient:
        def __init__(self, boxes):
            self.boxes = boxes

        async def patch(self, url, **kw):
            prop = next(iter(kw["json"]["properties"]))
            return _FakeResp(200 if prop in self.boxes else 400)

        async def get(self, url, **kw):
            props = {n: {"type": "checkbox"} for n in self.boxes}
            props["날짜"] = {"type": "date"}  # 체크박스가 아닌 건 목록에서 빠져야 한다
            return _FakeResp(200, {"properties": props})

    ok = asyncio.run(_set_checkbox(_FakeClient(["운동", "코테"]), "row", "운동"))
    assert ok is None, ok  # 있으면 조용히 켜고 끝난다
    try:
        asyncio.run(_set_checkbox(_FakeClient(["헬스", "코테"]), "row", "운동"))
        raise AssertionError("없는 항목인데 통과했다")
    except ItemMissing as e:
        assert "'운동' 체크박스가 없어요" in str(e), str(e)
        assert "헬스 / 코테" in str(e) and "날짜" not in str(e), str(e)
        # 예외 타입 이름이 아가씨께 새면 안 된다 (UserFacing이라 그대로 나간다)
        shown = context.as_message(e, "노션 처리 중 오류")
        assert shown.startswith("노션에 '운동'") and "ItemMissing" not in shown, shown

    # 부르는 말이 달라도 같은 항목으로 간다 (2026-08-05: '미라클 모닝'을 못 알아듣고
    # 모델이 '운동'을 임의로 켰다)
    for said, want in (
        ("미라클 모닝", "도착 8시"), ("미라클모닝", "도착 8시"), ("출근", "도착 8시"),
        ("도착", "도착 8시"), ("도착 8시", "도착 8시"),
        ("헬스", "운동"), ("운동", "운동"),
        ("야간자습", "어드민나잇"), ("야자", "어드민나잇"), ("어드민 나잇", "어드민나잇"),
        ("회화공부", "영어 스피킹"), ("회화 공부", "영어 스피킹"), ("영어", "영어 스피킹"),
        ("코딩테스트", "코테"), ("코테", "코테"), ("회고", "회고"),
    ):
        assert _resolve_item(said) == want, (said, _resolve_item(said))

    # 별칭 키에 공백이 있으면 _norm 때문에 영영 안 걸린다 — 넣을 때 실수하기 쉽다
    assert all(k == _norm(k) for k in ALIASES), [k for k in ALIASES if k != _norm(k)]
    assert all(v in ITEMS for v in ALIASES.values()), "없는 항목을 가리키는 별칭이 있다"

    # 그래도 모르는 말은 노션까지 가지 않는다 (비슷한 걸 임의로 켜면 안 된다)
    assert _resolve_item("명상") is None and _resolve_item("") is None

    # ⚠️ 별칭은 프롬프트에 실려야 쓸모가 있다. 모델은 도구를 부르기 **전에** 판단하므로,
    #    이 한 줄에서 빠진 별칭은 "그런 항목 없어요"로 막힌다(2026-08-05 '미라클모닝').
    guide = item_guide()
    for said in ALIASES:
        assert said in guide, said
    for prop in ITEMS.values():
        assert prop in guide, prop
    assert guide.count(" / ") == len(ITEMS) - 1, guide  # 항목 여섯 개가 한 줄에
    assert "회고(" not in guide  # 별칭 없는 항목은 빈 괄호를 달지 않는다

    # 중복 판정은 _norm 기준 — 공백이 달라도 같은 줄로 본다
    existing = {_norm(t) for t in ["임베딩 정리", "검증 데이터 정리"]}
    new = ["임베딩정리", "프로젝트 아키텍쳐 분석 정리", "검증 데이터  정리"]
    kept = [ln for ln in new if _norm(ln) not in existing]
    assert kept == ["프로젝트 아키텍쳐 분석 정리"], kept

    # 덧붙이기는 칸의 **끝**에 붙는다. 헤딩에 붙이면 최신이 맨 위로 와서
    # 회고를 위에서 아래로 읽을 때 시간이 거꾸로 흐른다.
    assert _append_anchor(section, "h-반성") == "p3"
    assert _append_anchor([], "h-반성") == "h-반성"  # 칸이 비어 있으면 헤딩 뒤
    assert isinstance(_append_anchor(section, "h-반성"), str)

    print("selftest OK")


if __name__ == "__main__":
    _selftest()
