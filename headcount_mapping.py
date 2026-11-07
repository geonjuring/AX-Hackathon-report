"""Build a calculation-only crew mapping without modifying attendance source rows."""
import pandas as pd


def applied_headcounts(rows):
    values = rows[['tot_cnt','cur_cnt','abs_cnt']].apply(pd.to_numeric,errors='coerce').astype('float64')
    base = values[['tot_cnt','abs_cnt']]
    valid = (base.notna().all(axis=1) & base.ge(0).all(axis=1) & base.mod(1).eq(0).all(axis=1) &
             values['abs_cnt'].le(values['tot_cnt']))
    # With no absence, a conflicting headcount can mean the total itself is wrong (e.g. 90 vs 9).
    valid &= values['abs_cnt'].gt(0) | values['cur_cnt'].isna() | values['cur_cnt'].eq(values['tot_cnt'])
    return (values['tot_cnt']-values['abs_cnt']).where(valid)


def map_headcount_crews(rows, production=None):
    result = rows.copy().reset_index(drop=True)
    result['jo_original'] = result['jo']
    result['jo_normalized'] = result['jo']
    factory2 = result['fac_cd'].eq('2')
    result.loc[factory2,'jo_normalized'] = result.loc[factory2,'jo'].replace({'D':'A','E':'B'})
    result['production_crew'] = ''
    result['jo_effective'] = ''
    result['crew_mapping_status'] = ''
    evidence = {}
    if production is not None and 'juya' in production:
        dates = pd.to_datetime(result['p_date'],format='%Y.%m.%d',errors='coerce')
        data = production[production['day'].isin(dates)].copy()
        data['fac_cd'] = data['line'].map(lambda line:'1' if int(line)<=8 else '2')
        for key, group in data.groupby(['day','fac_cd','juya']):
            evidence[key] = set(group['jo'].fillna(''))
    for idx,row in result.iterrows():
        normalized = row['jo_normalized']
        if normalized not in ['A','B']:
            result.loc[idx,'crew_mapping_status'] = '보류: 알 수 없는 원본 조 코드'
            continue
        if production is None:
            # Standalone callers can use the explicitly authorized default mapping.
            result.loc[idx,'jo_effective'] = normalized
            result.loc[idx,'crew_mapping_status'] = '기본 통일 (실적 대조 미실시)'
            continue
        key = (pd.to_datetime(row['p_date'],format='%Y.%m.%d',errors='coerce'),row['fac_cd'],row['juya'])
        actual = evidence.get(key,set())
        result.loc[idx,'production_crew'] = ','.join(sorted(actual))
        if len(actual)!=1 or not actual.issubset({'A','B'}):
            result.loc[idx,'crew_mapping_status'] = '보류: 생산실적 없음 또는 조 불명확'
            continue
        effective = next(iter(actual))
        result.loc[idx,'jo_effective'] = effective
        result.loc[idx,'crew_mapping_status'] = '실적 기준 보정' if effective!=normalized else '대응 일치'
    return result


def mapping_records(rows):
    result = []
    duplicate = rows.duplicated(['p_date','fac_cd','juya'],keep=False)
    applied = applied_headcounts(rows)
    for idx,row in rows.iterrows():
        valid = bool(pd.notna(applied.loc[idx]) and not duplicate.loc[idx])
        original = pd.to_numeric(row['cur_cnt'],errors='coerce')
        corrected = valid and (pd.isna(original) or original != applied.loc[idx])
        result.append({'생산일':row['p_date'],'공장':row['fac_cd']+'공장',
             '주야간':{'1':'주간 근무','2':'야간 근무'}.get(row['juya'],'확인 필요'),
             '원본조':row['jo_original'],'통일조':row['jo_normalized'],
             '생산실적조':row['production_crew'],'적용조':row['jo_effective'],
             '처리상태':row['crew_mapping_status'],'일보총원':row['tot_cnt'],
             '일보현원':row['cur_cnt'],'일보결원':row['abs_cnt'],
             '적용인원':int(applied.loc[idx]) if valid else None,
             '인원검증':'총원-결원 보정' if corrected else '정상' if valid else '총원·결원·중복 확인'})
    return result
