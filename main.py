import FinanceDataReader as fdr
import yfinance as yf
import feedparser
import requests
import pandas as pd
import numpy as np
import math
import time

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed
)

# ==================================================
# 디스코드 웹훅
# ==================================================
WEBHOOK_URL = "https://discord.com/api/webhooks/1507283857643143168/lcTH5vRk94YHIxb0zCf9Q7RJmqb2gse3sPcVsUp9FbnMPrm_pTgs16FnfWFmJY5QLCrf"

# ==================================================
# 테마 키워드
# ==================================================
themes = {

    "AI": [
        "AI",
        "인공지능",
        "챗GPT",
        "LLM"
    ],

    "반도체": [
        "반도체",
        "HBM",
        "엔비디아"
    ],

    "로봇": [
        "로봇",
        "자동화"
    ],

    "2차전지": [
        "배터리",
        "전기차"
    ],

    "방산": [
        "방산",
        "국방"
    ],

    "바이오": [
        "바이오",
        "제약",
        "FDA"
    ],

    "전력": [
        "전력",
        "원전"
    ]
}

# ==================================================
# 뉴스 점수 키워드
# ==================================================
positive_keywords = {

    "계약": 3,
    "수주": 3,
    "협약": 2,
    "투자": 2,
    "실적": 2,
    "정부": 2,
    "FDA": 3,
    "공급": 2,
    "양산": 2,
}

negative_keywords = {

    "유상증자": -5,
    "전환사채": -4,
    "적자": -3,
    "감자": -5,
}

# ==================================================
# 문자열 정리
# ==================================================
def clean_text(text):

    return str(text).replace(
        " ",
        ""
    ).lower()

# ==================================================
# 디스코드 메시지
# ==================================================
def send_discord_message(message):

    try:

        requests.post(
            WEBHOOK_URL,
            json={
                "content": message
            },
            timeout=10
        )

    except:
        pass

# ==================================================
# 시장 구분
# ==================================================
def get_market_suffix(market):

    if "KOSDAQ" in market:
        return ".KQ"

    return ".KS"

# ==================================================
# RSI 계산
# ==================================================
def calculate_rsi(close):

    delta = close.diff()

    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)

    ema_up = up.ewm(
        com=13,
        adjust=False
    ).mean()

    ema_down = down.ewm(
        com=13,
        adjust=False
    ).mean()

    rs = ema_up / ema_down

    rsi = (
        100
        - (100 / (1 + rs))
    )

    return rsi.iloc[-1]

# ==================================================
# ATR 계산
# ==================================================
def calculate_atr(data):

    data['Prev_Close'] = (
        data['Close'].shift(1)
    )

    data['TR1'] = (
        data['High']
        - data['Low']
    )

    data['TR2'] = abs(
        data['High']
        - data['Prev_Close']
    )

    data['TR3'] = abs(
        data['Low']
        - data['Prev_Close']
    )

    data['TR'] = data[
        ['TR1', 'TR2', 'TR3']
    ].max(axis=1)

    data['ATR'] = (
        data['TR']
        .rolling(14)
        .mean()
    )

    return data['ATR'].iloc[-1]

# ==================================================
# 시장 상태 분석
# ==================================================
def market_condition():

    try:

        kosdaq = yf.Ticker("^KQ11")

        data = kosdaq.history(
            period="5d"
        )

        if len(data) < 3:
            return "NEUTRAL"

        recent_change = (
            (
                data['Close'].iloc[-1]
                - data['Close'].iloc[-3]
            )
            / data['Close'].iloc[-3]
        ) * 100

        if recent_change > 2:
            return "BULL"

        elif recent_change < -2:
            return "BEAR"

        return "NEUTRAL"

    except:
        return "NEUTRAL"

# ==================================================
# 뉴스 분석
# ==================================================
def analyze_news(name):

    try:

        news_url = (
            f"https://news.google.com/rss/search?"
            f"q={name}+주식&hl=ko&gl=KR&ceid=KR:ko"
        )

        news = feedparser.parse(
            news_url
        )

        news_titles = []

        detected_themes = set()

        news_score = 0

        for item in news.entries[:5]:

            title = item.title

            if title in news_titles:
                continue

            news_titles.append(title)

            cleaned = clean_text(title)

            # 테마 분석
            for (
                theme,
                keywords
            ) in themes.items():

                for keyword in keywords:

                    if (
                        clean_text(keyword)
                        in cleaned
                    ):

                        detected_themes.add(
                            theme
                        )

            # 긍정 점수
            for (
                keyword,
                score
            ) in positive_keywords.items():

                if (
                    clean_text(keyword)
                    in cleaned
                ):

                    news_score += score

            # 부정 점수
            for (
                keyword,
                score
            ) in negative_keywords.items():

                if (
                    clean_text(keyword)
                    in cleaned
                ):

                    news_score += score

        return (
            news_titles[:3],
            list(detected_themes),
            news_score
        )

    except:

        return [], [], 0

