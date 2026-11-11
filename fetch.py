#!/usr/bin/env python3
"""
More Fit 健身房人數定時採集腳本（七張店、古亭店）
採集區間：
  - 平日（週一至週五）：17:30 – 21:00
  - 週末（週六至週日）：08:00 – 21:00
採集策略：每 3 分鐘抓取一次即時人數，每 15 分鐘批次合併存檔一次。
僅使用 Python 標準函式庫，免安裝任何第三方套件。

每次執行會更新：
  1. data/capacity_history.csv（歷史累積原始資料）
  2. data/capacity_data.js（供本機直接開啟 index.html 離線讀取用）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
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

# 鎖定目標分店（以分店名稱與不分大小寫 storeId 雙重比對）
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
BATCH_WINDOW_MINUTES = 15

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


def is_within_target_window(dt: datetime) -> bool:
    """
    檢查是否落在目標採集時段（含前後 2 分鐘排程緩衝）：
      - 週一至週五 (1~5)：17:28 ~ 21:02（對應 17:30 – 21:00）
      - 週六至週日 (6~7)：07:58 ~ 21:02（對應 08:00 – 21:00）
    """
    minutes_of_day = dt.hour * 60 + dt.minute
    if dt.isoweekday() <= 5:
        return (17 * 60 + 28) <= minutes_of_day <= (21 * 60 + 2)
    return (8 * 60 - 2) <= minutes_of_day <= (21 * 60 + 2)


def get_batch_deadline(start_dt: datetime) -> datetime:
    """
    計算本次 15 分鐘批次的結束截止時間：
    若排程提早數秒觸發（例如 17:29:55），先往後推 30 秒對齊目標 15 分鐘區間；
    在下一個 15 分鐘整點（:00/:15/:30/:45）前 75 秒結束，預留充裕時間給 git push，
    且絕不超過當日 21:01:30。
    """
    ref_dt = start_dt + timedelta(seconds=30) if (start_dt.minute % BATCH_WINDOW_MINUTES == BATCH_WINDOW_MINUTES - 1 and start_dt.second >= 30) else start_dt
    floored_minute = (ref_dt.minute // BATCH_WINDOW_MINUTES) * BATCH_WINDOW_MINUTES
    block_start = ref_dt.replace(minute=floored_minute, second=0, microsecond=0)
    next_block = block_start + timedelta(minutes=BATCH_WINDOW_MINUTES)
    block_cutoff = next_block - timedelta(seconds=75)

    end_of_evening = start_dt.replace(hour=21, minute=1, second=30, microsecond=0)
    return min(block_cutoff, end_of_evening)


def get_next_3m_tick(dt: datetime) -> datetime:
    """計算下一個有效的 3 分鐘整點時間（若距離過近 <45 秒則推進至下一格，避免同刻度重複喚醒）。"""
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


def run(force: bool = False, once: bool = False) -> int:
    now = datetime.now(TZ_TAIPEI)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if not force and not is_within_target_window(now):
        print(
            f"[{now_str}] 非目標採集時段（平日 17:30–21:00 / 週末 08:00–21:00），略過執行。"
            "若需手動測試寫入請加上 --force。"
        )
        return 0

    existing_keys = load_existing_keys()

    # 若為手動強制測試 (--force) 或指定單次 (--once)，抓完一筆立即結束存檔
    if force or once:
        capture_once(existing_keys)
        sync_js_data_bundle()
        return 0

    # 15 分鐘批次模式：每 3 分鐘對齊時鐘抓取一次，直到本 15 分鐘區間結束（或抵達 21:00）
    deadline = get_batch_deadline(now)
    print(
        f"[{now_str}] 啟動 15 分鐘批次採樣（每 {SAMPLE_INTERVAL_MINUTES} 分鐘抓取一次，"
        f"預計至 {deadline.strftime('%H:%M:%S')} 結束並統一存檔）",
        flush=True,
    )

    total_written = 0
    errors = 0

    while True:
        try:
            total_written += capture_once(existing_keys)
        except Exception as exc:
            errors += 1
            err_time = datetime.now(TZ_TAIPEI).strftime("%H:%M:%S")
            print(f"[{err_time}] 單次抓取發生錯誤（將於下個刻度重試）：{exc}", flush=True)

        after_dt = datetime.now(TZ_TAIPEI)
        next_tick = get_next_3m_tick(after_dt)

        if next_tick > deadline or not is_within_target_window(next_tick):
            break

        sleep_secs = max(1.0, (next_tick - after_dt).total_seconds())
        time.sleep(sleep_secs)

    sync_js_data_bundle()
    end_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{end_str}] 本批次完成，共新增 {total_written} 筆資料。", flush=True)

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
        help="僅抓取當下單筆紀錄即結束（不進入 15 分鐘循環）",
    )
    args = parser.parse_args()
    sys.exit(run(force=args.force, once=args.once))
