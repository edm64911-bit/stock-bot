"""
===================================================
  백테스트 스크립트
  - scan_*.json 파일 전부 읽어서
  - 각 종목이 실제로 목표가/손절가 도달했는지 확인
  - 적중률 / 평균 수익률 / RR 통계 출력
  - Discord로 월간 리포트 전송
===================================================
"""

import os
import json
import glob
import requests
import pandas as pd
import FinanceDataReader as fdr

from datetime import datetime, timedelta

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

def send_discord_message(message: str) -> None:
    if not WEBHOOK_URL:
        print(message)
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print("Discord 오류:", e)

# ==================================================
# scan_*.json 전체 로드
# ==================================================
def load_all_scans() -> list:
    files   = sorted(glob.glob("scan_*.json"))
    records = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                # 날짜 추출 (scan_20250526_0900.json)
                date_str = f.replace("scan_", "").replace(".json", "")
                for item in data:
                    item["scan_file"] = date_str
                records.extend(data)
        except Exception as e:
            print(f"  ⚠️ {f} 로딩 실패: {e}")
    print(f"  총 {len(records)}개 레코드 로드 ({len(files)}개 파일)")
    return records

# ==================================================
# 실제 가격 조회 (진입 다음날 ~ 5일 이내)
# ==================================================
def check_result(record: dict) -> dict:
    code         = record["code"]
    entry_price  = record["entry_price"]
    stop_loss    = record["stop_loss"]
    target_1     = record["target_price_1"]
    target_2     = record["target_price_2"]
    scanned_at   = record["scanned_at"]

    try:
        entry_date = datetime.strptime(scanned_at, "%Y-%m-%d %H:%M:%S")
        start      = (entry_date + timedelta(days=1)).strftime("%Y-%m-%d")
        end        = (entry_date + timedelta(days=10)).strftime("%Y-%m-%d")

        data = fdr.DataReader(code, start, end)
        data = data.dropna()

        if len(data) == 0:
            return {**record, "result": "데이터없음", "pnl_pct": 0}

        result   = "미달성"
        pnl_pct  = 0

        for _, row in data.iterrows():
            high = float(row["High"])
            low  = float(row["Low"])

            # 손절 먼저 체크 (당일 저가가 손절가 하회)
            if low <= stop_loss:
                result  = "손절"
                pnl_pct = round((stop_loss - entry_price) / entry_price * 100, 2)
                break

            # 2차 목표 체크
            if high >= target_2:
                result  = "2차목표달성"
                pnl_pct = round((target_2 - entry_price) / entry_price * 100, 2)
                break

            # 1차 목표 체크
            if high >= target_1:
                result  = "1차목표달성"
                pnl_pct = round((target_1 - entry_price) / entry_price * 100, 2)
                break

        return {**record, "result": result, "pnl_pct": pnl_pct}

    except Exception as e:
        return {**record, "result": f"오류({str(e)[:20]})", "pnl_pct": 0}

# ==================================================
# 통계 계산
# ==================================================
def calc_stats(results: list) -> dict:
    df = pd.DataFrame(results)
    df = df[df["result"].isin(["손절", "1차목표달성", "2차목표달성", "미달성"])]

    if len(df) == 0:
        return {}

    total      = len(df)
    wins       = len(df[df["result"].isin(["1차목표달성", "2차목표달성"])])
    losses     = len(df[df["result"] == "손절"])
    pending    = len(df[df["result"] == "미달성"])
    win_rate   = round(wins / (total - pending) * 100, 1) if (total - pending) > 0 else 0
    avg_pnl    = round(df["pnl_pct"].mean(), 2)
    avg_win    = round(df[df["pnl_pct"] > 0]["pnl_pct"].mean(), 2) if wins > 0 else 0
    avg_loss   = round(df[df["pnl_pct"] < 0]["pnl_pct"].mean(), 2) if losses > 0 else 0

    # 캔들 패턴별 승률
    candle_stats = {}
    for candle in ["장대양봉", "아랫꼬리양봉", "윗꼬리음봉", "보통"]:
        sub = df[(df["candle"] == candle) & (df["result"] != "미달성")]
        if len(sub) > 0:
            wr = round(len(sub[sub["result"].isin(["1차목표달성", "2차목표달성"])]) / len(sub) * 100, 1)
            candle_stats[candle] = f"{wr}% ({len(sub)}건)"

    return {
        "total":        total,
        "wins":         wins,
        "losses":       losses,
        "pending":      pending,
        "win_rate":     win_rate,
        "avg_pnl":      avg_pnl,
        "avg_win":      avg_win,
        "avg_loss":     avg_loss,
        "candle_stats": candle_stats,
    }

# ==================================================
# Discord 리포트
# ==================================================
def format_report(stats: dict, total_records: int) -> str:
    if not stats:
        return "📊 백테스트 데이터 부족 (결과 없음)"

    candle_lines = "\n".join(
        f"  {k}: {v}" for k, v in stats["candle_stats"].items()
    )

    return (
        f"📊 백테스트 리포트\n\n"
        f"🗂 분석 종목: {total_records}개\n"
        f"✅ 승리:     {stats['wins']}건\n"
        f"❌ 손절:     {stats['losses']}건\n"
        f"⏳ 미달성:   {stats['pending']}건\n\n"
        f"🎯 승률:     {stats['win_rate']}%\n"
        f"💰 평균 수익: {stats['avg_pnl']:+.2f}%\n"
        f"📈 평균 익절: {stats['avg_win']:+.2f}%\n"
        f"📉 평균 손절: {stats['avg_loss']:+.2f}%\n\n"
        f"🕯️ 캔들 패턴별 승률\n{candle_lines}\n"
    )

# ==================================================
# 메인
# ==================================================
def main() -> None:
    print("=" * 50)
    print("📊 백테스트 시작")
    print("=" * 50)

    records = load_all_scans()
    if not records:
        print("  ⚠️ scan_*.json 파일 없음 — 스캐너 먼저 실행하세요")
        return

    print(f"\n🔍 실제 결과 조회 중... ({len(records)}개)")
    results = []
    for i, record in enumerate(records, 1):
        result = check_result(record)
        results.append(result)
        print(f"  [{i}/{len(records)}] {record['name']} → {result['result']} ({result['pnl_pct']:+.1f}%)")

    # 결과 저장
    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n  💾 backtest_result.json 저장 완료")

    stats  = calc_stats(results)
    report = format_report(stats, len(records))
    print("\n" + report)
    send_discord_message(report)

if __name__ == "__main__":
    main()