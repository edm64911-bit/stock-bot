"""
===================================================
  백테스트 스크립트 v3.0
  변경사항 (v2.1 → v3.0):
    - 진입 구조: scan 당일종가 즉시 → WATCH→눌림→익일시가 진입
    - tracker v2.0과 동일 조건 재현
      (pullback 3~10%, MA20 위, RSI>=50, vol>=0.8*20d_avg)
    - 진입가 = 눌림 확인 익일 시가 (미래참조 제거)
    - STOP=2ATR / TP1=2ATR / TP2=3ATR
    - TP1 도달 → 50% 익절 + stop=entry (브레이크이븐)
    - TP2 도달 → 잔여 50% 청산
    - 브레이크이븐 후 손절 → 본절(0%)
    - 동일 종목 주간 중복 dedup (code+week 기준 최고 score)
    - 손절 우선 판정 (보수적 백테스트)
    - 분석 항목: 눌림발생률, 평균눌림깊이, days_to_entry 분포
===================================================
"""

import os
import json
import math
import glob
import requests
import logging
import pandas as pd
import FinanceDataReader as fdr

from datetime import datetime, timedelta
from collections import defaultdict

# ==================================================
# 상수 (tracker v2.0과 동일)
# ==================================================
WATCH_EXPIRE_DAYS = 15
PULLBACK_MIN      = 3.0
PULLBACK_MAX      = 10.0
VOL_RATIO_MIN     = 0.8
RSI_MIN           = 50.0
STOP_ATR_MULT     = 2.0
TP1_ATR_MULT      = 2.0
TP2_ATR_MULT      = 3.0
TRACK_DAYS        = 20   # 진입 후 최대 추적 거래일

WEBHOOK_STOCK_WEEKLY = os.getenv("WEBHOOK_STOCK_WEEKLY", "")

