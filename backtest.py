"""
===================================================
  백테스트 스크립트 v2.1
  변경사항:
    - 점수 x 캔들 교차분석 추가
    - 당일 상승률 구간별 승률 추가
===================================================
"""

import os
import json
import glob
import requests
import logging
import pandas as pd
import FinanceDataReader as fdr

from datetime import datetime, timedelta
from collections import defaultdict

WEBHOOK_STOCK_WEEKLY = os.getenv("WEBHOOK_STOCK_WEEKLY", "")

logging.basicConfig(
    filename=f"backtest_{datetime.now().strftime('%Y%m%d')}.log",
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# ==================================================
# Discord 전송
# ==================================================
def send_discord_message(message: str) -> None:
    if not WEBHOOK_STOCK_WEEKLY:
        print(message)
        return
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        try:
            requests.post(WEBHOOK_STOCK_WEEKLY, json={"content": chunk}, timeout=10)
        except Exception as e:
            logging.error(f"Discord 오류: {e}")

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

            date_str = f.replace("scan_", "").replace(".json", "")
            try:
                hour = int(date_str.split("_")[1][:2])
            except Exception:
                hour = 9

            for item in data:
                item["scan_file"] = date_str
                item["scan_hour"] = hour
            records.extend(data)

        except Exception as e:
            print(f"  ⚠️ {f} 로딩 실패: {e}")

    print(f"  총 {len(records)}개 레코드 ({len(files)}개 파일)")
    return records

# ==================================================
# 실제 결과 체크
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
            return {**record, "result": "데이터없음", "pnl_pct": 0, "days_to_result": None}

        result         = "미달성"
        pnl_pct        = 0
        days_to_result = None

        for day_idx, (_, row) in enumerate(data.iterrows(), start=1):
            high = float(row["High"])
            low  = float(row["Low"])

            if low <= stop_loss:
                result         = "손절"
                pnl_pct        = round((stop_loss - entry_price) / entry_price * 100, 2)
                days_to_result = day_idx
                break

            if high >= target_2:
                result         = "2차목표"
                pnl_pct        = round((target_2 - entry_price) / entry_price * 100, 2)
                days_to_result = day_idx
                break

            if high >= target_1:
                result         = "1차목표"
                pnl_pct        = round((target_1 - entry_price) / entry_price * 100, 2)
                days_to_result = day_idx
                break

        return {**record, "result": result, "pnl_pct": pnl_pct, "days_to_result": days_to_result}

    except Exception as e:
        logging.error(f"결과 체크 실패 [{code}]: {e}")
        return {**record, "result": "오류", "pnl_pct": 0, "days_to_result": None}

# ==================================================
# 승률 계산 헬퍼
# ==================================================
def calc_win_rate(group: list) -> dict:
    valid  = [r for r in group if r["result"] not in ["데이터없음", "오류", "미달성"]]
    total  = len(valid)
    if total == 0:
        return {"total": 0, "win2": 0, "win1": 0, "loss": 0, "win_rate": 0, "avg_pnl": 0}

    win2    = len([r for r in valid if r["result"] == "2차목표"])
    win1    = len([r for r in valid if r["result"] == "1차목표"])
    loss    = len([r for r in valid if r["result"] == "손절"])
    avg_pnl = round(sum(r["pnl_pct"] for r in valid) / total, 2)

    return {
        "total":    total,
        "win2":     win2,
        "win1":     win1,
        "loss":     loss,
        "win_rate": round((win2 + win1) / total * 100, 1),
        "avg_pnl":  avg_pnl,
    }

def stat_line(s: dict) -> str:
    if s["total"] == 0:
        return "데이터 없음"
    return (
        f"승률 {s['win_rate']}% "
        f"| 2차 {s['win2']}건 1차 {s['win1']}건 손절 {s['loss']}건 "
        f"| 평균 {s['avg_pnl']:+.1f}% ({s['total']}건)"
    )

# ==================================================
# 전체 통계
# ==================================================
def calc_all_stats(results: list) -> dict:
    valid   = [r for r in results if r["result"] not in ["데이터없음", "오류"]]
    pending = [r for r in valid if r["result"] == "미달성"]
    decided = [r for r in valid if r["result"] != "미달성"]

    overall = calc_win_rate(decided)

    # 점수 구간별
    score_groups = {
        "18점 이상 (강력추천)": [r for r in decided if r["score"] >= 18],
        "13~17점 (추천)":      [r for r in decided if 13 <= r["score"] < 18],
        "8~12점 (관망)":       [r for r in decided if 8 <= r["score"] < 13],
        "8점 미만 (비추천)":   [r for r in decided if r["score"] < 8],
    }
    score_stats = {k: calc_win_rate(v) for k, v in score_groups.items()}

    # 캔들 패턴별
    candle_groups = {
        "장대양봉":    [r for r in decided if r.get("candle") == "장대양봉"],
        "아랫꼬리양봉":[r for r in decided if r.get("candle") == "아랫꼬리양봉"],
        "윗꼬리음봉":  [r for r in decided if r.get("candle") == "윗꼬리음봉"],
        "보통":        [r for r in decided if r.get("candle") == "보통"],
    }
    candle_stats = {k: calc_win_rate(v) for k, v in candle_groups.items()}

    # 점수 x 캔들 교차분석
    cross_groups = {
        "강력추천+장대양봉":    [r for r in decided if r["score"] >= 18 and r.get("candle") == "장대양봉"],
        "강력추천+보통":        [r for r in decided if r["score"] >= 18 and r.get("candle") == "보통"],
        "추천+장대양봉":        [r for r in decided if 13 <= r["score"] < 18 and r.get("candle") == "장대양봉"],
        "추천+윗꼬리음봉":      [r for r in decided if 13 <= r["score"] < 18 and r.get("candle") == "윗꼬리음봉"],
        "고점수+윗꼬리음봉":    [r for r in decided if r["score"] >= 13 and r.get("candle") == "윗꼬리음봉"],
    }
    cross_stats = {k: calc_win_rate(v) for k, v in cross_groups.items()}

    # 당일 상승률 구간별
    change_groups = {
        "당일 5% 미만":      [r for r in decided if r.get("change", 0) < 5],
        "당일 5~10%":        [r for r in decided if 5 <= r.get("change", 0) < 10],
        "당일 10~15%":       [r for r in decided if 10 <= r.get("change", 0) < 15],
        "당일 15% 이상":     [r for r in decided if r.get("change", 0) >= 15],
    }
    change_stats = {k: calc_win_rate(v) for k, v in change_groups.items()}

    # 시간대별
    hour_groups = {
        "09시 (장 시작)": [r for r in decided if r.get("scan_hour") == 9],
        "11시":           [r for r in decided if r.get("scan_hour") == 11],
        "13시":           [r for r in decided if r.get("scan_hour") == 13],
        "15시 (장 마감)": [r for r in decided if r.get("scan_hour") == 15],
    }
    hour_stats = {k: calc_win_rate(v) for k, v in hour_groups.items()}

    # 테마별
    theme_map = defaultdict(list)
    for r in decided:
        themes = r.get("themes", [])
        if themes:
            for t in themes:
                theme_map[t].append(r)
        else:
            theme_map["테마없음"].append(r)
    theme_stats = {k: calc_win_rate(v) for k, v in theme_map.items()}

    # Top5 수익 / 손절
    top5 = sorted(
        [r for r in decided if r["result"] in ["1차목표", "2차목표"]],
        key=lambda x: x["pnl_pct"], reverse=True
    )[:5]
    bot5 = sorted(
        [r for r in decided if r["result"] == "손절"],
        key=lambda x: x["pnl_pct"]
    )[:5]

    return {
        "total":        len(valid),
        "pending":      len(pending),
        "decided":      len(decided),
        "overall":      overall,
        "score_stats":  score_stats,
        "candle_stats": candle_stats,
        "cross_stats":  cross_stats,
        "change_stats": change_stats,
        "hour_stats":   hour_stats,
        "theme_stats":  theme_stats,
        "top5":         top5,
        "bot5":         bot5,
    }

# ==================================================
# Discord 리포트
# ==================================================
def format_report(stats: dict, week_str: str) -> str:
    o = stats["overall"]

    msg = (
        f"📊 주간 백테스트 리포트\n"
        f"{week_str}\n\n"
        f"📡 총 신호:   {stats['total']}개\n"
        f"✅ 결과확정:  {stats['decided']}개\n"
        f"⏳ 미달성:    {stats['pending']}개\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 전체 적중률\n{stat_line(o)}\n\n"
    )

    # 점수 구간별
    msg += "━━━━━━━━━━━━━━━━━━━\n📊 점수 구간별 적중률\n"
    for label, s in stats["score_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    # 캔들 패턴별
    msg += "\n━━━━━━━━━━━━━━━━━━━\n🕯️ 캔들 패턴별 적중률\n"
    for label, s in stats["candle_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    # 점수 x 캔들 교차분석
    msg += "\n━━━━━━━━━━━━━━━━━━━\n🔀 점수×캔들 교차분석\n"
    for label, s in stats["cross_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    # 당일 상승률 구간별
    msg += "\n━━━━━━━━━━━━━━━━━━━\n📈 당일 상승률별 적중률\n"
    for label, s in stats["change_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    # 시간대별
    msg += "\n━━━━━━━━━━━━━━━━━━━\n⏰ 시간대별 적중률\n"
    for label, s in stats["hour_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    # 테마별
    msg += "\n━━━━━━━━━━━━━━━━━━━\n🏷️ 테마별 적중률\n"
    for label, s in sorted(stats["theme_stats"].items(), key=lambda x: -x[1]["win_rate"]):
        msg += f"  {label}: {stat_line(s)}\n"

    # Top5 수익
    if stats["top5"]:
        msg += "\n━━━━━━━━━━━━━━━━━━━\n🏆 수익 Top5\n"
        for r in stats["top5"]:
            msg += f"  {r['name']} | {r['pnl_pct']:+.1f}% | {r['result']} | 점수:{r['score']} | 캔들:{r.get('candle','?')} | 당일:{r.get('change',0):.1f}%\n"

    # Bot5 손절
    if stats["bot5"]:
        msg += "\n━━━━━━━━━━━━━━━━━━━\n💀 손절 Top5\n"
        for r in stats["bot5"]:
            msg += f"  {r['name']} | {r['pnl_pct']:+.1f}% | 점수:{r['score']} | 캔들:{r.get('candle','?')} | 당일:{r.get('change',0):.1f}%\n"

    # 권장 조정
    msg += "\n━━━━━━━━━━━━━━━━━━━\n🔧 권장 조정\n"
    best_score  = max(stats["score_stats"].items(),  key=lambda x: x[1]["win_rate"] if x[1]["total"] > 0 else 0)
    best_candle = max(stats["candle_stats"].items(), key=lambda x: x[1]["win_rate"] if x[1]["total"] > 0 else 0)
    best_hour   = max(stats["hour_stats"].items(),   key=lambda x: x[1]["win_rate"] if x[1]["total"] > 0 else 0)
    best_change = max(stats["change_stats"].items(), key=lambda x: x[1]["win_rate"] if x[1]["total"] > 0 else 0)

    msg += (
        f"  최고 점수구간: {best_score[0]} ({best_score[1]['win_rate']}%)\n"
        f"  최고 캔들:    {best_candle[0]} ({best_candle[1]['win_rate']}%)\n"
        f"  최고 시간대:  {best_hour[0]} ({best_hour[1]['win_rate']}%)\n"
        f"  최고 상승률:  {best_change[0]} ({best_change[1]['win_rate']}%)\n"
    )

    return msg

# ==================================================
# 메인
# ==================================================
def main() -> None:
    print("=" * 50)
    print("📊 백테스트 v2.1 시작")
    print("=" * 50)

    records = load_all_scans()
    if not records:
        print("  ⚠️ scan_*.json 없음 — 스캐너 먼저 실행하세요")
        return

    now        = datetime.now()
    week_start = (now - timedelta(days=7)).strftime("%m/%d")
    week_end   = now.strftime("%m/%d")
    week_str   = f"{week_start} ~ {week_end}"

    print(f"\n🔍 결과 조회 중... ({len(records)}개)")
    results = []
    for i, record in enumerate(records, 1):
        result = check_result(record)
        results.append(result)
        print(
            f"  [{i:>3}/{len(records)}] "
            f"{record['name']:<12} "
            f"→ {result['result']:<8} "
            f"({result['pnl_pct']:+.1f}%)"
        )

    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n  💾 backtest_result.json 저장 완료")

    stats  = calc_all_stats(results)
    report = format_report(stats, week_str)

    print("\n" + report)
    send_discord_message(report)

if __name__ == "__main__":
    main()