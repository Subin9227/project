"""블레이버스 비공식 API 접속 가능성 확인 스크립트 (1단계 게이트).

왜 도구를 바로 안 만들고 이걸 먼저 돌리나:
    블레이버스는 공식 API가 없다. 우리는 브라우저가 보내는 요청을 그대로 복제한다.
    그런데 서버가 브라우저만의 무언가를 더 검사하면(토큰에 박힌 IP, 디바이스 지문 등)
    이 방식 자체가 성립하지 않는다. 그 판정을 먼저 내고, 통과해야 도구를 짠다.

확인하는 것 3가지:
    1) 파이썬에서 로그인이 되는가          → 안 되면 여기서 전체 중단
    2) 토큰이 몇 분짜리인가                 → 재로그인 주기의 근거
    3) 받은 쿠키로 조회 요청이 통과하는가   → 인증 복제 성공 여부

사용법:
    .venv/bin/python -m scripts.blaybus_probe                  # 로그인만
    .venv/bin/python -m scripts.blaybus_probe '/agenda?page=1'  # 조회까지
    .venv/bin/python -m scripts.blaybus_probe --selftest       # JWT 파서 점검

⚠️ 읽기 전용이다. 로그인과 GET만 보낸다 — 실수로 출퇴근이 찍히지 않도록.
"""

from __future__ import annotations

import base64
import json
import sys
import time

import httpx

from secretary.config import BLAYBUS_API_BASE, BLAYBUS_LOGIN_ID, BLAYBUS_PASSWORD


def decode_jwt_exp(token: str) -> int | None:
    """JWT의 만료시각(exp, 유닉스초)을 꺼낸다. 없거나 못 읽으면 None.

    서명 검증은 하지 않는다 — 우리는 발급자가 아니라 소지자고, 알고 싶은 건
    '언제 죽나'뿐이다. 그래서 라이브러리 없이 payload만 base64로 푼다.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # base64url은 패딩이 잘려 있어 되붙인다
    try:
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _human(seconds: float) -> str:
    if seconds < 0:
        return f"만료됨({int(-seconds // 60)}분 전)"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 남음"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}시간 남음"
    return f"{seconds / 86400:.1f}일 남음"


def main() -> None:
    if not (BLAYBUS_LOGIN_ID and BLAYBUS_PASSWORD):
        print(".env에 BLAYBUS_LOGIN_ID / BLAYBUS_PASSWORD를 넣어주세요.")
        sys.exit(1)

    with httpx.Client(base_url=BLAYBUS_API_BASE, timeout=20.0) as client:
        # 1) 로그인. 토큰 3종이 Set-Cookie로 내려와 client의 쿠키함에 자동 저장된다.
        print(f"POST {BLAYBUS_API_BASE}/auth/user/sign-in")
        resp = client.post(
            "/auth/user/sign-in",
            json={"loginId": BLAYBUS_LOGIN_ID, "password": BLAYBUS_PASSWORD},
            headers={"Origin": "https://www.blaybus.com", "Referer": "https://www.blaybus.com/"},
        )
        print(f"  → {resp.status_code}")
        if resp.status_code != 200:
            print(f"  {resp.text[:400]}")
            print("\n❌ 로그인 실패. 파이썬에서의 인증 복제가 불가능합니다.")
            sys.exit(1)

        now = time.time()
        for name in ("access_token", "refresh_token", "cs_token"):
            value = client.cookies.get(name)
            if not value:
                print(f"  {name:16} 쿠키 없음 ⚠️")
                continue
            exp = decode_jwt_exp(value)
            print(f"  {name:16} {_human(exp - now) if exp else 'exp 없음'}")

        print("\n✅ 로그인 성공. 토큰 확보.")

        if len(sys.argv) < 2:
            print("조회 경로를 인자로 주면 인증 통과까지 확인합니다. 예: /agenda?page=1")
            return

        # 2) 받은 쿠키로 실제 조회. 쿠키는 client가 자동으로 싣지만 x-csrf-token
        #    헤더는 우리가 넣어야 한다 (cs_token만 HttpOnly가 아니라서 브라우저에서도
        #    JS가 읽어 헤더에 복사하는 double-submit 방식을 쓴다).
        path = sys.argv[1]
        print(f"\nGET {BLAYBUS_API_BASE}{path}")
        resp = client.get(
            path,
            headers={
                "Origin": "https://www.blaybus.com",
                "Referer": "https://www.blaybus.com/",
                "x-csrf-token": client.cookies.get("cs_token", ""),
                "x-client-platform": "web",
            },
        )
        print(f"  → {resp.status_code}")
        print(f"  {resp.text[:800]}")
        # 404와 401을 반드시 구분해야 한다. 개발자도구 Name 열은 경로의 마지막
        # 조각만 보여주므로(`start` ← 실제 `/task/{id}/session/start`) 경로를
        # 잘못 짚기 쉽고, 그때 오는 404를 인증 실패로 오해하면 엉뚱한 데를 판다.
        if resp.status_code == 200:
            print("\n✅ 인증 복제 성공. blaybus_tools.py 구현 가능.")
        elif resp.status_code == 404:
            print("\n⚠️ 인증은 통과(로그인 상태 인정). 경로가 틀렸습니다 — 전체 URL을 확인하세요.")
        elif resp.status_code in (401, 403):
            print("\n❌ 인증 거부. 쿠키 외 추가 검사가 있습니다.")
        else:
            print(f"\n❓ 예상 밖 응답({resp.status_code}).")


def _selftest() -> None:
    """exp=2000000000 (2033-05-18) 를 넣은 가짜 JWT로 파서를 확인한다."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    assert decode_jwt_exp(f"{b64({'alg': 'HS256'})}.{b64({'exp': 2000000000})}.sig") == 2000000000
    assert decode_jwt_exp("not-a-jwt") is None
    assert decode_jwt_exp("a.b.c") is None  # base64가 깨진 경우
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
