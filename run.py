"""공주비서 실행 진입점.

    python run.py

이 파일은 얇다. 실제 로직은 secretary/ 안에 있고, 여기선 봇의
main() 코루틴을 asyncio 이벤트 루프에 올려 실행하기만 한다.
"""

from __future__ import annotations

import asyncio

from secretary.bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n공주비서를 종료합니다. 안녕히 계세요, 아가씨.")
