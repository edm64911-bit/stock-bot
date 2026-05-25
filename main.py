import FinanceDataReader as fdr
import yfinance as yf
import feedparser
import requests
import time
import math

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed
)

# ====================================
# 디스코드 웹훅
# ====================================
WEBHOOK_URL = "https://discord.com/api/webhooks/1507283857643143168/lcTH5vRk94YHIxb0zCf9Q7RJmqb2gse3sPcVsUp9FbnMPrm_pTgs16FnfWFmJY5QLCrf"

# ====================================
# 테마 키워드
# ====================================
themes = {
    "AI": ["AI", "인공지능"],
    "반도체": ["반도체", "HBM", "엔비디아"],
    "로봇": ["로봇"],
    "2차전지": ["배터리", "전기차"],
    "방산": ["방산", "국방"],
    "바이오": ["바이오", "제약"],
    "전력": ["전력", "원전"],
}

# ====================================
# 문자열 정리
# ====================================
def clean_text(text):

    return str(text).replace(
        " ",
        ""
    ).lower()

# ====================================
# 시장 코드
# ====================================
def get_market_suffix(market):

    if "KOSDAQ" in market:
        return ".KQ"

    return ".KS"

# ====================================
# 디스코드 메시지 전송
# ====================================
def send_discord_message(message):

    data = {
        "content": message
    }

    requests.post(
        WEBHOOK_URL,
        json=data
    )

# ====================================
# RSI 계산
# ====================================
def calculate_rsi(close_prices):

    delta = close_prices.diff()

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
        100
        - (100 / (1 + rs))
    )

    return rsi.iloc[-1]

# ====================================
# ATR 계산
# ====================================
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
        .rolling(window=14)
        .mean()
    )

    return data['ATR'].iloc[-1]

# ====================================
# 종목 분석
# ====================================
def analyze_stock(row):

    try:

        code = row['Code']
        name = row['Name']
        market = row['Market']

        ticker_code = (
            code + get_market_suffix(market)
        )

        ticker = yf.Ticker(
            ticker_code
        )

        data = ticker.history(
            period="6mo"
        )

        data = data.dropna()

        if len(data) < 40:
            return None

        # ====================================
        # 거래량
        # ====================================
        avg_volume = (
            data["Volume"]
            .iloc[-11:-1]
            .mean()
        )

        today_volume = (
            data["Volume"]
            .iloc[-1]
        )

        if avg_volume <= 0:
            return None

        volume_ratio = (
            today_volume
            / avg_volume
        )

        # ====================================
        # 가격
        # ====================================
        yesterday_close = (
            data["Close"]
            .iloc[-2]
        )

        today_close = (
            data["Close"]
            .iloc[-1]
        )

        if (
            yesterday_close <= 0
            or today_close <= 0
        ):
            return None

        # ====================================
        # 등락률
        # ====================================
        change_percent = (
            (
                today_close
                - yesterday_close
            )
            / yesterday_close
        ) * 100

        if (
            math.isnan(change_percent)
            or math.isinf(change_percent)
        ):
            return None

        change_percent = round(
            change_percent,
            2
        )

        # ====================================
        # 거래대금
        # ====================================
        trading_value = (
            today_close
            * today_volume
        )

        # 거래대금 200억 이상
        if trading_value < 20000000000:
            return None

        # ====================================
        # 이동평균선
        # ====================================
        ma20 = (
            data["Close"]
            .tail(20)
            .mean()
        )

        # ====================================
        # RSI
        # ====================================
        today_rsi = calculate_rsi(
            data['Close']
        )

        if (
            math.isnan(today_rsi)
            or math.isinf(today_rsi)
        ):
            return None

        # ====================================
        # ATR
        # ====================================
        today_atr = calculate_atr(
            data
        )

        if (
            math.isnan(today_atr)
            or math.isinf(today_atr)
        ):
            return None

        # ====================================
        # 초기 수급 탐지
        # ====================================
        if (

            # 거래량 급증
            volume_ratio > 2

            # 아직 덜 오른 상태
            and change_percent > -1
            and change_percent < 3

            # RSI 과열 아님
            and today_rsi < 60

            # 추세 유지
            and today_close > ma20

        ):

            # ====================================
            # 뉴스
            # ====================================
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

                if title in news_titles:
                    continue

                news_titles.append(title)

                cleaned = clean_text(
                    title
                )

                # ====================================
                # 테마 분석
                # ====================================
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

            # ====================================
            # 진입/목표/손절
            # ====================================
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

            # ====================================
            # 추천 이유 생성
            # ====================================
            reasons = []

            if volume_ratio > 3:
                reasons.append(
                    "거래량 폭발"
                )

            if today_rsi < 55:
                reasons.append(
                    "초기 수급"
                )

            if len(detected_themes) >= 1:
                reasons.append(
                    "테마 자금 유입"
                )

            # ====================================
            # 추천 점수
            # ====================================
            score = 0

            # 거래량
            if volume_ratio > 3:
                score += 4
            elif volume_ratio > 2:
                score += 3

            # 초입 점수
            if (
                change_percent > 0
                and change_percent < 2
            ):
                score += 3

            # RSI
            if today_rsi < 55:
                score += 2

            # 거래대금
            if trading_value > 50000000000:
                score += 3

            # 테마
            if len(detected_themes) >= 1:
                score += 2

            return {

                "name": name,

                "score": score,

                "change": change_percent,

                "volume_ratio": round(
                    volume_ratio,
                    1
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

                "stop_loss": stop_loss,

                "rsi": round(
                    today_rsi,
                    1
                ),

                "reasons": reasons
            }

    except Exception as e:

        print(name, e)

        return None

# ====================================
# 메인 실행
# ====================================
def main():

    print("🚀 초기 수급 탐지 시작")

    stocks = (
        fdr.StockListing('KRX')
        .head(150)
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

    # ====================================
    # 점수순 정렬
    # ====================================
    results = sorted(
        results,
        key=lambda x: x['score'],
        reverse=True
    )

    # 상위 5개
    top_results = results[:5]

    # ====================================
    # 결과 없을 때
    # ====================================
    if not top_results:

        send_discord_message(
            "❌ 초기 수급 감지 종목 없음"
        )

        return

    # ====================================
    # 테마 집계
    # ====================================
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

    # ====================================
    # 시장 테마 메시지
    # ====================================
    theme_message = (
        "🔥 오늘 초기 수급 강한 테마\n\n"
    )

    for (
        theme,
        count
    ) in sorted_themes:

        theme_message += (
            f"- {theme} "
            f"({count}개 종목)\n"
        )

    send_discord_message(
        theme_message
    )

    time.sleep(1)

    # ====================================
    # 종목 메시지
    # ====================================
    for stock in top_results:

        reason_text = ", ".join(
            stock['reasons']
        )

        message = (

            f"🚨 초기 수급 감지\n\n"

            f"🔥 종목: "
            f"{stock['name']}\n\n"

            f"💡 추천 이유:\n"
            f"{reason_text}\n\n"

            f"⭐ 점수: "
            f"{stock['score']}점\n"

            f"📈 등락률: "
            f"{stock['change']}%\n"

            f"📊 거래량 배수: "
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

# ====================================
# 실행
# ====================================
if __name__ == "__main__":

    main()