"""
===================================================
  초기 수급 탐지 스캐너 v2.0
  개선사항:
    - RSI: rolling mean → Wilder's EWM 방식으로 수정
    - 상대강도(vs KOSPI) 추가
    - 52주 신고가 근접 여부 추가
    - 외국인/기관 순매수 방향 추가
    - 섹터 ETF 모멘텀 필터 추가
    - 거래량 급등 + 눌림 패턴 추가
    - 에러 로깅: 콘솔 + 파일 + Discord
    - 결과 JSON 저장 (백테스트용)
    - 하드코딩 날짜 → 동적 처리
    - list.append() 스레드 안전성 → Lock 적용
    - 실행 시간 측정
===================================================
"""

import os
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# ==================================================
# 날짜 동적 설정
# ==================================================
TODAY        = datetime.today()
START_DATE   = (TODAY - timedelta(days=365)).strftime("%Y-%m-%d")  # 1년치
START_DATE_1Y = START_DATE  # 52주 신고가용

# ==================================================
# 테마 키워드
# ==================================================
THEMES = {
    "AI":     ["AI", "인공지능", "LLM", "챗GPT", "생성형"],
    "반도체": ["반도체", "HBM", "엔비디아", "파운드리", "웨이퍼"],
    "로봇":   ["로봇", "자동화", "휴머노이드"],
    "2차전지":["배터리", "전기차", "양극재", "음극재", "전해질"],
    "방산":   ["방산", "국방", "K-방산", "무기"],
    "바이오": ["바이오", "FDA", "제약", "임상", "신약"],
}

# ==================================================
# 섹터 ETF (모멘텀 필터용)
# ==================================================
SECTOR_ETFS = {
    "반도체": "091160",   # KODEX 반도체
    "2차전지": "305720",  # KODEX 2차전지산업
    "바이오":  "244580",  # KODEX 바이오
    "AI":      "379800",  # KODEX 미국S&P500
    "방산":    "425810",  # KODEX K-방산
}

# ==================================================
# 결과 저장 Lock (스레드 안전)
# ==================================================
results_lock = Lock()

# ==================================================
# 문자열 정리
# ==================================================
def clean_text(text: str) -> str:
    return str(text).replace(" ", "").lower()

# ==================================================
# 디스코드 메시지 전송
# ==================================================
def send_discord_message(message: str) -> None:
    if not WEBHOOK_URL:
        print("[Discord] WEBHOOK_URL 미설정 — 콘솔 출력:\n", message)
        return
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"content": message},
            timeout=10
        )
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Discord 전송 오류: {e}")
        print("디스코드 오류:", e)

# ==================================================
# 디스코드 에러 알림 (치명적 오류만)
# ==================================================
def send_discord_error(msg: str) -> None:
    send_discord_message(f"⚠️ [스캐너 오류] {msg}")

# ==================================================
# RSI (Wilder's Smoothing 방식)
# ==================================================
def calculate_rsi(close: pd.Series, period: int = 14) -> float:
    """
    표준 RSI: Wilder's EWM(com=period-1) 사용
    rolling mean 방식은 실제 RSI와 다른 값이 나옴
    """
    delta = close.diff()
    gain  = delta.where(delta > 0, 0.0)
    loss  = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs  = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    return round(float(rsi.iloc[-1]), 2)

# ==================================================
# ATR (Average True Range)
# ==================================================
def calculate_atr(data: pd.DataFrame, period: int = 14) -> float:
    high_low    = data["High"] - data["Low"]
    high_close  = (data["High"] - data["Close"].shift()).abs()
    low_close   = (data["Low"]  - data["Close"].shift()).abs()

    true_range  = pd.concat(
        [high_low, high_close, low_close], axis=1
    ).max(axis=1)

    atr = true_range.rolling(period).mean()
    return float(atr.iloc[-1])

# ==================================================
# 52주 신고가 근접 여부
# ==================================================
def is_near_52w_high(data: pd.DataFrame, threshold: float = 0.90) -> bool:
    """
    현재가가 52주 최고가의 threshold% 이상이면 True
    신고가 돌파 직전 = 수급 집중 가능성 높음
    """
    high_52w    = data["Close"].tail(252).max()
    today_close = data["Close"].iloc[-1]
    return (today_close / high_52w) >= threshold

# ==================================================
# 거래량 급등 + 눌림 패턴
# ==================================================
def check_volume_consolidation(data: pd.DataFrame) -> bool:
    """
    최근 5일 중 거래량 상위 2일 이후
    가격이 3% 이내로 유지(눌림 없이 버팀)되는지 확인
    """
    recent      = data.tail(5)
    top2_idx    = recent["Volume"].nlargest(2).index
    avg_price   = recent.loc[top2_idx, "Close"].mean()
    today_close = data["Close"].iloc[-1]

    if avg_price <= 0:
        return False

    return (today_close / avg_price) >= 0.97

