"""
MADO-queue のエンドポイント・整合性・移行テスト。

本番の numbers.db を汚さないよう、一時ファイルを DB_PATH に設定してから
app を import する（app.py は import 時に DB_PATH を読むため順序が重要）。
各テストは setUp で DB を初期状態に戻すため、実行順に依存しない。
"""

import json
import os
import runpy
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

_tmp_fd, _tmp_path = tempfile.mkstemp(suffix='.db')
os.close(_tmp_fd)
os.environ['DB_PATH'] = _tmp_path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(BASE_DIR, 'init_db.py'))

import app as app_module
from app import app
from config import CATEGORY_MAX, CATEGORY_START

# テスト中に実機プリンターへ印刷しないよう無効化する
app_module.print_ticket = lambda *args, **kwargs: True


def tearDownModule():
    try:
        os.remove(_tmp_path)
    except OSError:
        pass


class MadoTestBase(unittest.TestCase):

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True
        self._reset_db()

    # --- ヘルパー -----------------------------------------------------------

    def _reset_db(self):
        """全テーブルを空にし、numbers を初期値で再シードする。"""
        conn = sqlite3.connect(app_module.DB_PATH)
        cur = conn.cursor()
        cur.execute('DELETE FROM processing_logs')   # 子から先に消す
        cur.execute('DELETE FROM event_logs')
        cur.execute('DELETE FROM numbers')
        cur.executemany(
            'INSERT INTO numbers (category, current_number) VALUES (?, ?)',
            list(CATEGORY_START.items()),
        )
        conn.commit()
        conn.close()

    def _query(self, sql, params=()):
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute('PRAGMA foreign_keys = ON')
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
        return rows

    def _issue(self, category='A', staff=1, button='住民票'):
        body = {'category': category, 'buttonText': button}
        if staff is not None:
            body['staffCount'] = staff
        return json.loads(self.app.post('/get_next_number', json=body).data)

    def _start(self, event_log_id):
        return self.app.post('/start_processing', json={'event_log_id': event_log_id})

    def _processing_id(self, event_log_id):
        rows = self._query(
            "SELECT id FROM processing_logs"
            " WHERE event_log_id = ? AND status = 'processing'"
            " ORDER BY id DESC LIMIT 1",
            (event_log_id,))
        return rows[0][0] if rows else None

    def _waiting_count(self):
        return json.loads(self.app.get('/display_data').data)['waiting_count']


class IssueTest(MadoTestBase):

    def test_issue_returns_start_number_and_event_log_id(self):
        data = self._issue('A')
        self.assertEqual(data['category'], 'A')
        self.assertEqual(data['next_number'], CATEGORY_START['A'])
        self.assertIsInstance(data['event_log_id'], int)

    def test_issue_reports_print_ok_on_success(self):
        data = self._issue('A')
        self.assertTrue(data['print_ok'])

    def test_issue_reports_print_ok_false_when_print_fails(self):
        original_print_ticket = app_module.print_ticket
        app_module.print_ticket = lambda *args, **kwargs: False
        try:
            data = self._issue('A')
        finally:
            app_module.print_ticket = original_print_ticket
        self.assertFalse(data['print_ok'])

    def test_issue_increments(self):
        first = self._issue('B')['next_number']
        second = self._issue('B')['next_number']
        self.assertEqual(second, first + 1)

    def test_missing_category_rejected(self):
        resp = self.app.post('/get_next_number', json={'staffCount': 1})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_category_rejected(self):
        resp = self.app.post('/get_next_number', json={'category': 'Z', 'staffCount': 1})
        self.assertEqual(resp.status_code, 404)

    def test_staff_count_optional_per_spec(self):
        # 仕様では staffCount は任意。無指定でも 200 で発行でき、staff_count は NULL。
        data = self._issue('A', staff=None)
        self.assertEqual(data['next_number'], CATEGORY_START['A'])
        rows = self._query('SELECT staff_count FROM event_logs WHERE id = ?',
                           (data['event_log_id'],))
        self.assertIsNone(rows[0][0])

    def test_staff_count_persisted_when_given(self):
        data = self._issue('A', staff=3)
        rows = self._query('SELECT staff_count FROM event_logs WHERE id = ?',
                           (data['event_log_id'],))
        self.assertEqual(rows[0][0], 3)

    def test_client_timestamp_is_ignored(self):
        # Issue #10: クライアント送信の timestamp は記録・印刷に使わない
        fake_ts = '2000-01-01T00:00:00+09:00'
        data = json.loads(self.app.post('/get_next_number', json={
            'category': 'A',
            'buttonText': '住民票',
            'timestamp': fake_ts,
            'staffCount': 1,
        }).data)
        rows = self._query(
            'SELECT timestamp FROM event_logs WHERE id = ?',
            (data['event_log_id'],),
        )
        self.assertNotEqual(rows[0][0], fake_ts)
        self.assertIn('T', rows[0][0])  # ISO8601


