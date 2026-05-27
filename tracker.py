"""
===================================================
  포지션 추적 스크립트
  - positions.json 읽어서
  - 진행중인 종목의 현재가 조회
  - 목표가/손절가 도달 여부 체크
  - 도달 시 Discord 알림 전송
  - positions.json 상태 업데이트
===================================================
"""

import os
import json
import requests
import logging
import FinanceDataReader as fdr

from datetime import datetime, timedelta

WEBHOOK_STOCK  = os.getenv("WEBHOOK_STOCK", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

def get_pullback_ai(name: str, code: str, entry_price: int, cur_close: float,
                    vol_ratio: float, fib_382: float, fib_618: float,
                    ma5: float, days_after: int, cond_count: int) -> str:
    if not GEMINI_API_KEY:
        return ""
    for model in GEMINI_MODELS:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{
                        "parts": [{"text": f"""당신은 퀀트 트레이더입니다.
아래 데이터를 보고 이 종목이 진입 가능한 눌림목인지 판단하세요.

종목: {name} ({code})
급등가: {entry_price:,}원
현재가: {cur_close:,}원 ({((cur_close-entry_price)/entry_price*100):+.1f}%)
급등 후 경과일: {days_after}일
거래량 비율 (급등일 대비): {vol_ratio*100:.0f}%
피보나치 구간: {fib_618:,.0f}~{fib_382:,.0f}원
MA5: {ma5:,.0f}원
조건 충족: {cond_count}/4

판단 기준:
- 거래량이 충분히 줄었는가 (50% 이하)
- 가격이 피보나치 눌림 구간에 있는가
- MA5 지지를 받고 있는가
- 급등 후 적정 시간이 지났는가

반드시 아래 형식으로만 답하세요:
[눌림 판단] 진입 가능 / 아직 이름 / 눌림 아님
[이유] 2줄 이내로"""}]
                    }]
                },
                timeout=30,
            )
            if resp.status_code == 429:
                continue
            resp.raise_for_status()
            content = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            content = content.replace("```", "").replace("**", "").strip()
            return content
        except Exception as e:
            logging.error(f"눌림 AI 실패 [{name}]: {e}")
            continue
    return ""
POSITION_FILE = "positions.json"

LOG_FILE = f"tracker_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

def send_discord_message(message: str) -> None:
    if not WEBHOOK_STOCK:
        print(message)
        return
    try:
        requests.post(WEBHOOK_STOCK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error(f"Discord 오류: {e}")

# ==================================================
# 현재가 조회
# ==================================================
def get_current_price(code: str) -> float | None:
    try:
        today = datetime.today()
        start = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        data  = fdr.DataReader(code, start)
        data  = data.dropna()
        if len(data) == 0:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logging.error(f"현재가 조회 실패 [{code}]: {e}")
        return None

# ==================================================
# 포지션 체크
# ==================================================
def check_position(pos: dict) -> dict:
    code         = pos["code"]
    name         = pos["name"]
    entry_price  = pos["entry_price"]
    stop_loss    = pos["stop_loss"]
    target_1     = pos["target_price_1"]
    target_2     = pos["target_price_2"]
    status       = pos["status"]

    # 이미 완료된 포지션 스킵
    if status in ["2차도달", "손절"]:
        return pos

    current_price = get_current_price(code)
    if current_price is None:
        print(f"  ⚠️ {name} 현재가 조회 실패")
        return pos

    pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)
    print(f"  📍 {name} | 현재가: {current_price:,}원 | 수익률: {pnl_pct:+.2f}%")

    updated = {**pos, "current_price": current_price, "pnl_pct": pnl_pct}

    # 손절 체크
    if current_price <= stop_loss:
        updated["status"] = "손절"
        updated["result"] = f"손절 ({pnl_pct:+.2f}%)"
        updated["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg = (
            f"🛑 손절 발생\n\n"
            f"종목: {name} ({code})\n"
            f"진입가:   {entry_price:,}원\n"
            f"현재가:   {current_price:,}원\n"
            f"손절가:   {stop_loss:,}원\n"
            f"수익률:   {pnl_pct:+.2f}%\n"
        )
        send_discord_message(msg)

    # 1차 목표 도달
    elif current_price >= target_2 and status == "진행중":
        updated["status"] = "2차도달"
        updated["result"] = f"2차목표 달성 ({pnl_pct:+.2f}%)"
        updated["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg = (
            f"🚀 2차 목표 달성!\n\n"
            f"종목: {name} ({code})\n"
            f"진입가:    {entry_price:,}원\n"
            f"현재가:    {current_price:,}원\n"
            f"2차목표:   {target_2:,}원\n"
            f"수익률:    {pnl_pct:+.2f}%\n\n"
            f"✅ 나머지 전량 청산"
        )
        send_discord_message(msg)

    elif current_price >= target_1 and status == "진행중":
        updated["status"] = "1차도달"
        updated["result"] = f"1차목표 달성 ({pnl_pct:+.2f}%)"

        msg = (
            f"🎯 1차 목표 달성!\n\n"
            f"종목: {name} ({code})\n"
            f"진입가:    {entry_price:,}원\n"
            f"현재가:    {current_price:,}원\n"
            f"1차목표:   {target_1:,}원\n"
            f"수익률:    {pnl_pct:+.2f}%\n\n"
            f"✅ 절반 청산\n"
            f"🔄 손절가를 본전({entry_price:,}원)으로 올리세요"
        )
        send_discord_message(msg)

    return updated

