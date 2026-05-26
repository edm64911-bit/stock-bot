import os
import time
import requests
import feedparser
import pandas as pd
import FinanceDataReader as fdr

# ==================================================
# 디스코드 웹훅
# ==================================================
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL"
)

# ==================================================
# 테마 키워드
# ==================================================
themes = {

    "AI": [
        "AI",
        "인공지능",
        "LLM",
        "챗GPT"
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
        "FDA",
        "제약"
    ]
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

    except Exception as e:

        print("디스코드 오류:", e)

# ==================================================
# RSI
# ==================================================
def calculate_rsi(close, period=14):

    delta = close.diff()

    gain = (
        delta.where(
            delta > 0,
            0
        )
    )

    loss = (
        -delta.where(
            delta < 0,
            0
        )
    )

    avg_gain = gain.rolling(
        period
    ).mean()

    avg_loss = loss.rolling(
        period
    ).mean()

    rs = avg_gain / avg_loss

    rsi = (
        100
        - (100 / (1 + rs))
    )

    return rsi.iloc[-1]

# ==================================================
# ATR
# ==================================================
def calculate_atr(data):

    high_low = (
        data['High']
        - data['Low']
    )

    high_close = abs(
        data['High']
        - data['Close'].shift()
    )

    low_close = abs(
        data['Low']
        - data['Close'].shift()
    )

    ranges = pd.concat(
        [
            high_low,
            high_close,
            low_close
        ],
        axis=1
    )

    true_range = ranges.max(axis=1)

    atr = (
        true_range
        .rolling(14)
        .mean()
    )

    return atr.iloc[-1]

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

        for item in news.entries[:3]:

            title = item.title

            news_titles.append(title)

            cleaned = clean_text(
                title
            )

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

        return (
            news_titles,
            list(detected_themes)
        )

    except:

        return [], []

# ==================================================
# 종목 분석
# ==================================================
def analyze_stock(row):

    try:

        code = row['Code']
        name = row['Name']

        # ==================================================
        # ETF 제거
        # ==================================================
        if "ETF" in name:
            return None

        # ==================================================
        # 우선주 제거
        # ==================================================
        if "우" in name:
            return None

        # ==================================================
        # 시총 필터
        # ==================================================
        marcap = row['Marcap']

        # 시총 15조 이상 제거
        if marcap > 15000000000000:
            return None

        # ==================================================
        # 데이터
        # ==================================================
        data = fdr.DataReader(
            code,
            '2025-01-01'
        )

        data = data.dropna()

        if len(data) < 30:
            return None

        # ==================================================
        # 가격
        # ==================================================
        today_close = (
            data['Close'].iloc[-1]
        )

        yesterday_close = (
            data['Close'].iloc[-2]
        )

        if (
            today_close <= 0
            or yesterday_close <= 0
        ):
            return None

        # ==================================================
        # 등락률
        # ==================================================
        change_percent = (

            (
                today_close
                - yesterday_close
            )

            / yesterday_close

        ) * 100

        # ==================================================
        # 최근 5일 상승률
        # ==================================================
        five_day_change = (

            (
                today_close
                - data['Close'].iloc[-5]
            )

            / data['Close'].iloc[-5]

        ) * 100

        # 최근 너무 오른 종목 제외
        if five_day_change > 15:
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
            data['Volume'].iloc[-1]
        )

        if avg_volume <= 0:
            return None

        volume_ratio = (
            today_volume
            / avg_volume
        )

        # ==================================================
        # 거래량 급증
        # ==================================================
        if volume_ratio < 1.8:
            return None

        # ==================================================
        # 거래대금
        # ==================================================
        trading_value = (
            today_close
            * today_volume
        )

        # 거래대금 30억 이상
        if trading_value < 3000000000:
            return None

        # ==================================================
        # RSI
        # ==================================================
        today_rsi = calculate_rsi(
            data['Close']
        )

        # RSI 과열 제외
        if today_rsi > 70:
            return None

        # ==================================================
        # ATR
        # ==================================================
        today_atr = calculate_atr(
            data
        )

        # ==================================================
        # 이동평균선
        # ==================================================
        ma20 = (
            data['Close']
            .tail(20)
            .mean()
        )

        # 추세 유지
        if today_close < ma20:
            return None

        # ==================================================
        # 뉴스
        # ==================================================
        (
            news_titles,
            detected_themes
        ) = analyze_news(name)

        # ==================================================
        # 진입 / 목표 / 손절
        # ==================================================
        entry_price = int(
            today_close
        )

        target_price = int(
            today_close
            + (today_atr * 1.5)
        )

        stop_loss = int(
            today_close
            - today_atr
        )

        # ==================================================
        # 점수
        # ==================================================
        score = 0

        # 거래량
        if volume_ratio > 3:
            score += 5

        elif volume_ratio > 2:
            score += 3

        # 등락률
        if 0 < change_percent < 5:
            score += 3

        # RSI
        if today_rsi < 60:
            score += 2

        # 거래대금
        if trading_value > 10000000000:
            score += 3

        # 테마
        score += (
            len(detected_themes)
            * 2
        )

        print(name, "통과")

        return {

            "name": name,

            "score": score,

            "change": round(
                change_percent,
                2
            ),

            "five_day_change": round(
                five_day_change,
                2
            ),

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

            "stop_loss": stop_loss
        }

    except Exception as e:

        print(name, e)

        return None

# ==================================================
# 메인
# ==================================================
def main():

    print("🚀 초기 수급 탐지 시작")

    # ==================================================
    # 종목 리스트
    # ==================================================
    stocks = fdr.StockListing(
        'KRX'
    )

    stocks = stocks[

        stocks['Market'].isin([
            'KOSPI',
            'KOSDAQ'
        ])

    ]

    # ==================================================
    # 시총 500억 이상
    # ==================================================
    stocks = stocks[
        stocks['Marcap'] > 50000000000
    ]

    results = []

    # ==================================================
    # 분석
    # ==================================================
    for _, row in stocks.iterrows():

        result = analyze_stock(row)

        if result:
            results.append(result)

    print(
        f"최종 후보 개수: {len(results)}"
    )

    # ==================================================
    # 정렬
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
            "❌ 초기 수급 감지 종목 없음"
        )

        return

    # ==================================================
    # 디스코드 출력
    # ==================================================
    for stock in top_results:

        message = (

            f"🚨 초기 수급 감지\n\n"

            f"🔥 종목: "
            f"{stock['name']}\n\n"

            f"⭐ 점수: "
            f"{stock['score']}점\n\n"

            f"📈 당일 상승률: "
            f"{stock['change']}%\n"

            f"📊 5일 상승률: "
            f"{stock['five_day_change']}%\n"

            f"📈 거래량 증가: "
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
            f"{stock['stop_loss']:,}원\n"

        )

        # ==================================================
        # 테마
        # ==================================================
        if stock['themes']:

            message += "\n🏷️ 테마\n"

            for theme in stock['themes']:

                message += (
                    f"- {theme}\n"
                )

        # ==================================================
        # 뉴스
        # ==================================================
        if stock['news']:

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