import unittest
from datetime import date, timedelta
from subprocess import CompletedProcess
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
import pandas as pd
from production_report import assign_groups, summarize, render_html, configure_schedule, energy_metrics, load_runtime, runtime_metrics, load_production_energy, headcount_metrics, FACTORY_LINES


EMPTY_RUNTIME = pd.DataFrame(columns=['day', 'line', 'hour_of_day', 'operating_minutes', 'invalid_rpm', 'downtime_minutes', 'minor_stop_minutes', 'samples'])


class ReportingTests(unittest.TestCase):
    def test_crew_filter_changes_production_trend_but_not_daily_metering(self):
        r = pd.DataFrame({'day': pd.to_datetime(['2026-11-01', '2026-11-02', '2026-11-02']),
                          'line': ['01'] * 3, 'jo': ['B', 'A', 'B'], 'juya':['2','1','2'], 'qty': [30., 100., 200.],
                          'lot_no': ['L0', 'L1', 'L1'], 'run': ['S0', 'G1', 'G1'],
                          'grade': ['X'] * 3, 'color': ['R'] * 3, 'group_type': ['통합 그룹'] * 3})
        e = pd.DataFrame({'day': pd.to_datetime(['2026-11-02']), 'sensor_id': ['400'], 'kwh': [25.]})
        staff = pd.DataFrame({'p_date': ['2026.11.02'] * 2, 'fac_cd': ['1'] * 2,
                              'juya': ['1', '2'], 'jo': ['A', 'B'],
                              'tot_cnt': ['5'] * 2, 'cur_cnt': ['5'] * 2, 'abs_cnt': ['0'] * 2})
        def compute(crew):
            return summarize(r, e, '2026-11-02', sorted(FACTORY_LINES['1']), runtime=EMPTY_RUNTIME, headcount=staff, crew=crew)
        total, _, _, trend = compute('전체')
        a, _, groups, a_trend = compute('A')
        b, _, _, _ = compute('B')
        self.assertEqual(total['total'], a['total'] + b['total'])
        self.assertEqual(a['total'], 100)
        self.assertEqual(a['month_total'], 100)
        self.assertEqual(b['month_total'], 230)
        self.assertEqual(a['working_days'], 1)
        self.assertEqual(b['working_days'], 2)
        self.assertEqual(groups['생산량_kg'].sum(), 100)
        self.assertEqual(a['people'], 5)
        self.assertEqual(a['per_person'], 20)
        self.assertEqual(a['energy'], total['energy'])
        self.assertEqual(trend.columns.tolist(), ['전체', 'A조', 'B조'])
        self.assertEqual(a_trend.columns.tolist(), ['A조'])
        self.assertEqual(a_trend['A조'].sum(), 100)

    def test_energy_business_day_boundaries_and_partial_status(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '01_분석용').mkdir()
            stamps = pd.date_range('2026-11-02 08:30', periods=96, freq='15min')
            frame = pd.DataFrame({'sensor_id': '400', '센서명': 'EX1', '일시': stamps.astype(str),
                                  '사용량_kWh': 1, '최대_kW': 4})
            edges = pd.DataFrame({'sensor_id': ['400', '400'], '센서명': ['EX1', 'EX1'],
                                  '일시': ['2026-11-02 08:15:00', '2026-11-03 08:30:00'],
                                  '사용량_kWh': [100, 200], '최대_kW': [4, 4]})
            pd.concat([frame, edges]).to_csv(root / '01_분석용/EMS_전력계_15min집계.csv', index=False)
            with patch('production_report.DATA', root), patch('production_report.ROOT', root):
                e = load_production_energy()
            complete = energy_metrics(e, date(2026, 11, 2), ['01'])
            self.assertEqual(complete['energy'], 96)
            self.assertTrue(complete['energy_complete'])
            partial = energy_metrics(e, date(2026, 11, 3), ['01'])
            self.assertEqual(partial['energy'], 200)
            self.assertFalse(partial['energy_complete'])

    def test_headcount_factory_shifts_no_line_allocation_and_invalid_records(self):
        rows = pd.DataFrame({'p_date': ['2026.11.02'] * 4, 'fac_cd': ['1', '1', '2', '2'],
                             'juya': ['1', '2', '1', '2'], 'jo': ['A', 'B', 'D', 'E'], 'cur_cnt': ['10', '10', '9', '9'],
                             'tot_cnt': ['10', '10', '9', '9'], 'abs_cnt': ['0'] * 4})
        self.assertEqual(headcount_metrics(rows, '2026-11-02', sorted(FACTORY_LINES['1']))['people'], 20)
        all_lines = sorted(FACTORY_LINES['1'] | FACTORY_LINES['2'])
        self.assertEqual(headcount_metrics(rows, '2026-11-02', all_lines)['people'], 38)
        self.assertEqual(headcount_metrics(rows, '2026-11-02', sorted(FACTORY_LINES['1']), crew='A')['people'], 10)
        self.assertEqual(headcount_metrics(rows, '2026-11-02', all_lines, crew='A')['people'], 19)
        self.assertEqual(headcount_metrics(rows, '2026-11-02', ['01'])['people'], 0)
        self.assertEqual(headcount_metrics(rows, '2026-11-02', ['01'], 3)['people'], 3)
        bad = rows.copy(); bad.loc[0, 'cur_cnt'] = '11'
        self.assertEqual(headcount_metrics(bad, '2026-11-02', all_lines)['people'], 0)
        self.assertEqual(headcount_metrics(rows.iloc[:-1], '2026-11-02', all_lines)['people'], 0)

    def test_incomplete_runtime_is_provisional_without_changing_denominator(self):
        h = pd.DataFrame({'day': pd.to_datetime(['2026-11-02']), 'line': ['01'], 'hour_of_day': ['08'],
                          'operating_minutes': [30], 'invalid_rpm': [0], 'samples': [60],
                          'downtime_minutes': [30], 'minor_stop_minutes': [0]})
        result = runtime_metrics(h, '2026-11-02', ['01'])
        self.assertEqual(result['utilization_status'], '잠정')
        self.assertEqual(result['missing_runtime_minutes'], 1380)
        self.assertAlmostEqual(result['utilization'], 30 / 1440 * 100)

    def aggregate_fixture(self, sample):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '01_분석용').mkdir()
            sample.to_csv(root / '01_분석용/HMI_feeder_list.csv', index=False)
            with patch('production_report.DATA', root), patch('production_report.ROOT', root):
                hourly = load_runtime()
                self.assertTrue(load_runtime().equals(hourly))
                return hourly

    def test_mes_filter_excludes_250_and_short_low_runs(self):
        rpm = [249] * 4 + [250] + [249] * 5 + [300] + [0] * 14
        stamps = pd.date_range('2026-01-03 09:00', periods=len(rpm), freq='min')
        sample = pd.DataFrame({'p_date': '2026.01.03', 'line': '01',
                               'times': stamps.strftime('%H:%M:%S'),
                               'input_dt': stamps.astype(str), 'mval02': rpm})
        hourly = self.aggregate_fixture(sample)
        self.assertEqual(hourly.downtime_minutes.sum(), 24)
        self.assertEqual(hourly.minor_stop_minutes.sum(), 0)
        self.assertEqual(hourly.operating_minutes.sum(), 1)
        result = runtime_metrics(hourly, date(2026, 1, 3), ['01'])
        self.assertAlmostEqual(result['utilization'], 1 / 1440 * 100)
        self.assertAlmostEqual(runtime_metrics(hourly, date(2026, 1, 3), ['01', '02'])['utilization'],1 / 1440 * 100)

    def test_mes_counts_raw_running_records_with_hourly_cap(self):
        stamps=pd.date_range('2026-01-03 09:00',periods=60,freq='min')
        sample=pd.DataFrame({'p_date':'2026.01.03','line':'01',
                             'times':stamps.strftime('%H:%M:%S'),
                             'input_dt':stamps.astype(str),'mval02':300})
        hourly=self.aggregate_fixture(pd.concat([sample,sample.iloc[:20]],ignore_index=True))
        self.assertEqual(hourly['operating_minutes'].sum(),60)
        self.assertEqual(hourly['samples'].sum(),60)

    def test_cross_business_date_low_run_is_not_split(self):
        stamps = pd.date_range('2026-01-03 08:27', periods=6, freq='min')
        hourly = self.aggregate_fixture(pd.DataFrame({
            'p_date': ['2026.01.02'] * 3 + ['2026.01.03'] * 3, 'line': '01',
            'times': stamps.strftime('%H:%M:%S'), 'input_dt': stamps.astype(str), 'mval02': 249}))
        self.assertEqual(hourly.downtime_minutes.tolist(), [3, 3])
        self.assertEqual(hourly.operating_minutes.sum(), 0)

    def test_missing_minutes_break_continuity_and_duplicates_do_not_extend_runs(self):
        stamps = pd.to_datetime(['2026-01-03 09:00', '2026-01-03 09:01', '2026-01-03 09:02',
                                 '2026-01-03 09:04', '2026-01-03 09:05', '2026-01-03 09:06'])
        sample = pd.DataFrame({'p_date': '2026.01.03', 'line': '01',
                               'times': stamps.strftime('%H:%M:%S'), 'input_dt': stamps.astype(str), 'mval02': 249})
        sample = pd.concat([sample, sample.iloc[[0, 1]]], ignore_index=True)
        hourly = self.aggregate_fixture(sample)
        self.assertEqual(hourly.samples.sum(), 6)
        self.assertEqual(hourly.downtime_minutes.sum(), 6)
        self.assertEqual(hourly.minor_stop_minutes.sum(), 0)

    def test_plant_total_fixed_line_metering_and_line8_shared_sensor(self):
        e = pd.DataFrame(dict(day=pd.to_datetime(['2026-01-03'] * 7),
                              sensor_id=['400', '503', '403', '510', '500', '217', '512'],
                              kwh=[100., 10., 200., 15., 50., 30., 25.]))
        all_lines = energy_metrics(e, date(2026, 1, 3), ['01', '08'])
        line8 = energy_metrics(e, date(2026, 1, 3), ['08'])
        self.assertEqual(all_lines['energy'], 400)  # shared utility included, solar excluded
        self.assertEqual(line8['energy'], all_lines['energy'])
        self.assertEqual(all_lines['extruder_energy'], 300)
        self.assertEqual(line8['extruder_energy'], 200)
        self.assertEqual(line8['heater_energy'], 40)
        self.assertEqual(line8['line_energy'], 240)
        missing = energy_metrics(e, date(2026, 1, 4), ['08'])
        self.assertIsNone(missing['energy'])
        self.assertIsNone(missing['extruder_energy'])

    @patch('production_report.subprocess.run')
    @patch('production_report.datetime')
    def test_schedule_preserves_existing_jobs_and_converts_korean_time(self, clock, run):
        clock.now.return_value.astimezone.return_value.utcoffset.return_value = timedelta(0)
        run.side_effect = [CompletedProcess([], 0, '15 2 * * * existing-job\n', ''), CompletedProcess([], 0, '', '')]
        configure_schedule(9, 30)
        saved = run.call_args.kwargs['input']
        self.assertIn('15 2 * * * existing-job', saved)
        self.assertIn('30 0 * * *', saved)
        self.assertIn('--date yesterday', saved)

    def test_mapped_and_standalone_lots_include_all_production_once(self):
        r = pd.DataFrame(dict(lot_no=['L1', 'L2', 'L3', 'L4', 'L1'], line=['01', '02', '01', '01', '01'],
                              day=pd.to_datetime(['2026-01-01'] * 5), qty=[100., 200., 300., 400., 50.],
                              jo=['A'] * 5, grade=['X', 'Y', 'X', 'X', 'X'], color=['R'] * 5))
        mapping = pd.DataFrame(dict(lot_no=['L1', 'L2'], grp_lot_no=['G1', 'G1']))
        r = assign_groups(r, mapping)
        self.assertEqual(len(r), 5)
        self.assertEqual(r.run.nunique(), 3)
        e = pd.DataFrame(columns=['day', 'sensor_id', 'kwh'])
        m, _, groups, _ = summarize(r, e, date(2026, 1, 1), ['01', '02'], runtime=EMPTY_RUNTIME)
        self.assertEqual(m['integrated_groups'], 1)
        self.assertEqual(m['standalone_lots'], 2)
        self.assertEqual(groups['생산량_kg'].sum(), 1050)
        self.assertEqual(m['integrated_lot'], 350)
        # A group remains one group even across different lines and recipe metadata.
        self.assertEqual(groups.loc[groups['묶음'] == '그룹:G1', '생산량_kg'].iloc[0], 350)
        filtered, _, _, _ = summarize(r, e, date(2026, 1, 1), ['02'], runtime=EMPTY_RUNTIME)
        self.assertEqual(filtered['integrated_lot'], 200)

    def test_conflicting_group_mapping_rejected(self):
        r = pd.DataFrame({'lot_no': ['L1']})
        mapping = pd.DataFrame({'lot_no': ['L1', 'L1'], 'grp_lot_no': ['G1', 'G2']})
        with self.assertRaises(ValueError):
            assign_groups(r, mapping)

    def test_daily_lot_month_filters_missing_energy_and_manual_denominators(self):
        r = pd.DataFrame(dict(day=pd.to_datetime(['2026-01-01', '2026-01-03', '2026-01-03', '2026-01-03', '2026-02-01']),
                              line=['01', '01', '01', '02', '01'], qty=[100., 60., 40., 500., 900.],
                              lot_no=['L1', 'L2', 'L2', 'L3', 'L4'], jo=['A', 'A', 'B', 'A', 'A'],
                              run=['1', '2', '2', '3', '4'], grade=['X'] * 5, color=['R'] * 5,
                              group_type=['단독 LOT'] * 5))
        e = pd.DataFrame(dict(day=pd.to_datetime(['2026-01-03']), sensor_id=['100'], kwh=[25.]))
        hourly = pd.DataFrame({'day': pd.to_datetime(['2026-01-03'] * 12), 'line': ['01'] * 12,
                               'hour_of_day': [f'{i:02d}' for i in range(12)],
                               'operating_minutes': [60] * 12, 'invalid_rpm': [0] * 12,
                               'downtime_minutes': [0] * 12, 'minor_stop_minutes': [0] * 12, 'samples': [60] * 12})
        m, _, _, _ = summarize(r, e, date(2026, 1, 3), ['01'], people=2, runtime=hourly)
        self.assertEqual(m['total'], m['a'] + m['b'])
        self.assertEqual(m['lots'], 1)
        self.assertEqual(m['average_lot'], 100)
        self.assertEqual(m['month_total'], 200)
        self.assertEqual(m['working_days'], 2)
        self.assertEqual(m['daily_average'], 100)
        self.assertEqual(m['per_person'], 50)
        self.assertEqual(m['utilization'], 50)
        self.assertEqual(m['energy'], 25)
        missing, lines, groups, _ = summarize(r, e, date(2026, 1, 2), ['01'], runtime=EMPTY_RUNTIME)
        self.assertIsNone(missing['energy'])
        self.assertFalse(missing['source_present'])
        self.assertEqual(missing['utilization_status'], '기록 없음')
        self.assertIsNone(missing['utilization'])


if __name__ == '__main__':
    unittest.main()