# ==================================================
# 종목 분석
# ==================================================
def analyze_stock(row, market_status):

    try:

        code = row['Code']
        name = row['Name']
        market = row['Market']

        ticker_code = (
            code
            + get_market_suffix(market)
        )

        ticker = yf.Ticker(
            ticker_code
        )

        data = ticker.history(
            period="6mo"
        )

        data = data.dropna()

        if len(data) < 60:
            return None

        # ==================================================
        # 가격
        # ==================================================
        today_close = (
            data['Close']
            .iloc[-1]
        )

        yesterday_close = (
            data['Close']
            .iloc[-2]
        )

        if (
            today_close <= 0
            or yesterday_close <= 0
        ):
            return None

        # ==================================================
        # 상승률
        # ==================================================
        change_percent = (
            (
                today_close
                - yesterday_close
            )
            / yesterday_close
        ) * 100

        change_percent = round(
            change_percent,
            2
        )

        # ==================================================
        # 위험 제거
        # ==================================================
        if change_percent > 7:
            return None

        if today_close < 2000:
            return None

        # ==================================================
        # 거래량
        # ==================================================
        avg_volume = (
            data['Volume']
            .iloc[-11:-1]
            .mean()
        )

        today_volume = (
            data['Volume']
            .iloc[-1]
        )

        if avg_volume <= 0:
            return None

        volume_ratio = (
            today_volume
            / avg_volume
        )

        # ==================================================
        # 거래대금
        # ==================================================
        trading_value = (
            today_close
            * today_volume
        )

        if trading_value < 30000000000:
            return None

        # ==================================================
        # 거래대금 지속성
        # ==================================================
        recent_value = (
            (
                data['Close']
                * data['Volume']
            )
            .tail(3)
            .mean()
        )

        old_value = (
            (
                data['Close']
                * data['Volume']
            )
            .iloc[-20:-3]
            .mean()
        )

        if recent_value < old_value * 1.3:
            return None

        # ==================================================
        # 이동평균선
        # ==================================================
        ma5 = (
            data['Close']
            .tail(5)
            .mean()
        )

        ma20 = (
            data['Close']
            .tail(20)
            .mean()
        )

        # 정배열
        if ma5 < ma20:
            return None

        # 눌림 유지
        if today_close < ma5:
            return None

        # ==================================================
        # RSI
        # ==================================================
        today_rsi = calculate_rsi(
            data['Close']
        )

        if (
            math.isnan(today_rsi)
            or math.isinf(today_rsi)
        ):
            return None

        # ==================================================
        # ATR
        # ==================================================
        today_atr = calculate_atr(
            data
        )

        if (
            math.isnan(today_atr)
            or math.isinf(today_atr)
        ):
            return None

        # ==================================================
        # 초기 수급 조건
        # ==================================================
        if not (

            volume_ratio > 2

            and change_percent > -1
            and change_percent < 3

            and today_rsi < 60

            and today_close > ma20

        ):

            return None

        # ==================================================
        # 뉴스 분석
        # ==================================================
        (
            news_titles,
            detected_themes,
            news_score
        ) = analyze_news(name)

        # ==================================================
        # 진입 / 목표 / 손절
        # ==================================================
        entry_price = int(
            today_close
        )

        target_price = int(
            today_close
            + (today_atr * 2)
        )

        stop_loss = int(
            today_close
            - (today_atr * 1.2)
        )

        # ==================================================
        # 추천 이유
        # ==================================================
        reasons = []

        if volume_ratio > 3:
            reasons.append(
                "거래량 급증"
            )

        if today_rsi < 55:
            reasons.append(
                "초기 수급"
            )

        if len(detected_themes) >= 1:
            reasons.append(
                "테마 자금 유입"
            )

        if news_score >= 3:
            reasons.append(
                "강한 뉴스 재료"
            )

        # ==================================================
        # 점수 계산
        # ==================================================
        score = 0

        # 거래량
        score += min(
            int(volume_ratio),
            5
        )

        # 거래대금
        if trading_value > 100000000000:
            score += 5

        elif trading_value > 50000000000:
            score += 3

        # RSI
        if 45 <= today_rsi <= 60:
            score += 3

        # 상승률
        if 0 < change_percent < 3:
            score += 4

        # 뉴스
        score += news_score

        # 테마
        score += (
            len(detected_themes)
            * 2
        )

        # ==================================================
        # 시장 상태 반영
        # ==================================================
        if market_status == "BULL":
            score += 2

        elif market_status == "BEAR":
            score -= 2

        # ==================================================
        # 등급
        # ==================================================
        if score >= 15:
            grade = "S"

        elif score >= 11:
            grade = "A"

        elif score >= 8:
            grade = "B"

        else:
            grade = "C"

        return {

            "name": name,

            "score": score,

            "grade": grade,

            "change": change_percent,

            "volume_ratio": round(
                volume_ratio,
                1
            ),

            "trading_value": int(
                trading_value
                / 100000000
            ),

            "rsi": round(
                today_rsi,
                1
            ),

            "themes": detected_themes,

            "news": news_titles,

            "entry_price": entry_price,

            "target_price": target_price,

            "stop_loss": stop_loss,

            "reasons": reasons
        }

    except Exception as e:

        print(e)

        return None

