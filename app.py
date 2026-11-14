"""
MADO-Queue — 窓口番号発券・呼び出し管理システム

画面構成:
  /           発券画面   (タブレット設置。来庁者が番号を取る)
  /processing 処理画面   (職員用。呼び出し・対応開始・完了を操作)
  /display    案内表示   (ロビーのモニター用。呼び出し番号を大画面表示)
  /dashboard  混雑傾向   (職員用。期間を指定して曜日・時間帯・日ごとの来庁者数を見る)

カテゴリ番号帯: A=001-499, B=500-799, C=800-999 (config.py 参照)
"""

from contextlib import contextmanager
import math
from datetime import date, datetime, timezone, timedelta
import os
import sqlite3

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

from config import CATEGORY_MAX, CATEGORY_START, configure_sqlite_connection
from printer import print_ticket

app = Flask(__name__)
_cors_origins = os.environ.get('CORS_ORIGINS', 'http://localhost:8000').split(',')
CORS(app, origins=_cors_origins)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'numbers.db'))

# ---------------------------------------------------------------------------
# DB ヘルパー
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    """SQLite 接続を提供するコンテキストマネージャ。例外時は自動ロールバック。"""
    conn = sqlite3.connect(DB_PATH)
    configure_sqlite_connection(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 入力検証ヘルパー
# ---------------------------------------------------------------------------

def _parse_int(value):
    """int に変換して返す。変換できない場合は None。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_valid_category(category):
    """category が定義済みカテゴリ（A/B/C/D）かどうか。"""
    return category in CATEGORY_START


# ---------------------------------------------------------------------------
# 共通 SQL
# ---------------------------------------------------------------------------

# 本日の未処理チケット一覧を返す SELECT。
# event_log_id が processing_logs に存在しない（=まだ呼び出し/削除されていない）本日の発券。
# event_log_id は発券ごとに一意なので、当日フィルタは event_logs 側だけで十分。
_WAITING_LIST_SQL = """
    SELECT id, current_number, button_text, timestamp, category
    FROM event_logs
    WHERE DATE(timestamp, 'localtime') = DATE('now', 'localtime')
    AND category != 'D'
    AND NOT EXISTS (
        SELECT 1 FROM processing_logs pl
        WHERE pl.event_log_id = event_logs.id
    )
    ORDER BY timestamp ASC
"""


# ---------------------------------------------------------------------------
# ルート
# ---------------------------------------------------------------------------

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/get_next_number', methods=['POST'])
def get_next_number():
    category = request.json.get('category')
    if not category:
        return jsonify({'error': 'Category is required'}), 400

    button_text = request.json.get('buttonText')
    staff_count = request.json.get('staffCount')
    # 発券時刻はサーバー側で確定する（クライアント送信の timestamp は使わない）。
    # 1リクエストにつき時刻は1回だけ取得し、記録・印刷・日付判定で共有する。
    now = datetime.now().astimezone()
    timestamp = now.isoformat()

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # 同時発券による番号重複を防ぐため、SELECT の前に書き込みロックを取得する
            cursor.execute('BEGIN IMMEDIATE')

            cursor.execute(
                'SELECT current_number, timestamp FROM numbers WHERE category = ?',
                (category,)
            )
            result = cursor.fetchone()
            if not result:
                return jsonify({'error': 'Invalid category'}), 404

            current_number, last_updated_date = result
            today_str = now.date().isoformat()

            # 日付が変わっていてかつ本日のログがまだ 0 件なら番号をリセットする。
            # timestamp だけではなくログ件数も確認するのは、サーバー再起動等で
            # timestamp が更新されないケースを防ぐため。
            if last_updated_date != today_str:
                cursor.execute(
                    "SELECT COUNT(*) FROM event_logs"
                    " WHERE category = ?"
                    " AND DATE(timestamp, 'localtime') = DATE('now', 'localtime')",
                    (category,)
                )
                if cursor.fetchone()[0] == 0:
                    current_number = CATEGORY_START[category]
                    new_number = current_number
                else:
                    new_number = current_number + 1
            else:
                new_number = current_number + 1

            # 表示番号が上限を超えたらカテゴリ開始番号へラップする（Issue #9）。
            # 表示番号の日内一意性は保証しない。チケット識別は event_logs.id を使う。
            if new_number > CATEGORY_MAX[category]:
                new_number = CATEGORY_START[category]

            cursor.execute(
                'UPDATE numbers SET current_number = ?, timestamp = ? WHERE category = ?',
                (new_number, today_str, category)
            )
            cursor.execute(
                'INSERT INTO event_logs'
                ' (category, button_text, timestamp, current_number, staff_count)'
                ' VALUES (?, ?, ?, ?, ?)',
                (category, button_text, timestamp, new_number, staff_count)
            )
            event_log_id = cursor.lastrowid
    except Exception as e:
        print(f"get_next_number error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

    # DB書き込み成功後に印刷（失敗しても発券結果は返す）
    print_ok = print_ticket(category, button_text or '', new_number, timestamp)

    return jsonify({
        'category': category,
        'next_number': new_number,
        'print_ok': print_ok,
        'event_log_id': event_log_id,
    })


@app.route('/start_processing', methods=['POST'])
def start_processing():
    # 安定キー event_log_id（発券レコードのID）で操作する。番号・カテゴリ等は
    # クライアントを信用せず event_logs から引く。
    event_log_id = _parse_int(request.json.get('event_log_id'))
    if event_log_id is None:
        return jsonify({'success': False, 'error': 'event_log_id is required'}), 400

    current_time   = datetime.now()
    start_time_str = current_time.isoformat()

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                'SELECT current_number, category, button_text, timestamp, staff_count'
                ' FROM event_logs WHERE id = ?',
                (event_log_id,)
            )
            ev = cursor.fetchone()
            if ev is None:
                return jsonify({'success': False, 'error': 'Event log not found'}), 404
            ticket_number, category, button_text, issued_ts, staff_count = ev

            # 二重呼び出しガード（冪等性）: 同じ発券が既に「対応中」なら新規行を作らない
            cursor.execute(
                "SELECT 1 FROM processing_logs"
                " WHERE event_log_id = ? AND status = 'processing'",
                (event_log_id,)
            )
            if cursor.fetchone():
                return jsonify({'success': True, 'already_processing': True})

            # 待ち時間（分）= 呼び出し時刻 - 発券時刻
            try:
                ticket_time = datetime.fromisoformat(issued_ts)
                if ticket_time.tzinfo is not None and current_time.tzinfo is None:
                    current_time = current_time.astimezone()
                wait_time_minutes = int((current_time - ticket_time).total_seconds() // 60)
            except Exception:
                wait_time_minutes = 0

            cursor.execute(
                'INSERT INTO processing_logs'
                ' (event_log_id, ticket_number, category, button_text, start_time,'
                '  wait_time, status, created_at, staff_count)'
                " VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)",
                (event_log_id, ticket_number, category, button_text, start_time_str,
                 wait_time_minutes, start_time_str, staff_count)
            )
    except Exception as e:
        print(f"start_processing error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

    return jsonify({'success': True})


@app.route('/end_processing', methods=['POST'])
def end_processing():
    # 安定キー processing_id（processing_logs.id）で操作するため、当日限定の絞り込みは不要。
    processing_id = _parse_int(request.json.get('processing_id'))
    if processing_id is None:
        return jsonify({'success': False, 'error': 'processing_id is required'}), 400

    current_time = datetime.now()
    end_time_str = current_time.isoformat()

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT start_time FROM processing_logs"
                " WHERE id = ? AND status = 'processing'",
                (processing_id,)
            )
            result = cursor.fetchone()
            if not result:
                return jsonify({'success': False, 'error': 'Processing record not found'}), 404

            try:
                start_time = datetime.fromisoformat(result[0])
                if start_time.tzinfo is not None and current_time.tzinfo is None:
                    current_time = current_time.astimezone()
                processing_time_seconds = int((current_time - start_time).total_seconds())
            except Exception:
                processing_time_seconds = 0

            cursor.execute(
                'UPDATE processing_logs'
                " SET end_time = ?, processing_time = ?, status = 'completed'"
                " WHERE id = ? AND status = 'processing'",
                (end_time_str, processing_time_seconds, processing_id)
            )
    except Exception as e:
        print(f"end_processing error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

    return jsonify({'success': True})


@app.route('/cancel_processing', methods=['POST'])
def cancel_processing():
    """対応中チケットを待ち行列に戻す（processing_logs レコードを削除してキューに復帰）。"""
    processing_id = _parse_int(request.json.get('processing_id'))
    if processing_id is None:
        return jsonify({'success': False, 'error': 'processing_id is required'}), 400

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM processing_logs WHERE id = ? AND status = 'processing'",
                (processing_id,)
            )
            if cursor.rowcount == 0:
                return jsonify({
                    'success': False,
                    'error': 'Processing record not found or not in processing status'
                }), 404
    except Exception as e:
        print(f"cancel_processing error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

    return jsonify({'success': True})


@app.route('/delete_ticket', methods=['POST'])
def delete_ticket():
    # 安定キー event_log_id で操作。'deleted' 行を残すことで待ち行列から外す（監査用に履歴保持）。
    event_log_id = _parse_int(request.json.get('event_log_id'))
    if event_log_id is None:
        return jsonify({'success': False, 'error': 'event_log_id is required'}), 400

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                'SELECT current_number, category, button_text FROM event_logs WHERE id = ?',
                (event_log_id,)
            )
            ev = cursor.fetchone()
            if ev is None:
                return jsonify({'success': False, 'error': 'Event log not found'}), 404
            ticket_number, category, button_text = ev

            current_time = datetime.now().isoformat()
            cursor.execute(
                'INSERT INTO processing_logs'
                ' (event_log_id, ticket_number, category, button_text, start_time, end_time,'
                '  wait_time, status, created_at)'
                " VALUES (?, ?, ?, ?, NULL, NULL, 0, 'deleted', ?)",
                (event_log_id, ticket_number, category, button_text, current_time)
            )
    except Exception as e:
        print(f"delete_ticket error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

    return jsonify({'success': True})


@app.template_filter('to_datetime')
def to_datetime(timestamp):
    return datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)


@app.route('/processing')
def processing():
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(_WAITING_LIST_SQL)
            waiting_list = cursor.fetchall()

            # created_at はオフセット無しのローカル時刻文字列のため 'localtime' 変換を掛けない
            cursor.execute(
                'SELECT id, ticket_number, button_text, start_time, category'
                ' FROM processing_logs'
                " WHERE status = 'processing'"
                " AND DATE(created_at) = DATE('now', 'localtime')"
                ' ORDER BY start_time ASC'
            )
            processing_list = cursor.fetchall()

        current_time = to_datetime(
            datetime.now(timezone.utc)
            .astimezone(timezone(timedelta(hours=9)))
            .isoformat()
        )
    except Exception as e:
        print(f"processing error: {e}")
        waiting_list    = []
        processing_list = []
        current_time    = datetime.now()

    return render_template(
        'syori.html',
        waiting_list=waiting_list,
        processing_list=processing_list,
        current_time=current_time,
    )


@app.route('/display')
def display():
    return render_template('display.html')


# 来庁者向けモニター表示用のキャッシュ。DB障害時も直前の正常値を表示し続けるため保持する。
# 単一プロセス(Flask開発サーバー)前提。gunicorn等の複数ワーカー構成では
# ワーカーごとにキャッシュが分かれるため別途共有ストレージへの変更を検討すること。
_display_data_cache = {'calling': [], 'waiting_count': 0}


@app.route('/display_data')
def display_data():
    global _display_data_cache
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # created_at はオフセット無しのローカル時刻文字列のため 'localtime' 変換を掛けない
            cursor.execute(
                'SELECT ticket_number, category, start_time, id'
                ' FROM processing_logs'
                " WHERE status = 'processing'"
                " AND DATE(created_at) = DATE('now', 'localtime')"
                ' ORDER BY start_time ASC'
            )
            now     = datetime.now()
            calling = []
            for r in cursor.fetchall():
                try:
                    seconds_since = int((now - datetime.fromisoformat(r[2])).total_seconds())
                except Exception:
                    seconds_since = 999
                calling.append({
                    'processing_id': r[3],
                    'number': r[0],
                    'category': r[1],
                    'seconds_since': seconds_since,
                })

            cursor.execute('SELECT COUNT(*) FROM (' + _WAITING_LIST_SQL + ')')
            waiting_count = cursor.fetchone()[0]

        # 取得成功時のみキャッシュを更新する。来庁者向けモニターは表示継続性を
        # ステータスコードの正確さより優先するため、常に200 + このキャッシュ値を返す。
        _display_data_cache = {'calling': calling, 'waiting_count': waiting_count}

    except Exception as e:
        # DB障害時は直前の正常値(起動直後で未取得なら初期値の空リスト)をそのまま返す。
        # ここで calling/waiting_count を空にリセットしないことで、フロント側の
        # prevCallingKeys(呼出しチャイム用の差分検出)が誤って初期化されるのを防ぐ。
        print(f"[display_data] DB error, returning cached data: {e}")

    return jsonify(_display_data_cache)


# ---------------------------------------------------------------------------
# 混雑傾向ダッシュボード
# ---------------------------------------------------------------------------
# event_logs（発券記録）だけを読む集計画面。書き込みは一切しない。
# 「どの曜日・時間帯が混むか」「どの日が混んだか」を期間を指定して見られるようにし、
# 職員が休みを入れる日を決める材料にする。

_WEEKDAY_LABELS = ('月', '火', '水', '木', '金', '土', '日')

# 既定は窓口の来庁者（A/B/C）。D（窓口以外の来庁者）は指定したときだけ含める。
_DASHBOARD_WINDOW_CATEGORIES = ('A', 'B', 'C')
_DASHBOARD_ALL_CATEGORIES = ('A', 'B', 'C', 'D')

_DASHBOARD_DEFAULT_PRESET = 'recent90'


def _parse_date(value):
    """YYYY-MM-DD を date に変換する。変換できない場合は None。"""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _dashboard_presets(today):
    """期間のショートカット。年度は4月始まり。"""
    first_this_month = today.replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    # 年度末の繁忙期（3〜4月）は、終わっている直近の回を指す
    busy_year = today.year if today.month >= 5 else today.year - 1
    return [
        {'key': 'fy_end', 'label': f'年度末（{busy_year}年3〜4月）',
         'start': date(busy_year, 3, 1), 'end': date(busy_year, 4, 30)},
        {'key': 'this_month', 'label': '今月',
         'start': first_this_month, 'end': today},
        {'key': 'prev_month', 'label': '先月',
         'start': last_prev_month.replace(day=1), 'end': last_prev_month},
        {'key': 'recent90', 'label': '直近90日',
         'start': today - timedelta(days=89), 'end': today},
        {'key': 'this_fy', 'label': '今年度',
         'start': date(fy_start_year, 4, 1), 'end': today},
        {'key': 'prev_fy', 'label': '前年度',
         'start': date(fy_start_year - 1, 4, 1), 'end': date(fy_start_year, 3, 31)},
    ]


def _ratio(value, max_value):
    """色の濃さ（0〜1）。最大値に対する割合。値が無い・最大値が0なら0。"""
    if not value or not max_value:
        return 0.0
    return value / max_value


def _spread(value, min_value, max_value):
    """色の濃さ（0〜1）。いちばん少ない値を0、いちばん多い値を1にする。

    日ごとの来庁者数は「0人の日」が無く値が上の方に固まるため、
    最大値との割合だと差が見えにくい。混んだ日を見分けやすくするために使う。
    """
    if value is None:
        return 0.0
    if max_value == min_value:
        return 1.0
    return (value - min_value) / (max_value - min_value)


# 折れ線グラフ（SVG）の描画領域。viewBox 上の座標で、表示幅に合わせて伸縮する。
_CHART_W, _CHART_H = 1000, 280
_CHART_LEFT, _CHART_RIGHT, _CHART_TOP, _CHART_BOTTOM = 56, 20, 16, 36

# 曜日ごとの線の色（月〜日）。色だけに頼らないよう、凡例とツールチップにも曜日名を出す。
_WEEKDAY_COLORS = ('#2a78d6', '#eb6834', '#1baf7a', '#c98500', '#e87ba4', '#008300', '#6250d6')


def _nice_ticks(max_value, count=4):
    """0 から max_value を覆う、切りのよい目盛り（1・2・5 刻み）を返す。"""
    if max_value <= 0:
        return [0, 1]
    raw = max_value / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    ticks = [0]
    while ticks[-1] < max_value:
        ticks.append(round(ticks[-1] + step, 6))
    return ticks


def _chart_frame(y_max):
    """y 軸の目盛りと、値→y 座標の変換関数を返す。"""
    ticks = _nice_ticks(y_max)
    plot_h = _CHART_H - _CHART_TOP - _CHART_BOTTOM
    top = ticks[-1]

    def y_of(value):
        return _CHART_TOP + plot_h * (1 - value / top)

    return [{'value': t, 'y': y_of(t)} for t in ticks], y_of


def _daily_chart(day_totals, start, end):
    """日ごとの来庁者数の折れ線。x は暦日なので、土日・休庁日はすき間として見える。"""
    span = (end - start).days or 1
    plot_w = _CHART_W - _CHART_LEFT - _CHART_RIGHT

    def x_of(d):
        return _CHART_LEFT + plot_w * (d - start).days / span

    y_ticks, y_of = _chart_frame(max(day_totals.values(), default=0))
    points = [{'x': x_of(d), 'y': y_of(c), 'date': d, 'label': _WEEKDAY_LABELS[d.weekday()],
               'count': c} for d, c in sorted(day_totals.items())]

    # 目盛り: 2か月以内なら毎週月曜、それより長ければ毎月1日
    x_ticks = []
    d = start
    while d <= end:
        if (span <= 62 and d.weekday() == 0) or (span > 62 and d.day == 1):
            text = f'{d.month}/{d.day}' if span <= 62 else f'{d.month}月'
            x_ticks.append({'x': x_of(d), 'text': text})
        d += timedelta(days=1)

    return {
        'points': points,
        'path': ' '.join(f"{p['x']:.1f},{p['y']:.1f}" for p in points),
        'y_ticks': y_ticks,
        'x_ticks': x_ticks,
    }


def _hourly_chart(heatmap, hours):
    """曜日ごとの、時間帯別の平均来庁者数の折れ線。"""
    plot_w = _CHART_W - _CHART_LEFT - _CHART_RIGHT
    step = plot_w / max(len(hours) - 1, 1)
    x_of = {h: _CHART_LEFT + step * i for i, h in enumerate(hours)}
    values = [c['value'] for row in heatmap for c in row['cells'] if c['value'] is not None]
    y_ticks, y_of = _chart_frame(max(values, default=0))

    lines = []
    for row in heatmap:
        if not row['open_days']:
            continue
        points = [{'x': x_of[c['hour']], 'y': y_of(c['value']), 'hour': c['hour'],
                   'value': c['value']} for c in row['cells']]
        lines.append({
            'label': row['label'],
            'color': _WEEKDAY_COLORS[row['weekday']],
            'points': points,
            'path': ' '.join(f"{p['x']:.1f},{p['y']:.1f}" for p in points),
        })
    return {
        'lines': lines,
        'y_ticks': y_ticks,
        'x_ticks': [{'x': x_of[h], 'text': f'{h}時'} for h in hours],
    }


def _dashboard_summary(start, end, include_d=False):
    """期間 [start, end]（両端を含む）の発券記録を、曜日×時間帯・日ごとに集計する。

    平均は「その曜日のうち、期間内に1件以上の発券があった日数」で割る。
    休庁日（土日・祝日・年末年始）で平均が薄まらないようにするため。
    """
    categories = _DASHBOARD_ALL_CATEGORIES if include_d else _DASHBOARD_WINDOW_CATEGORIES
    placeholders = ','.join('?' for _ in categories)

    # 日付・時刻の解釈は既存の集計（_WAITING_LIST_SQL）と同じく SQLite の 'localtime' に揃える
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DATE(timestamp, 'localtime') AS day,"
            "       CAST(strftime('%H', timestamp, 'localtime') AS INTEGER) AS hour,"
            '       COUNT(*)'
            ' FROM event_logs'
            " WHERE DATE(timestamp, 'localtime') BETWEEN ? AND ?"
            f' AND category IN ({placeholders})'
            ' GROUP BY day, hour',
            (start.isoformat(), end.isoformat(), *categories),
        ).fetchall()

    day_totals = {}    # date -> 件数
    slot_totals = {}   # (曜日, 時) -> 件数
    for day_str, hour, count in rows:
        if day_str is None or hour is None:
            continue   # 解釈できない timestamp は集計から外す
        d = date.fromisoformat(day_str)
        day_totals[d] = day_totals.get(d, 0) + count
        slot_totals[(d.weekday(), hour)] = slot_totals.get((d.weekday(), hour), 0) + count

    open_days = [0] * 7        # 曜日ごとの「発券があった日数」
    weekday_totals = [0] * 7
    for d, total in day_totals.items():
        open_days[d.weekday()] += 1
        weekday_totals[d.weekday()] += total

    def per_day(total, weekday):
        return total / open_days[weekday] if open_days[weekday] else None

    # 平日は常に並べ、土日は発券があったときだけ並べる。時間帯は発券があった時間だけ並べる
    weekdays = [wd for wd in range(7) if wd < 5 or open_days[wd]]
    hours = sorted({h for _, h in slot_totals})

    slot_avgs = {key: per_day(total, key[0]) for key, total in slot_totals.items()}
    day_avgs = {wd: per_day(weekday_totals[wd], wd) for wd in weekdays}
    slot_max = max(slot_avgs.values(), default=0)
    rated_day_avgs = [v for v in day_avgs.values() if v is not None]
    day_avg_min, day_avg_max = min(rated_day_avgs, default=0), max(rated_day_avgs, default=0)

    heatmap = []
    for wd in weekdays:
        cells = []
        for h in hours:
            value = slot_avgs.get((wd, h), 0.0 if open_days[wd] else None)
            cells.append({'hour': h, 'value': value, 'level': _ratio(value, slot_max)})
        heatmap.append({
            'weekday': wd,
            'label': _WEEKDAY_LABELS[wd],
            'open_days': open_days[wd],
            'cells': cells,
            'day_avg': day_avgs[wd],
            'day_level': _spread(day_avgs[wd], day_avg_min, day_avg_max),
        })

    # 日ごとのカレンダー（月曜始まり。期間外の日は空欄）
    day_min = min(day_totals.values(), default=0)
    day_max = max(day_totals.values(), default=0)
    weeks = []
    cursor = start - timedelta(days=start.weekday())
    last = end + timedelta(days=6 - end.weekday())
    while cursor <= last:
        week = []
        for _ in range(7):
            in_range = start <= cursor <= end
            count = day_totals.get(cursor) if in_range else None
            week.append({
                'date': cursor,
                'in_range': in_range,
                'count': count,
                'level': _spread(count, day_min, day_max),
            })
            cursor += timedelta(days=1)
        weeks.append(week)

    # 要点（期間内に発券が無ければ None）
    total = sum(day_totals.values())
    highlights = None
    if day_totals:
        rated = [(wd, v) for wd, v in day_avgs.items() if v is not None]
        busiest_wd, busiest_wd_avg = max(rated, key=lambda x: x[1])
        weekday_rated = [(wd, v) for wd, v in rated if wd < 5]
        quietest = (min(weekday_rated, key=lambda x: x[1])
                    if len(weekday_rated) >= 2 else None)
        (slot_wd, slot_hour), slot_avg = max(slot_avgs.items(), key=lambda x: x[1])
        busiest_day = max(sorted(day_totals), key=lambda d: day_totals[d])
        highlights = {
            'avg_per_day': total / len(day_totals),
            'busiest_weekday': {'label': _WEEKDAY_LABELS[busiest_wd], 'avg': busiest_wd_avg},
            'quietest_weekday': (
                {'label': _WEEKDAY_LABELS[quietest[0]], 'avg': quietest[1]}
                if quietest else None
            ),
            'busiest_slot': {
                'label': _WEEKDAY_LABELS[slot_wd], 'hour': slot_hour, 'avg': slot_avg,
            },
            'busiest_day': {
                'date': busiest_day,
                'label': _WEEKDAY_LABELS[busiest_day.weekday()],
                'count': day_totals[busiest_day],
            },
        }

    return {
        'total': total,
        'open_day_count': len(day_totals),
        'hours': hours,
        'heatmap': heatmap,
        'weeks': weeks,
        'highlights': highlights,
        'daily_chart': _daily_chart(day_totals, start, end),
        'hourly_chart': _hourly_chart(heatmap, hours),
    }


@app.route('/dashboard')
def dashboard():
    today = date.today()
    presets = _dashboard_presets(today)
    default = next(p for p in presets if p['key'] == _DASHBOARD_DEFAULT_PRESET)

    start_raw = request.args.get('start')
    end_raw = request.args.get('end')
    include_d = request.args.get('include_d') == '1'

    error = None
    if not start_raw and not end_raw:
        start, end = default['start'], default['end']
    else:
        start, end = _parse_date(start_raw), _parse_date(end_raw)
        if start is None or end is None:
            error = '開始日と終了日を両方入れてください（例: 2026-03-01）。'
        elif start > end:
            error = '開始日が終了日より後になっています。'

    summary = None
    if error is None:
        try:
            summary = _dashboard_summary(start, end, include_d)
        except Exception as e:
            print(f"dashboard error: {e}")
            error = '記録を読み込めませんでした。時間をおいて開き直してください。'

    return render_template(
        'dashboard.html',
        start_value=start.isoformat() if start else (start_raw or ''),
        end_value=end.isoformat() if end else (end_raw or ''),
        start=start,
        end=end,
        include_d=include_d,
        presets=presets,
        error=error,
        summary=summary,
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=os.environ.get('FLASK_DEBUG') == '1')
