"""
===================================================
  포지션 추적 스크립트 v2.0
  변경사항 (v1 → v2.0):
    - watchlist.json WATCH 종목 눌림 조건 충족 시 자동 진입
    - 진입 조건: pullback 3~10%, MA20 위, RSI>=50, vol>=0.8*20d_avg
    - WATCH 만료: 15거래일 초과 OR below_ma20_streak >= 3
    - STOP=2ATR / TP1=2ATR / TP2=3ATR (신규 진입분)
    - TP1 도달 시 stop→entry 자동 업데이트 + remaining_ratio=0.5
    - TP2 도달 시 remaining_ratio=0.0 + 청산
    - MAX_POSITIONS=8 (진행중+1차도달 합산)
    - 기존 포지션 하위호환 (.get() 기본값 처리)
    - ATR fallback 3% + logging.warning
    - 진입 순서: positions 저장 → watchlist 삭제
===================================================
"""

import os
import json
import math
import requests
import logging
import FinanceDataReader as fdr
import pandas as pd

from datetime import datetime, timedelta

# ==================================================
# 상수
# ==================================================
MAX_POSITIONS    = 8
WATCH_EXPIRE_DAYS = 15
BELOW_MA20_LIMIT  = 3
PULLBACK_MIN      = 3.0   # %
PULLBACK_MAX      = 10.0  # %
VOL_RATIO_MIN     = 0.8   # 20일 평균 대비
RSI_MIN           = 50.0
STOP_ATR_MULT     = 2.0
TP1_ATR_MULT      = 2.0
TP2_ATR_MULT      = 3.0

