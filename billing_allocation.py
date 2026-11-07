"""An estimated accounting series. Never modifies source meter observations."""
from calendar import monthrange

import duckdb
import numpy as np
import pandas as pd

from production_report import DATA, GENERATION_SENSORS, EXTRUDER_SENSORS, HEATER_SENSORS, heater_sensor_ids


def make_allocation(calendar, bills):
    rows = calendar[~calendar['sensor_id'].isin(GENERATION_SENSORS)].copy()
    rows = rows.sort_values(['day', 'sensor_id']).reset_index(drop=True)
    rows['month'] = rows['day'].dt.strftime('%Y-%m')
    rows['allocated_kwh'] = np.nan
    rows['factor'] = np.nan
    if rows[['day','sensor_id']].duplicated().any() or rows['kwh'].dropna().lt(0).any():
        raise ValueError('배분 원본에 중복 센서·날짜 또는 음수 사용량이 있습니다.')
    summaries = []
    for bill in bills.to_dict('records'):
        month = str(bill['청구월'])
        if not '2026-01' <= month <= '2026-10':
            continue
        selected = rows[rows['month'] == month]
        begin = pd.Timestamp(month+'-01')
        end = begin.replace(day=monthrange(begin.year, begin.month)[1])
        target = float(bill['사용량_kWh'])
        expected_days = set(pd.date_range(begin,end))
        observed_days = set(selected.loc[selected['kwh'].notna(), 'day'])
        total = selected['kwh'].sum(min_count=1)
        valid = observed_days == expected_days and pd.notna(total) and total > 0 and np.isfinite(target) and target >= 0
        entry = {'청구월':month,'센서합계_kWh':float(total) if pd.notna(total) else None,
                 '한전청구량_kWh':target,'보정계수':None,'배분합계_kWh':None,'청구차이_kWh':None,
                 '상태':'일간 원본 확인 필요'}
        if valid:
            # Largest remainders preserve the bill exactly at the source's 0.1-kWh precision.
            target_units = int(round(target*10))
            if abs(target_units/10-target) > 1e-6:
                raise ValueError('청구 사용량은 0.1 kWh 단위여야 합니다.')
            weights = selected['kwh'].fillna(0).to_numpy(dtype=float)
            exact = weights/float(total)*target_units
            units = np.floor(exact).astype('int64')
            remainder = target_units-int(units.sum())
            eligible = np.flatnonzero(weights>0)
            order = eligible[np.argsort(-(exact[eligible]-units[eligible]),kind='stable')]
            units[order[:remainder]] += 1
            allocated = units.astype(float)/10
            allocated[selected['kwh'].isna().to_numpy()] = np.nan
            rows.loc[selected.index,'allocated_kwh'] = allocated
            rows.loc[selected.index,'factor'] = target/float(total)
            entry.update(보정계수=target/float(total),배분합계_kWh=float(np.nansum(allocated)),
                         청구차이_kWh=round(float(np.nansum(allocated))-target,6),상태='청구 기준 추정 배분')
        summaries.append(entry)
    return rows, pd.DataFrame(summaries)


def load_billing_allocation():
    with duckdb.connect() as con:
        con.execute('SET threads=2')
        con.read_csv(str(DATA/'01_분석용/EMS_전력계_15min집계.csv'),all_varchar=True).create_view('raw')
        calendar = con.sql("""
            SELECT date_trunc('day',try_cast(일시 AS TIMESTAMP)) AS day,
                   sensor_id, any_value(센서명) AS name,
                   sum(try_cast(사용량_kWh AS DOUBLE)) AS kwh,
                   count(*) AS intervals,
                   count(*) FILTER(WHERE try_cast(사용량_kWh AS DOUBLE) IS NULL) AS invalid_intervals
            FROM raw GROUP BY 1,2
        """).df()
    bills = pd.read_csv(DATA/'01_분석용/전력요금_월별청구_2026.csv',dtype={'청구월':str})
    return make_allocation(calendar,bills)


def allocated_sensor_sum(rows, ids, column='allocated_kwh'):
    values = rows.groupby('sensor_id')[column].sum(min_count=1)
    if not ids or any(s not in values.index or pd.isna(values[s]) for s in ids):
        return None
    return float(values.loc[list(ids)].sum())


def allocation_by_line(rows, start, end, lines):
    selected = rows[rows['day'].between(pd.Timestamp(start),pd.Timestamp(end))]
    result = []
    for line in lines:
        motor = [EXTRUDER_SENSORS[line]]
        heater = heater_sensor_ids(line)
        result.append({'호기':line,'압출기_kWh':allocated_sensor_sum(selected,motor),
                       '히터·부속_kWh':allocated_sensor_sum(selected,heater),
                       '호기합계_kWh':allocated_sensor_sum(selected,motor+heater),
                       '압출기센서':motor[0],'히터·부속센서':', '.join(heater),
                       '주의':'한전 청구 기준 추정 배분 · 00시 기준'})
    return pd.DataFrame(result)


