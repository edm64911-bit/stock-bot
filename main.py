"""
===================================================
  초기 수급 탐지 스캐너 v3.1
  변경사항:
    - 시총 구간별 분석 (소형/중형/대형)
    - 구간별 거래량 기준 차등 적용
    - 우선주 필터 강화
    - 기관 3일 하드코딩 버그 수정
    - positions.json 중복 체크 수정 (진행중만)
    - 눌림 패턴 로직 수정
    - 거래량 표현 기준 조정
    - 섹터 ETF 테마 없음 처리 개선
    - is_market_open() KST 요일 버그 수정
===================================================
"""

import os
import sys
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
    now      = datetime.utcnow() + timedelta(hours=9)  # KST 변환
    kst_time = now.hour * 100 + now.minute
    weekday  = now.weekday()
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
WEBHOOK_STOCK        = os.getenv("WEBHOOK_STOCK", "")
WEBHOOK_STOCK_WEEKLY = os.getenv("WEBHOOK_STOCK_WEEKLY", "")
WEBHOOK_COIN         = os.getenv("WEBHOOK_COIN", "")
WEBHOOK_COIN_WEEKLY  = os.getenv("WEBHOOK_COIN_WEEKLY", "")

# ==================================================
# 날짜 동적 설정
# ==================================================
TODAY      = datetime.today()
START_DATE = (TODAY - timedelta(days=365)).strftime("%Y-%m-%d")

# ==================================================
# 시총 구간 정의
# ==================================================
GROUPS = {
    "소형": {
        "min":          50_000_000_000,
        "max":          300_000_000_000,
        "limit":        400,
        "vol_min":      2.0,
        "amount_min":   5_000_000_000,
    },
    "중형": {
        "min":          300_000_000_000,
        "max":          1_000_000_000_000,
        "limit":        400,
        "vol_min":      1.5,
        "amount_min":   3_000_000_000,
    },
    "대형": {
        "min":          1_000_000_000_000,
        "max":          5_000_000_000_000,
        "limit":        400,
        "vol_min":      1.3,
        "amount_min":   3_000_000_000,
    },
}

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
    "방산":    "474220",
}

results_lock = Lock()

def clean_text(text: str) -> str:
    return str(text).replace(" ", "").lower()

