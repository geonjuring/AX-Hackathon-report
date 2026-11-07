import unittest

import pandas as pd

from billing_allocation import make_allocation, apply_billing_basis, allocation_by_line
from period_reporting import build_period_report, render_period_html
import test_period_reporting


class BillingTests(unittest.TestCase):
    def fixture(self, target=100.):
        dates = pd.date_range('2026-01-01', '2026-01-31')
        calendar = pd.DataFrame([
            {'day':d, 'sensor_id':sensor, 'kwh':value, 'intervals':96, 'invalid_intervals':0}
            for d in dates for sensor,value in [('400',1.),('503',2.),('217',500.)]
        ])
        bills = pd.DataFrame({'청구월':['2026-01','2026-11'], '사용량_kWh':[target,1000.]})
        return calendar,bills

    def test_exact_bill_above_and_below_observed_total(self):
        for target in [50.,100.,0.]:
            raw,bills = self.fixture(target)
            rows,summary = make_allocation(raw,bills)
            self.assertAlmostEqual(rows['allocated_kwh'].sum(),target)
            self.assertEqual(summary['청구차이_kWh'].tolist(),[0.])
            self.assertNotIn('217',rows['sensor_id'].unique())
            self.assertAlmostEqual(summary.iloc[0]['보정계수'],target/93.)
            daily = rows.groupby('day')['allocated_kwh'].sum()
            weeks = daily.groupby((daily.index.day-1)//7+1).sum()
            self.assertAlmostEqual(daily.sum(),weeks.sum())
            self.assertTrue(((rows['allocated_kwh']*10).round() - rows['allocated_kwh']*10).abs().lt(1e-8).all())
            pd.testing.assert_frame_equal(raw,self.fixture(target)[0])

    def test_zero_null_missing_date_and_unbilled_month(self):
        raw,bills = self.fixture()
        raw.loc[0,'kwh'] = 0.
        raw.loc[1,'kwh'] = None
        rows,_ = make_allocation(raw,bills)
        self.assertEqual(rows.iloc[0]['allocated_kwh'],0.)
        self.assertTrue(pd.isna(rows.iloc[1]['allocated_kwh']))
        raw = raw[raw['day'] != pd.Timestamp('2026-01-15')]
        rows,summary = make_allocation(raw,bills)
        self.assertTrue(rows['allocated_kwh'].isna().all())
        self.assertEqual(summary.iloc[0]['상태'],'일간 원본 확인 필요')
        raw,_ = self.fixture()
        raw['day'] = raw['day'] + pd.Timedelta(days=304)
        rows,summary = make_allocation(raw,bills)
        self.assertTrue(rows['allocated_kwh'].isna().all())

    def test_application_preserves_sources_and_reuses_same_daily_series(self):
        raw,bills=self.fixture()
        rows,summary=make_allocation(raw,bills)
        r,e,h,staff=test_period_reporting.PeriodTests().fixture()
        original=build_period_report(r,e,h,staff,'2026-01-08',['01'],kind='월간')
        m,b,g,t,ledger,weekly=apply_billing_basis(original,rows,summary,['01'])
        self.assertEqual(m['energy_basis'],'청구 기준 추정 배분')
        self.assertAlmostEqual(m['energy'],100.)
        self.assertNotIn('energy_basis',original[0])
        self.assertNotIn('전력실측_생산일_kWh',original[4])
        self.assertEqual(m['sensor_energy_0830'],70.)
        self.assertEqual(m['energy_period_start'],'2026-01-01 00:00')
        self.assertEqual(m['energy_period_end'],'2026-02-01 00:00')
        # Electricity covers the whole month; absent production remains empty.
        self.assertEqual(len(ledger),31)
        self.assertTrue(ledger.loc[ledger['생산일']>'2026-01-08','생산량_kg'].isna().all())
        self.assertAlmostEqual(ledger['전력량_kWh'].sum(),weekly['전력량_kWh'].sum())
        self.assertTrue(m['allocation_source_incomplete'])
        html=render_period_html(m,b,g,ledger,weekly)
        self.assertIn('청구 기준 추정',html)
        self.assertIn('2026-02-01 00:00',html)
        self.assertNotIn('선택 호기 실측',html)
        day_result=build_period_report(r,e,h,staff,'2026-01-08',['01'])
        dm,*_=apply_billing_basis(day_result,rows,summary,['01'])
        self.assertAlmostEqual(dm['energy'],ledger.loc[ledger['생산일']=='2026-01-08','전력량_kWh'].iloc[0])
        table=allocation_by_line(rows,m['range_start'],m['range_end'],['01'])
        self.assertAlmostEqual(table.iloc[0]['압출기_kWh'],m['extruder_energy'])
        other,*_=apply_billing_basis(day_result,rows,summary,['02'])
        self.assertEqual(other['energy'],dm['energy'])
        self.assertIsNone(other['extruder_energy'])
        day_result[0]['range_start']='2026-11-01'
        day_result[0]['range_end']='2026-11-01'
        fallback,*_=apply_billing_basis(day_result,rows,summary,['01'])
        self.assertEqual(fallback['energy_basis'],'센서 실측')
        self.assertEqual(fallback['energy'],day_result[0]['energy'])


if __name__ == '__main__':
    unittest.main()