def apply_billing_basis(result, rows, summaries, lines):
    m, byline, groups, trend, ledger, weekly = result
    m = m.copy(); ledger = ledger.copy(); weekly = weekly.copy()
    month = m['range_start'][:7]
    bill = summaries[summaries['청구월'] == month]
    m['energy_basis'] = '센서 실측'
    if bill.empty or bill.iloc[0]['상태'] != '청구 기준 추정 배분':
        m['energy_basis_note'] = '해당 월의 청구 자료 또는 배분용 일간 원본이 없어 센서 실측값을 유지합니다.'
        return m,byline,groups,trend,ledger,weekly
    bill = bill.iloc[0]
    month_rows = rows[rows['month'] == month]
    selected = rows[rows['day'].between(pd.Timestamp(m['range_start']),pd.Timestamp(m['range_end']))]
    m.update(energy_basis='청구 기준 추정 배분',billing_month=month,
             billing_month_usage=float(bill['한전청구량_kWh']),billing_factor=float(bill['보정계수']),
             sensor_energy_0830=m['energy'],calendar_meter_energy=float(selected['kwh'].sum(min_count=1)),
             sensor_energy_complete_0830=m['energy_complete'],
             sensor_missing_energy_sensors_0830=m['missing_energy_sensors'],
             energy_period_start=m['range_start']+' 00:00',
             energy_period_end=(pd.Timestamp(m['range_end'])+pd.Timedelta(days=1)).strftime('%Y-%m-%d 00:00'),
             allocation_source_incomplete=bool((month_rows['intervals']!=96).any() or month_rows['invalid_intervals'].gt(0).any() or
                                             month_rows.groupby('day')['sensor_id'].nunique().lt(75).any()),
             energy_basis_note='월 청구량을 관측된 센서·일별 사용 패턴에 비례 배분한 추정값입니다. 원본 결측이나 계량 범위 차이를 복원한 실측값이 아닙니다.')
    m['energy'] = float(selected['allocated_kwh'].sum(min_count=1))
    motors = [EXTRUDER_SENSORS[line] for line in lines]
    heaters = [sensor for line in lines for sensor in heater_sensor_ids(line)]
    for key,ids in [('extruder_energy',motors),('heater_energy',heaters),('line_energy',motors+heaters)]:
        m[key] = allocated_sensor_sum(selected,ids)
    m['energy_complete'] = True  # Complete allocation, not a claim of complete measurement.
    m['line_energy_complete'] = m['line_energy'] is not None
    m['extruder_share'] = m['extruder_energy']/m['energy']*100 if m['extruder_energy'] is not None and m['energy'] else None
    m['missing_energy_sensors'] = []
    # Electricity can cover calendar dates beyond the production snapshot.
    # Keep those dates in the common ledger with empty production fields.
    calendar_dates = pd.date_range(month_rows['day'].min(), month_rows['day'].max())
    ledger = ledger.set_index('생산일').reindex(calendar_dates.strftime('%Y-%m-%d'))
    ledger.index.name = '생산일'
    ledger = ledger.reset_index()
    ledger['주차'] = (pd.to_datetime(ledger['생산일']).dt.day-1)//7+1
    ledger['전력실측_생산일_kWh'] = ledger['전력량_kWh']
    ledger['압출기실측_생산일_kWh'] = ledger['압출기_kWh']
    daily = rows[rows['month']==month].groupby('day')['allocated_kwh'].sum(min_count=1)
    daily_raw = rows[rows['month']==month].groupby('day')['kwh'].sum(min_count=1)
    daily_motor = pd.Series({day:allocated_sensor_sum(day_rows,motors)
                             for day,day_rows in month_rows.groupby('day')})
    keys = pd.to_datetime(ledger['생산일'])
    ledger['전력량_kWh'] = keys.map(daily)
    ledger['압출기_kWh'] = keys.map(daily_motor)
    ledger['전력실측_달력일_kWh'] = keys.map(daily_raw)
    ledger['전력상태'] = '청구 기준 추정 배분'
    ledger['전력시간기준'] = '00:00~다음 날 00:00'
    for i,row in weekly.iterrows():
        days = ledger[ledger['생산일'].between(row['시작일'],row['종료일'])]
        for key in ['전력량_kWh','압출기_kWh']:
            weekly.loc[i,key] = days[key].sum(min_count=1)
        weekly.loc[i,'전력집계일수'] = int(days['전력량_kWh'].notna().sum())
        weekly.loc[i,'전력기준'] = '청구 기준 추정 배분'
        if row['상태']=='확정': weekly.loc[i,'상태']='추정 배분'
    m['month_reconciliation'] = {col:{'일간합계':float(ledger[col].sum(min_count=1)),
                                           '주간합계':float(weekly[col].sum(min_count=1))}
                                 for col in ['전력량_kWh','압출기_kWh','생산량_kg']}
    return m,byline,groups,trend,ledger,weekly
