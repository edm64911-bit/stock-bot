import FinanceDataReader as fdr
import yfinance as yf
import feedparser
import requests
import time

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed
)

# =========================
# 디스코드 웹훅
# =========================
WEBHOOK_URL = "https://discord.com/api/webhooks/1507283857643143168/lcTH5vRk94YHIxb0zCf9Q7RJmqb2gse3sPcVsUp9FbnMPrm_pTgs16FnfWFmJY5QLCrf"

# =========================
# 테마 키워드
# =========================
themes = {
    "AI": ["AI", "인공지능"],
    "반도체": ["반도체", "HBM", "엔비디아"],
    "로봇": ["로봇"],
    "2차전지": ["배터리", "전기차"],
    "방산": ["방산", "국방"],
    "바이오": ["바이오", "제약"],
    "전력": ["전력", "원전"],
}

# =========================
# 문자열 정리
# =========================
def clean_text(text):

    return str(text).replace(
        " ",
        ""
    ).lower()

# =========================
# 시장 접미사
# =========================
def get_market_suffix(market):

    if "KOSDAQ" in market:
        return ".KQ"

    return ".KS"

# =========================
# 종목 분석
# =========================
def analyze_stock(row):

    code = row['Code']
    name = row['Name']
    market = row['Market']

    ticker_code = (
        code + get_market_suffix(market)
    )

    try:

        ticker = yf.Ticker(ticker_code)

        data = ticker.history(
            period="6mo"
        )

        if len(data) < 40:
            return None

        # =========================
        # 거래량
        # =========================
        avg_volume = (
            data["Volume"]
            .iloc[-11:-1]
            .mean()
        )

        today_volume = (
            data["Volume"]
            .iloc[-1]
        )

        if avg_volume == 0:
            return None

        # =========================
        # 가격
        # =========================
        yesterday_close = (
            data["Close"]
            .iloc[-2]
        )

        today_close = (
            data["Close"]
            .iloc[-1]
        )

        change_percent = (
            (today_close - yesterday_close)
            / yesterday_close
        ) * 100

        # =========================
        # 거래대금
        # =========================
        trading_value = (
            today_close
            * today_volume
        )

        # 거래대금 100억 이하 제거
        if trading_value < 10000000000:
            return None

        # =========================
        # 20일 이동평균
        # =========================
        ma20 = (
            data["Close"]
            .tail(20)
            .mean()
        )

        # =========================
        # RSI
        # =========================
        delta = data['Close'].diff()

        up = delta.clip(lower=0)

        down = (
            -1
            * delta.clip(upper=0)
        )

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
            100 - (100 / (1 + rs))
        )

        today_rsi = rsi.iloc[-1]

        # =========================
        # ATR 계산
        # =========================
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
            .rolling(window=14)
            .mean()
        )

        today_atr = (
            data['ATR']
            .iloc[-1]
        )

        # =========================
        # 최근 지지/저항
        # =========================
        high_20d = (
            data['High']
            .tail(20)
            .max()
        )

        low_20d = (
            data['Low']
            .tail(20)
            .min()
        )

        # =========================
        # 진입/익절/손절
        # =========================
        entry_price = int(
            today_close
        )

        if today_close >= high_20d * 0.98:

            target_price = int(
                today_close
                + (today_atr * 2)
            )

        else:

            target_price = int(
                high_20d
            )

        stop_loss = int(
            max(
                today_close
                - (today_atr * 1.5),
                low_20d
            )
        )

        # =========================
        # 추천 조건
        # =========================
        if (
            today_volume > avg_volume * 1.1
            and change_percent > 2
            and today_close > ma20
            and today_rsi < 75
        ):

            # =========================
            # 뉴스
            # =========================
            news_url = (
                f"https://news.google.com/rss/search?"
                f"q={name}+주식&hl=ko&gl=KR&ceid=KR:ko"
            )

            news = feedparser.parse(
                news_url
            )

            news_titles = []

            detected_themes = set()

            for item in news.entries[:5]:

                title = item.title

                # 뉴스 중복 제거
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

            # =========================
            # 추천 점수
            # =========================
            score = 0

            # 거래량 점수
            if today_volume > avg_volume * 1.5:
                score += 3
            else:
                score += 1

            # 상승률 점수
            if change_percent > 5:
                score += 3
            elif change_percent > 3:
                score += 2
            else:
                score += 1

            # 거래대금 점수
            if trading_value > 50000000000:
                score += 3

            # RSI 점수
            if today_rsi < 60:
                score += 1

            # 뉴스 점수
            if len(news_titles) >= 3:
                score += 2

            # 테마 점수
            if len(detected_themes) >= 1:
                score += 2

            return {
                "name": name,
                "score": score,
                "change": round(
                    change_percent,
                    2
                ),
                "volume": int(
                    today_volume
                    / avg_volume
                    * 100
                ),
                "themes": list(
                    detected_themes
                ),
                "news": news_titles[:3],
                "trading_value": int(
                    trading_value
                    / 100000000
                ),
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss": stop_loss
            }

    except:
        return None

