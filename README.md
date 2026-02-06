# Point Farming Bot

`funding_arbitrage_bot`에서 핵심 실행/주문 흐름만 분리한 포인트 파밍 전용 봇.

## 핵심 전략
- 펀딩비 계산은 사용하지 않음
- 대상 티커 풀(약 10개 권장)에서 사이클마다 포지션 운용
- 티커별 조합은 아래 3개만 사용
  - `GRVT + HYNA`
  - `GRVT + VAR`
  - `HYNA + VAR`
- 롱/숏은 랜덤 선택
- 사이클 주기: `4~8시간` 랜덤

## 사이클 규칙
1. 기존 포지션 티커마다 유지/청산 판단
- 보유시간 `>= 36시간`이면 강제 청산
- 그 외는 `50%` 확률로 청산

2. 신규 진입 규칙
- 이미 유지된 티커는 신규 진입 대상에서 제외
- 목표 활성 티커 수(`POINT_TARGET_ACTIVE_TICKERS`)까지 비어 있는 슬롯만 신규 진입
- 동일 `ticker + pair`에서 과거 방향이 있었으면 그 방향 유지
  - 예: `BCH + (GRVT,HYNA)`가 `GRVT Long / HYNA Short`였다면
  - 이후 같은 pair 재진입 시 역방향(`GRVT Short / HYNA Long`) 금지
- 수량/레버리지는 거래소 제약을 반영해 계산
  - `qty_step`, `min_qty`, `min_notional`, `max_qty`를 양 거래소 동시 만족하도록 계산
  - pair 레버리지는 `min(사용자설정, 거래소1 max_leverage, 거래소2 max_leverage)` 적용

## 경로
- 프로젝트 루트: `Perp_DEX/bots/point_farming_bot`
- 엔트리: `src/main.py`

## 실행
```bash
cd /home/jeonguk/projects/Perp_DEX/bots/point_farming_bot
.venv/bin/python -m src.main
```

## shared_crypto_lib
독립 레포 기준으로 `libs/shared_crypto_lib`를 `git submodule`로 포함하는 방식 권장.
`src/main.py`는 아래 순서로 라이브러리 경로를 탐색한다.
1. `point_farming_bot/libs`
2. 기존 모노레포 경로 `Perp_DEX/libs`

## 주요 ENV
공통 거래소 키는 `BOT_ENV_PATH` 기준으로 로드.
기본값: `Perp_DEX/private/Funding_Arbitrage.env`

포인트 파밍 전용 키:
- `POINT_SYMBOLS=AVNT,IP,BERA,RESOLV,ADA,BCH,SOL,XRP,DOGE,LINK`
- `POINT_TARGET_ACTIVE_TICKERS=3`
- `POINT_RETAIN_PROBABILITY=0.5`
- `POINT_MAX_HOLD_HOURS=36`
- `POINT_TARGET_LEVERAGE=3`
- `POINT_MARGIN_PER_LEG_USD=20`
- `POINT_ROTATION_MIN_HOURS=4`
- `POINT_ROTATION_MAX_HOURS=8`
- `POINT_LOOP_INTERVAL_S=3`
- `POINT_ENABLE_VAR=1`
- `POINT_DRY_RUN=1`
- `TRADING_START_PAUSED=1`
- `POINT_RANDOM_SEED=123` (선택)

## 운영 순서 권장
1. `POINT_DRY_RUN=1`, `TRADING_START_PAUSED=1`로 시작
2. 심볼/페어 가용성 로그 확인
3. `TRADING_START_PAUSED=0`으로 사이클 동작 확인
4. 이상 없으면 `POINT_DRY_RUN=0`으로 실거래 전환
