"""
===================================================
  초기 수급 탐지 스캐너 v2.7
  변경사항:
    - Groq JSON 파싱 강화
    - 정규식으로 JSON 블록 추출
    - 파싱 실패 시 텍스트에서 verdict 키워드 추출
===================================================
"""

import os
import sys
import re
import time
import logging
import traceback
import json
import requests
import feedparser
import pandas as pd
import FinanceDataReader as fdr

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ==================================================
# 장 시간 체크 (KST 09:00 ~ 15:30 평일)
# ==================================================
def is_market_open() -> bool:
    now        = datetime.utcnow()
    kst_hour   = (now.hour + 9) % 24
    kst_minute = now.minute
    kst_time   = kst_hour * 100 + kst_minute
    weekday    = now.weekday()
    if weekday >= 5:
        return False
    return 900 <= kst_time <= 1530

# ==================================================
# 로깅 설정
# ==================================================
LOG_FILE = f"scanner_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# ==================================================
# 환경 변수
# ==================================================
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ==================================================
# 날짜 동적 설정
# ==================================================
TODAY      = datetime.today()
START_DATE = (TODAY - timedelta(days=365)).strftime("%Y-%m-%d")

# ==================================================
# 테마 키워드
# ==================================================
THEMES = {
    "AI":      ["AI", "인공지능", "LLM", "챗GPT", "생성형"],
    "반도체":  ["반도체", "HBM", "엔비디아", "파운드리", "웨이퍼"],
    "로봇":    ["로봇", "자동화", "휴머노이드"],
    "2차전지": ["배터리", "전기차", "양극재", "음극재", "전해질"],
    "방산":    ["방산", "국방", "K-방산", "무기"],
    "바이오":  ["바이오", "FDA", "제약", "임상", "신약"],
}

# ==================================================
# 섹터 ETF
# ==================================================
SECTOR_ETFS = {
    "반도체":  "091160",
    "2차전지": "305720",
    "바이오":  "244580",
    "AI":      "379800",
    "방산":    "425810",
}

results_lock = Lock()

def clean_text(text: str) -> str:
    return str(text).replace(" ", "").lower()

def send_discord_message(message: str) -> None:
    if not WEBHOOK_URL:
        print("[Discord] WEBHOOK_URL 미설정\n", message)
        return
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        try:
            resp = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logging.error(f"Discord 오류: {e}")

def send_discord_error(msg: str) -> None:
    send_discord_message(f"⚠️ [스캐너 오류] {msg}")

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

def check_candle_pattern(data: pd.DataFrame) -> str:
    today      = data.iloc[-1]
    op, hi, lo, cl = float(today["Open"]), float(today["High"]), float(today["Low"]), float(today["Close"])
    total      = hi - lo
    if total == 0:
        return "보통"
    body       = abs(cl - op)
    upper_wick = hi - max(cl, op)
    lower_wick = min(cl, op) - lo
    if body / total > 0.7 and cl > op and upper_wick / total < 0.1:
        return "장대양봉"
    if lower_wick / total > 0.4 and cl > op:
        return "아랫꼬리양봉"
    if upper_wick / total > 0.4 and cl < op:
        return "윗꼬리음봉"
    return "보통"

def is_near_52w_high(data: pd.DataFrame, threshold: float = 0.90) -> bool:
    return (data["Close"].iloc[-1] / data["Close"].tail(252).max()) >= threshold

def check_volume_consolidation(data: pd.DataFrame) -> bool:
    recent    = data.tail(5)
    top2_idx  = recent["Volume"].nlargest(2).index
    avg_price = recent.loc[top2_idx, "Close"].mean()
    if avg_price <= 0:
        return False
    return (data["Close"].iloc[-1] / avg_price) >= 0.97

