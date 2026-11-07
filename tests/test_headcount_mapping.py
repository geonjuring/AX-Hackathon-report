import unittest
import pandas as pd
from headcount_mapping import map_headcount_crews, mapping_records
from production_report import FACTORY_LINES, headcount_metrics


class HeadcountMappingTests(unittest.TestCase):
    def fixture(self):
        staff=pd.DataFrame({'p_date':['2026.10.28']*4,'fac_cd':['1','1','2','2'],
            'juya':['1','2','1','2'],'jo':['A','B','D','E'],
            'tot_cnt':['10','10','9','9'],'cur_cnt':['10','10','9','9'],'abs_cnt':['0']*4})
        production=pd.DataFrame({'day':pd.to_datetime(['2026-10-28']*4),
            'line':['01','01','11','11'],'juya':['1','2','1','2'],'jo':['A','B','A','B']})
        return staff,production

    def test_default_mapping_and_originals_are_preserved(self):
        staff,production=self.fixture();original=staff.copy(deep=True)
        result=map_headcount_crews(staff,production)
        self.assertEqual(result['jo_original'].tolist(),['A','B','D','E'])
        self.assertEqual(result['jo_effective'].tolist(),['A','B','A','B'])
        pd.testing.assert_frame_equal(staff,original)
        lines=sorted(FACTORY_LINES['1']|FACTORY_LINES['2'])
        for crew in ['A','B']:
            m=headcount_metrics(staff,'2026-10-28',lines,crew=crew,production=production)
            self.assertEqual(m['people'],19)
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,production=production)['people'],38)

    def test_reversed_records_follow_same_date_factory_shift_production(self):
        staff,production=self.fixture()
        production['jo']=['B','A','B','A']
        staff.loc[3,['tot_cnt','cur_cnt']]=['8','8']
        result=map_headcount_crews(staff,production)
        self.assertEqual(result['jo_effective'].tolist(),['B','A','B','A'])
        self.assertTrue(result['crew_mapping_status'].eq('실적 기준 보정').all())
        self.assertEqual(result['cur_cnt'].tolist(),['10','10','9','8'])
        lines=sorted(FACTORY_LINES['1']|FACTORY_LINES['2'])
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='A',production=production)['people'],18)
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='B',production=production)['people'],19)

    def test_absent_or_ambiguous_evidence_holds_crew_count_but_not_total(self):
        staff,production=self.fixture();lines=sorted(FACTORY_LINES['1']|FACTORY_LINES['2'])
        production=production.iloc[:3]
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='A',production=production)['people'],19)
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='B',production=production)['people'],0)
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,production=production)['people'],38)
        mixed=pd.concat([production,production.iloc[[2]].assign(jo='B')])
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='A',production=mixed)['people'],0)
        # Matching on another date is never allowed.
        wrong_date=production.assign(day=pd.Timestamp('2026-10-27'))
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='A',production=wrong_date)['people'],0)

    def test_mapping_does_not_bypass_count_validation_or_allocate_individual_lines(self):
        staff,production=self.fixture();lines=sorted(FACTORY_LINES['1']|FACTORY_LINES['2'])
        staff.loc[2,'cur_cnt']='10'
        self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,crew='A',production=production)['people'],0)
        self.assertEqual(headcount_metrics(staff,'2026-10-28',['01'],crew='A',production=production)['people'],0)
        self.assertEqual(headcount_metrics(staff,'2026-10-28',['01'],manual_people=3,crew='A',production=production)['people'],3)
        original,production=self.fixture()
        duplicated=pd.concat([original,original.iloc[[0]]])
        self.assertEqual(headcount_metrics(duplicated,'2026-10-28',lines,crew='A',production=production)['people'],0)

    def test_absence_is_deducted_even_when_original_current_was_not_updated(self):
        staff,production=self.fixture();original=staff.copy(deep=True)
        staff.loc[0,'abs_cnt']='1'
        lines=sorted(FACTORY_LINES['1']|FACTORY_LINES['2'])
        m=headcount_metrics(staff,'2026-10-28',lines,crew='A',production=production)
        self.assertEqual(m['people'],18)  # 1 factory: 10-1, 2 factory: 9-0
        audit=m['people_mapping_records'][0]
        self.assertEqual(audit['일보현원'],'10')
        self.assertEqual(audit['적용인원'],9)
        self.assertEqual(audit['인원검증'],'총원-결원 보정')
        self.assertEqual(staff.loc[0,'cur_cnt'],original.loc[0,'cur_cnt'])

    def test_unusable_total_or_absence_still_holds_calculation(self):
        lines=sorted(FACTORY_LINES['1']|FACTORY_LINES['2'])
        for field,value in [('abs_cnt',''),('abs_cnt','-1'),('abs_cnt','11'),('abs_cnt','1.5'),('tot_cnt','90')]:
            staff,production=self.fixture()
            staff.loc[0,field]=value
            self.assertEqual(headcount_metrics(staff,'2026-10-28',lines,production=production)['people'],0,(field,value))


if __name__=='__main__': unittest.main()
