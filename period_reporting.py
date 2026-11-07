"""Calendar-month week bins, aggregated from the same daily report records."""
from calendar import monthrange

import pandas as pd
from crew_schedule import build_crew_schedule, shift_labels

from production_report import summarize, save_report, energy_by_line, period_runtime_metrics


def period_bounds(reference, kind='일간', week_number=None):
    day = pd.Timestamp(reference).normalize()
    last = monthrange(day.year, day.month)[1]
    if kind == '일간':
        return day, day
    if kind == '월간':
        return day.replace(day=1), day.replace(day=last)
    if kind != '주간':
        raise ValueError('집계 단위는 일간·주간·월간 중에서 선택하세요.')
    week = week_number if week_number is not None else (day.day - 1) // 7 + 1
    if not 1 <= week <= (last - 1) // 7 + 1:
        raise ValueError('해당 월에 없는 주차입니다.')
    return day.replace(day=(week - 1)*7 + 1), day.replace(day=min(week*7, last))


def known_sum(records, key):
    values = [x[key] for x in records if x[key] is not None]
    return float(sum(values)) if values else None


def build_period_report(r, e, runtime, headcount, reference, lines, crew='전체',
                        kind='일간', week_number=None, people=0):
    start, end = period_bounds(reference, kind, week_number)
    month_start, month_end = period_bounds(reference, '월간')
    candidates = [v for v in [r['day'].max(), e['day'].max(), runtime['day'].max()] if pd.notna(v)]
    if not candidates:
        raise ValueError('집계할 원본 날짜가 없습니다.')
    latest = max(candidates)
    data_end = min(month_end, latest)
    dates = pd.date_range(month_start, data_end)
    reports = []
    for day in dates:
        # A period's manual headcount must never be copied into each individual day.
        manual = people if kind == '일간' and day == start else 0
        reports.append(summarize(r, e, day, lines, people=manual, runtime=runtime,
                                 headcount=headcount, crew=crew))
    daily = [x[0] for x in reports]
    selected = [x for x in reports if start <= pd.Timestamp(x[0]['day']) <= end]
    records = [x[0] for x in selected]
    ledger = pd.DataFrame([{
        '생산일': x['day'], '주차': (pd.Timestamp(x['day']).day-1)//7+1,
        '생산량_kg': x['total'], 'A조_kg': x['a'], 'B조_kg': x['b'],
        '전력량_kWh': x['energy'], '압출기_kWh': x['extruder_energy'],
        'LOT수': x['lots'], '통합묶음수': x['integrated_groups']+x['standalone_lots'],
        '가동률_%': x['utilization'], '가동분': x['operating_minutes'],
        '누락분': x['missing_runtime_minutes'], '기록인원_명교대': x['people'] or None,
        '생산실적있음': x['source_present'], '가동률상태': x['utilization_status'],
        '휴무일': x['runtime_holiday'], '가동률기준분': x['runtime_expected_minutes'],
        '가동률기록분': x['observed_runtime_minutes'],
        '가동호기수':x['runtime_active_line_days'], '제외호기수':x['runtime_excluded_line_days'],
        '가동여부미확인호기수':x['runtime_unknown_line_days'], '가동호기':', '.join(x['runtime_active_lines']),
        '전력상태': '확정' if x['energy_complete'] else ('잠정' if x['energy'] is not None else '미산출'),
    } for x in daily], columns=['생산일','주차','생산량_kg','A조_kg','B조_kg','전력량_kWh','압출기_kWh',
                                'LOT수','통합묶음수','가동률_%','가동분','누락분','기록인원_명교대',
                                '생산실적있음','가동률상태','휴무일','가동률기준분','가동률기록분','가동호기수','제외호기수','가동여부미확인호기수','가동호기','전력상태'])
    weeks = []
    for w in range(1, (month_end.day-1)//7+2):
        ws, we = period_bounds(reference, '주간', w)
        entries = [x for x in daily if ws <= pd.Timestamp(x['day']) <= we]
        week_runtime = period_runtime_metrics(runtime, entries, lines, complete=len(entries)==(we-ws).days+1)
        weeks.append({'주차': f'{w}주차 ({ws.day}~{we.day}일)', '시작일': ws.strftime('%Y-%m-%d'),
                      '종료일': we.strftime('%Y-%m-%d'), '집계일수': len(entries),
                      '생산량_kg': known_sum(entries, 'total'),
                      'A조_kg': known_sum(entries, 'a'), 'B조_kg': known_sum(entries, 'b'),
                      '전력량_kWh': known_sum(entries, 'energy'),
                      '압출기_kWh': known_sum(entries, 'extruder_energy'),
                      '가동률기준일수': week_runtime['runtime_working_days'],
                      '휴무일수': week_runtime['holiday_days'],
                      '가동호기일수':week_runtime['runtime_active_line_days'],
                      '제외호기일수':week_runtime['runtime_excluded_line_days'],
                      '가동여부미확인호기일수':week_runtime['runtime_unknown_line_days'],
                      '가동률기준분': week_runtime['runtime_expected_minutes'],
                      '가동분': week_runtime['operating_minutes'],
                      '누락분': week_runtime['missing_runtime_minutes'],
                      '가동률_%': week_runtime['utilization'],
                      '가동률상태': week_runtime['utilization_status'],
                      '상태': ('자료 없음' if not entries else
                               '확정' if len(entries)==(we-ws).days+1 and
                               all(x['energy_complete'] and x['utilization_status'] in ['확정','휴무'] for x in entries)
                               else '잠정')})
    weekly = pd.DataFrame(weeks)
    if kind == '일간':
        m, byline, groups, trend = selected[0] if selected else summarize(r, e, start, lines, people=people, runtime=runtime, headcount=headcount, crew=crew)
    else:
        base, _, empty_groups, _ = summarize(r, e, start, lines, runtime=runtime, headcount=headcount, crew=crew)
        m = base.copy()
        for key in ['total', 'a', 'b', 'other', 'lots', 'integrated_groups', 'standalone_lots']:
            m[key] = sum(x[key] for x in records)
        m['month_total'] = sum(x['total'] for x in daily)
        m['working_days'] = sum(x['total'] > 0 for x in records)
        m['daily_average'] = m['total']/m['working_days'] if m['working_days'] else None
        m['average_lot'] = m['total']/m['lots'] if m['lots'] else None
        count = m['integrated_groups']+m['standalone_lots']
        m['integrated_lot'] = m['total']/count if count else None
        for key in ['energy', 'extruder_energy', 'heater_energy', 'line_energy']:
            m[key] = known_sum(records, key)
        m['energy_complete'] = bool(records) and len(records)==(end-start).days+1 and all(x['energy_complete'] for x in records)
        m['line_energy_complete'] = bool(records) and len(records)==(end-start).days+1 and all(x['line_energy_complete'] for x in records)
        m['extruder_share'] = m['extruder_energy']/m['energy']*100 if m['extruder_energy'] is not None and m['energy'] else None
        m['missing_energy_sensors'] = sorted({s for x in records for s in x['missing_energy_sensors']})
        m.update(period_runtime_metrics(runtime, records, lines, complete=len(records)==(end-start).days+1))
        contributing = [x for x in records if x['total'] > 0]
        valid_staff = bool(contributing) and all(x['people']>0 for x in contributing)
        m['people'] = int(people) if people>0 else sum(x['people'] for x in contributing) if valid_staff else 0
        m['people_source'] = '기간 누적 사용자 입력' if people>0 else '생산일별 일보 인원 누적' if valid_staff else '기간 인원 확인 필요'
        m['people_note'] = '기간 중 생산량이 있는 날짜의 (총원-결원) 인원 합계(명·교대)입니다. 일별 인원 미확인 시 임의 합산하지 않습니다.'
        m['people_note'] += ' 일별 인원은 원본 보존 후 D→A·E→B 통일 및 같은 날짜·공장·주야간 생산실적 기준 조 보정을 적용합니다.'
        m['people_mapping_records'] = [row for x in records for row in x.get('people_mapping_records',[])]
        m['people_mapping_corrections'] = sum(x.get('people_mapping_corrections',0) for x in records)
        m['people_mapping_held'] = sum(x.get('people_mapping_held',0) for x in records)
        m['people_missing_dates'] = [x['day'] for x in contributing if not x['people']]
        if not people and m['people_missing_dates']:
            m['people_note'] += ' 인원 확인이 필요한 생산일: ' + ', '.join(m['people_missing_dates'])
        m['per_person'] = m['total']/m['people'] if m['people'] else None
        m['source_present'] = any(x['source_present'] for x in records)
        line_frames = [x[1] for x in selected if not x[1].empty]
        if line_frames:
            byline = pd.concat(line_frames).groupby('라인',as_index=False)[['생산량_kg','LOT수']].sum()
            byline['평균LOT_kg'] = byline['생산량_kg']/byline['LOT수']
        else:
            byline = pd.DataFrame(columns=['라인','생산량_kg','LOT수','평균LOT_kg'])
        group_frames = [x[2].assign(생산일=x[0]['day']) for x in selected if not x[2].empty]
        groups = pd.concat(group_frames,ignore_index=True) if group_frames else empty_groups.assign(생산일=[])
        trend = ledger.set_index('생산일')[['생산량_kg','A조_kg','B조_kg']].rename(columns={'생산량_kg':'전체','A조_kg':'A조','B조_kg':'B조'})
        if crew != '전체':
            trend = trend[[crew+'조']]
        else:
            # Total is the selected crew aggregate, and in this mode crew is all.
            trend = trend[['전체','A조','B조']]
        trend = trend.loc[(trend.index>=start.strftime('%Y-%m-%d')) & (trend.index<=end.strftime('%Y-%m-%d'))]
    m.update(period_kind=kind, range_start=start.strftime('%Y-%m-%d'), range_end=end.strftime('%Y-%m-%d'),
             range_label=start.strftime('%Y-%m-%d') if start==end else f'{start:%Y-%m-%d} ~ {end:%Y-%m-%d}',
             data_through=min(end,data_end).strftime('%Y-%m-%d') if records else '자료 없음',
             aggregated_days=len(records), expected_minutes=m['runtime_expected_minutes'],
             period_incomplete=len(records)<(end-start).days+1,
             period_start=(start+pd.Timedelta(hours=8,minutes=30)).strftime('%Y-%m-%d %H:%M'),
             period_end=(end+pd.Timedelta(days=1,hours=8,minutes=30)).strftime('%Y-%m-%d %H:%M'))
    columns = ['전력량_kWh','압출기_kWh','생산량_kg']
    reconciliation = {col: {'일간합계': float(ledger[col].sum(min_count=1)) if ledger[col].notna().any() else None,
                             '주간합계': float(weekly[col].sum(min_count=1)) if weekly[col].notna().any() else None} for col in columns}
    m['month_reconciliation'] = reconciliation
    shift_end = min(end, r['day'].max()) if start <= r['day'].max() else start
    shifts = build_crew_schedule(r, start, shift_end, lines)
    m['crew_shift_schedule'] = shifts.to_dict('records')
    m['crew_shift_labels'] = shift_labels(shifts)
    return m, byline, groups, trend, ledger, weekly


def render_period_html(m, byline, groups, ledger, weekly):
    from production_report import render_html
    content = render_html(m, byline, groups)
    extra = '<section><h2>집계 원칙</h2><p>주별 집계는 매월 1~7·8~14·15~21·22~28·29~말일입니다. 생산량·전력량은 일간 원장의 합계입니다. 기간 LOT 수와 통합 묶음 수는 일별 건수의 합이며, 평균 SIZE는 총생산량을 이 건수로 나눕니다. 인당 생산량은 생산이 있는 날의 누적 인원으로, 가동률은 DB 일간 가동 분과 기준 분을 각각 합산한 뒤 나누어 계산합니다. 미래 또는 아직 확보하지 못한 날짜를 확정된 0으로 채우지 않습니다. 전력은 선택한 실측 또는 청구 기준 추정 배분 원장을 합산하며, 청구 기준은 월 청구량에 맞춘 추정값입니다.</p></section>'
    extra += '<section><h2>로컬 DB 일간 원장 · 해당 월</h2>'+ledger.to_html(index=False,escape=True,border=0)+'</section>'
    extra += '<section><h2>로컬 DB 주차별 합계 · 월내 고정 주차</h2>'+weekly.to_html(index=False,escape=True,border=0)+'</section>'
    return content.replace('</html>',extra+'</html>')


def save_period_report(m, byline, groups, ledger, weekly):
    folder = save_report(m, byline, groups)
    ledger.to_csv(folder/'daily_ledger.csv',index=False,encoding='utf-8-sig')
    weekly.to_csv(folder/'weekly_rollup.csv',index=False,encoding='utf-8-sig')
    (folder/'report.html').write_text(render_period_html(m, byline, groups, ledger, weekly),encoding='utf-8')
    return folder


def period_energy_by_line(e, start, end, lines):
    frames = [energy_by_line(e, day, lines) for day in pd.date_range(start, min(pd.Timestamp(end), e['day'].max()))]
    if not frames:
        return energy_by_line(e, start, lines)
    joined = pd.concat(frames)
    result = joined.groupby('호기')[['압출기_kWh','히터·부속_kWh','호기합계_kWh']].sum(min_count=1).reset_index()
    result = result.merge(frames[0][['호기','압출기센서','히터·부속센서']], on='호기')
    result['주의'] = '기록된 일간 값의 합계; 누락은 임의 보완하지 않음'
    return result