def get_relative_strength(stock_5d: float, kospi_data) -> float:
    if kospi_data is None or len(kospi_data) < 6:
        return 0.0
    kospi_5d = ((kospi_data["Close"].iloc[-1] / kospi_data["Close"].iloc[-5]) - 1) * 100
    return round(stock_5d - kospi_5d, 2)

def get_investor_sentiment(code: str) -> dict:
    try:
        start = (TODAY - timedelta(days=10)).strftime("%Y-%m-%d")
        df    = fdr.DataReader(code, start, exchange="KRX-INVESTOR")
        if df is None or len(df) < 1:
            return {"foreign": 0, "institution": 0}
        return {
            "foreign":     int(df["Foreign"].iloc[-3:].sum()),
            "institution": int(df["Institution"].iloc[-3:].sum()),
        }
    except Exception:
        return {"foreign": 0, "institution": 0}

def is_sector_etf_bullish(themes: list, etf_cache: dict) -> bool:
    if not themes:
        return True
    for theme in themes:
        etf_code = SECTOR_ETFS.get(theme)
        if etf_code and etf_code in etf_cache:
            etf_data = etf_cache[etf_code]
            if len(etf_data) >= 20:
                if etf_data["Close"].iloc[-1] >= etf_data["Close"].tail(20).mean():
                    return True
    return False

def load_etf_cache() -> dict:
    cache = {}
    for theme, code in SECTOR_ETFS.items():
        try:
            df = fdr.DataReader(code, START_DATE)
            if df is not None and len(df) >= 20:
                cache[code] = df
        except Exception as e:
            logging.error(f"ETF 로딩 실패 [{theme}/{code}]: {e}")
    print(f"  ETF 캐시 로딩 완료: {list(cache.keys())}")
    return cache

def analyze_news(name: str) -> tuple:
    try:
        url      = f"https://news.google.com/rss/search?q={name}+주식&hl=ko&gl=KR&ceid=KR:ko"
        feed     = feedparser.parse(url)
        titles   = []
        detected = set()
        for item in feed.entries[:3]:
            title = item.title
            titles.append(title)
            cleaned = clean_text(title)
            for theme, keywords in THEMES.items():
                if any(clean_text(kw) in cleaned for kw in keywords):
                    detected.add(theme)
        return titles, list(detected)
    except Exception as e:
        logging.error(f"뉴스 오류 [{name}]: {e}")
        return [], []

# ==================================================
# Groq 사용 가능한 모델 자동 선택
# ==================================================
def get_best_groq_model() -> str:
    priority = [
        "llama-3.3-70b-versatile",
        "llama3-70b-8192",
        "llama-3.1-70b-versatile",
        "llama3-8b-8192",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ]
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=10
        )
        resp.raise_for_status()
        available = {m["id"] for m in resp.json().get("data", [])}
        print(f"  Groq 사용 가능 모델: {len(available)}개")

        for model in priority:
            if model in available:
                print(f"  ✅ 선택된 모델: {model}")
                return model

        first = sorted(available)[0]
        print(f"  ⚠️ 우선순위 모델 없음 → 기본 사용: {first}")
        return first

    except Exception as e:
        logging.error(f"Groq 모델 목록 조회 실패: {e}")
        print(f"  ⚠️ 모델 목록 조회 실패 → llama3-70b-8192 사용")
        return "llama3-70b-8192"