# ==================================================
# 눌림 감지
# ==================================================
def check_pullback(pos: dict) -> None:
    code        = pos["code"]
    name        = pos["name"]
    entry_price = pos["entry_price"]

    try:
        today = datetime.today()
        start = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        data  = fdr.DataReader(code, start)
        data  = data.dropna()

        if len(data) < 3:
            return

        # 급등일 찾기 (entry_price 기준 ±3%)
        entry_row = None
        for i in range(len(data)):
            if abs(float(data["Close"].iloc[i]) - entry_price) / entry_price < 0.03:
                entry_row = i
                break

        if entry_row is None:
            return

        # 이미 급등일이면 스킵
        if entry_row == len(data) - 1:
            return

        surge_vol      = float(data["Volume"].iloc[entry_row])
        surge_body_low = min(float(data["Open"].iloc[entry_row]), float(data["Close"].iloc[entry_row]))
        cur_close      = float(data["Close"].iloc[-1])
        cur_vol        = float(data["Volume"].iloc[-1])
        days_after     = len(data) - 1 - entry_row

        # 눌림 조건
        vol_ratio  = cur_vol / surge_vol if surge_vol > 0 else 1
        vol_ok     = vol_ratio <= 0.5

        fib_382    = entry_price - (entry_price - surge_body_low) * 0.382
        fib_618    = entry_price - (entry_price - surge_body_low) * 0.618
        price_ok   = fib_618 <= cur_close <= fib_382

        ma5        = float(data["Close"].tail(5).mean()) if len(data) >= 5 else cur_close
        ma5_ok     = abs(cur_close - ma5) / ma5 <= 0.03

        candles_ok = days_after >= 1

        cond_count = sum([vol_ok, price_ok, ma5_ok, candles_ok])

        if cond_count >= 3:
            # AI 눌림 판단
            ai_result = get_pullback_ai(
                name, code, entry_price, cur_close,
                vol_ratio, fib_382, fib_618, ma5, days_after, cond_count
            )

            if "진입 가능" not in ai_result:
                print(f"  ⏭ AI 눌림 아님: {name} → {ai_result[:40]}")
                return

            pnl_pct  = round((cur_close - entry_price) / entry_price * 100, 2)
            cond_str = "\n".join(filter(None, [
                f"  ✅ 거래량 급등일 대비 {vol_ratio*100:.0f}% (50% 이하)" if vol_ok    else f"  ❌ 거래량 {vol_ratio*100:.0f}% (아직 높음)",
                f"  ✅ 피보나치 구간 ({fib_618:,.0f}~{fib_382:,.0f}원)"    if price_ok  else f"  ❌ 피보나치 구간 벗어남",
                f"  ✅ MA5 근접 ({ma5:,.0f}원)"                             if ma5_ok    else f"  ❌ MA5 거리 있음",
                f"  ✅ 급등 후 {days_after}일 경과"                          if candles_ok else "",
            ]))

            msg = (
                f"📌 눌림 진입 가능\n\n"
                f"종목: {name} ({code})\n"
                f"급등가:  {entry_price:,}원\n"
                f"현재가:  {cur_close:,}원  ({pnl_pct:+.1f}%)\n\n"
                f"🤖 AI 판단\n  {ai_result}\n\n"
                f"조건 충족 {cond_count}/4\n"
                f"{cond_str}\n\n"
                f"🎯 진입 구간: {fib_618:,.0f}~{fib_382:,.0f}원\n"
                f"🛑 손절가: {pos['stop_loss']:,}원"
            )
            print(f"  📌 눌림 진입 가능: {name} ({cur_close:,}원)")
            send_discord_message(msg)

    except Exception as e:
        logging.error(f"눌림 감지 실패 [{code}]: {e}")