logging.basicConfig(
    filename=f"backtest_{datetime.now().strftime('%Y%m%d')}.log",
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# ==================================================
# Discord
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
# scan_*.json 로드 + 주간 dedup
# ==================================================
def load_all_scans() -> list:
    import re as _re
    files   = sorted(glob.glob("scan_*.json"))
    raw_all = []

    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                raw = fp.read()
            raw  = _re.sub(r':\s*NaN',       ': null', raw)
            raw  = _re.sub(r':\s*Infinity',  ': null', raw)
            raw  = _re.sub(r':\s*-Infinity', ': null', raw)
            data = json.loads(raw)

            date_str = f.replace("scan_", "").replace(".json", "")
            try:
                hour = int(date_str.split("_")[1][:2])
            except Exception:
                hour = 9

            for item in data:
                item["scan_file"] = date_str
                item["scan_hour"] = hour
            raw_all.extend(data)
        except Exception as e:
            print(f"  ⚠️ {f} 로딩 실패: {e}")

    # 주간 dedup: (code, week) 기준 최고 score만 유지
    dedup: dict = {}
    for item in raw_all:
        try:
            scan_dt = datetime.strptime(item["scanned_at"], "%Y-%m-%d %H:%M:%S")
            week    = scan_dt.strftime("%Y-W%W")
        except Exception:
            week = "unknown"
        key = (item["code"], week)
        if key not in dedup or item["score"] > dedup[key]["score"]:
            dedup[key] = item

    records = list(dedup.values())
    print(f"  총 {len(raw_all)}개 레코드 → dedup 후 {len(records)}개 ({len(files)}개 파일)")
    return records

# ==================================================
# 지표 계산
# ==================================================
def calculate_rsi(close: pd.Series, period: int = 14) -> float:
    delta    = close.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    val      = float(rsi.iloc[-1])
    return val if not math.isnan(val) else 50.0

def calculate_atr(data: pd.DataFrame, period: int = 14) -> float:
    high_low   = data["High"] - data["Low"]
    high_close = (data["High"] - data["Close"].shift()).abs()
    low_close  = (data["Low"]  - data["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    val        = float(true_range.rolling(period).mean().iloc[-1])
    return val if not math.isnan(val) else 0.0

# ==================================================
# simulate_watch: WATCH → 눌림 확인 → 진입
# ==================================================
def simulate_watch(record: dict, hist: pd.DataFrame) -> dict | None:
    """
    record: scan_*.json 단일 레코드
    hist:   scan_date 이후 충분한 과거 OHLCV (캘린더 기준 60일치)
    반환:   진입 정보 dict or None (눌림 미발생)
    """
    watch_price = record["entry_price"]   # scan 당일 종가 = 감시기준가
    code        = record["code"]

    try:
        scan_dt  = datetime.strptime(record["scanned_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

    # scan_date 이후 데이터만 사용
    hist_after = hist[hist.index > pd.Timestamp(scan_dt.date())].copy()
    if len(hist_after) < 3:
        return None

    # 최대 WATCH_EXPIRE_DAYS 거래일 재생
    for i in range(min(WATCH_EXPIRE_DAYS, len(hist_after) - 1)):
        day_data = hist_after.iloc[:i+1]   # i+1일치 누적
        cur_row  = hist_after.iloc[i]
        cur_close = float(cur_row["Close"])

        if len(day_data) < 5:
            continue

        # MA20 계산 (scan 이전 데이터 포함 필요 → hist 전체 기준)
        hist_up_to = hist[hist.index <= hist_after.index[i]]
        if len(hist_up_to) < 20:
            continue

        ma20      = float(hist_up_to["Close"].tail(20).mean())
        avg20_vol = float(hist_up_to["Volume"].tail(20).mean())
        cur_vol   = float(cur_row["Volume"])
        rsi       = calculate_rsi(hist_up_to["Close"])

        pullback_pct = (watch_price - cur_close) / watch_price * 100

        cond_pullback = PULLBACK_MIN <= pullback_pct <= PULLBACK_MAX
        cond_ma20     = cur_close > ma20
        cond_rsi      = rsi >= RSI_MIN
        cond_vol      = avg20_vol > 0 and (cur_vol / avg20_vol) >= VOL_RATIO_MIN

        if not (cond_pullback and cond_ma20 and cond_rsi and cond_vol):
            continue

        # 눌림 확인 → 익일 시가 진입
        next_idx = i + 1
        if next_idx >= len(hist_after):
            return None   # 다음 거래일 데이터 없음

        next_row    = hist_after.iloc[next_idx]
        entry_price = float(next_row["Open"])
        entry_date  = hist_after.index[next_idx]

        # 진입일 ATR 계산
        hist_entry = hist[hist.index <= entry_date]
        atr        = calculate_atr(hist_entry)
        if math.isnan(atr) or atr <= 0:
            atr = entry_price * 0.03
            logging.warning(f"[{code}] backtest ATR fallback: {entry_price:,.0f} * 3%")

        return {
            "entry_price":   entry_price,
            "entry_date":    str(entry_date.date()),
            "days_to_entry": next_idx,   # 눌림 확인일(i+1일차) 기준 자연수 표현
            "pullback_pct":  round(pullback_pct, 2),
            "entry_atr":     round(atr, 2),
            "entry_rsi":     round(rsi, 2),
            "ma20_at_entry": round(ma20, 2),
        }

    return None   # 15거래일 내 눌림 미발생

# ==================================================
# simulate_position: 진입 후 STOP/TP 추적
# TP1: 50% 익절 + stop=entry (브레이크이븐)
# TP2: 잔여 50% 청산
# 브레이크이븐 후 손절: 본절(0%)
# 손절 우선 판정 (보수적)
# ==================================================
def simulate_position(entry_info: dict, hist: pd.DataFrame) -> dict:
    entry_price = entry_info["entry_price"]
    entry_date  = entry_info["entry_date"]
    atr         = entry_info["entry_atr"]

    stop_loss   = entry_price - STOP_ATR_MULT * atr
    tp1         = entry_price + TP1_ATR_MULT  * atr
    tp2         = entry_price + TP2_ATR_MULT  * atr

    hist_after  = hist[hist.index > pd.Timestamp(entry_date)].copy()

    tp1_hit        = False
    breakeven_stop = stop_loss   # 브레이크이븐 전까지는 원래 stop
    result         = "미달성"
    pnl_pct        = 0.0
    days_to_result = None

    for day_idx, (_, row) in enumerate(hist_after.head(TRACK_DAYS).iterrows(), start=1):
        high = float(row["High"])
        low  = float(row["Low"])

        if not tp1_hit:
            # TP1 미도달 구간: 손절 우선
            if low <= breakeven_stop:
                result         = "손절"
                pnl_pct        = round((breakeven_stop - entry_price) / entry_price * 100, 2)
                days_to_result = day_idx
                break

            if high >= tp2:
                # TP1, TP2 같은 날 동시 도달 → tp1_hit 플래그 포함
                tp1_hit        = True
                result         = "2차목표"
                pnl_pct        = round(
                    0.5 * (tp1 - entry_price) / entry_price * 100 +
                    0.5 * (tp2 - entry_price) / entry_price * 100,
                    2
                )
                days_to_result = day_idx
                break

            if high >= tp1:
                tp1_hit        = True
                breakeven_stop = entry_price  # 브레이크이븐 적용

        else:
            # TP1 도달 후: 브레이크이븐 stop 적용
            if low <= breakeven_stop:
                # 본절 (잔여 50% 기준 0%)
                result         = "본절"
                pnl_pct        = round(
                    0.5 * (tp1 - entry_price) / entry_price * 100 +
                    0.5 * 0.0,
                    2
                )
                days_to_result = day_idx
                break

            if high >= tp2:
                result         = "2차목표"
                pnl_pct        = round(
                    0.5 * (tp1 - entry_price) / entry_price * 100 +
                    0.5 * (tp2 - entry_price) / entry_price * 100,
                    2
                )
                days_to_result = day_idx
                break

    return {
        "result":         result,
        "pnl_pct":        pnl_pct,
        "days_to_result": days_to_result,
        "stop_loss":      round(stop_loss, 0),
        "target_price_1": round(tp1, 0),
        "target_price_2": round(tp2, 0),
        "tp1_hit":        tp1_hit,
    }

# ==================================================
# 단일 레코드 전체 시뮬레이션
# ==================================================
def run_simulation(record: dict) -> dict:
    code = record["code"]
    try:
        scan_dt    = datetime.strptime(record["scanned_at"], "%Y-%m-%d %H:%M:%S")
        hist_start = (scan_dt - timedelta(days=60)).strftime("%Y-%m-%d")
        hist_end   = (scan_dt + timedelta(days=60)).strftime("%Y-%m-%d")
        hist       = fdr.DataReader(code, hist_start, hist_end).dropna()

        if len(hist) < 20:
            return {**record, "result": "데이터없음", "pnl_pct": 0,
                    "entry_price": None, "entry_date": None,
                    "days_to_entry": None, "pullback_pct": None,
                    "days_to_result": None, "tp1_hit": False}

        entry_info = simulate_watch(record, hist)

        if entry_info is None:
            return {**record, "result": "눌림없음", "pnl_pct": 0,
                    "entry_price": None, "entry_date": None,
                    "days_to_entry": None, "pullback_pct": None,
                    "entry_rsi": None, "entry_atr": None,
                    "days_to_result": None, "tp1_hit": False}

        pos_result = simulate_position(entry_info, hist)

        return {
            **record,
            **entry_info,
            **pos_result,
        }

    except Exception as e:
        logging.error(f"시뮬레이션 실패 [{code}]: {e}")
        return {**record, "result": "오류", "pnl_pct": 0,
                "entry_price": None, "entry_date": None,
                "days_to_entry": None, "pullback_pct": None,
                "days_to_result": None, "tp1_hit": False}

# ==================================================
# 승률 계산
# ==================================================
def calc_win_rate(group: list) -> dict:
    valid  = [r for r in group if r["result"] not in ["데이터없음", "오류", "미달성", "눌림없음"]]
    total  = len(valid)
    if total == 0:
        return {"total": 0, "win2": 0, "win1_be": 0, "loss": 0, "win_rate": 0, "avg_pnl": 0}

    win2    = len([r for r in valid if r["result"] == "2차목표"])
    win1_be = len([r for r in valid if r["result"] == "본절"])
    loss    = len([r for r in valid if r["result"] == "손절"])
    avg_pnl = round(sum(r["pnl_pct"] for r in valid) / total, 2)

    return {
        "total":    total,
        "win2":     win2,
        "win1_be":  win1_be,
        "loss":     loss,
        "win_rate": round(win2 / total * 100, 1),   # 완전 익절만 승
        "avg_pnl":  avg_pnl,
    }

def stat_line(s: dict) -> str:
    if s["total"] == 0:
        return "데이터 없음"
    return (
        f"2차목표 {s['win2']}건 본절 {s['win1_be']}건 손절 {s['loss']}건 "
        f"| 완전익절률 {s['win_rate']}% | 평균 {s['avg_pnl']:+.1f}% ({s['total']}건)"
    )

# ==================================================
# 전체 통계
# ==================================================
def calc_all_stats(results: list) -> dict:
    decided  = [r for r in results if r["result"] not in ["데이터없음", "오류", "눌림없음", "미달성"]]
    no_entry = [r for r in results if r["result"] == "눌림없음"]
    overall  = calc_win_rate(decided)

    # 눌림 발생률 (진입 여부 기준 — 미달성 포함)
    total_valid = len([r for r in results if r["result"] != "데이터없음"])
    entered     = [r for r in results if r.get("entry_price") is not None]
    entry_rate  = round(len(entered) / total_valid * 100, 1) if total_valid > 0 else 0

    # 평균 눌림 깊이
    pullbacks    = [r["pullback_pct"] for r in entered if r.get("pullback_pct") is not None]
    avg_pullback = round(sum(pullbacks) / len(pullbacks), 2) if pullbacks else 0

    # 평균 진입 소요일 (진입 성공 전체 기준 — 미달성 포함)
    dte_list = [r["days_to_entry"] for r in entered if r.get("days_to_entry") is not None]
    avg_dte  = round(sum(dte_list) / len(dte_list), 1) if dte_list else 0

    # 점수 구간별
    score_groups = {
        "18점 이상": [r for r in decided if r["score"] >= 18],
        "13~17점":   [r for r in decided if 13 <= r["score"] < 18],
        "8~12점":    [r for r in decided if 8  <= r["score"] < 13],
        "8점 미만":  [r for r in decided if r["score"] < 8],
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

    # 당일 상승률 구간별
    change_groups = {
        "당일 5% 미만":  [r for r in decided if (r.get("change") or 0) < 5],
        "당일 5~10%":    [r for r in decided if 5  <= (r.get("change") or 0) < 10],
        "당일 10~15%":   [r for r in decided if 10 <= (r.get("change") or 0) < 15],
    }
    change_stats = {k: calc_win_rate(v) for k, v in change_groups.items()}

    # 눌림 깊이 구간별
    pullback_groups = {
        "3~5% 눌림":  [r for r in decided if 3  <= (r.get("pullback_pct") or 0) < 5],
        "5~7% 눌림":  [r for r in decided if 5  <= (r.get("pullback_pct") or 0) < 7],
        "7~10% 눌림": [r for r in decided if 7  <= (r.get("pullback_pct") or 0) <= 10],
    }
    pullback_stats = {k: calc_win_rate(v) for k, v in pullback_groups.items()}

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

    # Top5 / Bot5
    top5 = sorted(
        [r for r in decided if r["result"] == "2차목표"],
        key=lambda x: x["pnl_pct"], reverse=True
    )[:5]
    bot5 = sorted(
        [r for r in decided if r["result"] == "손절"],
        key=lambda x: x["pnl_pct"]
    )[:5]

    return {
        "total":          len(results),
        "no_entry":       len(no_entry),
        "decided":        len(decided),
        "entry_rate":     entry_rate,
        "avg_pullback":   avg_pullback,
        "avg_dte":        avg_dte,
        "overall":        overall,
        "score_stats":    score_stats,
        "candle_stats":   candle_stats,
        "change_stats":   change_stats,
        "pullback_stats": pullback_stats,
        "theme_stats":    theme_stats,
        "top5":           top5,
        "bot5":           bot5,
    }

# ==================================================
# Discord 리포트
# ==================================================
def format_report(stats: dict, week_str: str) -> str:
    o = stats["overall"]

    msg = (
        f"📊 백테스트 리포트 v3.0\n"
        f"{week_str}\n\n"
        f"📡 총 신호:      {stats['total']}개\n"
        f"✅ 눌림 진입:    {stats['decided']}개 (발생률 {stats['entry_rate']}%)\n"
        f"⏭️ 눌림 미발생:  {stats['no_entry']}개\n"
        f"📉 평균 눌림:    {stats['avg_pullback']}%\n"
        f"📅 평균 진입일:  {stats['avg_dte']}거래일\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 전체 결과\n{stat_line(o)}\n\n"
    )

    msg += "━━━━━━━━━━━━━━━━━━━\n📊 점수 구간별\n"
    for label, s in stats["score_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━\n🕯️ 캔들 패턴별\n"
    for label, s in stats["candle_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━\n📈 당일 상승률별\n"
    for label, s in stats["change_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━\n📉 눌림 깊이별\n"
    for label, s in stats["pullback_stats"].items():
        msg += f"  {label}: {stat_line(s)}\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━\n🏷️ 테마별\n"
    for label, s in sorted(stats["theme_stats"].items(), key=lambda x: -x[1]["avg_pnl"]):
        msg += f"  {label}: {stat_line(s)}\n"

    if stats["top5"]:
        msg += "\n━━━━━━━━━━━━━━━━━━━\n🏆 수익 Top5\n"
        for r in stats["top5"]:
            msg += (
                f"  {r['name']} | {r['pnl_pct']:+.1f}% | "
                f"눌림:{r.get('pullback_pct','?')}% | "
                f"점수:{r['score']} | 캔들:{r.get('candle','?')}\n"
            )

    if stats["bot5"]:
        msg += "\n━━━━━━━━━━━━━━━━━━━\n💀 손절 Top5\n"
        for r in stats["bot5"]:
            msg += (
                f"  {r['name']} | {r['pnl_pct']:+.1f}% | "
                f"눌림:{r.get('pullback_pct','?')}% | "
                f"점수:{r['score']} | 캔들:{r.get('candle','?')}\n"
            )

    # 권장 조정
    def best_of(d: dict) -> tuple:
        return max(d.items(), key=lambda x: x[1]["avg_pnl"] if x[1]["total"] > 0 else -999)

    bs = best_of(stats["score_stats"])
    bc = best_of(stats["candle_stats"])
    bp = best_of(stats["pullback_stats"])

    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━\n🔧 권장 조정\n"
        f"  최고 점수구간: {bs[0]} (평균 {bs[1]['avg_pnl']:+.1f}%)\n"
        f"  최고 캔들:    {bc[0]} (평균 {bc[1]['avg_pnl']:+.1f}%)\n"
        f"  최고 눌림깊이: {bp[0]} (평균 {bp[1]['avg_pnl']:+.1f}%)\n"
    )

    return msg

# ==================================================
# 메인
# ==================================================
def main() -> None:
    print("=" * 50)
    print("📊 백테스트 v3.0 시작")
    print(f"   전략: WATCH→눌림({PULLBACK_MIN}~{PULLBACK_MAX}%)→익일시가진입")
    print(f"   STOP={STOP_ATR_MULT}ATR / TP1={TP1_ATR_MULT}ATR / TP2={TP2_ATR_MULT}ATR")
    print("=" * 50)

    records = load_all_scans()
    if not records:
        print("  ⚠️ scan_*.json 없음 — 스캐너 먼저 실행하세요")
        return

    now        = datetime.now()
    week_start = (now - timedelta(days=7)).strftime("%m/%d")
    week_end   = now.strftime("%m/%d")
    week_str   = f"{week_start} ~ {week_end}"

    print(f"\n🔍 시뮬레이션 중... ({len(records)}개)")
    results = []
    for i, record in enumerate(records, 1):
        result = run_simulation(record)
        results.append(result)
        entry_str = f"진입:{result.get('entry_price', '-')}" if result.get('entry_price') else "눌림없음"
        print(
            f"  [{i:>3}/{len(records)}] "
            f"{record['name']:<12} "
            f"→ {result['result']:<8} "
            f"({result['pnl_pct']:+.1f}%) "
            f"[{entry_str}]"
        )

    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print("\n  💾 backtest_result.json 저장 완료")

    stats  = calc_all_stats(results)
    report = format_report(stats, week_str)

    print("\n" + report)
    send_discord_message(report)

if __name__ == "__main__":
    main()