# ==================================================
# JSON 안전 파싱 (강화 버전)
# ==================================================
def safe_parse_groq_response(content: str) -> dict:
    """
    AI 응답에서 JSON 추출 — 3단계 시도
    1. 그대로 파싱
    2. 정규식으로 JSON 블록 추출 후 파싱
    3. 텍스트에서 verdict 키워드 직접 추출
    """
    # 1단계: 마크다운 제거 후 직접 파싱
    cleaned = content.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 2단계: 정규식으로 { } 블록 추출
    match = re.search(r'\{[^{}]+\}', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass

    # 3단계: 텍스트에서 verdict 키워드 직접 추출
    verdict = "관망"
    if "진입추천" in content or "진입 추천" in content:
        verdict = "진입추천"
    elif "진입금지" in content or "진입 금지" in content:
        verdict = "진입금지"

    # reason 추출 시도
    reason = ""
    risk   = ""
    lines  = [l.strip() for l in content.split("\n") if l.strip()]
    if len(lines) > 1:
        reason = lines[1][:100]
    if len(lines) > 2:
        risk = lines[2][:100]

    return {"verdict": verdict, "reason": reason, "risk": risk}

# ==================================================
# Groq AI 분석
# ==================================================
def analyze_with_groq(stock: dict, model: str) -> dict:
    if not GROQ_API_KEY:
        return {"verdict": "분석불가", "reason": "GROQ_API_KEY 미설정", "risk": ""}

    prompt = (
        f"당신은 한국 주식 단기 트레이딩 전문가입니다.\n"
        f"아래 종목 데이터를 분석하고 JSON으로만 답변하세요.\n\n"
        f"종목명: {stock['name']} ({stock['code']})\n"
        f"당일 상승률: {stock['change']}%\n"
        f"5일 상승률: {stock['five_day_change']}%\n"
        f"거래량 증가: {stock['volume_ratio']}배\n"
        f"거래대금: {stock['trading_value']}억\n"
        f"RSI: {stock['rsi']}\n"
        f"MA20: {'위' if stock['above_ma20'] else '아래'}\n"
        f"상대강도: {stock['relative_strength']:+.1f}%\n"
        f"캔들: {stock['candle']}\n"
        f"52주 신고가 근접: {'예' if stock['near_52w_high'] else '아니오'}\n"
        f"눌림 패턴: {'예' if stock['vol_consolidation'] else '아니오'}\n"
        f"외국인 3일: {stock['foreign_net']:+,}주\n"
        f"기관 3일: {stock['institution_net']:+,}주\n"
        f"섹터 강세: {'예' if stock['sector_bullish'] else '아니오'}\n"
        f"테마: {', '.join(stock['themes']) if stock['themes'] else '없음'}\n"
        f"진입가: {stock['entry_price']:,}원 / 손절: {stock['stop_loss']:,}원 / RR: 1:{stock['rr_ratio']}\n\n"
        f"반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이:\n"
        f'{"{"}"verdict": "진입추천" 또는 "관망" 또는 "진입금지", "reason": "근거 2줄", "risk": "리스크 1줄"{"}"}'
    )

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model":       model,
                "messages":    [
                    {
                        "role":    "system",
                        "content": "You must respond only with valid JSON. No markdown, no explanation, no extra text."
                    },
                    {
                        "role":    "user",
                        "content": prompt
                    }
                ],
                "temperature":    0.1,
                "max_tokens":     300,
                "response_format": {"type": "json_object"},
            },
            timeout=15
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        return safe_parse_groq_response(content)

    except Exception as e:
        logging.error(f"Groq 분석 오류 [{stock['name']}]: {e}")
        return {"verdict": "분석실패", "reason": str(e)[:50], "risk": ""}

