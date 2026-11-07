"""Crew shifts observed in production records, without inferring missing shifts."""
import pandas as pd


def build_crew_schedule(production, start, end, lines):
    factories = sorted({1 if int(line)<=8 else 2 for line in lines})
    data = production.copy()
    data['factory'] = data['line'].map(lambda line: 1 if int(line)<=8 else 2)
    rows = []
    for day in pd.date_range(start, end):
        for factory in factories:
            subset = data[data['day'].eq(day) & data['factory'].eq(factory)]
            entry = {'생산일':day.strftime('%Y-%m-%d'), '공장':f'{factory}공장'}
            notes = []
            for crew in ['A','B']:
                observed = subset[subset['jo'].eq(crew)]
                if observed.empty:
                    label = '생산실적 없음'
                elif 'juya' not in observed:
                    label = '주야 미기록'
                else:
                    codes = set(observed['juya'].fillna('').astype(str))
                    known = [name for code,name in [('1','주간'),('2','야간')] if code in codes]
                    label = '·'.join(known) if known else '주야 미기록'
                    if codes - {'1','2'}:
                        label += ' (미확인 기록 포함)' if known else ''
                        notes.append(f'{crew}조 주야 코드 확인 필요')
                    if {'1','2'}.issubset(codes):
                        notes.append(f'{crew}조 주간·야간 기록 혼재')
                entry[crew+'조'] = label
            if subset.empty:
                notes.append('생산실적이 없어 주야 확인 불가')
            if entry['A조'] == entry['B조'] and entry['A조'] in ['주간','야간']:
                notes.append('A/B조의 주야 기록이 같아 확인 필요')
            entry['확인사항'] = '; '.join(notes)
            rows.append(entry)
    return pd.DataFrame(rows,columns=['생산일','공장','A조','B조','확인사항'])


def shift_labels(schedule):
    result = {}
    for crew in ['A','B']:
        labels = schedule[crew+'조'].unique()
        result[crew] = str(labels[0]) if len(labels)==1 else '날짜별 확인' if schedule['생산일'].nunique()>1 else '공장별 확인'
    return result