# ==================================================
# 상대강도 (vs KOSPI 5일)
# ==================================================
def get_relative_strength(
    stock_5d_change: float,
    kospi_data: pd.DataFrame
) -> float:
    """
    종목 5일 수익률 - KOSPI 5일 수익률
    양수일수록 시장 대비 강한 종목
    """
    if kospi_data is None or len(kospi_data) < 6:
        return 0.0

    kospi_5d = (
        (kospi_data["Close"].iloc[-1] / kospi_data["Close"].iloc[-5]) - 1
    ) * 100

    return round(stock_5d_change - kospi_5d, 2)

# ==================================================
# 외국인/기관 순매수 방향 (최근 3일)
# ==================================================
def get_investor_sentiment(code: str) -> dict:
    """
    FinanceDataReader 투자자별 매매동향
    외국인·기관 3일 순매수 합계 반환
    실패 시 {"foreign": 0, "institution": 0} 반환
    """
    try:
        start = (TODAY - timedelta(days=10)).strftime("%Y-%m-%d")
        df    = fdr.DataReader(
            code, start,
            exchange="KRX-INVESTOR"
        )
        if df is None or len(df) < 1:
            return {"foreign": 0, "institution": 0}

        foreign_net     = int(df["Foreign"].iloc[-3:].sum())
        institution_net = int(df["Institution"].iloc[-3:].sum())

        return {
            "foreign":     foreign_net,
            "institution": institution_net
        }
    except Exception:
        # 데이터 없는 종목 많으므로 로깅 생략
        return {"foreign": 0, "institution": 0}

# ==================================================
# 섹터 ETF 모멘텀 (MA20 위에 있는지)
# ==================================================
def is_sector_etf_bullish(themes: list, etf_cache: dict) -> bool:
    """
    감지된 테마의 ETF가 MA20 위에 있으면 True
    테마 없으면 기본 True (필터 미적용)
    """
    if not themes:
        return True

    for theme in themes:
        etf_code = SECTOR_ETFS.get(theme)
        if etf_code and etf_code in etf_cache:
            etf_data = etf_cache[etf_code]
            if len(etf_data) >= 20:
                ma20        = etf_data["Close"].tail(20).mean()
                etf_close   = etf_data["Close"].iloc[-1]
                if etf_close >= ma20:
                    return True

    return False

# ==================================================
# 섹터 ETF 데이터 사전 로딩
# ==================================================
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

# ==================================================
# 뉴스 분석
# ==================================================
def analyze_news(name: str) -> tuple[list, list]:
    try:
        url  = (
            f"https://news.google.com/rss/search?"
            f"q={name}+주식&hl=ko&gl=KR&ceid=KR:ko"
        )
        feed        = feedparser.parse(url)
        titles      = []
        detected    = set()

        for item in feed.entries[:3]:
            title = item.title
            titles.append(title)
            cleaned = clean_text(title)
            for theme, keywords in THEMES.items():
                if any(clean_text(kw) in cleaned for kw in keywords):
                    detected.add(theme)

        return titles, list(detected)

    except Exception as e:
        logging.error(f"뉴스 파싱 오류 [{name}]: {e}")
        return [], []