# ==================================================
# 종목 분석
# ==================================================
def analyze_stock(row, kospi_data, etf_cache) -> dict | None:
    code = row["Code"]
    name = row["Name"]
    try:
        if "ETF" in name or name.endswith("우"):
            return None
        if row["Marcap"] > 5_000_000_000_000:
            return None

        data = fdr.DataReader(code, START_DATE)
        data = data.dropna()
        if len(data) < 30:
            return None

        today_close     = float(data["Close"].iloc[-1])
        yesterday_close = float(data["Close"].iloc[-2])
        if today_close <= 0 or yesterday_close <= 0:
            return None

        change_pct      = (today_close - yesterday_close) / yesterday_close * 100
        five_day_change = (today_close - data["Close"].iloc[-5]) / data["Close"].iloc[-5] * 100
        if five_day_change > 20:
            return None

        avg_volume   = data["Volume"].iloc[-11:-1].mean()
        today_volume = float(data["Volume"].iloc[-1])
        if avg_volume <= 0:
            return None

        volume_ratio  = today_volume / avg_volume
        trading_value = today_close * today_volume
        if volume_ratio < 1.3:
            return None
        if trading_value < 3_000_000_000:
            return None

        today_rsi = calculate_rsi(data["Close"])
        if today_rsi > 75:
            return None

        ma20       = float(data["Close"].tail(20).mean())
        above_ma20 = today_close >= ma20
        today_atr  = calculate_atr(data)
        candle     = check_candle_pattern(data)

        entry_price    = int(today_close)
        stop_loss      = int(today_close - today_atr)
        target_price_1 = int(today_close + today_atr * 1.5)
        target_price_2 = int(today_close + today_atr * 2.0)

        risk   = entry_price - stop_loss
        reward = target_price_2 - entry_price
        if risk <= 0:
            return None
        rr_ratio = round(reward / risk, 2)
        if rr_ratio < 2.0:
            return None

        near_52w_high     = is_near_52w_high(data)
        vol_consolidation = check_volume_consolidation(data)
        relative_strength = get_relative_strength(five_day_change, kospi_data)
        investor          = get_investor_sentiment(code)
        news_titles, detected_themes = analyze_news(name)
        sector_bullish    = is_sector_etf_bullish(detected_themes, etf_cache)

        score = 0
        if volume_ratio > 3:           score += 5
        elif volume_ratio > 2:         score += 3
        elif volume_ratio > 1.5:       score += 1
        if 0 < change_pct < 5:         score += 3
        elif change_pct >= 5:          score += 1
        if today_rsi < 60:             score += 2
        elif today_rsi < 70:           score += 1
        if trading_value > 10_000_000_000:  score += 3
        elif trading_value > 5_000_000_000: score += 1
        score += len(detected_themes) * 2
        if near_52w_high:              score += 3
        if vol_consolidation:          score += 2
        if above_ma20:                 score += 2
        else:                          score -= 1
        if relative_strength > 5:      score += 3
        elif relative_strength > 2:    score += 1
        if investor["foreign"] > 0:    score += 3
        if investor["institution"] > 0:score += 2
        if not sector_bullish:         score -= 3
        if candle == "장대양봉":           score += 3
        elif candle == "아랫꼬리양봉":     score += 2
        elif candle == "윗꼬리음봉":       score -= 2

        if score < 3:
            return None

        print(f"  ✅ {name} | 점수:{score} | RSI:{today_rsi} | 거래량:{volume_ratio:.1f}배 | 캔들:{candle}")

        return {
            "name":              name,
            "code":              code,
            "score":             score,
            "change":            round(change_pct, 2),
            "five_day_change":   round(five_day_change, 2),
            "volume_ratio":      round(volume_ratio, 1),
            "trading_value":     int(trading_value / 100_000_000),
            "rsi":               today_rsi,
            "above_ma20":        above_ma20,
            "relative_strength": relative_strength,
            "near_52w_high":     near_52w_high,
            "vol_consolidation": vol_consolidation,
            "foreign_net":       investor["foreign"],
            "institution_net":   investor["institution"],
            "sector_bullish":    sector_bullish,
            "candle":            candle,
            "rr_ratio":          rr_ratio,
            "themes":            detected_themes,
            "news":              news_titles,
            "entry_price":       entry_price,
            "stop_loss":         stop_loss,
            "target_price_1":    target_price_1,
            "target_price_2":    target_price_2,
            "scanned_at":        TODAY.strftime("%Y-%m-%d %H:%M:%S"),
        }

    except Exception as e:
        logging.error(f"[{name}/{code}]:\n{traceback.format_exc()}")
        if "ConnectionError" in type(e).__name__:
            send_discord_error(f"연결 오류 [{name}]")
        return None