class StartProcessingTest(MadoTestBase):

    def test_requires_event_log_id(self):
        resp = self.app.post('/start_processing', json={})
        self.assertEqual(resp.status_code, 400)

    def test_non_numeric_event_log_id_rejected(self):
        resp = self.app.post('/start_processing',
                             json={'event_log_id': '<script>'})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_event_log_id_returns_404(self):
        resp = self._start(999999)
        self.assertEqual(resp.status_code, 404)

    def test_start_creates_processing_row(self):
        eid = self._issue('A')['event_log_id']
        resp = self._start(eid)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.data)['success'])
        rows = self._query(
            "SELECT event_log_id, ticket_number, status FROM processing_logs"
            " WHERE event_log_id = ?", (eid,))
        self.assertEqual(rows, [(eid, CATEGORY_START['A'], 'processing')])

    def test_double_start_is_idempotent(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        r2 = self._start(eid)
        self.assertTrue(json.loads(r2.data).get('already_processing'))
        rows = self._query(
            "SELECT id FROM processing_logs WHERE event_log_id = ? AND status = 'processing'",
            (eid,))
        self.assertEqual(len(rows), 1)


class LifecycleTest(MadoTestBase):

    def test_start_moves_ticket_out_of_waiting(self):
        eid = self._issue('A')['event_log_id']
        self.assertEqual(self._waiting_count(), 1)
        self._start(eid)
        self.assertEqual(self._waiting_count(), 0)

    def test_end_processing_completes(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        pid = self._processing_id(eid)
        resp = self.app.post('/end_processing', json={'processing_id': pid})
        self.assertTrue(json.loads(resp.data)['success'])
        rows = self._query(
            "SELECT status, processing_time FROM processing_logs WHERE id = ?", (pid,))
        self.assertEqual(rows[0][0], 'completed')
        self.assertIsNotNone(rows[0][1])

    def test_end_requires_processing_id(self):
        resp = self.app.post('/end_processing', json={})
        self.assertEqual(resp.status_code, 400)

    def test_end_unknown_id_returns_404(self):
        resp = self.app.post('/end_processing', json={'processing_id': 999999})
        self.assertEqual(resp.status_code, 404)

    def test_cancel_returns_ticket_to_waiting(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        pid = self._processing_id(eid)
        self.assertEqual(self._waiting_count(), 0)
        resp = self.app.post('/cancel_processing', json={'processing_id': pid})
        self.assertTrue(json.loads(resp.data)['success'])
        self.assertEqual(self._waiting_count(), 1)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM processing_logs WHERE id = ?", (pid,))[0][0], 0)

    def test_cancel_unknown_id_returns_404(self):
        resp = self.app.post('/cancel_processing', json={'processing_id': 999999})
        self.assertEqual(resp.status_code, 404)

    def test_delete_records_deleted_row_and_removes_from_waiting(self):
        eid = self._issue('A')['event_log_id']
        self.assertEqual(self._waiting_count(), 1)
        resp = self.app.post('/delete_ticket', json={'event_log_id': eid})
        self.assertTrue(json.loads(resp.data)['success'])
        rows = self._query(
            "SELECT status FROM processing_logs WHERE event_log_id = ?", (eid,))
        self.assertEqual(rows, [('deleted',)])
        self.assertEqual(self._waiting_count(), 0)

    def test_delete_unknown_event_log_returns_404(self):
        resp = self.app.post('/delete_ticket', json={'event_log_id': 999999})
        self.assertEqual(resp.status_code, 404)


class DisplayCallingTest(MadoTestBase):

    def test_wrapped_same_number_has_distinct_processing_ids(self):
        first = self._issue('A')
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute('UPDATE numbers SET current_number = ? WHERE category = ?',
                     (CATEGORY_MAX['A'], 'A'))
        conn.commit()
        conn.close()
        second = self._issue('A')
        self.assertEqual(first['next_number'], second['next_number'])
        self._start(first['event_log_id'])
        self._start(second['event_log_id'])
        calling = self.app.get('/display_data').get_json()['calling']
        self.assertEqual(len(calling), 2)
        self.assertEqual({c['number'] for c in calling}, {CATEGORY_START['A']})
        self.assertEqual({c['processing_id'] for c in calling}, {
            self._processing_id(first['event_log_id']),
            self._processing_id(second['event_log_id']),
        })

    def test_polling_keeps_processing_id(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        first = self.app.get('/display_data').get_json()
        second = self.app.get('/display_data').get_json()
        self.assertEqual(first['calling'][0]['processing_id'],
                         second['calling'][0]['processing_id'])

    def test_cancel_and_recall_has_new_processing_id(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        old_id = self._processing_id(eid)
        self.app.post('/cancel_processing', json={'processing_id': old_id})
        self._start(eid)
        self.assertNotEqual(old_id, self._processing_id(eid))

    def test_db_failure_keeps_cached_processing_ids(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        normal = self.app.get('/display_data').get_json()
        with patch.object(app_module, 'get_db', side_effect=sqlite3.OperationalError('test failure')):
            response = self.app.get('/display_data')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), normal)
        self.assertIsInstance(normal['calling'][0]['processing_id'], int)


class IntegrityTest(MadoTestBase):
    """設計準拠の DB 制約（FK / CHECK）が効いていること"""

    def _insert_processing(self, **cols):
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute('PRAGMA foreign_keys = ON')
        try:
            keys = ', '.join(cols)
            ph = ', '.join('?' * len(cols))
            conn.execute(f'INSERT INTO processing_logs ({keys}) VALUES ({ph})',
                        tuple(cols.values()))
            conn.commit()
        finally:
            conn.close()

    def test_foreign_key_rejects_orphan_event_log_id(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_processing(
                event_log_id=999999, ticket_number=1,
                status='processing', created_at='2026-06-21T09:00:00+09:00')

    def test_check_rejects_unknown_status(self):
        eid = self._issue('A')['event_log_id']
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_processing(
                event_log_id=eid, ticket_number=1,
                status='bogus', created_at='2026-06-21T09:00:00+09:00')

    def test_indexes_exist(self):
        idx = [r[0] for r in self._query(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='processing_logs'")]
        self.assertIn('idx_pl_event_log_id', idx)
        self.assertIn('idx_pl_status', idx)


class WaitingListTest(MadoTestBase):

    def test_category_d_excluded(self):
        self._issue('A')
        self._issue('D')
        self.assertEqual(self._waiting_count(), 1)

    def test_multiple_tickets_counted(self):
        self._issue('A'); self._issue('A'); self._issue('B')
        self.assertEqual(self._waiting_count(), 3)

    def test_processing_page_renders(self):
        eid = self._issue('A')['event_log_id']
        self._start(eid)
        self.assertEqual(self.app.get('/processing').status_code, 200)


class DailyResetTest(MadoTestBase):

    def test_resets_on_new_day(self):
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute(
            "UPDATE numbers SET current_number = 42, timestamp = '2000-01-01' WHERE category = 'A'")
        conn.commit()
        conn.close()
        self.assertEqual(self._issue('A')['next_number'], CATEGORY_START['A'])

    def test_no_reset_within_same_day(self):
        self.assertEqual(self._issue('A')['next_number'], CATEGORY_START['A'])
        self.assertEqual(self._issue('A')['next_number'], CATEGORY_START['A'] + 1)


class CategoryMaxWrapTest(MadoTestBase):
    """Issue #9: 表示番号が CATEGORY_MAX を超えたら開始番号へラップする。"""

    def test_all_category_boundaries(self):
        for category, upper in CATEGORY_MAX.items():
            with self.subTest(category=category):
                self._set_counter(category, upper - 1)
                last = self._issue(category)
                first = self._issue(category)
                following = self._issue(category)
                self.assertEqual(last['next_number'], upper)
                self.assertEqual(first['next_number'], CATEGORY_START[category])
                self.assertEqual(following['next_number'], CATEGORY_START[category] + 1)
                self.assertNotEqual(last['event_log_id'], first['event_log_id'])

    def _set_counter(self, category, current_number, timestamp=None):
        if timestamp is None:
            timestamp = date.today().isoformat()
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute(
            'UPDATE numbers SET current_number = ?, timestamp = ? WHERE category = ?',
            (current_number, timestamp, category),
        )
        conn.commit()
        conn.close()

    def test_wraps_when_exceeding_category_max(self):
        self._set_counter('C', CATEGORY_MAX['C'])
        data = self._issue('C')
        self.assertEqual(data['next_number'], CATEGORY_START['C'])
        self.assertIsInstance(data['event_log_id'], int)

    def test_increments_after_wrap(self):
        self._set_counter('A', CATEGORY_MAX['A'])
        first = self._issue('A')
        second = self._issue('A')
        self.assertEqual(first['next_number'], CATEGORY_START['A'])
        self.assertEqual(second['next_number'], CATEGORY_START['A'] + 1)
        self.assertNotEqual(first['event_log_id'], second['event_log_id'])

    def test_daily_reset_takes_precedence_over_stale_max(self):
        # 前日に上限付近まで進んでいても、当日ログが無ければ開始番号へ日次リセット
        self._set_counter('B', CATEGORY_MAX['B'], timestamp='2000-01-01')
        self.assertEqual(self._issue('B')['next_number'], CATEGORY_START['B'])

    def test_wrap_still_applies_within_same_day_after_reset_path_skipped(self):
        # 当日ログがあるため日次リセットはせず、+1 が上限超えならラップする
        first = self._issue('C')
        self.assertEqual(first['next_number'], CATEGORY_START['C'])
        self._set_counter('C', CATEGORY_MAX['C'])
        wrapped = self._issue('C')
        self.assertEqual(wrapped['next_number'], CATEGORY_START['C'])
        self.assertNotEqual(first['event_log_id'], wrapped['event_log_id'])


class DashboardTest(MadoTestBase):
    """混雑傾向ダッシュボード（/dashboard）の集計"""

    def _insert_event(self, dt, category='A', button='住民票'):
        # 実行環境のタイムゾーンに依存しないよう、ローカル時刻にオフセットを付けて保存する
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute(
            'INSERT INTO event_logs (category, button_text, timestamp, current_number)'
            ' VALUES (?, ?, ?, ?)',
            (category, button, dt.astimezone().isoformat(), 1))
        conn.commit()
        conn.close()

    def _summary(self, start, end, include_d=False):
        return app_module._dashboard_summary(start, end, include_d)

    def test_page_renders_with_default_period(self):
        res = self.app.get('/dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn('混雑傾向', res.get_data(as_text=True))

    def test_page_renders_with_records(self):
        self._insert_event(datetime(2026, 3, 4, 13, 10))
        res = self.app.get('/dashboard?start=2026-03-01&end=2026-03-31')
        self.assertEqual(res.status_code, 200)
        self.assertIn('いちばん混む曜日', res.get_data(as_text=True))

    def test_averages_divide_by_days_with_records(self):
        wed1, thu, wed2 = date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 11)
        self.assertEqual([wed1.weekday(), thu.weekday(), wed2.weekday()], [2, 3, 2])
        for minute in (5, 20, 40):
            self._insert_event(datetime(2026, 3, 4, 13, minute))
        self._insert_event(datetime(2026, 3, 11, 13, 15))
        self._insert_event(datetime(2026, 3, 5, 9, 0))

        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        wed = next(r for r in s['heatmap'] if r['weekday'] == 2)
        self.assertEqual(wed['open_days'], 2)
        cell_13 = next(c for c in wed['cells'] if c['hour'] == 13)
        self.assertAlmostEqual(cell_13['value'], 2.0)      # 4人 ÷ 水曜2日分
        self.assertAlmostEqual(wed['day_avg'], 2.0)

        h = s['highlights']
        self.assertEqual(s['total'], 5)
        self.assertEqual(h['busiest_weekday']['label'], '水')
        self.assertEqual(h['quietest_weekday']['label'], '木')
        self.assertEqual((h['busiest_slot']['label'], h['busiest_slot']['hour']), ('水', 13))
        self.assertEqual(h['busiest_day']['date'], wed1)
        self.assertEqual(h['busiest_day']['count'], 3)

    def test_weekday_without_records_is_blank_not_zero(self):
        self._insert_event(datetime(2026, 3, 4, 10, 0))
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        mon = next(r for r in s['heatmap'] if r['weekday'] == 0)
        self.assertEqual(mon['open_days'], 0)
        self.assertIsNone(mon['day_avg'])
        self.assertTrue(all(c['value'] is None for c in mon['cells']))

    def test_period_bounds_are_inclusive(self):
        self._insert_event(datetime(2026, 2, 28, 10, 0))
        self._insert_event(datetime(2026, 3, 1, 10, 0))
        self._insert_event(datetime(2026, 3, 31, 16, 0))
        self._insert_event(datetime(2026, 4, 1, 10, 0))
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(s['total'], 2)

    def test_category_d_only_when_requested(self):
        self._insert_event(datetime(2026, 3, 4, 10, 0), category='A')
        self._insert_event(datetime(2026, 3, 4, 10, 30), category='D')
        self.assertEqual(self._summary(date(2026, 3, 1), date(2026, 3, 31))['total'], 1)
        self.assertEqual(
            self._summary(date(2026, 3, 1), date(2026, 3, 31), include_d=True)['total'], 2)

    def test_hours_are_those_with_records(self):
        self._insert_event(datetime(2026, 3, 4, 9, 30))
        self._insert_event(datetime(2026, 3, 4, 18, 30))
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(s['hours'], [9, 18])

    def test_busiest_and_quietest_days_get_full_color_range(self):
        for _ in range(3):
            self._insert_event(datetime(2026, 3, 4, 10, 0))
        self._insert_event(datetime(2026, 3, 5, 10, 0))
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        days = {d['date']: d for week in s['weeks'] for d in week}
        self.assertEqual(days[date(2026, 3, 4)]['level'], 1.0)
        self.assertEqual(days[date(2026, 3, 5)]['level'], 0.0)

    def test_weekend_row_only_when_records_exist(self):
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual([r['weekday'] for r in s['heatmap']], [0, 1, 2, 3, 4])
        sat = date(2026, 3, 7)
        self.assertEqual(sat.weekday(), 5)
        self._insert_event(datetime(2026, 3, 7, 10, 0))
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual([r['weekday'] for r in s['heatmap']], [0, 1, 2, 3, 4, 5])

    def test_calendar_covers_whole_weeks_and_marks_out_of_range(self):
        self._insert_event(datetime(2026, 3, 4, 10, 0))
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        days = [d for week in s['weeks'] for d in week]
        self.assertTrue(all(len(week) == 7 for week in s['weeks']))
        self.assertEqual(days[0]['date'].weekday(), 0)
        self.assertEqual(days[-1]['date'].weekday(), 6)
        in_range = [d for d in days if d['in_range']]
        self.assertEqual(len(in_range), 31)
        self.assertEqual(next(d for d in in_range if d['date'] == date(2026, 3, 4))['count'], 1)
        self.assertIsNone(next(d for d in in_range if d['date'] == date(2026, 3, 5))['count'])

    def test_no_records_has_no_highlights(self):
        s = self._summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(s['total'], 0)
        self.assertIsNone(s['highlights'])
        res = self.app.get('/dashboard?start=2026-03-01&end=2026-03-31')
        self.assertIn('発券記録はありません', res.get_data(as_text=True))

    def test_reversed_or_incomplete_period_shows_message(self):
        res = self.app.get('/dashboard?start=2026-03-31&end=2026-03-01')
        self.assertEqual(res.status_code, 200)
        self.assertIn('開始日が終了日より後', res.get_data(as_text=True))
        res = self.app.get('/dashboard?start=2026-03-01')
        self.assertIn('両方入れてください', res.get_data(as_text=True))

    def test_presets_follow_fiscal_year(self):
        def by_key(today):
            return {p['key']: (p['start'], p['end']) for p in app_module._dashboard_presets(today)}

        autumn = by_key(date(2026, 11, 14))
        self.assertEqual(autumn['fy_end'], (date(2026, 3, 1), date(2026, 4, 30)))
        self.assertEqual(autumn['this_fy'], (date(2026, 4, 1), date(2026, 11, 14)))
        self.assertEqual(autumn['prev_fy'], (date(2025, 4, 1), date(2026, 3, 31)))
        self.assertEqual(autumn['prev_month'], (date(2026, 10, 1), date(2026, 10, 31)))

        # 4月中はまだ年度末の繁忙期が終わっていないので、前年の3〜4月を指す
        april = by_key(date(2026, 4, 10))
        self.assertEqual(april['fy_end'], (date(2025, 3, 1), date(2025, 4, 30)))
        self.assertEqual(april['this_fy'], (date(2026, 4, 1), date(2026, 4, 10)))


class MigrationTest(unittest.TestCase):
    """旧スキーマ→新スキーマ移行（event_log_id バックフィル＋制約付与）"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        import safe_migrate_db
        self.smd = safe_migrate_db
        self.smd.db_path = self.db   # モジュールのグローバルを差し替え

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def _build_legacy(self, with_orphan=False):
        """event_log_id も FK も無い旧スキーマの DB を作る。"""
        conn = sqlite3.connect(self.db)
        c = conn.cursor()
        c.execute("""CREATE TABLE event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category CHAR(1) NOT NULL,
            button_text TEXT, timestamp TEXT NOT NULL, current_number INTEGER NOT NULL,
            staff_count INTEGER)""")
        c.execute("""CREATE TABLE processing_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_number INTEGER NOT NULL,
            category TEXT, status TEXT, wait_time INTEGER, processing_time INTEGER,
            created_at TEXT NOT NULL)""")
        ts = '2026-06-21T09:00:00+09:00'
        c.execute("INSERT INTO event_logs (category, button_text, timestamp, current_number)"
                  " VALUES ('A', '住民票', ?, 5)", (ts,))
        # 旧処理ログ: event_log_id を持たず、番号+カテゴリ+当日でしか辿れない
        c.execute("INSERT INTO processing_logs (ticket_number, category, status, created_at)"
                  " VALUES (5, 'A', 'completed', ?)", (ts,))
        if with_orphan:
            c.execute("INSERT INTO processing_logs (ticket_number, category, status, created_at)"
                      " VALUES (777, 'A', 'completed', ?)", (ts,))
        conn.commit()
        conn.close()

    def _fk_to_event_logs(self):
        conn = sqlite3.connect(self.db)
        fks = conn.execute("PRAGMA foreign_key_list(processing_logs)").fetchall()
        conn.close()
        return any(fk[2] == 'event_logs' for fk in fks)

    def test_backfills_and_adds_constraints(self):
        self._build_legacy()
        self.smd.safe_migrate()
        self.assertTrue(self._fk_to_event_logs())
        conn = sqlite3.connect(self.db)
        # 旧 OR 照合がバックフィルされ、event_log_id が親 (=1) を指す
        row = conn.execute(
            "SELECT event_log_id FROM processing_logs WHERE ticket_number = 5").fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_idempotent(self):
        self._build_legacy()
        self.smd.safe_migrate()
        self.smd.safe_migrate()  # 2回目は "Already migrated" で何もしない
        self.assertTrue(self._fk_to_event_logs())

    def test_aborts_on_orphan_without_changing_data(self):
        self._build_legacy(with_orphan=True)
        self.smd.safe_migrate()
        # 参照先のない行があるので移行は中止され、旧スキーマ（FK 無し）のまま
        self.assertFalse(self._fk_to_event_logs())


if __name__ == '__main__':
    unittest.main()
