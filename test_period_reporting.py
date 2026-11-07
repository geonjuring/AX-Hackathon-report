import unittest
import pandas as pd
from period_reporting import period_bounds, build_period_report, render_period_html
from production_report import FACTORY_LINES, runtime_metrics, period_runtime_metrics


class PeriodTests(unittest.TestCase):
    def test_month_week_boundaries(self):
        for ref, week, expected in [('2026-10-10',1,('2026-10-01','2026-10-07')),
                                    ('2026-10-10',5,('2026-10-29','2026-10-31')),
                                    ('2024-02-01',5,('2024-02-29','2024-02-29'))]:
            self.assertEqual(tuple(x.strftime('%Y-%m-%d') for x in period_bounds(ref,'주간',week)),expected)
        with self.assertRaises(ValueError):
            period_bounds('2026-02-01','주간',5)

    def fixture(self):
        dates = pd.date_range('2026-01-01','2026-01-08')
        r = pd.DataFrame({'day': pd.to_datetime(['2026-01-01','2026-01-08']), 'line':['01','01'],
                          'jo':['A','B'], 'qty':[100.,200.], 'lot_no':['L1','L1'], 'run':['G1','G1'],
                          'grade':['X','X'],'color':['R','R'],'group_type':['통합 그룹']*2})
        e = pd.DataFrame({'day':dates,'sensor_id':'400','kwh':[10.,10.,10.,None,10.,10.,10.,10.]})
        h = pd.DataFrame([{'day':d,'line':line,'hour_of_day':f'{hour:02d}','operating_minutes':60,
                           'samples':60,'invalid_rpm':0,'downtime_minutes':0,'minor_stop_minutes':0}
                          for d in dates for line in FACTORY_LINES['1'] for hour in range(24)
                          if not (d.day==3 and line=='01' and hour==0)])
        staff=pd.DataFrame([{'p_date':d,'fac_cd':'1','juya':j,'jo':crew,'tot_cnt':str(n),'cur_cnt':str(n),'abs_cnt':'0'}
                            for d,n in [('2026.01.01',2),('2026.01.08',3)] for j,crew in [('1','A'),('2','B')]])
        return r,e,h,staff

    def test_daily_weekly_monthly_conservation_and_weighted_ratios(self):
        r,e,h,staff=self.fixture()
        m,_,g,_,ledger,weeks=build_period_report(r,e,h,staff,'2026-01-08',sorted(FACTORY_LINES['1']),kind='월간')
        self.assertEqual(m['total'],300)
        self.assertEqual(ledger['생산량_kg'].sum(),weeks['생산량_kg'].sum())
        self.assertEqual(m['energy'],70)
        self.assertEqual(ledger['전력량_kWh'].sum(),weeks['전력량_kWh'].sum())
        self.assertEqual(m['average_lot'],150)  # two daily LOT occurrences
        self.assertEqual(m['integrated_lot'],150)
        self.assertEqual(g['생산량_kg'].sum(),300)
        self.assertEqual(m['people'],10)  # 2+2+3+3, not daily people averaged
        self.assertEqual(m['per_person'],30)
        self.assertEqual(m['working_days'],2)
        self.assertEqual(m['utilization_status'],'잠정')
        self.assertAlmostEqual(m['utilization'],(8*8*1440-60)/(8*8*1440)*100)
        self.assertEqual(m['expected_minutes'],8*8*1440)
        self.assertEqual(m['holiday_days'],0)
        self.assertEqual(m['missing_runtime_minutes'],60)
        self.assertTrue(m['period_incomplete'])
        self.assertTrue(pd.isna(weeks.loc[2,'생산량_kg']))

    def test_week_filter_and_no_future_zeroes(self):
        r,e,h,staff=self.fixture()
        m,b,g,t,ledger,w=build_period_report(r,e,h,staff,'2026-01-08',['01'],kind='주간',week_number=2)
        self.assertEqual(m['total'],200)
        self.assertEqual(m['energy'],10)
        self.assertEqual(m['aggregated_days'],1)
        self.assertEqual(m['range_end'],'2026-01-14')
        self.assertEqual(g['생산량_kg'].sum(),200)
        html=render_period_html(m,b,g,ledger,w)
        self.assertIn('2026-01-08 ~ 2026-01-14',html)
        self.assertIn('일간 원장',html)
        empty,*_=build_period_report(r,e,h,staff,'2026-01-08',['01'],kind='주간',week_number=5)
        self.assertEqual(empty['aggregated_days'],0)
        self.assertIsNone(empty['energy'])
        self.assertFalse(empty['source_present'])

    def test_crew_filter_carries_into_rollup(self):
        r,e,h,staff=self.fixture()
        m,_,_,t,_,w=build_period_report(r,e,h,staff,'2026-01-08',sorted(FACTORY_LINES['1']),kind='월간',crew='A')
        self.assertEqual(m['total'],100)
        self.assertEqual(w['생산량_kg'].sum(),100)
        self.assertEqual(t.columns.tolist(),['A조'])
        self.assertEqual(m['energy'],70)


    def test_no_production_day_is_included_in_mes_denominator(self):
        r,e,h,staff=self.fixture()
        m,*_=build_period_report(r,e,h,staff,'2026-01-03',['01'])
        self.assertEqual(m['total'],0)
        self.assertFalse(m['runtime_holiday'])
        self.assertEqual(m['expected_minutes'],1440)
        self.assertEqual(m['missing_runtime_minutes'],60)
        self.assertAlmostEqual(m['utilization'],1380/1440*100)

    def test_completely_missing_day_is_excluded_and_keeps_unknown_status(self):
        r,e,h,staff=self.fixture()
        h=h[~h['day'].eq(pd.Timestamp('2026-01-03'))]
        m,*_=build_period_report(r,e,h,staff,'2026-01-03',['01'])
        self.assertIsNone(m['utilization'])
        self.assertEqual(m['utilization_status'],'기록 없음')
        self.assertEqual(m['missing_runtime_minutes'],0)
        self.assertEqual(m['expected_minutes'],0)
        self.assertEqual(m['runtime_missing_line_days'],1)

    def test_period_numerators_denominators_conserve_all_days(self):
        r,e,h,staff=self.fixture()
        m,_,_,_,ledger,weeks=build_period_report(r,e,h,staff,'2026-01-08',['01'],kind='월간')
        self.assertEqual(m['expected_minutes'],8*1440)
        self.assertEqual(m['holiday_days'],0)
        self.assertEqual(ledger['가동률기준분'].sum(),m['expected_minutes'])
        self.assertEqual(weeks['가동률기준분'].sum(),m['expected_minutes'])
        self.assertEqual(weeks['가동분'].sum(),m['operating_minutes'])
        self.assertEqual(weeks['누락분'].sum(),m['missing_runtime_minutes'])

    def active_fixture(self):
        rows=[]
        for day in pd.to_datetime(['2026-01-01','2026-01-02']):
            for line in ['01','02']:
                operating=50 if day.day==1 and line=='01' else 30 if day.day==2 and line=='02' else 0
                for hour in range(24):
                    rows.append(dict(day=day,line=line,hour_of_day=f'{hour:02}',samples=60,
                                     operating_minutes=operating,downtime_minutes=60-operating,
                                     minor_stop_minutes=0,invalid_rpm=0))
        return pd.DataFrame(rows)

    def test_only_running_lines_are_included_but_their_stops_remain(self):
        h=self.active_fixture()
        m=runtime_metrics(h,'2026-01-01',['01','02'])
        self.assertAlmostEqual(m['utilization'],20/24*100)
        self.assertEqual(m['operating_minutes'],1200)
        self.assertEqual(m['downtime_minutes'],240)
        self.assertEqual(m['runtime_expected_minutes'],1440)
        self.assertEqual(m['runtime_active_lines'],['01'])
        self.assertEqual(m['runtime_excluded_lines'],['02'])
        self.assertEqual(m['utilization_status'],'확정')
        self.assertEqual([row['호기'] for row in m['runtime_active_records']],['01'])

    def test_period_selects_running_pairs_each_day_not_union_of_lines(self):
        h=self.active_fixture()
        records=[{'day':'2026-01-01'},{'day':'2026-01-02'}]
        m=period_runtime_metrics(h,records,['01','02'])
        self.assertEqual(m['runtime_active_line_days'],2)
        self.assertEqual(m['runtime_expected_minutes'],2880)
        self.assertEqual(m['operating_minutes'],1200+720)
        self.assertAlmostEqual(m['utilization'],(1200+720)/2880*100)
        self.assertEqual([(r['생산일'],r['호기']) for r in m['runtime_active_records']],
                         [('2026-01-01','01'),('2026-01-02','02')])

    def test_unknown_excluded_lines_are_not_confirmed_downtime(self):
        h=self.active_fixture()
        m=runtime_metrics(h[h['line']=='01'],'2026-01-01',['01','02'])
        self.assertEqual(m['runtime_expected_minutes'],1440)
        self.assertEqual(m['runtime_coverage'],100.)
        self.assertEqual(m['runtime_missing_line_days'],1)
        self.assertEqual(m['runtime_unknown_line_days'],1)
        self.assertEqual(m['utilization_status'],'잠정')
        empty=runtime_metrics(h,'2026-01-01',['02'])
        self.assertIsNone(empty['utilization'])
        self.assertEqual(empty['runtime_expected_minutes'],0)
        self.assertEqual(empty['utilization_status'],'가동 이력 없음')

if __name__=='__main__':
    unittest.main()