# ==================================================
# 오래된 포지션 정리 (30일 이상 미결)
# ==================================================
def cleanup_old_positions(positions: list) -> list:
    cleaned = []
    now     = datetime.now()
    for pos in positions:
        try:
            entered = datetime.strptime(pos["entered_at"], "%Y-%m-%d %H:%M:%S")
            days    = (now - entered).days
            if days > 30 and pos["status"] == "진행중":
                pos["status"] = "기간만료"
                pos["result"] = "30일 미달성"
                pos["closed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        cleaned.append(pos)
    return cleaned

# ==================================================
# 메인
# ==================================================
def main() -> None:
    print("=" * 50)
    print(f"📍 포지션 추적 시작")
    print(f"   실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    if not os.path.exists(POSITION_FILE):
        print("  ℹ️ positions.json 없음 — 추적할 포지션이 없습니다")
        return

    with open(POSITION_FILE, "r", encoding="utf-8") as f:
        positions = json.load(f)

    active = [p for p in positions if p["status"] in ["진행중", "1차도달"]]
    done   = [p for p in positions if p["status"] not in ["진행중", "1차도달"]]

    print(f"\n  추적 중: {len(active)}개 | 완료: {len(done)}개\n")

    if not active:
        print("  ℹ️ 진행 중인 포지션 없음")
        return

    updated_active = [check_position(pos) for pos in active]

    print("\n  📌 눌림 감지 체크 중...\n")
    for pos in active:
        if pos["status"] == "진행중":
            check_pullback(pos)
    all_positions  = cleanup_old_positions(updated_active + done)

    with open(POSITION_FILE, "w", encoding="utf-8") as f:
        json.dump(all_positions, f, ensure_ascii=False, indent=2)

    print(f"\n  💾 positions.json 업데이트 완료")

    # 현황 요약 Discord 전송
    active_now = [p for p in all_positions if p["status"] in ["진행중", "1차도달"]]
    if active_now:
        summary = "📍 포지션 현황\n\n"
        for p in active_now:
            pnl = p.get("pnl_pct", 0)
            summary += (
                f"• {p['name']} ({p['status']})\n"
                f"  진입: {p['entry_price']:,}원 | 현재: {p.get('current_price', '?'):,}원 | {pnl:+.2f}%\n"
                f"  손절: {p['stop_loss']:,}원 | 1차: {p['target_price_1']:,}원 | 2차: {p['target_price_2']:,}원\n\n"
            )
        send_discord_message(summary)

if __name__ == "__main__":
    main()