Siu Autotrade GUI — Supertrend 77 v2

[요구 사항]
- Python 3.10 권장 (3.11도 가능)
- Git (자동 업데이트용 선택)

[설치/실행]
1) 처음 한 번:
   - 터미널에서 리포 폴더로 이동
   - python run_local.py
     (자동: 가상환경 생성 → 라이브러리 설치 → 최신 코드 pull → 앱 실행)

2) 다음부터는:
   - python run_local.py
   또는 Windows: start.bat 더블클릭
   또는 macOS/Linux: ./update.sh

[사용]
- 브라우저가 열리면 사이드바에서 거래소/심볼/주기, API키, 전략 파라미터 입력 후 ▶ 실행
- 백테스트 결과: 거래 리스트, 총손익, 승률, MDD, 샤프, 에쿼티 커브, 월별 손익
- CSV/설정 JSON 다운로드 가능

[주의]
- 교육/연구용 예시. 실제 거래 책임은 사용자 본인.
- API 키는 브라우저에서만 입력. 서버/코드에 저장하지 마세요(옵션 .env 저장 제외).