# ==================================================
# 종목 분석 (메인 로직)
# ==================================================
def analyze_stock(
    row,
    kospi_data: pd.DataFrame,
    etf_cache: dict
) -> dict | None:

    code = row["Code"]
    name = row["Name"]

    try:
        # ----- 기본 필터 -----
        if "ETF" in name or "우" in name:
            return None

        marcap = row["Marcap"]
        if marcap > 15_000_000_000_000:   # 15조 초과 제외
            return None

        # ----- 데이터 로딩 -----
        data = fdr.DataReader(code, START_DATE)
        data = data.dropna()

        if len(data) < 60:   # 충분한 데이터 필요 (52주 계산 등)
            return None

        today_close     = float(data["Close"].iloc[-1])
        yesterday_close = float(data["Close"].iloc[-2])

        if today_close <= 0 or yesterday_close <= 0:
            return None

        # ----- 등락률 -----
        change_pct = (today_close - yesterday_close) / yesterday_close * 100

        # ----- 5일 상승률 -----
        five_day_change = (
            (today_close - data["Close"].iloc[-5]) / data["Close"].iloc[-5] * 100
        )

        # 최근 급등 제외 (5일 15% 초과)
        if five_day_change > 15:
            return None

        # ----- 거래량 -----
        avg_volume   = data["Volume"].iloc[-11:-1].mean()
        today_volume = float(data["Volume"].iloc[-1])

        if avg_volume <= 0:
            return None

        volume_ratio = today_volume / avg_volume

        if volume_ratio < 1.8:
            return None

        # ----- 거래대금 -----
        trading_value = today_close * today_volume
        if trading_value < 3_000_000_000:   # 30억 미만 제외
            return None

        # ----- RSI (Wilder's EWM) -----
        today_rsi = calculate_rsi(data["Close"])
        if today_rsi > 70:
            return None

        # ----- MA20 위에 있어야 함 -----
        ma20 = float(data["Close"].tail(20).mean())
        if today_close < ma20:
            return None

        # ----- ATR -----
        today_atr = calculate_atr(data)

        # ----- [신규] 52주 신고가 근접 -----
        near_52w_high = is_near_52w_high(data)

        # ----- [신규] 거래량 급등 + 눌림 패턴 -----
        vol_consolidation = check_volume_consolidation(data)

        # ----- [신규] 상대강도 -----
        relative_strength = get_relative_strength(five_day_change, kospi_data)

        # ----- [신규] 외국인/기관 수급 -----
        investor = get_investor_sentiment(code)

        # ----- 뉴스 + 테마 -----
        news_titles, detected_themes = analyze_news(name)

        # ----- [신규] 섹터 ETF 모멘텀 -----
        sector_bullish = is_sector_etf_bullish(detected_themes, etf_cache)

        # ----- 진입 / 목표 / 손절 -----
        entry_price  = int(today_close)
        target_price = int(today_close + today_atr * 1.5)
        stop_loss    = int(today_close - today_atr)

        # ----- 점수 계산 -----
        score = 0

        # 거래량 배율
        if volume_ratio > 3:
            score += 5
        elif volume_ratio > 2:
            score += 3

        # 당일 상승률 (0~5% 구간이 가장 이상적)
        if 0 < change_pct < 5:
            score += 3

        # RSI
        if today_rsi < 60:
            score += 2

        # 거래대금
        if trading_value > 10_000_000_000:
            score += 3

        # 테마
        score += len(detected_themes) * 2

        # [신규] 52주 신고가 근접
        if near_52w_high:
            score += 3

        # [신규] 거래량 눌림 패턴
        if vol_consolidation:
            score += 2

        # [신규] 상대강도 (시장 대비 +5% 초과)
        if relative_strength > 5:
            score += 3
        elif relative_strength > 2:
            score += 1

        # [신규] 외국인 순매수
        if investor["foreign"] > 0:
            score += 3

        # [신규] 기관 순매수
        if investor["institution"] > 0:
            score += 2

        # [신규] 섹터 ETF 역행 시 감점
        if not sector_bullish:
            score -= 3

        print(f"  ✅ {name} 통과 | 점수: {score} | RSI: {today_rsi} | 거래량: {volume_ratio:.1f}배")

        return {
            "name":             name,
            "code":             code,
            "score":            score,
            "change":           round(change_pct, 2),
            "five_day_change":  round(five_day_change, 2),
            "volume_ratio":     round(volume_ratio, 1),
            "trading_value":    int(trading_value / 100_000_000),   # 억 단위
            "rsi":              today_rsi,
            "relative_strength": relative_strength,
            "near_52w_high":    near_52w_high,
            "vol_consolidation": vol_consolidation,
            "foreign_net":      investor["foreign"],
            "institution_net":  investor["institution"],
            "sector_bullish":   sector_bullish,
            "themes":           detected_themes,
            "news":             news_titles,
            "entry_price":      entry_price,
            "target_price":     target_price,
            "stop_loss":        stop_loss,
            "scanned_at":       TODAY.strftime("%Y-%m-%d %H:%M:%S"),
        }

    except Exception as e:
        err_msg = traceback.format_exc()
        logging.error(f"[{name}/{code}] 분석 오류:\n{err_msg}")

        # 연결 오류는 Discord로도 알림
        if "ConnectionError" in type(e).__name__:
            send_discord_error(f"연결 오류 발생 [{name}]")

        return None

# ==================================================
# 결과 JSON 저장 (백테스트용)
# ==================================================
def save_results(results: list) -> None:
    filename = f"scan_{TODAY.strftime('%Y%m%d_%H%M')}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 결과 저장 완료: {filename}")
    except Exception as e:
        logging.error(f"결과 저장 실패: {e}")

