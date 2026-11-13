#!/usr/bin/env python3
"""
More Fit 健身房人數定時採集腳本（七張店、古亭店）
採集區間：
  - 平日（週一至週五）：17:30 – 21:00（支援 17:20 起預熱待命）
  - 週末（週六至週日）：08:00 – 21:00（支援 07:50 起預熱待命）
採集策略：
  - 單次長連線常駐採集（最長涵蓋整個平日晚間 3.5 小時，週末每 ~4.2 小時無縫接力），
    徹底避免 GitHub Actions 排程漏觸發導致中途斷線的問題。
  - 每 3 分鐘對齊時鐘抓取一筆即時人數，每集滿 5 筆（15 分鐘）自動執行 git commit & push 同步上雲端。
僅使用 Python 標準函式庫，免安裝任何第三方套件。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# 台灣標準時間 (UTC+8，無日光節約時間)
TZ_TAIPEI = timezone(timedelta(hours=8))

API_URL = "https://ec.bookfastpos.com/api/brand/capacity/XeZ5lZZEp1"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://ec.bookfastpos.com/XeZ5lZZEp1/?type=plan",
    "Accept": "application/json, text/plain, */*",
}

TARGET_STORES = {"七張店", "古亭店"}
TARGET_IDS_LOWER = {"by9rbngren", "bknnj81ngw"}

WEEKDAY_NAMES = {
    1: "週一",
    2: "週二",
    3: "週三",
    4: "週四",
    5: "週五",
    6: "週六",
    7: "週日",
}

SAMPLE_INTERVAL_MINUTES = 3
SYNC_EVERY_N_SAMPLES = 5  # 每 5 個刻度（15 分鐘）自動 git push 一次

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CSV_PATH = os.path.join(DATA_DIR, "capacity_history.csv")
JS_DATA_PATH = os.path.join(DATA_DIR, "capacity_data.js")

CSV_FIELDNAMES = [
    "timestamp",
    "date",
    "weekday",
    "weekday_name",
    "time_slot",
    "store_id",
    "store_name",
    "current_total",
    "capacity_limit",
    "usage_rate_pct",
]


def get_nearest_3m_slot(dt: datetime) -> str:
    """將時間對齊至最近的 3 分鐘刻度（例如 18:01 -> 18:00, 18:02 -> 18:03）。"""
    exact_minutes = dt.hour * 60 + dt.minute + (dt.second / 60.0)
    rounded_minutes = int(round(exact_minutes / SAMPLE_INTERVAL_MINUTES) * SAMPLE_INTERVAL_MINUTES)
    slot_hour = (rounded_minutes // 60) % 24
    slot_minute = rounded_minutes % 60
    return f"{slot_hour:02d}:{slot_minute:02d}"


def get_window_start_dt(dt: datetime) -> datetime:
    """回傳當日正式開始採集的時間（平日 17:30:00 / 週末 08:00:00）。"""
    if dt.isoweekday() <= 5:
        return dt.replace(hour=17, minute=30, second=0, microsecond=0)
    return dt.replace(hour=8, minute=0, second=0, microsecond=0)


def is_within_or_prewarm_window(dt: datetime) -> bool:
    """
    檢查是否落在有效採集區間（含提早 10 分鐘預熱緩衝）：
      - 平日 (1~5)：17:20 ~ 21:02（正式採集 17:30 – 21:00）
      - 週末 (6~7)：07:50 ~ 21:02（正式採集 08:00 – 21:00）
    """
    minutes_of_day = dt.hour * 60 + dt.minute
    if dt.isoweekday() <= 5:
        return (17 * 60 + 20) <= minutes_of_day <= (21 * 60 + 2)
    return (7 * 60 + 50) <= minutes_of_day <= (21 * 60 + 2)


def is_within_active_sampling_window(dt: datetime) -> bool:
    """檢查是否落在正式採集時段（平日 17:29~21:01 / 週末 07:59~21:01）。"""
    minutes_of_day = dt.hour * 60 + dt.minute
    if dt.isoweekday() <= 5:
        return (17 * 60 + 29) <= minutes_of_day <= (21 * 60 + 1)
    return (7 * 60 + 59) <= minutes_of_day <= (21 * 60 + 1)


def get_session_deadline(start_dt: datetime) -> datetime:
    """
    計算本次常駐階段的結束時間：
      - 平日晚間（17:30–21:00 共 3.5 小時）：直接一路常駐執行到 21:01:00 收工！
      - 週末全天（08:00–21:00 共 13 小時）：因 GitHub 單次 Job 上限為 6 小時，
        拆為三段無縫接力（早班至 12:06:30、午班至 16:36:30、晚班至 21:01:00），
        精準對齊 :07 / :37 備援排程，確保 0 漏抓。
    """
    end_of_day = start_dt.replace(hour=21, minute=1, second=0, microsecond=0)
    if start_dt.isoweekday() <= 5:
        return end_of_day

    mins = start_dt.hour * 60 + start_dt.minute
    if mins < 11 * 60 + 50:
        return start_dt.replace(hour=12, minute=6, second=30, microsecond=0)
    if mins < 16 * 60 + 20:
        return start_dt.replace(hour=16, minute=36, second=30, microsecond=0)
    return end_of_day


def get_next_3m_tick(dt: datetime) -> datetime:
    """計算下一個有效的 3 分鐘整點時間（若距離 <45 秒則推進至下一格，避免重複抓取）。"""
    next_min_block = ((dt.minute // SAMPLE_INTERVAL_MINUTES) + 1) * SAMPLE_INTERVAL_MINUTES
    base_hour = dt.replace(minute=0, second=0, microsecond=0)
    candidate = base_hour + timedelta(minutes=next_min_block)
    if (candidate - dt).total_seconds() < 45:
        candidate += timedelta(minutes=SAMPLE_INTERVAL_MINUTES)
    return candidate


def load_existing_keys() -> set[tuple[str, str, str]]:
    """讀取目前已存在的 (date, time_slot, store_name) 避免同刻度重複寫入。"""
    if not os.path.exists(CSV_PATH):
        return set()
    keys: set[tuple[str, str, str]] = set()
    with open(CSV_PATH, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys.add((
                row.get("date", ""),
                row.get("time_slot", ""),
                row.get("store_name", ""),
            ))
    return keys


def sync_js_data_bundle() -> None:
    """同步輸出 data/capacity_data.js，讓本機直接雙擊開啟 index.html 也能正常讀取資料。"""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, mode="r", encoding="utf-8-sig") as f:
        csv_text = f.read()
    js_content = (
        "// 自動由 fetch.py 產生，供本機直接開啟 index.html (file://) 時讀取\n"
        f"window.MOREFIT_CSV = {json.dumps(csv_text, ensure_ascii=False)};\n"
    )
    with open(JS_DATA_PATH, mode="w", encoding="utf-8") as f:
        f.write(js_content)


def git_sync_data() -> None:
    """在 GitHub Actions 常駐執行期間，每 15 分鐘將最新 CSV 與 JS 同步提交並推送至儲存庫。"""
    sync_js_data_bundle()
    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            cwd=BASE_DIR,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=BASE_DIR,
            check=True,
        )
        subprocess.run(
            ["git", "add", "data/capacity_history.csv", "data/capacity_data.js"],
            cwd=BASE_DIR,
            check=True,
        )
        diff_proc = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=BASE_DIR,
        )
        if diff_proc.returncode == 0:
            return

        now_tag = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
        subprocess.run(
            ["git", "commit", "-m", f"chore(data): sync capacity batch ({now_tag})"],
            cwd=BASE_DIR,
            check=True,
        )
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        subprocess.run(
            ["git", "pull", "--rebase", "-X", "ours", "origin", branch],
            cwd=BASE_DIR,
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            cwd=BASE_DIR,
            check=True,
        )
        print(f"[{now_tag}] 已成功將本批數據同步推送至 GitHub！", flush=True)
    except Exception as exc:
        err_t = datetime.now(TZ_TAIPEI).strftime("%H:%M:%S")
        print(f"[{err_t}] Git 同步警告（資料仍保留於本機，將於下批重試推送）：{exc}", flush=True)


def fetch_stores_with_retry(retries: int = 2) -> list[dict]:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, headers=HEADERS, method="GET")
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(3)
    assert last_err is not None
    raise last_err


def capture_once(existing_keys: set[tuple[str, str, str]]) -> int:
    """執行單次 API 抓取並寫入 CSV，回傳新增筆數。"""
    now = datetime.now(TZ_TAIPEI)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")
    weekday = now.isoweekday()
    weekday_name = WEEKDAY_NAMES[weekday]
    time_slot = get_nearest_3m_slot(now)

    stores = fetch_stores_with_retry()
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.exists(CSV_PATH) and os.path.getsize(CSV_PATH) > 0

    new_rows = []
    for s in stores:
        store_name = str(s.get("storeName", "")).strip()
        store_id = str(s.get("storeId", "")).strip()

        if store_name not in TARGET_STORES and store_id.lower() not in TARGET_IDS_LOWER:
            continue

        dedup_key = (date_str, time_slot, store_name)
        if dedup_key in existing_keys:
            print(f"[{now_str}] {store_name} 在 {date_str} {time_slot} 已有紀錄，略過重複寫入。", flush=True)
            continue

        total = int(s.get("total", 0))
        limit = max(int(s.get("capacityLimit", 1)), 1)
        rate = round((total / limit) * 100, 1)

        new_rows.append({
            "timestamp": now_str,
            "date": date_str,
            "weekday": weekday,
            "weekday_name": weekday_name,
            "time_slot": time_slot,
            "store_id": store_id,
            "store_name": store_name,
            "current_total": total,
            "capacity_limit": limit,
            "usage_rate_pct": rate,
        })
        existing_keys.add(dedup_key)

    if not new_rows:
        return 0

    with open(CSV_PATH, mode="a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    summary = ", ".join(
        f"{r['store_name']} {r['current_total']}/{r['capacity_limit']}人 ({r['usage_rate_pct']}%)"
        for r in new_rows
    )
    print(f"[{now_str}] 記錄成功 ({weekday_name} {time_slot})：{summary}", flush=True)
    return len(new_rows)


def run(force: bool = False, once: bool = False, git_sync: bool = False) -> int:
    now = datetime.now(TZ_TAIPEI)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if not force and not is_within_or_prewarm_window(now):
        print(
            f"[{now_str}] 非目標採集時段（平日 17:30–21:00 / 週末 08:00–21:00），略過執行。"
            "若需手動測試寫入請加上 --force。"
        )
        return 0

    existing_keys = load_existing_keys()

    # 若為手動強制測試 (--force) 或指定單次 (--once)，抓完一筆立即結束存檔
    if force or once:
        capture_once(existing_keys)
        if git_sync:
            git_sync_data()
        else:
            sync_js_data_bundle()
        return 0

    # 若由預熱排程提早喚醒（如 17:22 或 07:52），先待機至正式開始時間 (17:30:00 / 08:00:00)
    win_start = get_window_start_dt(now)
    if now < win_start:
        wait_secs = (win_start - now).total_seconds()
        print(
            f"[{now_str}] 預熱喚醒成功！等待 {int(wait_secs)} 秒至 {win_start.strftime('%H:%M:%S')} 準時開抓...",
            flush=True,
        )
        time.sleep(max(0.0, wait_secs))
        now = datetime.now(TZ_TAIPEI)

    deadline = get_session_deadline(now)
    print(
        f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 啟動常駐採集階段（每 {SAMPLE_INTERVAL_MINUTES} 分鐘抓取一次，"
        f"每 15 分鐘自動同步雲端，持續運行至 {deadline.strftime('%H:%M:%S')}）",
        flush=True,
    )

    total_written = 0
    ticks_since_sync = 0
    synced_first = False
    errors = 0

    while True:
        cur_dt = datetime.now(TZ_TAIPEI)
        if is_within_active_sampling_window(cur_dt):
            try:
                added = capture_once(existing_keys)
                total_written += added
                ticks_since_sync += 1
            except Exception as exc:
                errors += 1
                err_time = datetime.now(TZ_TAIPEI).strftime("%H:%M:%S")
                print(f"[{err_time}] 單次抓取發生錯誤（將於下個刻度重試）：{exc}", flush=True)

            # 首筆寫入立即同步上雲端，之後每集滿 5 個刻度（15 分鐘）或遇到 :12/:27/:42/:57 刻度自動同步推送一次
            cur_slot_min = int(get_nearest_3m_slot(datetime.now(TZ_TAIPEI)).split(":")[1])
            should_sync = (
                (not synced_first and total_written > 0)
                or ticks_since_sync >= SYNC_EVERY_N_SAMPLES
                or cur_slot_min in (12, 27, 42, 57)
            )
            if git_sync and should_sync:
                git_sync_data()
                ticks_since_sync = 0
                if total_written > 0:
                    synced_first = True

        after_dt = datetime.now(TZ_TAIPEI)
        next_tick = get_next_3m_tick(after_dt)

        if next_tick > deadline or not is_within_active_sampling_window(next_tick):
            break

        sleep_secs = max(1.0, (next_tick - after_dt).total_seconds())
        time.sleep(sleep_secs)

    if git_sync:
        git_sync_data()
    else:
        sync_js_data_bundle()

    end_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{end_str}] 本階段採集完成，共新增 {total_written} 筆資料。", flush=True)

    if total_written == 0 and errors > 0:
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="More Fit 七張/古亭 即時人數採集腳本")
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略時段限制，立即強制抓取單筆紀錄（供測試用）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="僅抓取當下單筆紀錄即結束",
    )
    parser.add_argument(
        "--git-sync",
        action="store_true",
        help="每 15 分鐘自動執行 git commit & push 同步至遠端儲存庫（GitHub Actions 專用）",
    )
    args = parser.parse_args()
    sys.exit(run(force=args.force, once=args.once, git_sync=args.git_sync))
