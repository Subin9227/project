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
from datetime import date as date_cls
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool
from PIL import Image

from secretary.config import (
    NOTION_API_BASE,
    NOTION_MAX_UPLOAD_BYTES,
    NOTION_ROUTINE_DS_ID,
    NOTION_TOKEN,
    NOTION_VERSION,
)

# --- 항목 매핑 -----------------------------------------------------------
# 사용자가 말하는 항목명 → (체크박스 속성명, 본문 헤딩명)
# ⚠️ 체크박스 "어드민나잇"(붙임) vs 헤딩 "어드민 나잇"(공백)이 다르므로 분리해서 보관.
ITEMS: dict[str, dict[str, str]] = {
    "코테": {"checkbox": "코테", "heading": "코테"},
    "도착8시": {"checkbox": "도착 8시", "heading": "도착 8시"},
    "운동": {"checkbox": "운동", "heading": "운동"},
    "영어스피킹": {"checkbox": "영어 스피킹", "heading": "영어 스피킹"},
    "어드민나잇": {"checkbox": "어드민나잇", "heading": "어드민 나잇"},
    "회고": {"checkbox": "회고", "heading": "회고"},
}

# 사용자가 다르게 말할 수 있는 표현 → 표준 키
ALIASES: dict[str, str] = {
    "코딩테스트": "코테",
    "코딩": "코테",
    "도착": "도착8시",
    "8시": "도착8시",
    "출근": "도착8시",
    "영어": "영어스피킹",
    "스피킹": "영어스피킹",
    "어드민": "어드민나잇",
    "어드민나이트": "어드민나잇",
}


def _norm(text: str) -> str:
    """공백을 없애 비교용으로 정규화한다. ('도착 8시' == '도착8시')"""
    return "".join(text.split())


def _resolve_item(item: str) -> dict[str, str] | None:
    """사용자가 말한 항목명을 표준 항목 정보로 변환. 못 찾으면 None."""
    key = _norm(item)
    if key in ITEMS:
        return ITEMS[key]
    if key in ALIASES:
        return ITEMS[ALIASES[key]]
    return None


# --- 노션 REST 헬퍼 -------------------------------------------------------
def _headers(json: bool = True) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }
    if json:
        h["Content-Type"] = "application/json"
    return h


async def _find_or_create_row(client: httpx.AsyncClient, day: str) -> str:
    """대상 날짜의 데일리루틴 행 page_id를 돌려준다. 없으면 만들고 헤딩을 깐다."""
    # 1) 날짜로 기존 행 조회
    resp = await client.post(
        f"{NOTION_API_BASE}/data_sources/{NOTION_ROUTINE_DS_ID}/query",
        headers=_headers(),
        json={"filter": {"property": "날짜", "date": {"equals": day}}},
    )
    resp.raise_for_status()
    rows = resp.json().get("results", [])
    if rows:
        return rows[0]["id"]

    # 2) 없으면 새 행 생성 (제목=날짜, 날짜 속성 설정)
    create = await client.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(),
        json={
            "parent": {"type": "data_source_id", "data_source_id": NOTION_ROUTINE_DS_ID},
            "properties": {
                "인증": {"title": [{"text": {"content": day}}]},
                "날짜": {"date": {"start": day}},
            },
        },
    )
    create.raise_for_status()
    page_id = create.json()["id"]

    # 3) 6개 '## 항목' 헤딩 + 빈 문단을 본문에 깐다 (사진 넣을 자리)
    heading_order = ["도착8시", "코테", "영어스피킹", "운동", "어드민나잇", "회고"]
    children = []
    for k in heading_order:
        children.append(
            {
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": ITEMS[k]["heading"]}}]},
            }
        )
        children.append({"type": "paragraph", "paragraph": {"rich_text": []}})
    patch = await client.patch(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_headers(),
        json={"children": children},
    )
    patch.raise_for_status()
    return page_id


async def _find_heading_block(
    client: httpx.AsyncClient, page_id: str, heading_name: str
) -> str | None:
    """행 본문에서 '## {heading_name}' 헤딩 블록 id를 찾는다 (공백 무시)."""
    target = _norm(heading_name)
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = await client.get(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=_headers(json=False),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") != "heading_2":
                continue
            rich = block["heading_2"].get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich)
            if _norm(text) == target:
                return block["id"]
        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


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
    """데일리루틴 항목에 인증 사진(+선택 메모)을 넣고, 필요하면 체크박스를 켠다.

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

    day = date_cls.today().isoformat() if date in ("", "today", None) else date

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            row_id = await _find_or_create_row(client, day)

            heading_id = await _find_heading_block(client, row_id, resolved["heading"])
            if heading_id is None:
                return (
                    f"{day} 행에서 '## {resolved['heading']}' 헤딩을 못 찾았어요. "
                    "노션 페이지 구조를 확인해 주세요."
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
                chk = await client.patch(
                    f"{NOTION_API_BASE}/pages/{row_id}",
                    headers=_headers(),
                    json={"properties": {resolved["checkbox"]: {"checkbox": True}}},
                )
                chk.raise_for_status()

        # 결과 문구를 실제 수행한 내용에 맞춰 조립
        did = f"메모('{note_text}')와 사진을" if note_text else "사진을"
        if check:
            return (
                f"{day} 데일리루틴 '{resolved['heading']}'에 {did} 넣고 "
                f"체크박스를 켰어요. (달성률 자동 갱신됨)"
            )
        return (
            f"{day} 데일리루틴 '{resolved['heading']}'에 {did} 넣었어요. "
            f"체크박스는 켜지 않았어요."
        )
    except httpx.HTTPStatusError as e:
        return f"노션 처리 중 오류가 났어요 ({e.response.status_code}): {e.response.text[:300]}"
    except Exception as e:  # noqa: BLE001 - 도구는 예외를 문자열로 돌려줘야 에이전트가 전달함
        return f"처리 중 예상치 못한 오류: {type(e).__name__}: {e}"


# agent.py가 가져다 쓰는 도구 목록
ROUTINE_TOOLS = [attach_routine_photo]