# ==================================================
# 결과 JSON 저장
# ==================================================
def save_results(results: list) -> None:
    filename = f"scan_{TODAY.strftime('%Y%m%d_%H%M')}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 결과 저장: {filename}")
    except Exception as e:
        logging.error(f"결과 저장 실패: {e}")

# ==================================================
# tracker 연동
# ==================================================
def save_positions(top_results: list) -> None:
    filename = "positions.json"
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                positions = json.load(f)
        else:
            positions = []

        existing_codes = {p["code"] for p in positions}

        for stock in top_results:
            if stock["code"] in existing_codes:
                continue
            if stock.get("ai_verdict") == "진입금지":
                continue
            positions.append({
                "code":           stock["code"],
                "name":           stock["name"],
                "entry_price":    stock["entry_price"],
                "stop_loss":      stock["stop_loss"],
                "target_price_1": stock["target_price_1"],
                "target_price_2": stock["target_price_2"],
                "entered_at":     TODAY.strftime("%Y-%m-%d %H:%M:%S"),
                "status":         "진행중",
                "result":         None,
                "ai_verdict":     stock.get("ai_verdict", ""),
            })

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        print(f"  📌 포지션 저장: {filename} ({len(positions)}개)")
    except Exception as e:
        logging.error(f"포지션 저장 실패: {e}")

# ==================================================
# Discord 포맷
# ==================================================
def format_discord_message(stock: dict, rank: int) -> str:
    candle_emoji = {
        "장대양봉":    "🕯️ 장대양봉",
        "아랫꼬리양봉":"📌 아랫꼬리양봉",
        "윗꼬리음봉":  "⚠️ 윗꼬리음봉",
        "보통":        "➖ 보통",
    }.get(stock["candle"], "➖ 보통")

    flags = " ".join(filter(None, [
        "🔑 신고가 근접"                              if stock["near_52w_high"]         else "",
        "🧱 눌림 패턴"                                if stock["vol_consolidation"]      else "",
        f"📡 시장대비 +{stock['relative_strength']}%" if stock["relative_strength"] > 2  else "",
        "📊 MA20 위"                                  if stock["above_ma20"]             else "📊 MA20 아래",
        "⚠️ 섹터역행"                                 if not stock["sector_bullish"]     else "",
    ]))

    verdict       = stock.get("ai_verdict", "")
    verdict_emoji = {
        "진입추천": "✅",
        "관망":     "⚠️",
        "진입금지": "❌",
        "분석실패": "❓",
        "분석불가": "❓",
    }.get(verdict, "❓")

    ai_reason = stock.get("ai_reason", "")
    ai_risk   = stock.get("ai_risk", "")

    msg = (
        f"🚨 수급 감지 #{rank}\n\n"
        f"🔥 종목: {stock['name']} ({stock['code']})\n\n"
        f"⭐ 점수: {stock['score']}점\n"
        f"{candle_emoji}\n"
        f"{flags}\n\n"
        f"📈 당일 상승률:  {stock['change']}%\n"
        f"📊 5일 상승률:   {stock['five_day_change']}%\n"
        f"📈 거래량 증가:  {stock['volume_ratio']}배\n"
        f"💰 거래대금:     {stock['trading_value']}억\n"
        f"📉 RSI:          {stock['rsi']}\n"
        f"🌐 상대강도:     {stock['relative_strength']:+.1f}%\n"
        f"👥 외국인 3일:   {stock['foreign_net']:+,}주\n"
        f"🏦 기관 3일:     {stock['institution_net']:+,}주\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 진입가:    {stock['entry_price']:,}원\n"
        f"🚀 1차 목표:  {stock['target_price_1']:,}원  → 절반 청산 후 손절 본전으로\n"
        f"🚀 2차 목표:  {stock['target_price_2']:,}원  → 나머지 전량 청산\n"
        f"🛑 손절가:    {stock['stop_loss']:,}원  (절대 불변)\n"
        f"📐 RR:        1 : {stock['rr_ratio']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 AI 분석: {verdict_emoji} {verdict}\n"
        f"근거: {ai_reason}\n"
        f"리스크: {ai_risk}\n"
    )

    if stock["themes"]:
        msg += "\n🏷️ 테마\n" + "\n".join(f"  - {t}" for t in stock["themes"])
    if stock["news"]:
        msg += "\n\n📰 뉴스\n" + "\n".join(f"  • {n}" for n in stock["news"])

    return msg

