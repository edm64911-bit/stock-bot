import FinanceDataReader as fdr
import yfinance as yf
import feedparser
import requests
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

# 디스코드 웹훅 URL
WEBHOOK_URL = "https://discord.com/api/webhooks/1507283857643143168/lcTH5vRk94YHIxb0zCf9Q7RJmqb2gse3sPcVsUp9FbnMPrm_pTgs16FnfWFmJY5QLCrf"

# 테마 키워드
themes = {
    "AI": ["AI", "인공지능"],
    "반도체": ["반도체", "HBM", "엔비디아"],
    "로봇": ["로봇"],
    "2차전지": ["배터리", "전기차"],
    "방산": ["방산", "국방"],
    "바이오": ["바이오", "제약"],
}

def clean_text(text):

    return str(text).replace(" ", "").lower()

def get_market_suffix(market_name):

    if "KOSDAQ" in market_name:
        return ".KQ"

    return ".KS"

def analyze_stock(row):

    code = row['Code']
    name = row['Name']
    market = row['Market']

    ticker_code = (
        code + get_market_suffix(market)
    )

    try:

        ticker = yf.Ticker(ticker_code)

        data = ticker.history(period="6mo")

        if len(data) < 40:
            return None

        # 거래량
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

        # 가격
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

        # 이동평균선
        ma20 = (
            data["Close"]
            .tail(20)
            .mean()
        )

        # RSI
        delta = data['Close'].diff()

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
            100 - (100 / (1 + rs))
        )

        today_rsi = rsi.iloc[-1]

        # 추천 조건
        if (
            today_volume > avg_volume * 1.1
            and change_percent > 2
            and today_close > ma20
            and today_rsi < 70
        ):

            # 뉴스 가져오기
            news_url = (
                f"https://news.google.com/rss/search?"
                f"q={name}+주식&hl=ko&gl=KR&ceid=KR:ko"
            )

            news = feedparser.parse(news_url)

            news_titles = []

            detected_themes = set()

            for item in news.entries[:3]:

                title = item.title

                news_titles.append(title)

                cleaned = clean_text(title)

                for theme, keywords in themes.items():

                    for keyword in keywords:

                        if (
                            clean_text(keyword)
                            in cleaned
                        ):

                            detected_themes.add(theme)

            # 점수 계산
            score = 0

            if today_volume > avg_volume * 1.5:
                score += 2
            else:
                score += 1

            if change_percent > 5:
                score += 2
            else:
                score += 1

            if today_rsi < 60:
                score += 1

            if len(news_titles) >= 3:
                score += 2

            if len(detected_themes) >= 1:
                score += 2

            return {
                "name": name,
                "score": score,
                "change": round(change_percent, 2),
                "volume": int(
                    today_volume / avg_volume * 100
                ),
                "themes": list(detected_themes),
                "news": news_titles
            }

    except:
        return None

def send_discord_message(message):

    data = {
        "content": message
    }

    requests.post(
        WEBHOOK_URL,
        json=data
    )

def main():

    print("🚀 종목 분석 시작")

    stocks = (
        fdr.StockListing('KRX')
        .head(50)
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
            for _, row in stocks.iterrows()
        ]

        for future in as_completed(futures):

            result = future.result()

            if result:
                results.append(result)

    # 점수순 정렬
    results = sorted(
        results,
        key=lambda x: x['score'],
        reverse=True
    )

    # 상위 3개만 전송
    top_results = results[:3]

    if not top_results:

        send_discord_message(
            "❌ 오늘 조건 만족 종목 없음"
        )

        return

    for stock in top_results:

        message = (
            f"🔥 추천 종목: {stock['name']}\n\n"
            f"⭐ 추천 점수: {stock['score']}점\n"
            f"📈 등락률: {stock['change']}%\n"
            f"📊 거래량 증가: {stock['volume']}%\n\n"
            f"🏷️ 테마:\n"
        )

        if stock['themes']:

            for theme in stock['themes']:

                message += f"- {theme}\n"

        message += "\n📰 뉴스:\n"

        for news in stock['news']:

            message += f"• {news}\n"

        send_discord_message(message)

        time.sleep(1)

if __name__ == "__main__":

    main()