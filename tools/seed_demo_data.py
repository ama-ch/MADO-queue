"""開発・デモ用: 混雑傾向ダッシュボード（/dashboard）を確かめるためのダミー発券記録を入れる。

実データではない。本番の DB には絶対に使わないこと。

使い方:
    DB_PATH=data/numbers.db python init_db.py
    python tools/seed_demo_data.py data/numbers.db

期間は「前年度の4月1日〜昨日」の平日。祝日は 2025〜2026年のおおよその分だけ休みにしている。
仕込んだ傾向: 水曜が混む・水曜午後はさらに混む・木曜はすいている・3月下旬と4月上旬が繁忙期。
"""
import math
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
_today = date.today()
START = date(_today.year - 1 if _today.month >= 4 else _today.year - 2, 4, 1)
END = _today - timedelta(days=1)   # 今日は入れない（処理画面の待ち一覧に出てしまうため）

HOLIDAYS = {date.fromisoformat(s) for s in (
    '2025-04-29 2025-05-05 2025-05-06 2025-07-21 2025-08-11 2025-09-15 2025-09-23 '
    '2025-10-13 2025-11-03 2025-11-24 2025-12-29 2025-12-30 2025-12-31 2026-01-01 '
    '2026-01-02 2026-01-12 2026-02-11 2026-02-23 2026-03-20 2026-04-29 2026-05-04 '
    '2026-05-05 2026-05-06 2026-07-20 2026-08-11 2026-09-21 2026-09-22 2026-09-23 '
    '2026-10-12 2026-11-03').split()}

HOUR_BASE = {8: 3, 9: 12, 10: 14, 11: 12, 12: 6, 13: 12, 14: 11, 15: 9, 16: 6}
WEEKDAY = {0: 1.15, 1: 0.95, 2: 1.2, 3: 0.8, 4: 1.05}
CATS = [('A', 0.6), ('B', 0.25), ('C', 0.1), ('D', 0.05)]
CAT_START = {'A': 1, 'B': 500, 'C': 800, 'D': 0}
BUTTONS = {
    'A': ['住民票', '戸籍謄本/抄本', '印鑑証明', 'らくらく証明', '現況届'],
    'B': ['転出', '転入', 'マイカ受取'],
    'C': ['戸籍届出', '年金', 'おくやみ'],
    'D': ['窓口以外の来庁者'],
}


def season(d):
    if d.month == 3:
        return 1.8 if d.day >= 20 else 1.4
    if d.month == 4:
        return 1.6 if d.day <= 10 else 1.2
    return {5: 1.1, 8: 0.9, 12: 0.95}.get(d.month, 1.0)


def poisson(rng, lam):
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    k, p, target = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= target:
            return k
        k += 1


def main():
    db_path = sys.argv[1]
    force = '--force' in sys.argv
    conn = sqlite3.connect(db_path)
    if conn.execute('SELECT COUNT(*) FROM event_logs').fetchone()[0] and not force:
        sys.exit('event_logs に既に記録があります。上書きせず中止しました（--force で追記）。')

    rng = random.Random(20260314)
    rows = []
    d, prev_closed = START, False
    while d <= END:
        if d.weekday() >= 5 or d in HOLIDAYS:
            prev_closed = d.weekday() < 5 or prev_closed
            d += timedelta(days=1)
            continue
        after_holiday = 1.2 if prev_closed and d.weekday() != 0 else 1.0
        prev_closed = False
        numbers = dict(CAT_START)
        first = {c: True for c in CAT_START}
        staff = rng.choice([3, 4, 4, 5, 5, 6])
        events = []
        for hour, base in HOUR_BASE.items():
            pm = 1.3 if d.weekday() == 2 and hour >= 13 else 1.0
            lam = base * WEEKDAY[d.weekday()] * pm * season(d) * after_holiday
            for _ in range(poisson(rng, lam)):
                minute = rng.randint(30, 59) if hour == 8 else rng.randint(0, 59)
                events.append(datetime(d.year, d.month, d.day, hour, minute, rng.randint(0, 59), tzinfo=JST))
        for ts in sorted(events):
            cat = rng.choices([c for c, _ in CATS], [w for _, w in CATS])[0]
            if first[cat]:
                first[cat] = False
            else:
                numbers[cat] += 1
            rows.append((cat, rng.choice(BUTTONS[cat]), ts.isoformat(), numbers[cat], staff))
        d += timedelta(days=1)

    conn.executemany(
        'INSERT INTO event_logs (category, button_text, timestamp, current_number, staff_count)'
        ' VALUES (?, ?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()
    print(f'{len(rows)} 件のダミー発券記録を入れました: {db_path}')


if __name__ == '__main__':
    main()
