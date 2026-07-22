"""data/memory.sqlite 들여다보기 도구.

왜 필요한가:
    체크포인트 테이블의 `checkpoint` 열은 msgpack 바이너리라 VSCode나 sqlite3 CLI로
    열면 hex 덩어리로만 보인다. (`metadata`는 사실 그냥 JSON인데 BLOB이라 역시 hex로 뜬다.)
    LangGraph가 쓰는 직렬화기(JsonPlusSerializer)로 풀어야 사람이 읽을 수 있다.

사용법:
    .venv/bin/python scripts/peek_memory.py              # 전체 목록 (스레드 + 스냅샷 요약)
    .venv/bin/python scripts/peek_memory.py 6e57         # 그 체크포인트 하나 자세히
    .venv/bin/python scripts/peek_memory.py 6e57 --full  # 메시지 본문 안 자르고 전부
    .venv/bin/python scripts/peek_memory.py --db data/memory.sqlite.bak

체크포인트 id는 앞 몇 글자만 쳐도 된다 (부분 일치).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 넣어 어디서 실행하든 동작하게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

SERDE = JsonPlusSerializer()
PREVIEW_LEN = 200  # --full 없을 때 메시지 본문을 자를 길이


def _load_metadata(type_: str, blob: bytes) -> dict:
    """metadata 열을 dict로 푼다. (보통 평범한 JSON, 옛 버전은 직렬화기 경유)"""
    try:
        return json.loads(blob)
    except Exception:
        return SERDE.loads_typed((type_, blob))


def _describe(msg) -> tuple[str, str]:
    """메시지 하나를 (종류, 사람이 읽을 내용)으로 요약한다.

    도구 호출은 content가 비어 있고 tool_calls에 알맹이가 있어서 따로 꺼낸다.
    """
    kind = type(msg).__name__
    calls = getattr(msg, "tool_calls", None)
    if calls:
        parts = [f"{c.get('name')}({json.dumps(c.get('args', {}), ensure_ascii=False)})" for c in calls]
        return kind, "🔧 " + " / ".join(parts)
    content = msg.content
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return kind, content


def list_all(db: sqlite3.Connection) -> None:
    """스레드별로 스냅샷을 시간순으로 훑어 한 줄씩 보여준다."""
    rows = db.execute(
        "select thread_id, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata "
        "from checkpoints order by thread_id, checkpoint_id"
    ).fetchall()
    if not rows:
        print("체크포인트가 없습니다. (봇을 한 번도 안 돌렸거나 빈 DB)")
        return

    current = None
    for thread_id, cid, pid, type_, cp_blob, md_blob in rows:
        if thread_id != current:
            current = thread_id
            n = sum(1 for r in rows if r[0] == thread_id)
            print(f"\n=== thread_id {thread_id}  (스냅샷 {n}장) ===")
            print(f"{'시각':<14}{'step':<6}{'source':<8}{'id':<10}{'parent':<10}메시지")

        cp = SERDE.loads_typed((type_, cp_blob))
        md = _load_metadata(type_, md_blob)
        msgs = cp["channel_values"].get("messages", [])
        kinds = ",".join(type(m).__name__.replace("Message", "") for m in msgs) or "-"
        ts = cp["ts"][11:23]  # 시:분:초.밀리 (UTC)
        parent = pid[9:13] if pid else "ROOT"
        print(f"{ts:<14}{md.get('step'):<6}{md.get('source', ''):<8}{cid[9:13]:<10}{parent:<10}{len(msgs)}개 [{kinds}]")

    print("\n(시각은 UTC. 한국 시간은 +9시간 — 06:10 = 오후 3시 10분)")
    print("자세히 보려면: peek_memory.py <id앞자리>   예) peek_memory.py 6e57")


def show_one(db: sqlite3.Connection, needle: str, full: bool) -> None:
    """체크포인트 하나를 골라 메타데이터·메시지·딸린 쪽지까지 펼쳐 보여준다."""
    rows = db.execute(
        "select thread_id, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata "
        "from checkpoints where checkpoint_id like ?",
        (f"%{needle}%",),
    ).fetchall()
    if not rows:
        print(f"'{needle}'에 맞는 체크포인트가 없습니다.")
        return
    if len(rows) > 1:
        print(f"'{needle}'에 {len(rows)}개가 걸립니다. 더 길게 지정해 주세요:")
        for r in rows:
            print("  ", r[1])
        return

    thread_id, cid, pid, type_, cp_blob, md_blob = rows[0]
    cp = SERDE.loads_typed((type_, cp_blob))
    md = _load_metadata(type_, md_blob)

    print(f"thread_id : {thread_id}   (디스코드 채널 ID)")
    print(f"id        : {cid}")
    print(f"parent    : {pid or '(없음 — 이 스레드의 첫 스냅샷)'}")
    print(f"저장시각  : {cp['ts']}  (UTC)")
    print(f"metadata  : {json.dumps(md, ensure_ascii=False)}")

    msgs = cp["channel_values"].get("messages", [])
    print(f"\n--- 이 시점에 봇이 기억하던 대화 ({len(msgs)}개) ---")
    for i, m in enumerate(msgs):
        kind, text = _describe(m)
        if not full and len(text) > PREVIEW_LEN:
            text = text[:PREVIEW_LEN] + f" …({len(text)}자, --full로 전체)"
        print(f"[{i}] {kind}: {text}")

    writes = db.execute(
        "select task_id, idx, channel, type, value from writes "
        "where thread_id = ? and checkpoint_id = ? order by rowid",
        (thread_id, cid),
    ).fetchall()
    if writes:
        print(f"\n--- 이 스냅샷을 보고 일한 결과(쪽지) {len(writes)}개 ---")
        print("   (다음 스냅샷에 반영된다)")
        for task_id, idx, channel, wtype, value in writes:
            try:
                decoded = SERDE.loads_typed((wtype, value))
            except Exception:
                decoded = f"<{len(value)}바이트>"
            # messages 채널의 값은 메시지 1개일 수도, 리스트일 수도 있다.
            # 날것(AIMessage(content=[...]))으로 찍으면 못 읽으니 요약 형태로 바꾼다.
            if channel == "messages":
                items = decoded if isinstance(decoded, list) else [decoded]
                text = " | ".join(
                    "{}: {}".format(*_describe(m)) if hasattr(m, "content") else str(m)
                    for m in items
                )
            else:
                text = str(decoded)
            if not full and len(text) > PREVIEW_LEN:
                text = text[:PREVIEW_LEN] + " …"
            print(f"  task {task_id[:6]} #{idx} [{channel}] {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="공주비서 대화기억(memory.sqlite) 뷰어")
    parser.add_argument("checkpoint", nargs="?", help="체크포인트 id 일부. 생략하면 전체 목록")
    parser.add_argument("--db", default="data/memory.sqlite", help="DB 경로")
    parser.add_argument("--full", action="store_true", help="긴 내용을 자르지 않고 전부 출력")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"{path} 가 없습니다.")
        return

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # 읽기 전용으로 열어 실수 방지
    try:
        if args.checkpoint:
            show_one(db, args.checkpoint, args.full)
        else:
            list_all(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