# ==================================================
# Discord 출력 포맷
# ==================================================
def format_discord_message(stock: dict) -> str:
    flag_52w  = "🔑 신고가 근접" if stock["near_52w_high"]    else ""
    flag_vol  = "🧱 눌림 패턴"  if stock["vol_consolidation"] else ""
    flag_sect = ""              if stock["sector_bullish"]    else "⚠️ 섹터 역행"
    flag_rs   = (
        f"📡 시장 대비 +{stock['relative_strength']}%"
        if stock["relative_strength"] > 2 else ""
    )

    flags = " ".join(f for f in [flag_52w, flag_vol, flag_rs, flag_sect] if f)

    msg = (
        f"🚨 초기 수급 감지\n\n"
        f"🔥 종목: {stock['name']} ({stock['code']})\n\n"
        f"⭐ 점수: {stock['score']}점\n"
        f"{flags}\n\n"
        f"📈 당일 상승률:   {stock['change']}%\n"
        f"📊 5일 상승률:    {stock['five_day_change']}%\n"
        f"📈 거래량 증가:   {stock['volume_ratio']}배\n"
        f"💰 거래대금:      {stock['trading_value']}억\n"
        f"📉 RSI:           {stock['rsi']}\n"
        f"🌐 상대강도:      {stock['relative_strength']:+.1f}%\n"
        f"👥 외국인 3일:    {stock['foreign_net']:+,}주\n"
        f"🏦 기관 3일:      {stock['institution_net']:+,}주\n\n"
        f"🎯 진입가:  {stock['entry_price']:,}원\n"
        f"🚀 목표가:  {stock['target_price']:,}원\n"
        f"🛑 손절가:  {stock['stop_loss']:,}원\n"
    )

    if stock["themes"]:
        msg += "\n🏷️ 테마\n"
        msg += "\n".join(f"  - {t}" for t in stock["themes"])

    if stock["news"]:
        msg += "\n\n📰 뉴스\n"
        msg += "\n".join(f"  • {n}" for n in stock["news"])

    return msg

# ==================================================
# 메인
# ==================================================
def main() -> None:
    start_time = time.time()
    print("=" * 50)
    print(f"🚀 초기 수급 탐지 스캐너 v2.0")
    print(f"   실행 시각: {TODAY.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # ----- KOSPI 데이터 로딩 (상대강도 계산용) -----
    print("\n📊 KOSPI 데이터 로딩 중...")
    try:
        kospi_data = fdr.DataReader("KS11", START_DATE)
    except Exception as e:
        logging.error(f"KOSPI 로딩 실패: {e}")
        kospi_data = None
        print("  ⚠️ KOSPI 데이터 로딩 실패 — 상대강도 비교 생략")

    # ----- 섹터 ETF 사전 로딩 -----
    print("\n📦 섹터 ETF 캐시 로딩 중...")
    etf_cache = load_etf_cache()

    # ----- 전체 종목 로딩 -----
    print("\n📋 KRX 종목 리스트 로딩 중...")
    stocks = fdr.StockListing("KRX")
    stocks = stocks[stocks["Market"].isin(["KOSPI", "KOSDAQ"])]
    stocks = stocks[stocks["Marcap"] > 50_000_000_000]     # 500억 이상
    stocks = stocks.sort_values(
        by="Marcap", ascending=False
    ).head(200)

    print(f"  분석 대상: {len(stocks)}개 종목\n")

    results = []

    # ----- 멀티스레드 분석 -----
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                analyze_stock, row, kospi_data, etf_cache
            ): row["Name"]
            for _, row in stocks.iterrows()
        }

        for future in as_completed(futures):
            result = future.result()
            if result:
                with results_lock:
                    results.append(result)

    # ----- 점수 정렬 -----
    results.sort(key=lambda x: x["score"], reverse=True)
    top_results = results[:5]

    elapsed = round(time.time() - start_time, 1)
    print(f"\n{'='*50}")
    print(f"  최종 후보: {len(results)}개 | 소요시간: {elapsed}초")
    print(f"{'='*50}\n")

    # ----- 결과 없음 -----
    if not top_results:
        msg = f"❌ 초기 수급 감지 종목 없음 (스캔 완료: {elapsed}초)"
        print(msg)
        send_discord_message(msg)
        return

    # ----- JSON 저장 -----
    save_results(results)

    # ----- Discord 전송 -----
    for stock in top_results:
        message = format_discord_message(stock)
        print(message)
        print("-" * 40)
        send_discord_message(message)
        time.sleep(0.3)

    print(f"\n✅ 완료 | 소요시간: {elapsed}초")

# ==================================================
# 실행
# ==================================================
if __name__ == "__main__":
    main()