def send_discord_message(message: str, webhook: str = "") -> None:
    url = webhook or WEBHOOK_STOCK
    if not url:
        print("[Discord] 웹훅 미설정\n", message)
        return
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        try:
            resp = requests.post(url, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logging.error(f"Discord 오류: {e}")

def send_discord_error(msg: str) -> None:
    send_discord_message(f"⚠️ [스캐너 오류] {msg}", WEBHOOK_STOCK)

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
    if len(data) < 5:
        return False
    recent      = data.tail(5)
    today_close = float(data["Close"].iloc[-1])
    peak_idx    = recent["Volume"].idxmax()
    peak_loc    = recent.index.get_loc(peak_idx)
    if peak_loc == len(recent) - 1:
        return False
    peak_close  = float(recent.loc[peak_idx, "Close"])
    if peak_close <= 0:
        return False
    return (today_close / peak_close) >= 0.97

def get_relative_strength(stock_5d: float, kospi_data) -> float:
    if kospi_data is None or len(kospi_data) < 6:
        return 0.0
    kospi_5d = ((kospi_data["Close"].iloc[-1] / kospi_data["Close"].iloc[-5]) - 1) * 100
    return round(stock_5d - kospi_5d, 2)

# ==================================================
# OpenRouter AI 분석
# ==================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

def get_ai_analysis(stock: dict) -> str:
    if not GEMINI_API_KEY:
        return ""
    for model in GEMINI_MODELS:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{
                        "parts": [{"text": f"""당신은 세계 최고 수준의 퀀트 트레이더이자 단타/스윙 전략 분석가입니다.
기대값(EV)이 높은 자리인지 판단하고, 실패 확률이 높은 패턴을 제거하며, 실제로 돈이 들어오는 차트를 구분하세요.
감정이 아니라 데이터와 확률 기반으로 판단하세요.

[종목 데이터]
종목: {stock['name']} ({stock['code']}) / {stock.get('group','')}주
당일: 시가 {stock.get('open_price',0):,}원 / 고가 {stock.get('high_price',0):,}원 / 저가 {stock.get('low_price',0):,}원 / 종가 {stock['entry_price']:,}원
당일 상승률: {stock['change']}% / 5일 상승률: {stock['five_day_change']}%
거래량: {stock['volume_ratio']}배 / 거래대금: {stock['trading_value']}억
캔들: {stock['candle']} / 몸통비율: {stock.get('body_ratio',0)}% / 윗꼬리: {stock.get('upper_tail',0)}% / 종가위치: {stock.get('close_pos',0)}%
RSI: {stock['rsi']} / 상대강도: {stock['relative_strength']}%
MA5: {stock.get('ma5',0):,}원 / MA20: {'위' if stock['above_ma20'] else '아래'} / MA60: {stock.get('ma60',0):,}원
이평선 정렬: {stock.get('ma_align','?')}
볼린저밴드: 상단 {stock.get('bb_upper',0):,}원 / 하단 {stock.get('bb_lower',0):,}원 / 위치: {stock.get('bb_pos','?')}
MACD: {stock.get('macd_cross','?')}
52주 신고가 근접: {stock['near_52w_high']} / 눌림패턴: {stock['vol_consolidation']}
외국인 3일: {stock['foreign_net']:+,}주 / 기관 3일: {stock['institution_net']:+,}주
테마: {', '.join(stock['themes']) if stock['themes'] else '없음'}
뉴스: {' / '.join(stock['news'][:2]) if stock['news'] else '없음'}

[출력 형식 - 반드시 이 형식으로]
[퀀트 점수] 0~100
[기대값] 높음/보통/낮음
[차트 위치] 돌파 초입/첫 눌림/과열/고점 분배 가능성/애매
[수급 분석] 매집/분배 가능성, 거래량 질 평가
[강한 요소] 핵심 3가지
[위험 요소] 핵심 3가지
[최종 판단] 적극 매수 가능/눌림 후 매수/돌파 확인 필요/관망/추격 금지
[한줄 결론] 냉정하게 한줄 요약"""}]
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
            logging.error(f"AI 분석 실패 [{stock['name']}] ({model}): {e}")
            print(f"  ❌ AI 분석 실패 [{stock['name']}] ({model}): {e}")
            continue
    return ""


def get_investor_sentiment(code: str) -> dict:
    try:
        from pykrx import stock
        end   = (TODAY - timedelta(days=1)).strftime("%Y%m%d")
        start = (TODAY - timedelta(days=10)).strftime("%Y%m%d")
        df    = stock.get_market_net_purchases_of_equities_investors(start, end, [code])
        if df is None or df.empty:
            return {"foreign": 0, "institution": 0}
        return {
            "foreign":     int(df["외국인합계"].iloc[-3:].sum()) if "외국인합계" in df.columns else 0,
            "institution": int(df["기관합계"].iloc[-3:].sum())   if "기관합계"   in df.columns else 0,
        }
    except Exception:
        return {"foreign": 0, "institution": 0}

def is_sector_etf_bullish(themes: list, etf_cache: dict) -> bool:
    if not themes:
        return None
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

NEWS_EVENT_KEYWORDS = [
    "수주", "계약", "MOU", "특허", "인수", "합병",
    "FDA", "임상", "허가", "공시", "유상증자", "전환사채",
    "거래정지", "불성실", "감사의견", "횡령", "배임",
]

def has_event_news(titles: list) -> bool:
    for title in titles:
        cleaned = clean_text(title)
        if any(clean_text(kw) in cleaned for kw in NEWS_EVENT_KEYWORDS):
            return True
    return False

def is_preferred_stock(name: str) -> bool:
    suffixes = ["우", "우B", "우C", "2우", "3우", "2우B", "3우B"]
    return any(name.endswith(s) for s in suffixes)

def generate_verdict(stock: dict) -> dict:
    score   = stock["score"]
    reasons = []
    risks   = []

    vr = stock["volume_ratio"]
    if vr > 3:
        reasons.append(f"거래량 {vr}배 급증 (강한 수급 유입)")
    elif vr > 1.5:
        reasons.append(f"거래량 {vr}배 증가 (수급 유입 신호)")
    else:
        reasons.append(f"거래량 {vr}배 증가")

    if stock["above_ma20"]:
        reasons.append("MA20 위 유지 (상승 추세)")
    if stock["near_52w_high"]:
        reasons.append("52주 신고가 근접 (강한 모멘텀)")
    if stock["vol_consolidation"]:
        reasons.append("거래량 급등 후 가격 유지 (눌림 패턴)")
    if stock["relative_strength"] > 5:
        reasons.append(f"시장 대비 +{stock['relative_strength']}% 초과 상승")
    if stock["foreign_net"] > 0:
        reasons.append(f"외국인 3일 순매수 {stock['foreign_net']:,}주")
    if stock["institution_net"] > 0:
        reasons.append(f"기관 3일 순매수 {stock['institution_net']:,}주")
    if stock["themes"]:
        reasons.append(f"{', '.join(stock['themes'])} 테마")
    if stock["candle"] == "장대양봉":
        reasons.append("장대양봉 (강한 매수세)")
    elif stock["candle"] == "아랫꼬리양봉":
        reasons.append("아랫꼬리양봉 (저점 매수세)")

    if stock["five_day_change"] > 15:
        risks.append(f"5일 상승률 {stock['five_day_change']}% (단기 급등 부담)")
    if stock["change"] > 10:
        risks.append(f"당일 {stock['change']}% 급등 (추격 매수 주의)")
    if stock["rsi"] > 65:
        risks.append(f"RSI {stock['rsi']} (과열 구간 접근)")
    if not stock["above_ma20"]:
        risks.append("MA20 하향 (추세 약화)")
    if stock["sector_bullish"] is False:
        risks.append("섹터 ETF 약세 (섹터 역행)")
    if stock["candle"] == "윗꼬리음봉":
        risks.append("윗꼬리음봉 (매도 압력)")
    if stock["relative_strength"] < 0:
        risks.append(f"시장 대비 {stock['relative_strength']}% 하회")
    if stock.get("event_news"):
        risks.append("수주/계약 등 단발성 뉴스 — 다음날 빠질 수 있음")

    group = stock.get("group", "")
    if group == "소형":
        risks.append("소형주 — 변동성 크므로 포지션 사이즈 주의")

    if score >= 18:
        verdict = "✅ 강력 추천"
    elif score >= 13:
        verdict = "🟡 추천"
    elif score >= 8:
        verdict = "⚠️ 관망"
    else:
        verdict = "❌ 비추천"

    return {
        "verdict": verdict,
        "reasons": reasons[:3],
        "risks":   risks[:2],
    }

def analyze_stock(row, kospi_data, etf_cache, group_cfg: dict, group_name: str) -> dict | None:
    code = row["Code"]
    name = row["Name"]

    try:
        if "ETF" in name:
            return None
        if is_preferred_stock(name):
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

        if volume_ratio < group_cfg["vol_min"]:
            return None
        if trading_value < group_cfg["amount_min"]:
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

        # 추가 지표
        ma60       = float(data["Close"].tail(60).mean()) if len(data) >= 60 else 0
        ma5        = float(data["Close"].tail(5).mean())
        bb_std     = float(data["Close"].tail(20).std())
        bb_upper   = round(ma20 + bb_std * 2, 0)
        bb_lower   = round(ma20 - bb_std * 2, 0)
        bb_pos     = "상단 근접" if today_close >= bb_upper * 0.98 else "하단 근접" if today_close <= bb_lower * 1.02 else "중간"
        ema12      = data["Close"].ewm(span=12).mean()
        ema26      = data["Close"].ewm(span=26).mean()
        macd_line  = ema12 - ema26
        signal     = macd_line.ewm(span=9).mean()
        macd_val   = float(macd_line.iloc[-1])
        signal_val = float(signal.iloc[-1])
        macd_cross = "골든크로스" if macd_val > signal_val and float(macd_line.iloc[-2]) <= float(signal.iloc[-2]) else \
                     "데드크로스" if macd_val < signal_val and float(macd_line.iloc[-2]) >= float(signal.iloc[-2]) else \
                     "상승중" if macd_val > signal_val else "하락중"
        today_row  = data.iloc[-1]
        open_p     = float(today_row["Open"])
        high_p     = float(today_row["High"])
        low_p      = float(today_row["Low"])
        close_p    = float(today_row["Close"])
        body_ratio = round(abs(close_p - open_p) / (high_p - low_p) * 100, 1) if high_p != low_p else 0
        upper_tail = round((high_p - max(close_p, open_p)) / (high_p - low_p) * 100, 1) if high_p != low_p else 0
        close_pos  = round((close_p - low_p) / (high_p - low_p) * 100, 1) if high_p != low_p else 50
        ma_align   = "정배열" if ma5 > ma20 > ma60 and ma60 > 0 else "역배열" if ma5 < ma20 < ma60 and ma60 > 0 else "혼조"
        relative_strength = get_relative_strength(five_day_change, kospi_data)
        investor          = get_investor_sentiment(code)
        news_titles, detected_themes = analyze_news(name)
        sector_bullish    = is_sector_etf_bullish(detected_themes, etf_cache)
        event_news        = has_event_news(news_titles)

        score = 0

        if volume_ratio > 3:           score += 5
        elif volume_ratio > 2:         score += 3
        elif volume_ratio > 1.5:       score += 1

        if 0 < change_pct < 5:         score += 3
        elif change_pct >= 5:          score += 1

        if today_rsi < 60:             score += 2
        elif today_rsi < 65:           score += 1
        elif today_rsi < 70:           score += 0
        elif today_rsi < 75:           score -= 2

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

        if sector_bullish is False:    score -= 3

        if candle == "장대양봉":           score += 3
        elif candle == "아랫꼬리양봉":     score += 2
        elif candle == "윗꼬리음봉":       score -= 2

        if group_name == "소형" and volume_ratio >= 2.0:
            score += 2

        if event_news:                 score -= 2

        if score < 3:
            return None

        print(f"  ✅ [{group_name}] {name} | 점수:{score} | RSI:{today_rsi} | 거래량:{volume_ratio:.1f}배 | 캔들:{candle}")

        return {
            "name":              name,
            "code":              code,
            "group":             group_name,
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
            "event_news":        event_news,
            "entry_price":       entry_price,
            "stop_loss":         stop_loss,
            "target_price_1":    target_price_1,
            "target_price_2":    target_price_2,
            "scanned_at":        TODAY.strftime("%Y-%m-%d %H:%M:%S"),
            "ma5":               round(ma5, 0),
            "ma60":              round(ma60, 0),
            "bb_upper":          bb_upper,
            "bb_lower":          bb_lower,
            "bb_pos":            bb_pos,
            "macd_cross":        macd_cross,
            "body_ratio":        body_ratio,
            "upper_tail":        upper_tail,
            "close_pos":         close_pos,
            "ma_align":          ma_align,
            "open_price":        int(open_p),
            "high_price":        int(high_p),
            "low_price":         int(low_p),
        }

    except Exception as e:
        logging.error(f"[{name}/{code}]:\n{traceback.format_exc()}")
        if "ConnectionError" in type(e).__name__:
            send_discord_error(f"연결 오류 [{name}]")
        return None

def sanitize_for_json(obj):
    if isinstance(obj, float):
        if obj != obj or obj == float('inf') or obj == float('-inf'):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    return obj

def save_results(results: list) -> None:
    filename = f"scan_{TODAY.strftime('%Y%m%d_%H%M')}.json"
    try:
        clean = sanitize_for_json(results)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 결과 저장: {filename}")
    except Exception as e:
        logging.error(f"결과 저장 실패: {e}")

def save_positions(top_results: list) -> None:
    filename = "positions.json"
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                positions = json.load(f)
        else:
            positions = []

        existing_codes = {p["code"] for p in positions if p["status"] == "진행중"}

        for stock in top_results:
            if stock["code"] in existing_codes:
                continue
            if "비추천" in stock.get("verdict", ""):
                continue
            positions.append({
                "code":           stock["code"],
                "name":           stock["name"],
                "group":          stock.get("group", ""),
                "entry_price":    stock["entry_price"],
                "stop_loss":      stock["stop_loss"],
                "target_price_1": stock["target_price_1"],
                "target_price_2": stock["target_price_2"],
                "entered_at":     TODAY.strftime("%Y-%m-%d %H:%M:%S"),
                "status":         "진행중",
                "result":         None,
                "verdict":        stock.get("verdict", ""),
            })

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        print(f"  📌 포지션 저장: {filename} ({len(positions)}개)")
    except Exception as e:
        logging.error(f"포지션 저장 실패: {e}")

def format_discord_message(stock: dict, rank: int) -> str:
    candle_emoji = {
        "장대양봉":    "🕯️ 장대양봉",
        "아랫꼬리양봉":"📌 아랫꼬리양봉",
        "윗꼬리음봉":  "⚠️ 윗꼬리음봉",
        "보통":        "➖ 보통",
    }.get(stock["candle"], "➖ 보통")

    sector_str = ""
    if stock["sector_bullish"] is True:
        sector_str = ""
    elif stock["sector_bullish"] is False:
        sector_str = "⚠️ 섹터역행"

    flags = " ".join(filter(None, [
        "🔑 신고가 근접"                              if stock["near_52w_high"]         else "",
        "🧱 눌림 패턴"                                if stock["vol_consolidation"]      else "",
        f"📡 시장대비 +{stock['relative_strength']}%" if stock["relative_strength"] > 2  else "",
        "📊 MA20 위"                                  if stock["above_ma20"]             else "📊 MA20 아래",
        sector_str,
    ]))

    group_emoji   = {"소형": "🔹", "중형": "🔷", "대형": "🔶"}.get(stock.get("group", ""), "")
    event_warn    = "⚡ 단발뉴스주의" if stock.get("event_news") else ""
    overheat_warn = ""
    if stock["rsi"] >= 70 and stock["change"] >= 15:
        overheat_warn = f"🔥 과열주의 (RSI {stock['rsi']} + 당일 {stock['change']}%)"

    verdict = stock.get("verdict", "")
    reasons = stock.get("reasons", [])
    risks   = stock.get("risks", [])

    warn_line = "  ".join(filter(None, [event_warn, overheat_warn]))

    msg = (
        f"🚨 수급 감지 #{rank}\n\n"
        f"🔥 종목: {stock['name']} ({stock['code']})  {group_emoji}{stock.get('group','')}주"
        + (f"  {warn_line}" if warn_line else "") +
        f"\n⭐ 점수: {stock['score']}점  {verdict}\n\n"
    )

    if stock.get("ai_analysis"):
        msg += f"🤖 AI 분석\n  {stock['ai_analysis']}\n\n"

    msg += (
        f"💡 핵심: 거래량 {stock['volume_ratio']}배 · RSI {stock['rsi']} · 당일 {stock['change']:+.1f}% · 5일 {stock['five_day_change']:+.1f}%"
        + (f" · {'/'.join(stock['themes'])} 테마" if stock['themes'] else "") +
        f"\n\n"
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
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if reasons:
        msg += "\n📋 추천 근거\n"
        msg += "\n".join(f"  ✔ {r}" for r in reasons)

    if risks:
        msg += "\n\n⚠️ 리스크\n"
        msg += "\n".join(f"  • {r}" for r in risks)

    if stock["themes"]:
        msg += "\n\n🏷️ 테마\n" + "\n".join(f"  - {t}" for t in stock["themes"])
    if stock["news"]:
        msg += "\n\n📰 뉴스\n" + "\n".join(f"  • {n}" for n in stock["news"])

    msg += "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    return msg

def main() -> None:

    if not is_market_open():
        now_kst = datetime.utcnow() + timedelta(hours=9)
        print(f"⏸ 장 시간 외 — 실행 생략 (현재 KST {now_kst.hour:02d}:{now_kst.minute:02d})")
        sys.exit(0)

    start_time = time.time()
    print("=" * 50)
    print(f"🚀 초기 수급 탐지 스캐너 v3.1")
    print(f"   실행 시각: {TODAY.strftime('%Y-%m-%d %H:%M:%S')} KST")
    print("=" * 50)

    print("\n📊 KOSPI 데이터 로딩 중...")
    try:
        kospi_data = fdr.DataReader("KS11", START_DATE)
    except Exception as e:
        logging.error(f"KOSPI 로딩 실패: {e}")
        kospi_data = None
        print("  ⚠️ KOSPI 로딩 실패 — 상대강도 생략")

    print("\n📦 섹터 ETF 로딩 중...")
    etf_cache = load_etf_cache()

    print("\n📋 KRX 종목 로딩 중...")
    all_stocks = fdr.StockListing("KRX")
    all_stocks = all_stocks[all_stocks["Market"].isin(["KOSPI", "KOSDAQ"])]

    target_stocks = []
    for group_name, cfg in GROUPS.items():
        group = all_stocks[
            (all_stocks["Marcap"] >= cfg["min"]) &
            (all_stocks["Marcap"] <  cfg["max"])
        ].sort_values(by="Marcap", ascending=False).head(cfg["limit"])
        group = group.copy()
        group["_group"]     = group_name
        group["_group_cfg"] = [cfg] * len(group)
        target_stocks.append(group)
        print(f"  {group_name}주: {len(group)}개")

    combined = pd.concat(target_stocks)
    print(f"  총 분석 대상: {len(combined)}개\n")

    results = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {
            executor.submit(
                analyze_stock,
                row,
                kospi_data,
                etf_cache,
                row["_group_cfg"],
                row["_group"]
            ): row["Name"]
            for _, row in combined.iterrows()
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
        send_discord_message(msg, WEBHOOK_STOCK)
        return

    for stock in top_results:
        verdict_info      = generate_verdict(stock)
        stock["verdict"]  = verdict_info["verdict"]
        stock["reasons"]  = verdict_info["reasons"]
        stock["risks"]    = verdict_info["risks"]
        stock["ai_analysis"] = get_ai_analysis(stock)
        time.sleep(1)

    save_results(results)
    save_positions(top_results)

    for rank, stock in enumerate(top_results, start=1):
        message = format_discord_message(stock, rank)
        print(message)
        print("-" * 40)
        send_discord_message(message, WEBHOOK_STOCK)
        time.sleep(0.3)

    print(f"\n✅ 완료 | 소요: {elapsed}초")


if __name__ == "__main__":
    main()