WEBHOOK_STOCK  = os.getenv("WEBHOOK_STOCK", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

POSITION_FILE  = "positions.json"
WATCHLIST_FILE = "watchlist.json"

LOG_FILE = f"tracker_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# ==================================================
# Discord
# ==================================================
def send_discord_message(message: str) -> None:
    if not WEBHOOK_STOCK:
        print(message)
        return
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        try:
            requests.post(WEBHOOK_STOCK, json={"content": chunk}, timeout=10)
        except Exception as e:
            logging.error(f"Discord 오류: {e}")

# ==================================================
# 현재가 + OHLCV 조회 (눌림 조건용)
# ==================================================
def get_ohlcv(code: str, days: int = 25) -> pd.DataFrame | None:
    try:
        today = datetime.today()
        start = (today - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        data  = fdr.DataReader(code, start).dropna()
        if len(data) < 5:
            return None
        return data
    except Exception as e:
        logging.error(f"OHLCV 조회 실패 [{code}]: {e}")
        return None

def get_current_price(code: str) -> float | None:
    data = get_ohlcv(code, days=5)
    if data is None or len(data) == 0:
        return None
    return float(data["Close"].iloc[-1])

def calculate_rsi(close: pd.Series, period: int = 14) -> float:
    delta    = close.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)

def calculate_atr(data: pd.DataFrame, period: int = 14) -> float:
    high_low   = data["High"] - data["Low"]
    high_close = (data["High"] - data["Close"].shift()).abs()
    low_close  = (data["Low"]  - data["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return float(true_range.rolling(period).mean().iloc[-1])

# ==================================================
# WATCH 만료 처리 + below_ma20_streak 갱신
# ==================================================
def process_watchlist_expiry(watchlist: list) -> list:
    now     = datetime.now()
    active  = []
    for w in watchlist:
        code = w["code"]
        name = w["name"]

        # 거래일 기준 경과 계산 (캘린더일로 근사: *5/7)
        try:
            watch_date = datetime.strptime(w["watch_date"], "%Y-%m-%d")
            cal_days   = (now - watch_date).days
            trade_days = int(cal_days * 5 / 7)
        except Exception:
            trade_days = 0

        if trade_days > WATCH_EXPIRE_DAYS:
            print(f"  🗑️ WATCH 만료 (기간): {name} ({trade_days}거래일)")
            logging.warning(f"WATCH 만료(기간): {code} {name}")
            continue

        # MA20 이탈 streak 갱신
        data = get_ohlcv(code, days=25)
        if data is not None and len(data) >= 20:
            cur_close = float(data["Close"].iloc[-1])
            ma20      = float(data["Close"].tail(20).mean())
            if cur_close < ma20:
                w["below_ma20_streak"] = w.get("below_ma20_streak", 0) + 1
            else:
                w["below_ma20_streak"] = 0

            if w.get("below_ma20_streak", 0) >= BELOW_MA20_LIMIT:
                print(f"  🗑️ WATCH 만료 (MA20 {BELOW_MA20_LIMIT}일 이탈): {name}")
                logging.warning(f"WATCH 만료(MA20): {code} {name}")
                continue

        active.append(w)
    return active

# ==================================================
# 눌림 조건 체크 → 진입 후보 반환
# ==================================================
def evaluate_pullback_candidates(watchlist: list, existing_codes: set) -> list:
    candidates = []
    for w in watchlist:
        code        = w["code"]
        name        = w["name"]
        watch_price = w["watch_price"]

        if code in existing_codes:
            continue

        data = get_ohlcv(code, days=25)
        if data is None or len(data) < 20:
            continue

        cur_close   = float(data["Close"].iloc[-1])
        ma20        = float(data["Close"].tail(20).mean())
        avg20_vol   = float(data["Volume"].tail(20).mean())
        cur_vol     = float(data["Volume"].iloc[-1])
        rsi         = calculate_rsi(data["Close"])
        atr         = calculate_atr(data)
        if math.isnan(atr) or atr <= 0:
            atr = cur_close * 0.03
            logging.warning(f"[{code}] evaluate_pullback ATR 이상 → 3% fallback")

        pullback_pct = (watch_price - cur_close) / watch_price * 100

        # 눌림 조건 4개
        cond_pullback = PULLBACK_MIN <= pullback_pct <= PULLBACK_MAX
        cond_ma20     = cur_close > ma20
        cond_rsi      = rsi >= RSI_MIN
        cond_vol      = avg20_vol > 0 and (cur_vol / avg20_vol) >= VOL_RATIO_MIN

        if not (cond_pullback and cond_ma20 and cond_rsi and cond_vol):
            continue

        candidates.append({
            "watch":      w,
            "cur_close":  cur_close,
            "atr":        atr,
            "pullback":   round(pullback_pct, 2),
            "rsi":        rsi,
        })
        print(f"  ✅ 눌림 후보: {name} | pullback:{pullback_pct:.1f}% | RSI:{rsi} | ATR:{atr:,.0f}")

    # score 높은 순 정렬
    candidates.sort(key=lambda x: x["watch"]["score"], reverse=True)
    return candidates

# ==================================================
# 신규 진입 생성
# ==================================================
def create_position(w: dict, cur_close: float, atr: float) -> dict:
    code       = w["code"]
    watch_price = w["watch_price"]

    # ATR fallback
    if atr is None or math.isnan(atr) or atr <= 0:
        atr = watch_price * 0.03
        logging.warning(f"[{code}] ATR 없음 → 3% fallback 사용 (watch_price={watch_price:,})")

    entry_price = int(cur_close)
    stop_loss   = int(cur_close - STOP_ATR_MULT * atr)
    tp1         = int(cur_close + TP1_ATR_MULT  * atr)
    tp2         = int(cur_close + TP2_ATR_MULT  * atr)

    return {
        "code":               code,
        "name":               w["name"],
        "group":              w.get("group", ""),
        "entry_price":        entry_price,
        "stop_loss":          stop_loss,
        "target_price_1":     tp1,
        "target_price_2":     tp2,
        "entered_at":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":             "진행중",
        "result":             None,
        "verdict":            w.get("verdict", ""),
        "remaining_ratio":    1.0,
        "entry_atr":          round(atr, 2),
        # 메타데이터 (분석용)
        "entry_rsi":          w.get("entry_rsi", 0),
        "entry_daily_change": w.get("entry_daily_change", 0),
        "entry_5day_change":  w.get("entry_5day_change", 0),
        "entry_score":        w.get("entry_score", 0),
        "watch_price":        watch_price,
        "watch_date":         w.get("watch_date", ""),
    }

# ==================================================
# 기존 포지션 체크 (손절/TP1/TP2)
# ==================================================
def check_position(pos: dict) -> dict:
    code            = pos["code"]
    name            = pos["name"]
    entry_price     = pos["entry_price"]
    stop_loss       = pos["stop_loss"]
    target_1        = pos["target_price_1"]
    target_2        = pos["target_price_2"]
    status          = pos["status"]
    remaining_ratio = pos.get("remaining_ratio", 1.0)

    if status in ["2차도달", "손절", "기간만료"]:
        return pos

    current_price = get_current_price(code)
    if current_price is None:
        print(f"  ⚠️ {name} 현재가 조회 실패")
        return pos

    pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)
    print(f"  📍 {name} | 현재가: {current_price:,}원 | 수익률: {pnl_pct:+.2f}% | 잔여: {int(remaining_ratio*100)}%")

    updated = {**pos, "current_price": current_price, "pnl_pct": pnl_pct}

    # 손절
    if current_price <= stop_loss:
        updated["status"]    = "손절"
        updated["result"]    = f"손절 ({pnl_pct:+.2f}%)"
        updated["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updated["remaining_ratio"] = 0.0
        send_discord_message(
            f"🛑 손절 발생\n\n"
            f"종목: {name} ({code})\n"
            f"진입가:   {entry_price:,}원\n"
            f"현재가:   {current_price:,}원\n"
            f"손절가:   {stop_loss:,}원\n"
            f"수익률:   {pnl_pct:+.2f}%\n"
            f"잔여물량: {int(remaining_ratio*100)}% 전량 손절"
        )

    # TP2 도달
    elif current_price >= target_2 and status in ["진행중", "1차도달"]:
        updated["status"]    = "2차도달"
        updated["result"]    = f"2차목표 달성 ({pnl_pct:+.2f}%)"
        updated["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updated["remaining_ratio"] = 0.0
        send_discord_message(
            f"🚀 2차 목표 달성!\n\n"
            f"종목: {name} ({code})\n"
            f"진입가:    {entry_price:,}원\n"
            f"현재가:    {current_price:,}원\n"
            f"2차목표:   {target_2:,}원\n"
            f"수익률:    {pnl_pct:+.2f}%\n\n"
            f"✅ 나머지 {int(remaining_ratio*100)}% 전량 청산"
        )

    # TP1 도달 (진행중일 때만 — 중복 실행 방지)
    elif current_price >= target_1 and status == "진행중":
        updated["status"]          = "1차도달"
        updated["result"]          = f"1차목표 달성 ({pnl_pct:+.2f}%)"
        updated["remaining_ratio"] = 0.5
        updated["stop_loss"]       = entry_price  # 브레이크이븐 자동 적용
        send_discord_message(
            f"🎯 1차 목표 달성!\n\n"
            f"종목: {name} ({code})\n"
            f"진입가:    {entry_price:,}원\n"
            f"현재가:    {current_price:,}원\n"
            f"1차목표:   {target_1:,}원\n"
            f"수익률:    {pnl_pct:+.2f}%\n\n"
            f"✅ 50% 익절\n"
            f"🔄 손절가 자동 본전 이동: {entry_price:,}원\n"
            f"🎯 잔여 50% → 2차목표 {target_2:,}원 대기"
        )

    return updated

# ==================================================
# positions.json / watchlist.json I/O
# ==================================================
def load_json(path: str, default) -> list:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"JSON 로딩 실패 [{path}]: {e}")
        return default

def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==================================================
# 메인
# ==================================================
def main() -> None:
    print("=" * 50)
    print(f"📍 포지션 추적 v2.0 시작")
    print(f"   실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    positions = load_json(POSITION_FILE, [])
    watchlist = load_json(WATCHLIST_FILE, [])

    # --------------------------------------------------
    # 1. 기존 포지션 체크 (손절/TP1/TP2)
    # --------------------------------------------------
    active = [p for p in positions if p["status"] in ["진행중", "1차도달"]]
    done   = [p for p in positions if p["status"] not in ["진행중", "1차도달"]]

    print(f"\n  포지션 — 활성: {len(active)}개 | 완료: {len(done)}개")

    updated_active = [check_position(p) for p in active]
    positions      = updated_active + done
    save_json(POSITION_FILE, positions)

    # --------------------------------------------------
    # 2. WATCH 만료 처리
    # --------------------------------------------------
    print(f"\n  WATCH — 현재: {len(watchlist)}개")
    watchlist = process_watchlist_expiry(watchlist)

    # --------------------------------------------------
    # 3. 눌림 조건 체크 → 신규 진입
    # --------------------------------------------------
    active_now    = [p for p in positions if p["status"] in ["진행중", "1차도달"]]
    active_count  = len(active_now)
    slots         = max(0, MAX_POSITIONS - active_count)
    existing_codes = {p["code"] for p in active_now}

    print(f"\n  슬롯: {slots}개 여유 (MAX={MAX_POSITIONS}, 현재={active_count}개)\n")
    print("  📌 눌림 조건 체크 중...\n")

    new_positions  = []
    entered_codes  = set()

    if slots > 0 and watchlist:
        candidates = evaluate_pullback_candidates(watchlist, existing_codes)

        for c in candidates[:slots]:
            w       = c["watch"]
            code    = w["code"]
            name    = w["name"]

            new_pos = create_position(w, c["cur_close"], c["atr"])
            new_positions.append(new_pos)
            entered_codes.add(code)

            send_discord_message(
                f"🟢 신규 진입 (눌림)\n\n"
                f"종목: {name} ({code})\n"
                f"감시가:    {w['watch_price']:,}원\n"
                f"진입가:    {new_pos['entry_price']:,}원  "
                f"(눌림 {c['pullback']:.1f}%)\n"
                f"RSI:       {c['rsi']}\n\n"
                f"🛑 손절가:  {new_pos['stop_loss']:,}원  (-{STOP_ATR_MULT:.0f}ATR)\n"
                f"🎯 1차목표: {new_pos['target_price_1']:,}원  (+{TP1_ATR_MULT:.0f}ATR)\n"
                f"🚀 2차목표: {new_pos['target_price_2']:,}원  (+{TP2_ATR_MULT:.0f}ATR)\n"
                f"📐 ATR:     {new_pos['entry_atr']:,}원\n"
                f"⭐ 원래 score: {w.get('score', 0)}점"
            )
            print(f"  🟢 진입: {name} @ {new_pos['entry_price']:,}원")

    # --------------------------------------------------
    # 4. positions 저장 → watchlist 삭제 (순서 중요)
    # --------------------------------------------------
    if new_positions:
        positions = load_json(POSITION_FILE, [])  # 최신 reload
        positions.extend(new_positions)
        save_json(POSITION_FILE, positions)
        print(f"\n  💾 positions.json 업데이트 ({len(new_positions)}개 신규 진입)")

        watchlist = [w for w in watchlist if w["code"] not in entered_codes]
        save_json(WATCHLIST_FILE, watchlist)
        print(f"  🗑️ watchlist에서 진입 종목 {len(entered_codes)}개 제거")
    else:
        save_json(WATCHLIST_FILE, watchlist)

    # --------------------------------------------------
    # 5. 현황 요약 Discord
    # --------------------------------------------------
    positions   = load_json(POSITION_FILE, [])
    active_now  = [p for p in positions if p["status"] in ["진행중", "1차도달"]]

    if active_now:
        summary = f"📍 포지션 현황 ({len(active_now)}/{MAX_POSITIONS})\n\n"
        for p in active_now:
            pnl   = p.get("pnl_pct", 0)
            ratio = int(p.get("remaining_ratio", 1.0) * 100)
            summary += (
                f"• {p['name']} ({p['status']}) [{ratio}%]\n"
                f"  진입: {p['entry_price']:,}원 | 현재: {p.get('current_price', p['entry_price']):,}원 | {pnl:+.2f}%\n"
                f"  손절: {p['stop_loss']:,}원 | 1차: {p['target_price_1']:,}원 | 2차: {p['target_price_2']:,}원\n\n"
            )
        send_discord_message(summary)

    print(f"\n  WATCH 잔여: {len(watchlist)}개")
    print(f"  활성 포지션: {len(active_now)}개")
    print(f"\n✅ tracker v2.0 완료")

if __name__ == "__main__":
    main()