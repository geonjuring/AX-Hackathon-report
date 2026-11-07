"""Aggregate report daily values for chart axes without averaging daily rates."""
import pandas as pd


def period_labels(dates, unit):
    dates = pd.to_datetime(dates)
    if unit == '월별':
        return dates.dt.strftime('%Y-%m')
    if unit == '주별':
        return dates.dt.strftime('%Y-%m') + ' ' + ((dates.dt.day - 1) // 7 + 1).astype(str) + '주차'
    return dates.dt.strftime('%Y-%m-%d')


def aggregate_chart_data(ledger, active_records, unit):
    daily = ledger.copy()
    daily['기간'] = period_labels(daily['생산일'], unit)
    columns = ['생산량_kg', 'A조_kg', 'B조_kg', '전력량_kWh', 'LOT수', '통합묶음수']
    totals = daily.groupby('기간', sort=True)[columns].sum(min_count=1).reset_index()
    active = pd.DataFrame(active_records)
    if active.empty:
        return totals, pd.DataFrame(columns=['기간', '호기', '가동분', '누락분', '가동률_%'])
    active['기간'] = period_labels(active['생산일'], unit)
    grouped = active.groupby(['기간', '호기'], sort=True).agg(
        가동분=('가동분', 'sum'), 누락분=('누락분', 'sum'), 가동일=('생산일', 'size')).reset_index()
    grouped['가동률_%'] = grouped['가동분'] / (grouped['가동일'] * 1440) * 100
    return totals, grouped


def line_energy_efficiency(power, production, start, end):
    """Use all crews for each line because meters are not split by crew."""
    selected = production[production['day'].between(pd.Timestamp(start), pd.Timestamp(end))]
    quantities = selected.groupby('line')['qty'].sum().rename('생산량_kg')
    result = power.merge(quantities, left_on='호기', right_index=True, how='left')
    result['생산량_톤'] = result['생산량_kg'] / 1000
    result['톤당전력량'] = result['호기합계_kWh'].div(result['생산량_톤'].where(result['생산량_톤'] > 0))
    result['호기번호'] = pd.to_numeric(result['호기'])
    result['호기명'] = result['호기번호'].astype(str) + '호기'
    return result.sort_values('호기번호')


def build_chart_history(production, energy, runtime, allocation, bills, lines, crew, year):
    """Build only chart inputs with grouped data; skip personnel/report rendering."""
    from production_report import GENERATION_SENSORS

    months = sorted(production.loc[production['day'].dt.year == year, 'day'].dt.month.unique())
    latest = max(frame['day'].max() for frame in [production, energy, runtime] if not frame.empty)
    calendar_dates = []
    for month in months:
        start = pd.Timestamp(year=year, month=int(month), day=1)
        end = start + pd.offsets.MonthEnd(0)
        bill = bills[bills['청구월'] == start.strftime('%Y-%m')]
        allocated_month = allocation[allocation['month'] == start.strftime('%Y-%m')]
        if not bill.empty and bill.iloc[0]['상태'] == '청구 기준 추정 배분':
            calendar_dates.extend(pd.date_range(allocated_month['day'].min(), allocated_month['day'].max()))
        elif start <= latest:
            calendar_dates.extend(pd.date_range(start, min(end, latest)))
    dates = pd.DatetimeIndex(calendar_dates, name='day')
    result = pd.DataFrame(index=dates)
    chosen = production[production['line'].isin(lines)]
    selected = chosen if crew == '전체' else chosen[chosen['jo'] == crew]
    positive = selected[selected['qty'] > 0]
    result['생산량_kg'] = positive.groupby('day')['qty'].sum().reindex(dates, fill_value=0)
    for code in ['A', 'B']:
        result[code + '조_kg'] = chosen[chosen['jo'] == code].groupby('day')['qty'].sum().reindex(dates, fill_value=0)
    result['LOT수'] = positive.drop_duplicates(['day','line','lot_no']).groupby('day').size().reindex(dates, fill_value=0)
    result['통합묶음수'] = positive.groupby('day')['run'].nunique().reindex(dates, fill_value=0)
    # Billing can extend beyond the production snapshot; keep unknown future values empty.
    result.loc[result.index > latest, ['생산량_kg','A조_kg','B조_kg','LOT수','통합묶음수']] = float('nan')
    meters = energy[~energy['sensor_id'].isin(GENERATION_SENSORS)]
    expected = meters['sensor_id'].nunique()
    measured = meters.groupby('day')['kwh'].agg(['sum','count'])
    result['전력량_kWh'] = measured['sum'].where(measured['count'] == expected).reindex(dates)
    valid_months = bills.loc[bills['상태'] == '청구 기준 추정 배분', '청구월']
    allocated = allocation[allocation['month'].isin(valid_months)].groupby('day')['allocated_kwh'].sum(min_count=1)
    billed_dates = result.index.strftime('%Y-%m').isin(valid_months)
    result.loc[billed_dates, '전력량_kWh'] = allocated.reindex(result.index[billed_dates]).to_numpy()
    hours = runtime[runtime['day'].isin(dates) & runtime['line'].isin(lines)]
    active = hours.groupby(['day','line'])[['operating_minutes','samples']].sum()
    active = active[active['operating_minutes'] > 0].reset_index()
    records = pd.DataFrame({
        '생산일': active['day'].dt.strftime('%Y-%m-%d'), '호기':active['line'],
        '가동분': active['operating_minutes'].astype(int), '확보분':active['samples'].astype(int),
        '누락분': (1440-active['samples']).clip(lower=0).astype(int),
        '가동률_%':active['operating_minutes']/1440*100,
    })
    result.index = result.index.strftime('%Y-%m-%d')
    return result.rename_axis('생산일').reset_index(), records.to_dict('records')
