"""
===================================================
  코인 수급 탐지 스캐너 v1.2
  변경사항:
    - 4시간봉 선필터로 불필요한 API 호출 제거
    - ThreadPoolExecutor 병렬 처리 (max_workers=5)
    - ticker 1차 필터링 (완전 죽은 코인 제거)
    - 전체 소요시간 7분 → 1~2분 목표
===================================================
"""

import os
import time
import logging
import json
import requests
import pandas as pd

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ==================================================
# 환경 변수
# ==================================================
WEBHOOK_COIN = os.getenv("WEBHOOK_COIN", "")

# ==================================================
# 로깅
# ==================================================
LOG_FILE = f"coin_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# ==================================================
# 스테이블코인 제외 목록
# ==================================================
STABLE_COINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP",
    "GUSD", "FRAX", "LUSD", "SUSD", "UST", "FDUSD"
}

ALERT_CACHE_FILE = "coin_alert_cache.json"
results_lock     = Lock()

def load_alert_cache() -> dict:
    try:
        if os.path.exists(ALERT_CACHE_FILE):
            with open(ALERT_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_alert_cache(cache: dict) -> None:
    try:
        with open(ALERT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"알림 캐시 저장 실패: {e}")

def is_recently_alerted(code: str, cache: dict, hours: int = 4) -> bool:
    if code not in cache:
        return False
    last_alert = datetime.fromisoformat(cache[code])
    return (datetime.now() - last_alert).total_seconds() < hours * 3600

def send_discord_message(message: str) -> None:
    if not WEBHOOK_COIN:
        print("[Discord] WEBHOOK_COIN 미설정\n", message)
        return
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        try:
            resp = requests.post(WEBHOOK_COIN, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logging.error(f"Discord 오류: {e}")

def safe_value(v):
    if isinstance(v, float) and (v != v):
        return None
    return v

def sanitize_dict(d: dict) -> dict:
    return {k: safe_value(v) for k, v in d.items()}

def get_upbit_markets() -> list:
    try:
        resp = requests.get("https://api.upbit.com/v1/market/all", timeout=10)
        resp.raise_for_status()
        return [m["market"] for m in resp.json() if m["market"].startswith("KRW-")]
    except Exception as e:
        logging.error(f"마켓 조회 실패: {e}")
        return []

def get_candles(market: str, unit: int, count: int = 30) -> pd.DataFrame:
    try:
        url  = f"https://api.upbit.com/v1/candles/minutes/{unit}"
        resp = requests.get(url, params={"market": market, "count": count}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.rename(columns={
            "candle_date_time_kst":    "datetime",
            "opening_price":           "Open",
            "high_price":              "High",
            "low_price":               "Low",
            "trade_price":             "Close",
            "candle_acc_trade_volume": "Volume",
            "candle_acc_trade_price":  "TradeValue",
        })
        df = df[["datetime", "Open", "High", "Low", "Close", "Volume", "TradeValue"]]
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        logging.error(f"캔들 조회 실패 [{market}/{unit}분]: {e}")
        return pd.DataFrame()

def get_ticker(markets: list) -> dict:
    try:
        codes = ",".join(markets)
        resp  = requests.get(
            "https://api.upbit.com/v1/ticker",
            params={"markets": codes},
            timeout=10
        )
        resp.raise_for_status()
        return {t["market"]: t for t in resp.json()}
    except Exception as e:
        logging.error(f"시세 조회 실패: {e}")
        return {}

# ==================================================
# 시장 상황 체크
# ==================================================
def get_fear_greed_index() -> dict:
    try:
        resp  = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        resp.raise_for_status()
        data  = resp.json()["data"][0]
        return {"value": int(data["value"]), "label": data["value_classification"]}
    except Exception as e:
        logging.error(f"공포탐욕지수 조회 실패: {e}")
        return {"value": 50, "label": "Neutral"}

def get_btc_dominance() -> float:
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        resp.raise_for_status()
        return round(resp.json()["data"]["market_cap_percentage"]["btc"], 2)
    except Exception as e:
        logging.error(f"도미넌스 조회 실패: {e}")
        return 50.0

def get_kimchi_premium() -> float:
    try:
        upbit_resp    = requests.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-BTC"}, timeout=10)
        upbit_price   = upbit_resp.json()[0]["trade_price"]
        binance_resp  = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
        binance_price = float(binance_resp.json()["price"])
        fx_resp       = requests.get("https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD", timeout=10)
        usd_krw       = fx_resp.json()[0]["basePrice"]
        return round((upbit_price / (binance_price * usd_krw) - 1) * 100, 2)
    except Exception as e:
        logging.error(f"김치프리미엄 조회 실패: {e}")
        return 0.0

def check_btc_trend() -> dict:
    try:
        df    = get_candles("KRW-BTC", 240, count=30)
        if df.empty or len(df) < 20:
            return {"bullish": False, "ma20": 0, "rsi": 50, "price": 0}
        close    = df["Close"]
        ma20     = float(close.tail(20).mean())
        cur      = float(close.iloc[-1])
        delta    = close.diff()
        gain     = delta.where(delta > 0, 0.0)
        loss     = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=13, min_periods=14).mean()
        avg_loss = loss.ewm(com=13, min_periods=14).mean()
        rs       = avg_gain / avg_loss.replace(0, float("nan"))
        rsi      = float((100 - (100 / (1 + rs))).iloc[-1])
        return {"bullish": cur > ma20, "ma20": round(ma20), "rsi": round(rsi, 1), "price": round(cur)}
    except Exception as e:
        logging.error(f"BTC 추세 체크 실패: {e}")
        return {"bullish": False, "ma20": 0, "rsi": 50, "price": 0}

def format_market_status(market_status: dict, market_score: int) -> str:
    fg       = market_status["fear_greed"]
    btc      = market_status["btc"]
    dom      = market_status["dominance"]
    kp       = market_status["kimchi_premium"]
    fg_emoji = "😱" if fg["value"] < 25 else "😰" if fg["value"] < 45 else "😐" if fg["value"] < 55 else "😊" if fg["value"] < 75 else "🤑"
    kp_emoji = "⚠️ 과열" if kp > 10 else "✅"
    if market_score >= 3:   signal = "🟢 매수 시그널 — 시장 상황 양호"
    elif market_score == 2: signal = "🟡 중립 — 선별적 진입"
    else:                   signal = "🔴 전체 매수 금지 — 시장 상황 불량"
    return (
        f"📊 코인 시장 상황 요약\n\n"
        f"  {signal}\n\n"
        f"  BTC 추세:     {'✅' if btc['bullish'] else '❌'} {'상승' if btc['bullish'] else '하락'} (RSI {btc['rsi']} / {btc['price']:,}원)\n"
        f"  공포탐욕:     {fg_emoji} {fg['value']} ({fg['label']})\n"
        f"  BTC 도미넌스: {dom}%\n"
        f"  김치프리미엄: {kp}% {kp_emoji}\n"
        f"  시장 점수:    {market_score}/3\n"
    )

def calculate_rsi(close: pd.Series, period: int = 14) -> float:
    delta    = close.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    val      = float(rsi.iloc[-1])
    return round(val, 2) if val == val else 50.0

# ==================================================
# 코인 개별 분석 (4시간봉 선필터 → 통과시 1시간/15분봉)
# ==================================================
def analyze_coin(market: str, ticker: dict) -> dict | None:
    code = market.replace("KRW-", "")
    if code in STABLE_COINS:
        return None

    try:
        t              = ticker.get(market, {})
        cur_price      = float(t.get("trade_price", 0))
        change_pct     = float(t.get("signed_change_rate", 0)) * 100
        trade_value    = float(t.get("acc_trade_price_24h", 0))
        trade_value_억  = int(trade_value / 100_000_000)

        if cur_price <= 0:
            return None

        # ① ticker 1차 필터 - 완전 죽은 코인 제거
        if trade_value < 500_000_000:       # 5억 미만 제거
            return None
        if change_pct < -15:                # 15% 이상 폭락 제거
            return None

        # ② 4시간봉 먼저 호출 → 거래량 급등 확인
        df_4h = get_candles(market, 240, count=30)
        if df_4h.empty or len(df_4h) < 20:
            return None

        # 신규 상장 30일 미만 제외
        first_date  = pd.to_datetime(df_4h["datetime"].iloc[0])
        days_listed = (datetime.now() - first_date).days
        if days_listed < 30:
            return None

        # 4시간봉 거래량 급등 체크 (선필터)
        vol_4h      = float(df_4h["Volume"].iloc[-1])
        avg_vol_4h  = float(df_4h["Volume"].iloc[-11:-1].mean())
        vol_ratio_4h = round(vol_4h / avg_vol_4h, 1) if avg_vol_4h > 0 else 0
        if vol_ratio_4h < 1.3:              # 거래량 1.3배 미만이면 스킵
            return None

        # ③ 4시간봉 통과한 것만 1시간/15분봉 호출
        df_1h = get_candles(market, 60, count=30)
        if df_1h.empty or len(df_1h) < 20:
            return None

        df_15m = get_candles(market, 15, count=30)
        if df_15m.empty or len(df_15m) < 20:
            return None

        # 4시간봉 지표
        close_4h    = df_4h["Close"]
        ma20_4h     = float(close_4h.tail(20).mean())
        rsi_4h      = calculate_rsi(close_4h)
        above_ma_4h = cur_price >= ma20_4h

        # 1시간봉 지표
        close_1h     = df_1h["Close"]
        rsi_1h       = calculate_rsi(close_1h)
        vol_1h       = float(df_1h["Volume"].iloc[-1])
        avg_vol_1h   = float(df_1h["Volume"].iloc[-11:-1].mean())
        vol_ratio_1h = round(vol_1h / avg_vol_1h, 1) if avg_vol_1h > 0 else 0

        # 15분봉 지표
        close_15m     = df_15m["Close"]
        rsi_15m       = calculate_rsi(close_15m)
        last_15m      = df_15m.iloc[-1]
        bullish_15m   = float(last_15m["Close"]) > float(last_15m["Open"])
        vol_15m       = float(df_15m["Volume"].iloc[-1])
        avg_vol_15m   = float(df_15m["Volume"].iloc[-11:-1].mean())
        vol_ratio_15m = round(vol_15m / avg_vol_15m, 1) if avg_vol_15m > 0 else 0

        # 타임프레임 일치
        tf_bullish = sum([above_ma_4h, vol_ratio_1h > 1.5, bullish_15m])

        # 신고가 근접
        high_max  = float(df_4h["High"].max())
        near_high = (cur_price / high_max) >= 0.90 if high_max > 0 else False

        # 눌림 패턴
        if len(df_4h) >= 5:
            recent_4h  = df_4h.tail(5)
            peak_idx   = recent_4h["Volume"].idxmax()
            peak_loc   = recent_4h.index.get_loc(peak_idx)
            peak_close = float(recent_4h.loc[peak_idx, "Close"])
            pullback   = peak_loc < len(recent_4h) - 1 and peak_close > 0 and (cur_price / peak_close) >= 0.97
        else:
            pullback = False

        # 세력 펌핑 의심
        pump_warning = vol_ratio_1h >= 5 or vol_ratio_15m >= 5

        # ATR
        high_low   = df_4h["High"] - df_4h["Low"]
        high_close = (df_4h["High"] - df_4h["Close"].shift()).abs()
        low_close  = (df_4h["Low"]  - df_4h["Close"].shift()).abs()
        atr_val    = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean().iloc[-1]
        atr        = float(atr_val) if atr_val == atr_val else cur_price * 0.03

        entry_price    = cur_price
        stop_loss      = round(cur_price - atr, 2)
        target_price_1 = round(cur_price + atr * 1.5, 2)
        target_price_2 = round(cur_price + atr * 2.0, 2)

        risk   = entry_price - stop_loss
        reward = target_price_2 - entry_price
        if risk <= 0:
            return None
        rr_ratio = round(reward / risk, 2)
        if rr_ratio < 2.0:
            return None

        # 점수 계산
        score = 0
        if tf_bullish == 3:        score += 8
        elif tf_bullish == 2:      score += 4
        elif tf_bullish == 1:      score += 1
        else:                      score -= 2

        if above_ma_4h:            score += 3
        else:                      score -= 1

        if rsi_4h < 60:            score += 2
        elif rsi_4h < 65:          score += 1
        elif rsi_4h >= 70:         score -= 2

        if vol_ratio_1h > 3:       score += 3
        elif vol_ratio_1h > 2:     score += 2
        elif vol_ratio_1h > 1.5:   score += 1

        if bullish_15m:            score += 2
        if vol_ratio_15m > 2:      score += 2
        if near_high:              score += 3
        if pullback:               score += 2

        if trade_value_억 > 500:   score += 3
        elif trade_value_억 > 100: score += 1

        if 0 < change_pct < 5:         score += 3
        elif 5 <= change_pct < 10:     score += 1
        elif change_pct >= 10:         score -= 1

        if pump_warning:           score -= 3
        if rsi_4h >= 70:           score -= 2

        if score < 5:
            return None

        print(f"  ✅ {market} | 점수:{score} | RSI4h:{rsi_4h} | TF:{tf_bullish}/3 | 펌핑:{'⚠️' if pump_warning else '✅'}")

        return sanitize_dict({
            "market":         market,
            "code":           code,
            "score":          score,
            "price":          cur_price,
            "change_pct":     round(change_pct, 2),
            "trade_value_억": trade_value_억,
            "rsi_4h":         rsi_4h,
            "rsi_1h":         rsi_1h,
            "rsi_15m":        rsi_15m,
            "above_ma_4h":    above_ma_4h,
            "vol_ratio_4h":   vol_ratio_4h,
            "vol_ratio_1h":   vol_ratio_1h,
            "vol_ratio_15m":  vol_ratio_15m,
            "tf_bullish":     tf_bullish,
            "bullish_15m":    bullish_15m,
            "near_high":      near_high,
            "pullback":       pullback,
            "pump_warning":   pump_warning,
            "entry_price":    entry_price,
            "stop_loss":      stop_loss,
            "target_price_1": target_price_1,
            "target_price_2": target_price_2,
            "rr_ratio":       rr_ratio,
            "scanned_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    except Exception as e:
        logging.error(f"코인 분석 실패 [{market}]: {e}")
        return None
    
# ==================================================
# OpenRouter AI 분석
# ==================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

def get_ai_analysis(coin: dict) -> str:
    if not OPENROUTER_API_KEY:
        return ""
    try:
        prompt = f"""당신은 암호화폐 트레이딩 전문가입니다.
아래 코인 데이터를 보고 매수/관망/비추천 중 하나로 판단하고 이유를 2-3줄로 설명해주세요.

종목: {coin['code']}/KRW
당일 변동: {coin['change_pct']}%
거래대금: {coin['trade_value_억']}억
4시간봉 RSI: {coin['rsi_4h']}
1시간봉 RSI: {coin['rsi_1h']}
15분봉 RSI: {coin['rsi_15m']}
MA20: {'위' if coin['above_ma_4h'] else '아래'}
타임프레임 일치: {coin['tf_bullish']}/3
1시간봉 거래량: {coin['vol_ratio_1h']}배
15분봉 거래량: {coin['vol_ratio_15m']}배
고점 근접: {coin['near_high']}
눌림 패턴: {coin['pullback']}
펌핑 의심: {coin['pump_warning']}

한국어로 3줄 이내로 답변하세요."""

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/stock-bot",
                "X-Title": "Stock Bot",
            },
            json={
                "models": [
                    "google/gemini-2.0-flash-exp:free",
                    "meta-llama/llama-3.3-70b-instruct:free",
                    "mistralai/mistral-7b-instruct:free",
                ],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
            },
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"  🤖 AI 응답: {result}")
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.error(f"AI 분석 실패 [{coin['code']}]: {e}")
        print(f"  ❌ AI 분석 실패 [{coin['code']}]: {e}")
        return ""
    
def generate_verdict(coin: dict) -> dict:
    score   = coin["score"]
    reasons = []
    risks   = []

    if coin["tf_bullish"] == 3:
        reasons.append("4시간/1시간/15분 3개 타임프레임 모두 상승")
    elif coin["tf_bullish"] == 2:
        reasons.append("2개 타임프레임 상승 일치")
    if coin["above_ma_4h"]:
        reasons.append("4시간봉 MA20 위 (상승 추세)")
    if coin["vol_ratio_1h"] > 2:
        reasons.append(f"1시간봉 거래량 {coin['vol_ratio_1h']}배 급증")
    if coin["bullish_15m"]:
        reasons.append("15분봉 양봉 전환 (단기 진입 신호)")
    if coin["near_high"]:
        reasons.append("고점 근접 (강한 모멘텀)")
    if coin["pullback"]:
        reasons.append("거래량 급등 후 가격 유지 (눌림 패턴)")

    if coin["pump_warning"]:
        risks.append("거래량 5배 이상 — 세력 펌핑 의심")
    if coin["rsi_4h"] > 65:
        risks.append(f"4시간봉 RSI {coin['rsi_4h']} (과열 구간)")
    if coin["change_pct"] > 10:
        risks.append(f"당일 {coin['change_pct']}% 급등 (추격 주의)")
    if not coin["above_ma_4h"]:
        risks.append("4시간봉 MA20 아래 (추세 약화)")

    if score >= 18:   verdict = "✅ 강력 추천"
    elif score >= 13: verdict = "🟡 추천"
    elif score >= 8:  verdict = "⚠️ 관망"
    else:             verdict = "❌ 비추천"

    return {"verdict": verdict, "reasons": reasons[:3], "risks": risks[:2]}

def format_coin_message(coin: dict, rank: int, market_status: dict) -> str:
    verdict   = coin.get("verdict", "")
    reasons   = coin.get("reasons", [])
    risks     = coin.get("risks", [])
    pump_warn = "⚡ 펌핑의심" if coin["pump_warning"] else ""
    overheat  = f"🔥 과열주의 (RSI {coin['rsi_4h']} + 당일 {coin['change_pct']}%)" if coin["rsi_4h"] >= 70 and coin["change_pct"] >= 10 else ""
    warn_line = "  ".join(filter(None, [pump_warn, overheat]))
    tf_bar    = "".join(["✅" if i < coin["tf_bullish"] else "❌" for i in range(3)])

    msg = (
        f"🪙 코인 감지 #{rank}"
        + (f"  {warn_line}" if warn_line else "") +
        f"\n\n"
        f"💎 종목: {coin['code']}/KRW\n"
        f"⭐ 점수: {coin['score']}점  {verdict}\n\n"
    )

    if coin.get("ai_analysis"):
        msg += f"🤖 AI 분석\n  {coin['ai_analysis']}\n\n"

    msg += (
        f"💡 핵심: 거래량(1h) {coin['vol_ratio_1h']}배 · RSI {coin['rsi_4h']} · 당일 {coin['change_pct']:+.1f}% · TF {coin['tf_bullish']}/3\n\n"
        f"📈 타임프레임  {tf_bar}\n"
        f"  4시간봉: {'MA20 위 ✅' if coin['above_ma_4h'] else 'MA20 아래 ❌'}  RSI {coin['rsi_4h']}  거래량 {coin['vol_ratio_4h']}배\n"
        f"  1시간봉: 거래량 {coin['vol_ratio_1h']}배  RSI {coin['rsi_1h']}\n"
        f"  15분봉:  {'양봉 ✅' if coin['bullish_15m'] else '음봉 ❌'}  거래량 {coin['vol_ratio_15m']}배  RSI {coin['rsi_15m']}\n\n"
        f"💰 현재가:   {coin['price']:,}원\n"
        f"📈 당일 변동: {coin['change_pct']:+.2f}%\n"
        f"💵 거래대금:  {coin['trade_value_억']}억\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 진입가:   {coin['entry_price']:,}원\n"
        f"🚀 1차 목표: {coin['target_price_1']:,}원  → 절반 청산 후 손절 본전으로\n"
        f"🚀 2차 목표: {coin['target_price_2']:,}원  → 나머지 전량 청산\n"
        f"🛑 손절가:   {coin['stop_loss']:,}원  (절대 불변)\n"
        f"📐 RR:       1 : {coin['rr_ratio']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if reasons:
        msg += "\n📋 추천 근거\n"
        msg += "\n".join(f"  ✔ {r}" for r in reasons)
    if risks:
        msg += "\n\n⚠️ 리스크\n"
        msg += "\n".join(f"  • {r}" for r in risks)

    msg += "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    return msg

# ==================================================
# 메인
# ==================================================
def main() -> None:
    start_time = time.time()
    now        = datetime.utcnow() + timedelta(hours=9)
    print("=" * 50)
    print(f"🪙 코인 수급 탐지 스캐너 v1.2")
    print(f"   실행 시각: {now.strftime('%Y-%m-%d %H:%M:%S')} KST")
    print("=" * 50)

    alert_cache = load_alert_cache()

    # ① 시장 상황 체크
    print("\n📊 시장 상황 체크 중...")
    fear_greed = get_fear_greed_index()
    dominance  = get_btc_dominance()
    kimchi     = get_kimchi_premium()
    btc_trend  = check_btc_trend()

    print(f"  공포탐욕지수: {fear_greed['value']} ({fear_greed['label']})")
    print(f"  BTC 도미넌스: {dominance}%")
    print(f"  김치프리미엄: {kimchi}%")
    print(f"  BTC 추세: {'상승 ✅' if btc_trend['bullish'] else '하락 ❌'} (RSI {btc_trend['rsi']})")

    market_status = {"fear_greed": fear_greed, "dominance": dominance, "kimchi_premium": kimchi, "btc": btc_trend}
    market_score  = sum([btc_trend["bullish"], 25 <= fear_greed["value"] <= 75, kimchi < 10])

    status_msg = format_market_status(market_status, market_score)
    print("\n" + status_msg)
    send_discord_message(status_msg)

    # ② 마켓 목록 + 시세 조회
    print("\n📋 업비트 원화 마켓 조회 중...")
    markets = get_upbit_markets()
    print(f"  총 {len(markets)}개 마켓")

    print("\n💰 현재 시세 조회 중...")
    tickers = get_ticker(markets)

    # ③ 병렬 분석
    print(f"\n🔍 코인 분석 중...\n")
    results = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_coin, market, tickers): market for market in markets}
        done    = 0
        for future in as_completed(futures):
            result = future.result()
            if result:
                with results_lock:
                    results.append(result)
            done += 1
            if done % 20 == 0:
                print(f"  진행: {done}/{len(markets)}")

    results.sort(key=lambda x: x["score"], reverse=True)
    top_results = results[:10]

    elapsed = round(time.time() - start_time, 1)
    print(f"\n{'='*50}")
    print(f"  최종 후보: {len(results)}개 | 소요: {elapsed}초")
    print(f"{'='*50}\n")

    filename = f"coin_{now.strftime('%Y%m%d_%H%M')}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"  💾 결과 저장: {filename}")
    except Exception as e:
        logging.error(f"결과 저장 실패: {e}")

    if not top_results:
        msg = f"❌ 코인 감지 종목 없음 (스캔완료 {elapsed}초)"
        print(msg)
        send_discord_message(msg)
        return

    for coin in top_results:
        verdict_info      = generate_verdict(coin)
        coin["verdict"]   = verdict_info["verdict"]
        coin["reasons"]   = verdict_info["reasons"]
        coin["risks"]     = verdict_info["risks"]
        coin["ai_analysis"] = get_ai_analysis(coin)

    sent_count = 0
    for rank, coin in enumerate(top_results, start=1):
        if is_recently_alerted(coin["market"], alert_cache):
            print(f"  ⏭ {coin['market']} → 4시간 내 알림 생략")
            continue
        if market_score < 2 and coin["score"] < 13:
            continue

        message = format_coin_message(coin, rank, market_status)
        print(message)
        print("-" * 40)
        send_discord_message(message)
        alert_cache[coin["market"]] = datetime.now().isoformat()
        sent_count += 1
        time.sleep(0.3)

    save_alert_cache(alert_cache)
    print(f"\n✅ 완료 | 전송: {sent_count}개 | 소요: {elapsed}초")


if __name__ == "__main__":
    main()