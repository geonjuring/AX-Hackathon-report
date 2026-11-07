import unittest
import pandas as pd
from crew_schedule import build_crew_schedule, shift_labels


class CrewScheduleTests(unittest.TestCase):
    def fixture(self):
        return pd.DataFrame({'day':pd.to_datetime(['2026-10-28']*4+['2026-11-02']*4+['2026-11-04']),
            'line':['01','01','11','11']*2+['01'],
            'jo':['A','B','A','B']*2+['B'], 'juya':['1','2','1','2','2','1','2','1','1']})

    def test_rotating_crews_and_factory_scope(self):
        d=self.fixture()
        first=build_crew_schedule(d,'2026-10-28','2026-10-28',['01','11'])
        self.assertEqual(first['A조'].tolist(),['주간','주간'])
        self.assertEqual(first['B조'].tolist(),['야간','야간'])
        second=build_crew_schedule(d,'2026-11-02','2026-11-02',['02'])
        self.assertEqual(second['공장'].tolist(),['1공장'])
        self.assertEqual(shift_labels(second),{'A':'야간','B':'주간'})

    def test_missing_is_not_inferred_from_the_other_crew(self):
        d=self.fixture()
        result=build_crew_schedule(d,'2026-11-04','2026-11-04',['01','11'])
        self.assertEqual(result.iloc[0]['A조'],'생산실적 없음')
        self.assertEqual(result.iloc[0]['B조'],'주간')
        self.assertEqual(result.iloc[1]['B조'],'생산실적 없음')
        unknown=build_crew_schedule(d.drop(columns=['juya']),'2026-10-28','2026-10-28',['01'])
        self.assertEqual(unknown.iloc[0]['A조'],'주야 미기록')

    def test_mixed_and_invalid_records_remain_visible(self):
        d=self.fixture().iloc[:4].copy()
        d=pd.concat([d,d.iloc[[0]].assign(juya='2'),d.iloc[[0]].assign(juya='9')])
        result=build_crew_schedule(d,'2026-10-28','2026-10-28',['01','11'])
        self.assertIn('주간·야간',result.iloc[0]['A조'])
        self.assertIn('확인',result.iloc[0]['확인사항'])
        self.assertEqual(shift_labels(result)['A'],'공장별 확인')

    def test_dates_without_records_are_retained(self):
        result=build_crew_schedule(self.fixture(),'2026-10-28','2026-10-30',['01','11'])
        self.assertEqual(len(result),6)
        self.assertEqual(result.loc[result['생산일']=='2026-10-29','A조'].tolist(),['생산실적 없음']*2)


if __name__=='__main__': unittest.main()