# =========================
# 디스코드 메시지
# =========================
def send_discord_message(message):

    data = {
        "content": message
    }

    requests.post(
        WEBHOOK_URL,
        json=data
    )

# =========================
# 메인 실행
# =========================
def main():

    print("🚀 종목 분석 시작")

    stocks = (
        fdr.StockListing('KRX')
        .head(100)
    )

    results = []

    with ThreadPoolExecutor(
        max_workers=10
    ) as executor:

        futures = [

            executor.submit(
                analyze_stock,
                row
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

    # =========================
    # 점수 정렬
    # =========================
    results = sorted(
        results,
        key=lambda x: x['score'],
        reverse=True
    )

    # 상위 5개
    top_results = results[:5]

    # =========================
    # 결과 없으면
    # =========================
    if not top_results:

        send_discord_message(
            "❌ 오늘 조건 만족 종목 없음"
        )

        return

    # =========================
    # 시장 테마 분석
    # =========================
    all_themes = []

    for stock in top_results:

        all_themes.extend(
            stock['themes']
        )

    theme_rank = {}

    for theme in all_themes:

        theme_rank[theme] = (
            theme_rank.get(theme, 0)
            + 1
        )

    sorted_themes = sorted(
        theme_rank.items(),
        key=lambda x: x[1],
        reverse=True
    )

    # =========================
    # 테마 메시지
    # =========================
    theme_message = (
        "🔥 오늘 강한 테마\n\n"
    )

    for theme, count in sorted_themes:

        theme_message += (
            f"- {theme} "
            f"({count}개 종목)\n"
        )

    send_discord_message(
        theme_message
    )

    time.sleep(1)

    # =========================
    # 종목 메시지
    # =========================
    for stock in top_results:

        message = (
            f"🔥 추천 종목: "
            f"{stock['name']}\n\n"

            f"⭐ 추천 점수: "
            f"{stock['score']}점\n"

            f"📈 등락률: "
            f"{stock['change']}%\n"

            f"📊 거래량 증가: "
            f"{stock['volume']}%\n"

            f"💰 거래대금: "
            f"{stock['trading_value']}억\n\n"

            f"🎯 진입가: "
            f"{stock['entry_price']:,}원\n"

            f"🚀 목표가: "
            f"{stock['target_price']:,}원\n"

            f"🛑 손절가: "
            f"{stock['stop_loss']:,}원\n\n"

            f"🏷️ 테마:\n"
        )

        if stock['themes']:

            for theme in stock['themes']:

                message += (
                    f"- {theme}\n"
                )

        message += "\n📰 뉴스:\n"

        for news in stock['news']:

            message += (
                f"• {news}\n"
            )

        send_discord_message(
            message
        )

        time.sleep(1)

# =========================
# 실행
# =========================
if __name__ == "__main__":

    main()