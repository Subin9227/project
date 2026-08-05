"""로컬 MLX 모델이 봇의 도구 17개를 제대로 고르는지 재보는 프로브. 읽기 전용.

왜 필요한가:
    12주차 실험에선 도구를 4개만 물려 4B가 성공했다. 봇의 실제 부하는 17개다.
    "도구가 많아지면 못 고른다"가 작은 모델의 대표 실패 모드라, 붙이기 전에 잰다.

⚠️ 도구를 **실행하지 않는다.** bind_tools + 1회 호출로 모델이 뱉은 tool_call만 본다.
   그래서 노션에 행이 생기거나 블레이버스 세션이 시작되는 일이 없다.

⚠️ agent.py `_build_model()`의 vllm 분기와 **똑같은 인자**로 모델을 만든다.
   여기서만 되는 설정으로 재면 봇에 붙였을 때 다르게 동작한다.

쓰기:
    optiq serve --model mlx-community/Qwen3.5-4B-OptiQ-4bit --host 127.0.0.1 --port 8080
    .venv/bin/python -m scripts.mlx_probe
    .venv/bin/python -m scripts.mlx_probe --model mlx-community/Qwen3.5-9B-OptiQ-4bit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from langchain_core.messages import HumanMessage, SystemMessage

from secretary.agent import _prompt_with_today, build_tools
from secretary.config import MAX_TOKENS
from secretary.persona import SYSTEM_PROMPT

# (사용자 발화, 기대하는 도구 이름 후보들). 후보가 여럿인 건 두 경로가 다 말이 되는 경우다.
CASES: list[tuple[str, tuple[str, ...]]] = [
    ("오늘 운동 다녀왔어", ("routine_check",)),
    ("오늘 루틴 어떻게 돼?", ("routine_today",)),
    ("지금 뭐 하고 있었지?", ("blaybus_status",)),
    ("이번 주 과제 뭐 남았어?", ("homework_status",)),
    # 이름이 중복이라 되묻는 게 맞지만, 그러려면 먼저 목록을 봐야 한다 (#8-4).
    # ⚠️ blaybus_status도 정답이다. '오전' 태스크가 11개라 바로 시작할 수 없고,
    #    지금 돌아가는 게 있는지 보는 것도 합리적인 첫 수순이다. 처음엔 빼뒀는데,
    #    그래서 9B가 맞게 행동하고도 오답으로 세어졌다(2026-08-05).
    ("오전 작업 시작해줘", ("blaybus_start", "blaybus_list", "blaybus_status")),
    ("그만할래", ("blaybus_stop",)),
]


def _build_llm(base_url: str, model: str, api_key: str):
    """agent.py의 vllm 분기와 동일하게 만든다 (extra_body까지 그대로)."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_tokens=MAX_TOKENS,
        temperature=0.7,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


# 되묻기 예외 조항을 걷어낸 프롬프트. 가설 검증용이지 제품 프롬프트가 아니다.
#
# 왜 재보나: 실패가 전부 '쓰기형 도구'에 몰렸고, 실패 응답이 하나같이 되묻기였다.
# SYSTEM_PROMPT는 "명확하면 곧바로 실행"이라 해놓고 되묻기 예외를 4개 붙여둔다.
# 작은 모델이 그 예외를 기본값으로 오해하는 것인지 가른다.
LEAN_PROMPT = SYSTEM_PROMPT.split("[실행 원칙]")[0] + (
    "[실행 원칙] 너는 수다형 챗봇이 아니라 '한 방 명령 실행기'야.\n"
    "- 사용자가 무언가를 했다/해달라고 하면 되묻지 말고 곧바로 도구를 호출해.\n"
    "- 정보가 부족하면 되묻기 전에 먼저 조회 도구로 확인해라.\n"
)