# ==================================================
# 메인 실행
# ==================================================
def main():

    print("🚀 초기 수급 탐지 시작")

    market_status = market_condition()

    print(f"시장 상태: {market_status}")

    # ==================================================
    # 종목 리스트
    # ==================================================
    stocks = fdr.StockListing('KRX')

    stocks = stocks[
        stocks['Market'].isin([
            'KOSPI',
            'KOSDAQ'
        ])
    ]

    results = []

    # ==================================================
    # 멀티스레드 분석
    # ==================================================
    with ThreadPoolExecutor(
        max_workers=10
    ) as executor:

        futures = [

            executor.submit(
                analyze_stock,
                row,
                market_status
            )

            for _, row
            in stocks.iterrows()
        ]

        for future in as_completed(
            futures
        ):

            result = future.result()

            if result:
                results.append(result)

    # ==================================================
    # 점수 정렬
    # ==================================================
    results = sorted(
        results,
        key=lambda x: x['score'],
        reverse=True
    )

    top_results = results[:5]

    # ==================================================
    # 결과 없음
    # ==================================================
    if not top_results:

        send_discord_message(
            "❌ 조건 만족 종목 없음"
        )

        return

    # ==================================================
    # 시장 메시지
    # ==================================================
    market_message = (

        f"📊 시장 상태: {market_status}\n\n"

        f"🔥 초기 수급 후보 "
        f"{len(top_results)}개 탐지"
    )

    send_discord_message(
        market_message
    )

    time.sleep(1)

    # ==================================================
    # 종목 메시지
    # ==================================================
    for stock in top_results:

        message = (

            f"🚨 [{stock['grade']}급] "
            f"초기 수급 감지\n\n"

            f"🔥 종목: "
            f"{stock['name']}\n\n"

            f"⭐ 점수: "
            f"{stock['score']}점\n\n"

            f"📈 등락률: "
            f"{stock['change']}%\n"

            f"📊 거래량: "
            f"{stock['volume_ratio']}배\n"

            f"💰 거래대금: "
            f"{stock['trading_value']}억\n"

            f"📉 RSI: "
            f"{stock['rsi']}\n\n"

            f"🎯 진입가: "
            f"{stock['entry_price']:,}원\n"

            f"🚀 목표가: "
            f"{stock['target_price']:,}원\n"

            f"🛑 손절가: "
            f"{stock['stop_loss']:,}원\n\n"

            f"💡 추천 이유\n"
        )

        for reason in stock['reasons']:

            message += (
                f"- {reason}\n"
            )

        message += "\n🏷️ 테마\n"

        if stock['themes']:

            for theme in stock['themes']:

                message += (
                    f"- {theme}\n"
                )

        message += "\n📰 뉴스\n"

        for news in stock['news']:

            message += (
                f"• {news}\n"
            )

        send_discord_message(
            message
        )

        time.sleep(1)

# ==================================================
# 실행
# ==================================================
if __name__ == "__main__":

    main()