# ==================================================
# 메인
# ==================================================
def main() -> None:

    # ① 장 시간 체크
    if not is_market_open():
        now_utc = datetime.utcnow()
        kst_h   = (now_utc.hour + 9) % 24
        kst_m   = now_utc.minute
        print(f"⏸ 장 시간 외 — 실행 생략 (현재 KST {kst_h:02d}:{kst_m:02d})")
        sys.exit(0)

    start_time = time.time()
    print("=" * 50)
    print(f"🚀 초기 수급 탐지 스캐너 v2.7")
    print(f"   실행 시각: {TODAY.strftime('%Y-%m-%d %H:%M:%S')} KST")
    print("=" * 50)

    # ② KOSPI 로딩
    print("\n📊 KOSPI 데이터 로딩 중...")
    try:
        kospi_data = fdr.DataReader("KS11", START_DATE)
    except Exception as e:
        logging.error(f"KOSPI 로딩 실패: {e}")
        kospi_data = None
        print("  ⚠️ KOSPI 로딩 실패 — 상대강도 생략")

    # ③ 섹터 ETF 캐시
    print("\n📦 섹터 ETF 로딩 중...")
    etf_cache = load_etf_cache()

    # ④ 종목 리스트
    print("\n📋 KRX 종목 로딩 중...")
    stocks = fdr.StockListing("KRX")
    stocks = stocks[stocks["Market"].isin(["KOSPI", "KOSDAQ"])]
    stocks = stocks[stocks["Marcap"] > 50_000_000_000]
    stocks = stocks.sort_values(by="Marcap", ascending=False).head(400)
    print(f"  분석 대상: {len(stocks)}개\n")

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(analyze_stock, row, kospi_data, etf_cache): row["Name"]
            for _, row in stocks.iterrows()
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                with results_lock:
                    results.append(result)

    results.sort(key=lambda x: x["score"], reverse=True)
    top_results = results[:10]

    elapsed = round(time.time() - start_time, 1)
    print(f"\n{'='*50}")
    print(f"  최종 후보: {len(results)}개 | 소요: {elapsed}초")
    print(f"{'='*50}\n")

    if not top_results:
        msg = f"❌ 수급 감지 종목 없음 (스캔완료 {elapsed}초)"
        print(msg)
        send_discord_message(msg)
        return

    # ⑤ Groq 모델 자동 선택
    print("\n🤖 Groq 모델 선택 중...")
    groq_model = get_best_groq_model()

    # ⑥ Groq AI 분석
    print("\n🤖 Groq AI 분석 중...")
    for stock in top_results:
        ai_result = analyze_with_groq(stock, groq_model)
        stock["ai_verdict"] = ai_result.get("verdict", "분석실패")
        stock["ai_reason"]  = ai_result.get("reason", "")
        stock["ai_risk"]    = ai_result.get("risk", "")
        print(f"  {stock['name']} → {stock['ai_verdict']}")
        time.sleep(0.5)

    # ⑦ JSON 저장
    save_results(results)

    # ⑧ positions 저장
    save_positions(top_results)

    # ⑨ Discord 전송
    for rank, stock in enumerate(top_results, start=1):
        message = format_discord_message(stock, rank)
        print(message)
        print("-" * 40)
        send_discord_message(message)
        time.sleep(0.3)

    print(f"\n✅ 완료 | 소요: {elapsed}초")


if __name__ == "__main__":
    main()