def _system_prompt(lean: bool) -> str:
    """봇이 실제로 보내는 시스템 메시지 그대로.

    ⚠️ SYSTEM_PROMPT만 쓰면 안 된다. 오늘 날짜와 데일리루틴 항목 별칭은
       agent.py `_prompt_with_today()`가 붙이므로, 여기서 직접 조립하면
       봇보다 짧은 프롬프트로 재게 된다(2026-08-05에 실제로 197자가 빠졌다).
       그 함수를 그대로 불러 어긋날 여지를 없앤다.
    """
    built = _prompt_with_today({"messages": [HumanMessage(content="측정용")]})[0].content
    return built if not lean else built.replace(SYSTEM_PROMPT, LEAN_PROMPT)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default="mlx-community/Qwen3.5-4B-OptiQ-4bit")
    # ⚠️ optiq serve는 'sk-optiq-'로 시작하는 Bearer만 받는다. config.py의
    #    VLLM_API_KEY 기본값('not-needed')으로는 401이 난다 — .env에 넣어줘야 한다.
    ap.add_argument("--api-key", default="sk-optiq-local")
    ap.add_argument("--persona", choices=("full", "lean"), default="full")
    # ⚠️ temperature=0.7(봇과 같은 값)이라 같은 설정도 회차마다 흔들린다. 6케이스 1회로는
    #    프롬프트 차이를 못 가른다 — 실제로 같은 조건에서 2/6·3/6·4/6이 다 나왔다
    #    (2026-08-05). 여러 번 돌려 케이스별 성공 횟수로 봐야 한다.
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    prompt = _system_prompt(args.persona == "lean")

    tools = build_tools()
    names = sorted(t.name for t in tools)
    print(f"도구 {len(tools)}개: {', '.join(names)}\n")
    assert len(tools) == 17, f"도구 개수가 17이 아니다: {len(tools)}"

    # 도구 목록이 실제로 몇 글자나 실리는지 — 12주차 컨텍스트 문제의 크기를 눈으로 본다.
    # ⚠️ 둘 다 실제 페이로드에 맞춰야 한다.
    #    ① ensure_ascii=False — 기본값이면 한글 한 자가 '오' 여섯 자로 부푼다
    #    ② description(=docstring)도 함께 센다. 인자 스키마만 세면 절반이 빠지는데,
    #       모델을 움직이는 안내문이 대부분 거기 있다(routine_write 하나가 1,175자).
    schema_chars = sum(
        len(json.dumps(
            {"name": t.name, "description": t.description,
             "input_schema": t.args_schema.model_json_schema()},
            ensure_ascii=False,
        ))
        for t in tools
    )
    print(f"도구 스키마 총 {schema_chars:,}자 (대략 {schema_chars // 3:,} 토큰) "
          f"+ 페르소나({args.persona}) {len(prompt):,}자\n")

    llm = _build_llm(args.base_url, args.model, args.api_key).bind_tools(tools)
    system = SystemMessage(content=prompt)

    hits: dict[str, int] = {said: 0 for said, _ in CASES}
    times: list[float] = []
    for round_no in range(1, args.repeat + 1):
        for said, wanted in CASES:
            start = time.monotonic()
            try:
                reply = await llm.ainvoke([system, HumanMessage(content=said)])
            except Exception as e:  # noqa: BLE001 - 무엇이 터지든 나머지 케이스는 계속 잰다
                print(f"❌ {said!r}\n     터짐: {type(e).__name__}: {str(e)[:200]}")
                times.append(time.monotonic() - start)
                continue
            elapsed = time.monotonic() - start
            times.append(elapsed)

            called = [c["name"] for c in (reply.tool_calls or [])]
            hit = any(c in wanted for c in called)
            hits[said] += hit
            detail = ", ".join(
                f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})"
                for c in (reply.tool_calls or [])
            ) or f"(도구 안 부름) {str(reply.content)[:80]!r}"
            print(f"{'✅' if hit else '❌'} [{round_no}] {said!r}  {elapsed:5.1f}s\n"
                  f"     기대 {wanted} → 실제 {detail}")

    print(f"\n케이스별 성공 (각 {args.repeat}회)")
    for said, _ in CASES:
        print(f"  {hits[said]}/{args.repeat}  {said}")
    total = sum(hits.values())
    denom = len(CASES) * args.repeat
    print(f"\n{total}/{denom} 통과 ({total / denom:.0%})")
    if times:
        ordered = sorted(times)
        median = ordered[len(ordered) // 2]
        print(f"1회 왕복: 중앙값 {median:.1f}s / 최대 {max(times):.1f}s")
        # ReAct 루프는 한 메시지에 LLM을 2~4번 부른다. 봇 타임아웃은 그 합을 견뎌야 한다.
        print(f"→ 메시지 1건(호출 4회) 예상 {median * 4:.0f}s "
              f"(현재 AGENT_TIMEOUT_SEC=90)")
    return 0 if total == denom else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
