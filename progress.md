- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #1 갈아엎기](https://hwamgai.tistory.com/98)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #2 노션 MCP 연결](https://hwamgai.tistory.com/102)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #3 오류 수정하기 -- 10달러 다 썼다](https://hwamgai.tistory.com/106)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #3-1 오류 수정하기](https://hwamgai.tistory.com/107)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #3-2 오류 수정하기](https://hwamgai.tistory.com/110)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #4 sqlite 뜯어보기](https://hwamgai.tistory.com/111)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #4 trim 수정](https://hwamgai.tistory.com/112)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #5 유닉스, wireshark](https://hwamgai.tistory.com/113)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #6-1 Docker, AWS EC2 (11주차 과제)](https://hwamgai.tistory.com/118)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #6-2 Github Actions, CI/CD (11주차 과제)](https://hwamgai.tistory.com/119)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #6-3 공부 (11주차 과제)](https://hwamgai.tistory.com/121)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #7 vllm (12주차 과제)](https://hwamgai.tistory.com/122)

- [[개인프로젝트] 🎀공주비서🤵🏻‍♂️ 만들기 #8-1 블레이버스 API](https://hwamgai.tistory.com/123)  






<table>
  <thead>
    <tr>
      <th style="width: 5%;">#</th>
      <th style="width: 10%;">글</th>
      <th style="width: 13%;">주제</th>
      <th style="width: 45%;">핵심내용</th>
      <th style="width: 50%;">코드에서 볼 곳</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td>
      <td>#1 갈아엎기</td>
      <td>Discord + Claude 최소 봇</td>
      <td>
        - Codex판 폐기·Slack→Discord 전환<br>
        - 봇 (message content intent) 연결<br>
        - Anthropic SDK 단발 호출 + '아가씨' 페르소나. 도구·기억 없음
      </td>
      <td>
        ⚠️ 현재, 원본 discord_claude_chat_app.py는 삭제됨.<br>
        main commit에 내용이 들어있음.
      </td>
    </tr>
    <tr>
      <td>2</td>
      <td>#2 노션 MCP</td>
      <td>단발 호출 → ReAct 에이전트</td>
      <td>
        create_react_agent + SqliteSaver(채널별 기억) + npx @notionhq/notion-mcp-server(도구 24개)<br>
        MCP에 파일 업로드가 없어 REST 직접 호출로 우회<br>
        Pillow 5MB 압축
      </td>
      <td>
        agent.py:111-141(조립)<br>
        tools.py:21-42(MCP)<br>
        notion_tools.py:1-20·234+(사진인증)<br>
        bot.py:90-96(첨부→URL)
      </td>
    </tr>
    <tr>
      <td>3</td>
      <td>#3 오류 수정</td>
      <td>$10 토큰 소진 사고 원인분석</td>
      <td>
        5중 연쇄: 날짜 미주입 → 옛 대화 앵커링 → 도구가 체크박스 강제 → MCP 400 → 상한 없는 무한 재시도.<br>
        MAX_TOKENS는 출력 1회 상한이라 턴 반복을 못 막는다는 결론
      </td>
      <td>
        config.py:27(MAX_TOKENS의 한계)<br>
        bot.py:29-34(상한 상수의 존재 이유)
      </td>
    </tr>
    <tr>
      <td>4</td>
      <td>#3-1</td>
      <td>5종 버그 수정</td>
      <td>
        ① 노션 인테그레이션 권한<br>
        ② 안전벨트 3겹(recursion_limit 12 / 90s 타임아웃 / 예외 처리)<br>
        ③ KST 오늘 날짜 매 메시지 주입<br>
        ④ 사진·체크박스 분리(check/note)<br>
        ⑤ 전용 DS로 이전<br>
      </td>
      <td>
        bot.py:104-137(3겹)<br>
        agent.py:50-84(_prompt_with_today)<br>
        notion_tools.py:234+<br>
        config.py:51-53
      </td>
    </tr>
    <tr>
      <td>5</td>
      <td>#3-2</td>
      <td>관측 + 토큰 절감</td>
      <td>
        LangSmith 트레이싱 연결로 58.6K 토큰 폭증 확인 → "초미니 윈도우"로 10K대 고정.<br>
        기억은 노션(진짜 기록)에, 채팅은 명령 창구로. 애매하면 되묻기<br>
      </td>
      <td>
        agent.py:40-47(RECENT_WINDOW)<br>
        persona.py:21-26(실행 원칙/되묻기)<br>
        .env의 LANGSMITH_*
      </td>
    </tr>
    <tr>
      <td>6</td>
      <td>#4 sqlite 뜯어보기</td>
      <td>기억 저장소 해부</td>
      <td>
        data/memory.sqlite의 BLOB이 msgpack이라 열람 불가 → 디코더 작성.<br>
        checkpoints(단계 스냅샷)/writes(노드 결과) 구조 파악.<br>
        Claude엔 2~3턴만, 로컬엔 전량 보관<br>
      </td>
      <td>
        scripts/peek_memory.py:1-30(JsonPlusSerializer로 역직렬화)<br>
        bot.py:65(AsyncSqliteSaver)
      </td>
    </tr>
    <tr>
      <td>7</td>
      <td>#4 trim 수정</td>
      <td>윈도우 버그 근본수정</td>
      <td>
        trim_messages(max_tokens=3)는 도구 2회↑ 시 마지막 3개가 [Tool,AI,Tool]이 되어 Human이 밀려남 → 빈 목록 → BadRequestError.<br>
        자르는 단위를 메시지 개수 → 대화 턴으로 변경<br>
      </td>
      <td>
        agent.py:66-82 (버그 경위가 주석에 그대로 있음)
      </td>
    </tr>
    <tr>
      <td>8</td>
      <td>#5 유닉스·Wireshark</td>
      <td>10주차: 프로세스/메모리 + 패킷</td>
      <td>
        I/O bound(CPU 0%), 스레드 4→8 확장<br>
        npx 자식이 호출당 1쌍·수명 1~2초(0.2초 감시 루프로만 포착), RSS 142MB.<br>
        LISTEN 소켓 0개 = 순수 클라이언트.<br>
        HTTP /health는 평문 전량 노출 vs HTTPS는 SNI만 노출<br>
      </td>
      <td>
        scripts/inspect_process.sh<br>
        scripts/watch_children.sh<br>
        reports/process-memory.md<br>
        reports/captures/https-anthropic.pcapng
      </td>
    </tr>
    <tr>
      <td>9</td>
      <td>#6-1 Docker·EC2</td>
      <td>11주차 배포</td>
      <td>
        slim+Node20 이미지, .env는 굽지 않고 env_file 주입·data/는 볼륨.<br>
        EC2 Ubuntu t3.micro + swap 2GB, scp .env.<br>
        함정: main에서 브랜치를 따 webserver.py 누락 → 8000 안 열림 → week10 병합
      </td>
      <td>
        Dockerfile, docker-compose.yml, .dockerignore<br>
        커밋 043321b(병합 흔적)
      </td>
    </tr>
    <tr>
      <td>10</td>
      <td>#6-2 CI/CD</td>
      <td>GitHub Actions</td>
      <td>
        Elastic IP, Secrets 3종(EC2_HOST/USER/SSH_KEY), SSH 22 임시 개방, appleboy/ssh-action.<br>
        고정 sleep 8초로는 부족 → /health 200까지 3초×30회 readiness 재시도
      </td>
      <td>
        .github/workflows/deploy.yml<br>
        커밋 d01942d
      </td>
    </tr>
    <tr>
      <td>11</td>
      <td>#6-3 공부</td>
      <td>개념 정리편</td>
      <td>
        Dockerfile=레시피 / 이미지=붕어빵 틀 / 컨테이너=굽는 중.<br>
        배포 3방식(재빌드·레지스트리·save/load).<br>
        외부 접근 4관문: 퍼블릭IP → 보안그룹 → 포트매핑 → 0.0.0.0 바인딩
      </td>
      <td>
        config.py:75-83(HEALTH_HOST 바인딩 이유 주석)
      </td>
    </tr>
    <tr>
      <td>12</td>
      <td>#7 vLLM</td>
      <td>12주차: 봇의 뇌 교체</td>
      <td>
        Colab Pro L4에 Qwen/Qwen3.5-4B 서빙 + cloudflared 터널.<br>
        파서는 hermes 아니라 qwen3_xml, thinking ON이면 답을 못 냄(72초/None) → OFF.<br>
        미해결: --max-model-len 8192 vs 도구 24개 스키마
      </td>
      <td>
        agent.py:87-108(_build_model 스위치)<br>
        config.py:29-35<br>
        notebooks/vllm_qwen35.ipynb<br>
        scripts/chat_cli.py
      </td>
    </tr>
    <tr>
      <td>13</td>
      <td>#8-1 블레이버스 API</td>
      <td>비공식 API 역공학</td>
      <td>
        개발자도구(Fetch/XHR·Preserve log)로 엔드포인트 4개 확보.<br>
        쿠키 3종(access 1h / refresh 3d / cs_token) + double-submit CSRF → 401은 x-csrf-token 헤더로 해결.<br>
        JWT payload base64 디코딩으로 exp-iat=3600 확인
      </td>
      <td>
        🆕 secretary/blaybus_tools.py<br>
        scripts/blaybus_probe.py<br>
        config.py:63-73
      </td>
    </tr>
  </tbody>
</table>