"""Local production reporting. Raw employee and payroll records are never loaded."""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from html import escape
import json
from pathlib import Path
import subprocess
import os
import tempfile
from zoneinfo import ZoneInfo

import pandas as pd
import duckdb
from crew_schedule import build_crew_schedule, shift_labels
from headcount_mapping import map_headcount_crews, mapping_records, applied_headcounts

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'v1' / '해커톤_업로드_2026'
SETTINGS = ROOT / 'report_settings.json'
KST = ZoneInfo('Asia/Seoul')
CRON_MARKER = '# slp-production-daily-report'
EXTRUDER_SENSORS = dict(zip(
    ['01', '02', '03', '04', '05', '06', '07', '08', '11', '12', '13', '14', '15', '16', '17', '18', '19'],
    ['400', '401', '402', '508', '406', '405', '404', '403', '104', '105', '106', '107', '108', '100', '101', '102', '103']))
HEATER_SENSORS = dict(zip(EXTRUDER_SENSORS,
    ['503', '504', '505', '514', '515', '516', '511', '512', '202', '203', '204', '205', '206', '207', '214', '215', '216']))
GENERATION_SENSORS = {'217'}  # Solar generation is not electricity consumption.
LOW_RPM = 250
MIN_STOP_MINUTES = 5
FACTORY_LINES = {'1': {f'{i:02d}' for i in range(1, 9)}, '2': {f'{i:02d}' for i in range(11, 20)}}


def heater_sensor_ids(line):
    ids = [HEATER_SENSORS[line]] if line in HEATER_SENSORS else []
    return ids + (['510'] if line == '08' else [])


def load_production_energy():
    source = DATA / '01_분석용/EMS_전력계_15min집계.csv'
    stat = source.stat()
    folder = ROOT / '.report_cache'
    folder.mkdir(exist_ok=True)
    cache = folder / f'energy-0830-v1-{stat.st_mtime_ns}-{stat.st_size}.parquet'
    with duckdb.connect() as con:
        con.execute('SET threads=2')
        con.execute("SET memory_limit='512MB'")
        if not cache.exists():
            con.read_csv(str(source), all_varchar=True).create_view('energy_intervals')
            daily = con.sql("""
                SELECT date_trunc('day', try_cast(일시 AS TIMESTAMP) - INTERVAL '8 hours 30 minutes') AS day,
                       sensor_id, any_value(센서명) AS 센서명,
                       sum(try_cast(사용량_kWh AS DOUBLE)) AS kwh,
                       max(try_cast(최대_kW AS DOUBLE)) AS 최대_kW,
                       count(DISTINCT 일시) AS intervals, count(*) AS records,
                       count(*) FILTER (WHERE try_cast(사용량_kWh AS DOUBLE) IS NULL) AS invalid_intervals
                FROM energy_intervals GROUP BY 1, 2
            """)
            fd, temporary = tempfile.mkstemp(dir=folder, suffix='.parquet')
            os.close(fd)
            try:
                daily.write_parquet(temporary)
                os.replace(temporary, cache)
            finally:
                if Path(temporary).exists(): Path(temporary).unlink()
        result = con.read_parquet(str(cache)).df()
    if result['day'].isna().any() or (result['records'] != result['intervals']).any():
        raise ValueError('전력 계측 시각이 잘못됐거나 중복된 15분 구간이 있습니다.')
    result['일자'] = result['day'].dt.strftime('%Y-%m-%d')
    result['사용량_kWh'] = result['kwh']
    return result


def load_headcount():
    return read_csv('01_분석용/p_prod_rpt.csv', ['p_date', 'fac_cd', 'juya', 'jo', 'tot_cnt', 'cur_cnt', 'abs_cnt'])


def headcount_metrics(report, report_date, lines, manual_people=0, crew='전체', production=None):
    if manual_people > 0:
        return dict(people=int(manual_people), people_source='사용자 입력', people_note='선택 라인에 실제 투입한 주·야간 인원의 합계입니다.')
    selected = set(lines)
    factories = [fac for fac, members in FACTORY_LINES.items() if selected & members]
    if not factories or selected != set().union(*(FACTORY_LINES[f] for f in factories)):
        return dict(people=0, people_source='자동 산출 불가', people_note='호기별 인원 배치 자료가 없습니다. 전체 공장 라인을 선택하거나 실제 투입 인원을 직접 입력하세요.')
    day = pd.Timestamp(report_date).strftime('%Y.%m.%d')
    rows = report[(report['p_date'] == day) & report['fac_cd'].isin(factories)].copy()
    rows = map_headcount_crews(rows, production)
    rows['applied_people'] = applied_headcounts(rows)
    audit = dict(people_mapping_records=mapping_records(rows),
                 people_mapping_corrections=int(rows['crew_mapping_status'].eq('실적 기준 보정').sum()),
                 people_mapping_held=int(rows['jo_effective'].eq('').sum()))
    expected = {(f, shift) for f in factories for shift in ['1', '2']}
    keys = list(zip(rows['fac_cd'], rows['juya']))
    valid = (set(keys) == expected and len(keys) == len(expected) and
             rows['applied_people'].notna().all())
    if not valid:
        return dict(people=0, people_source='일보 확인 필요', people_note='선택일의 공장·교대 인원 기록이 누락·중복되었거나 총원·결원 값이 없거나 유효하지 않습니다. 확인 후 직접 입력하세요.', **audit)
    if crew != '전체':
        matched = rows[rows['jo_effective'] == crew]
        unresolved = rows['jo_effective'].eq('') & (
            rows['jo_normalized'].eq(crew) | ~rows['jo_normalized'].isin(['A','B']) |
            rows['production_crew'].str.split(',').map(lambda codes: crew in codes))
        if set(matched['fac_cd']) != set(factories) or unresolved.any():
            return dict(people=0, people_source='조별 인원 확인 필요',
                        people_note='같은 날짜·공장·주야간의 생산실적으로 선택 조의 인원을 확인하지 못했습니다. 확인 후 직접 입력하세요.', **audit)
        rows = matched
    return dict(people=int(rows['applied_people'].sum()), people_source='생산일보 총원-결원 · 조 코드 연결',
                people_note=('선택 조의 ' if crew != '전체' else '') + '공장별 주·야간 일보의 총원(tot_cnt)-결원(abs_cnt) 합계(명·교대)입니다. 원본 현원(cur_cnt)은 보존합니다. 2공장 D→A·E→B 통일 후 같은 날짜·공장·주야간의 생산실적 조를 우선합니다. 실제 출근을 검증한 근태 인원은 아닙니다.', **audit)


def load_runtime():
    """Reproduce validated MES daily counts: raw RPM >250 records, capped at 60/hour."""
    source = DATA / '01_분석용/HMI_feeder_list.csv'
    stat = source.stat()
    cache_dir = ROOT / '.report_cache'
    cache_dir.mkdir(exist_ok=True)
    cache = cache_dir / f'runtime-mes-over250-hourcap60-v1-{stat.st_mtime_ns}-{stat.st_size}.parquet'
    with duckdb.connect() as con:
        con.execute('SET threads=2')
        con.execute("SET memory_limit='512MB'")
        if not cache.exists():
            con.read_csv(str(source), all_varchar=True).create_view('raw_runtime')
            hourly = con.sql(f"""
                WITH parsed AS (
                    SELECT try_strptime(p_date, '%Y.%m.%d') AS day,
                           lpad(line, 2, '0') AS line, substr(times, 1, 2) AS hour_of_day,
                           try_cast(input_dt AS TIMESTAMP) AS stamp,
                           date_trunc('minute', try_cast(input_dt AS TIMESTAMP)) AS minute_stamp,
                           try_cast(mval02 AS DOUBLE) AS rpm
                    FROM raw_runtime
                )
                SELECT day, line, hour_of_day,
                       least(count(*) FILTER (WHERE rpm > 250),60) AS operating_minutes,
                       greatest(0,least(count(DISTINCT minute_stamp),60) -
                           least(count(*) FILTER (WHERE rpm > 250),60)) AS downtime_minutes,
                       0 AS minor_stop_minutes,
                       least(count(DISTINCT minute_stamp),60) AS samples,
                       count(*) FILTER (WHERE rpm IS NULL OR stamp IS NULL) AS invalid_rpm
                FROM parsed GROUP BY 1, 2, 3
            """)
            fd, temporary = tempfile.mkstemp(dir=cache_dir, suffix='.parquet')
            os.close(fd)
            try:
                hourly.write_parquet(temporary)
                os.replace(temporary, cache)
            finally:
                if Path(temporary).exists():
                    Path(temporary).unlink()
        return con.read_parquet(str(cache)).df()


def selected_runtime_metrics(hourly, days, lines, complete=True):
    days = [pd.Timestamp(day) for day in days]
    h = hourly[hourly['day'].isin(days) & hourly['line'].isin(lines)].copy()
    h['invalid_time'] = ~h['hour_of_day'].str.fullmatch(r'(?:[01][0-9]|2[0-3])').fillna(False)
    keys = pd.MultiIndex.from_product([days, lines], names=['day','line'])
    totals = h.groupby(['day','line'])[['operating_minutes','samples','invalid_rpm','invalid_time']].sum().reindex(keys,fill_value=0)
    active = totals[totals['operating_minutes'].gt(0)]
    excluded = totals[totals['operating_minutes'].eq(0)]
    absent = excluded['samples'].eq(0)
    confirmed_zero = excluded['samples'].eq(1440) & excluded['invalid_rpm'].eq(0) & excluded['invalid_time'].eq(0)
    unknown = ~confirmed_zero
    included = h[h.set_index(['day','line']).index.isin(active.index)]
    expected = len(active)*1440
    observed = int(active['samples'].sum())
    operating = int(active['operating_minutes'].sum())
    invalid = bool(active['invalid_rpm'].sum() or active['invalid_time'].sum())
    value = operating/expected*100 if expected and not invalid else None
    status = ('가동 이력 없음' if not expected else '미산출' if invalid else
              '잠정' if observed < expected or unknown.any() or not complete else '확정')
    if not expected and (not days or absent.any()):
        status = '기록 없음'
    records = [{'생산일':day.strftime('%Y-%m-%d'),'호기':line,
                '가동분':int(row['operating_minutes']), '확보분':int(row['samples']),
                '누락분':max(0,1440-int(row['samples'])),
                '가동률_%':float(row['operating_minutes'])/1440*100}
               for (day,line),row in active.iterrows()]
    return dict(utilization=value, utilization_status=status,
                operating_minutes=operating if expected else None,
                downtime_minutes=int(included['downtime_minutes'].sum()),
                minor_stop_minutes=int(included['minor_stop_minutes'].sum()),
                observed_runtime_minutes=observed, missing_runtime_minutes=max(0,expected-observed),
                runtime_coverage=observed/expected*100 if expected else None,
                runtime_expected_minutes=expected, runtime_holiday=False,
                runtime_working_days=len(active.index.get_level_values('day').unique()), holiday_days=0,
                observed_runtime_hours=len(included), expected_runtime_hours=len(active)*24,
                rpm_threshold=LOW_RPM, minimum_stop_minutes=0,
                utilization_source='업로드 DB RPM 계산 · 가동 이력 있는 호기만',
                runtime_active_line_days=len(active),
                runtime_active_lines=sorted(active.index.get_level_values('line').unique()),
                runtime_excluded_line_days=len(excluded), runtime_unknown_line_days=int(unknown.sum()),
                runtime_missing_line_days=int(absent.sum()),
                runtime_excluded_lines=sorted(excluded.index.get_level_values('line').unique()),
                missing_runtime_lines=sorted(excluded[absent].index.get_level_values('line').unique()),
                runtime_active_records=records)


def runtime_metrics(hourly, report_date, lines):
    return selected_runtime_metrics(hourly, [report_date], lines)


def period_runtime_metrics(hourly, records, lines, complete=True):
    return selected_runtime_metrics(hourly, [x['day'] for x in records], lines, complete=complete)


def energy_metrics(e, report_date, lines):
    daily = e[e['day'] == pd.Timestamp(report_date)]
    if daily['sensor_id'].duplicated().any():
        raise ValueError('같은 날짜·센서의 전력 기록이 중복되었습니다.')
    values = daily.set_index('sensor_id')['kwh']
    consumption = sorted(set(e['sensor_id']) - GENERATION_SENSORS)

    def measured(ids):
        ids = list(ids)
        if not ids or any(s not in values.index or pd.isna(values[s]) for s in ids):
            return None
        return float(values.loc[ids].sum())

    motors = [EXTRUDER_SENSORS[x] for x in lines if x in EXTRUDER_SENSORS]
    heaters = [sensor for line in lines for sensor in heater_sensor_ids(line)]
    known = all(x in EXTRUDER_SENSORS for x in lines)
    total = measured(consumption)
    extruder = measured(motors) if known else None
    incomplete = set()
    if 'intervals' in daily:
        incomplete = set(daily.loc[(daily['intervals'] != 96) | (daily['invalid_intervals'] > 0), 'sensor_id'])
    unavailable = {s for s in consumption if s not in values.index or pd.isna(values[s])}
    return dict(energy=total, extruder_energy=extruder,
                energy_complete=total is not None and not (incomplete | unavailable),
                line_energy_complete=known and measured(motors + heaters) is not None and not (set(motors + heaters) & incomplete),
                heater_energy=measured(heaters) if known else None,
                line_energy=measured(motors + heaters) if known else None,
                extruder_share=extruder / total * 100 if extruder is not None and total else None,
                sensors=consumption, line_sensors=motors + heaters,
                missing_energy_sensors=sorted(incomplete | unavailable))


def energy_by_line(e, report_date, lines):
    rows = []
    for line in lines:
        m = energy_metrics(e, report_date, [line])
        rows.append({'호기': line, '압출기_kWh': m['extruder_energy'],
                     '히터·부속_kWh': m['heater_energy'], '호기합계_kWh': m['line_energy'],
                     '압출기센서': EXTRUDER_SENSORS.get(line, '미연결'),
                     '히터·부속센서': ', '.join(heater_sensor_ids(line)) or '미연결'})
    return pd.DataFrame(rows)


def configure_schedule(hour, minute, enabled=True):
    """Preserve unrelated user cron entries; host cron must run in UTC."""
    if enabled and not (9 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('생산일 종료 후 집계하도록 한국시간 09:00~23:59 사이로 설정하세요.')
    if datetime.now().astimezone().utcoffset() != timedelta(0):
        raise ValueError('현재 서버는 UTC가 아닙니다. 관리자에게 cron 시간대 확인을 요청하세요.')
    current = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
    if current.returncode and 'no crontab' not in current.stderr.lower():
        raise RuntimeError('사용자 crontab을 읽을 수 없습니다. 서버의 cron 권한을 확인하세요.')
    entries = [line for line in current.stdout.splitlines() if CRON_MARKER not in line]
    # A pre-existing TZ override would alter this entry's meaning.
    if enabled and any(line.strip().startswith(('CRON_TZ=', 'TZ=')) for line in entries):
        raise ValueError('기존 crontab에 별도 시간대 설정이 있습니다. 수동 등록이 필요합니다.')
    if enabled:
        (ROOT / 'reports').mkdir(exist_ok=True)
        entries.append(f'{minute} {(hour - 9) % 24} * * * /data/hackathon/.venv/bin/python /data/hackathon/production_report.py --date yesterday >> /data/hackathon/reports/automation.log 2>&1 {CRON_MARKER}')
    result = subprocess.run(['crontab', '-'], input='\n'.join(entries) + '\n', capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError('예약을 저장하지 못했습니다. 서버의 cron 권한을 확인하세요.')


def read_csv(name, columns=None):
    return pd.read_csv(DATA / name, dtype=str, usecols=columns, encoding='utf-8-sig').fillna('')


def assign_groups(result, mapping):
    links = mapping[['lot_no', 'grp_lot_no']].drop_duplicates()
    if links.eq('').any().any() or links.isna().any().any():
        raise ValueError('통합 LOT 연결표에 비어 있는 LOT·그룹 번호가 있습니다.')
    if links['lot_no'].duplicated().any():
        raise ValueError('한 LOT에 여러 통합 그룹이 연결되어 있습니다. 연결표를 확인하세요.')
    if result['lot_no'].eq('').any() or result['lot_no'].isna().any():
        raise ValueError('생산 실적에 LOT 번호가 비어 있습니다.')
    r = result.merge(links, on='lot_no', how='left', validate='many_to_one')
    linked = r['grp_lot_no'].notna()
    r['run'] = '단독:' + r['lot_no']
    r.loc[linked, 'run'] = '그룹:' + r.loc[linked, 'grp_lot_no']
    r['group_type'] = linked.map({True: '통합 그룹', False: '단독 LOT'})
    return r


def load_data():
    r = read_csv('03_에이전트용/p_prod_result.csv', ['lot_no', 'p_date', 'line', 'jo', 'juya', 'pr_qty'])
    r['day'] = pd.to_datetime(r['p_date'], format='%Y.%m.%d', errors='coerce')
    r['qty'] = pd.to_numeric(r['pr_qty'], errors='coerce')
    if r['day'].isna().any() or r['qty'].isna().any() or (r['qty'] < 0).any():
        raise ValueError('생산 실적의 날짜·수량에 결측 또는 음수가 있습니다. 원본을 확인하세요.')
    r['line'] = r['line'].str.zfill(2)
    p = read_csv('03_에이전트용/p_prod_plan.csv', ['lot_no', 'line', 'grade', 'color'])
    p['line'] = p['line'].str.zfill(2)
    if p['lot_no'].duplicated().any():
        raise ValueError('생산 계획에 중복 LOT가 있어 연결을 중단했습니다.')
    r = r.merge(p[['lot_no', 'line', 'grade', 'color']], on=['lot_no', 'line'], how='left', validate='many_to_one')
    r[['grade', 'color']] = r[['grade', 'color']].fillna('미연결')
    mapping = read_csv('01_분석용/p_grplot_list.csv', ['lot_no', 'grp_lot_no'])
    r = assign_groups(r, mapping)
    e = load_production_energy()
    return r, e


def summarize(r, e, report_date, lines, people=0, runtime=None, headcount=None, crew='전체'):
    if crew not in ['전체', 'A', 'B']:
        raise ValueError('근무조는 전체, A, B 중에서 선택하세요.')
    day = pd.Timestamp(report_date)
    chosen = r[r['line'].isin(lines)]
    comparison_day = chosen[chosen['day'] == day]
    if crew != '전체':
        chosen = chosen[chosen['jo'] == crew]
    d = chosen[(chosen['day'] == day) & (chosen['qty'] > 0)].copy()
    month = chosen[(chosen['day'] >= day.replace(day=1)) & (chosen['day'] <= day)]
    working_days = int(month.loc[month['qty'] > 0, 'day'].nunique())
    total = float(d['qty'].sum())
    lot_count = len(d[['line', 'lot_no']].drop_duplicates())
    def values(series):
        return ', '.join(sorted(series.unique()))
    # Mapping identity is authoritative: metadata differences must not split a group.
    groups = d.groupby('run', as_index=False).agg(
        구분=('group_type', 'first'), 라인=('line', values), GRADE=('grade', values),
        COLOR=('color', values), 생산량_kg=('qty', 'sum'), LOT수=('lot_no', 'nunique'))
    groups = groups.rename(columns={'run': '묶음'})
    grouped_count = int(groups['구분'].eq('통합 그룹').sum())
    single_count = int(groups['구분'].eq('단독 LOT').sum())
    power = energy_metrics(e, day, lines)
    month_total = float(month['qty'].sum())
    if runtime is None:
        runtime = load_runtime()
    operation = runtime_metrics(runtime, day, lines)
    if day < r['day'].min() or day > max(v for v in [r['day'].max(), runtime['day'].max()] if pd.notna(v)):
        operation.update(utilization=None, utilization_status='미산출')
    if headcount is None:
        headcount = load_headcount()
    staffing = headcount_metrics(headcount, day, lines, people, crew=crew, production=r)
    people = staffing['people']
    metrics = dict(day=day.strftime('%Y-%m-%d'), lines=list(lines), crew=crew,
                   crew_label='전체' if crew == '전체' else crew + '조', total=total,
                   period_start=(day + pd.Timedelta(hours=8, minutes=30)).strftime('%Y-%m-%d %H:%M'),
                   period_end=(day + pd.Timedelta(days=1, hours=8, minutes=30)).strftime('%Y-%m-%d %H:%M'),
                   a=float(comparison_day.loc[comparison_day['jo'] == 'A', 'qty'].sum()),
                   b=float(comparison_day.loc[comparison_day['jo'] == 'B', 'qty'].sum()),
                   other=float(d.loc[~d['jo'].isin(['A', 'B']), 'qty'].sum()), lots=lot_count,
                   average_lot=total / lot_count if lot_count else None,
                   integrated_lot=total / len(groups) if len(groups) else None,
                   integrated_groups=grouped_count, standalone_lots=single_count,
                   month_total=month_total, working_days=working_days,
                   daily_average=month_total / working_days if working_days else None,
                   **staffing, per_person=total / people if people > 0 else None,
                   **power, **operation,
                   active_lines=int(d['line'].nunique()), line_ratio=d['line'].nunique() / len(lines) * 100 if lines else None,
                   source_present=bool((chosen['day'] == day).any()))
    shifts = build_crew_schedule(r, day, day, lines)
    metrics['crew_shift_schedule'] = shifts.to_dict('records')
    metrics['crew_shift_labels'] = shift_labels(shifts)
    byline = d.groupby('line', as_index=False).agg(생산량_kg=('qty', 'sum'), LOT수=('lot_no', 'nunique'))
    byline['평균LOT_kg'] = byline['생산량_kg'] / byline['LOT수']
    byline = byline.rename(columns={'line': '라인'})
    start = day.replace(day=1)
    trend = month.groupby(['day', 'jo'])['qty'].sum().unstack(fill_value=0)
    trend = trend.reindex(pd.date_range(start, day), fill_value=0)
    trend = trend.reindex(columns=['A', 'B'] if crew == '전체' else [crew], fill_value=0)
    trend = trend.rename(columns={'A': 'A조', 'B': 'B조'})
    if crew == '전체':
        trend.insert(0, '전체', month.groupby('day')['qty'].sum().reindex(trend.index, fill_value=0))
    trend.index.name = '일자'
    return metrics, byline, groups, trend


def number(value, unit=''):
    return '확인 필요' if value is None else f'{value:,.1f}{unit}'


def utilization_number(value):
    if value is None:
        return '확인 필요'
    rounded = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return f'{rounded:,.2f} %'


def render_html(m, byline, groups):
    kind = m.get('period_kind', '일간')
    kind_label = {'일간':'일별','주간':'주별','월간':'월별'}[kind]
    label_range = m.get('range_label', m['day'])
    billing = m.get('energy_basis') == '청구 기준 추정 배분'
    energy_note = (m.get('energy_basis_note', '') + (f" 월 청구량 {m['billing_month_usage']:,.1f} kWh · 보정계수 {m['billing_factor']:.6f}. 전력 집계 {m['energy_period_start']} ~ {m['energy_period_end']} KST." if billing else ' 센서 실측은 생산일 08:30부터 다음 날 08:30까지 합산하며 한전 청구량과 별도 기준입니다.'))
    crew_labels = m.get('crew_shift_labels',{})
    labels = [(m['crew_label'] + ' ' + kind_label + ' 생산량', 'total', ' kg'), ('A조 생산량 · ' + (crew_labels.get('A','주야 확인') if kind=='일간' else '기간 비교'), 'a', ' kg'), ('B조 생산량 · ' + (crew_labels.get('B','주야 확인') if kind=='일간' else '기간 비교'), 'b', ' kg'),
              ('평균 LOT SIZE', 'average_lot', ' kg/LOT'), ('통합 LOT SIZE', 'integrated_lot', ' kg/LOT'),
              (('가동률 · 가동 호기 기준' if kind == '일간' else '가동률 · 가동 호기 일간 합산'), 'utilization', ' %'), (('금월 가동일' if kind == '일간' else '기간 가동일'), 'working_days', ' 일'),
              ('금월 총생산량', 'month_total', ' kg'), (('금월 일평균' if kind == '일간' else '기간 일평균'), 'daily_average', ' kg/일'),
              ('인당 생산량 · 명·교대 기준', 'per_person', ' kg/명·교대'), ('전체 사용 전력량 · 공용 포함', 'energy', ' kWh'),
              ('압출기 전력량 · 선택 호기', 'extruder_energy', ' kWh'),
              ('히터·부속 전력량 · 선택 호기', 'heater_energy', ' kWh'),
              ('호기별 전력량 합계 · 선택 호기', 'line_energy', ' kWh')]
    def status_label(label, key):
        if key == 'utilization' and m.get('runtime_holiday'):
            return label + ' · 휴무 산정 제외'
        if billing and key in ['energy', 'extruder_energy', 'heater_energy', 'line_energy']:
            return label + ' · 청구 기준 추정'
        provisional = ((key == 'utilization' and m['utilization_status'] == '잠정') or
                       (key == 'energy' and not m['energy_complete']) or
                       (key in ['extruder_energy', 'heater_energy', 'line_energy'] and not m['line_energy_complete']))
        return label + (' · 잠정' if provisional else '')
    def display_value(key,unit):
        if key == 'utilization':
            if m['utilization_status'] in ['가동 이력 없음','기록 없음']:
                return m['utilization_status']
            return utilization_number(m[key])
        return number(m[key],unit)
    cards = ''.join(f'<div class="card"><small>{status_label(label,key)}</small><strong>{display_value(key,unit)}</strong></div>' for label,key,unit in labels)

    warnings = []
    if not m['source_present'] and not m.get('runtime_holiday'): warnings.append('해당 날짜의 생산 실적이 없습니다. 0은 확정 생산량이 아닙니다.')
    if m.get('runtime_unknown_line_days',0): warnings.append(f"가동률 대상에서 제외된 호기·날짜 중 기록이 없거나 불완전해 가동 여부를 모르는 경우가 {m['runtime_unknown_line_days']}건 있습니다. 실제 비가동으로 확정하지 않았습니다.")
    if m['utilization_status'] in ['가동 이력 없음','기록 없음']: warnings.append('선택 범위에 가동 이력이 확인된 호기가 없어 가동률을 계산하지 않았습니다.')
    elif m.get('runtime_holiday'): warnings.append('전체 공장·전체 조의 생산량 합계가 0인 휴무일로 가동률 산정에서 제외합니다.')
    elif m['utilization'] is None: warnings.append('선택 호기의 센서 기록 누락 또는 RPM 값 오류로 가동률은 미산출입니다.')
    elif m['missing_runtime_minutes']: warnings.append(f"가동률은 잠정값입니다. 기록 확보율 {m['runtime_coverage']:.1f}%, 기록 누락 {m['missing_runtime_minutes']:,}분. 누락 시간을 실제 비가동으로 확정하지 않았습니다.")
    if not m['people']: warnings.append('실제 투입 인원이 입력되지 않아 인당 생산량은 미산출입니다.')
    if m['energy'] is None: warnings.append('해당 날짜의 전력 계측 누락으로 전체 사용량은 미산출입니다. 누락 센서: ' + ', '.join(m['missing_energy_sensors']))
    elif not m['energy_complete']: warnings.append('전력량은 생산일 종료까지 계측값이 누락된 잠정 합계입니다. 해당 센서: ' + ', '.join(m['missing_energy_sensors']))
    if m.get('period_incomplete'):
        warnings.append('요청 기간의 자료가 전부 확보되지 않았습니다. 집계 자료 기준일: ' + m['data_through'])
    if billing:
        warnings.append(energy_note)
        if m['allocation_source_incomplete']:
            warnings.append('배분 가중치의 센서 원본에 미수집·결측이 있습니다. 청구 총량은 일치하지만 날짜·설비별 실측을 복구한 것은 아닙니다.')
    summary = f"{label_range} 선택 라인·{m['crew_label']} 생산량은 {m['total']:,.0f} kg입니다. 같은 라인의 기간 비교값은 A조 {m['a']:,.0f} kg, B조 {m['b']:,.0f} kg입니다. 선택 조의 금월 누계는 {m['month_total']:,.0f} kg이며, 가동일 기준 일평균은 {number(m['daily_average'], ' kg')}입니다."
    if m.get('runtime_holiday'): summary = '전체 공장·전체 조의 생산량 합계가 0인 휴무일입니다. 가동률은 산정에서 제외합니다.'
    elif not m['source_present']: summary = '해당 날짜의 원본 실적이 없어 생산 현황을 확정할 수 없습니다.'
    shift_table = pd.DataFrame(m.get('crew_shift_schedule',[]))
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(label_range)} 생산 {kind_label} 리포트</title><style>
body{{font-family:Arial,'Malgun Gothic',sans-serif;background:#f2f5f9;color:#172538;max-width:1100px;margin:40px auto;padding:24px}}h1{{letter-spacing:-1px}}.eyebrow{{color:#137e7b;font-size:13px;letter-spacing:2px}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card,section{{background:white;padding:20px;border:1px solid #dce4eb;border-radius:12px;margin-bottom:12px}}small{{color:#607386}}strong{{display:block;font-size:24px;margin-top:10px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:9px;border-bottom:1px solid #dde4ec;text-align:right}}.warning{{color:#915907}}p{{line-height:1.8}}@media(max-width:650px){{.grid{{grid-template-columns:1fr 1fr}}body{{margin:0;padding:16px}}}}@media print{{body{{background:white;margin:0}}button{{display:none}}.card{{break-inside:avoid}}}}</style>
<div class="eyebrow">OPERATIONS INTELLIGENCE / REPORT</div><h1>생산 {kind_label} 리포트 <span style="color:#137e7b">{label_range}</span></h1>
<p>라인 {escape(', '.join(m['lines']))} · 근무조 {m['crew_label']} · 생산 집계 {m['period_start']} ~ {m['period_end']} KST · 생성 {datetime.now(KST):%Y-%m-%d %H:%M} KST</p><p>생산·LOT·금월 지표는 선택 조 기준입니다. A/B조 비교 카드와 전력량·가동률은 조회 기간 전체 기준이며 조별로 배분하지 않습니다.</p><button onclick="window.print()">인쇄 / PDF 저장</button>
<section><h2>오늘의 생산 요약</h2><p>{escape(summary)}</p></section><div class="grid">{cards}</div>
<section><h2>데이터 확인 사항</h2>{''.join('<p class="warning">'+escape(w)+'</p>' for w in warnings) or '<p>선택한 기준으로 산출했습니다.</p>'}
<p>전력 센서: {escape(', '.join(m['sensors'])) or '미선택'} · 인원: {m['people']}명·교대 ({escape(m['people_source'])}) · {escape(m['people_note'])} · DB 가동 분 판정: RPM 250 초과 기록 수, 시간당 최대 60분 · 가동 호기·일: {m.get('runtime_active_line_days', 0)}건 · 제외 호기·일: {m.get('runtime_excluded_line_days', 0)}건 · 가동률 산정일: {m.get('runtime_working_days', 1)}일 · 기준 분: {m.get('runtime_expected_minutes', 0):,}분 · 가동 분 합계: {number(m['operating_minutes'], '분')} · 비가동 {m['downtime_minutes']:,}분 · 소정지 {m['minor_stop_minutes']:,}분</p></section>
<section><h2>날짜별 A/B조 주야 근무</h2>{shift_table.to_html(index=False,escape=True,border=0)}<p>선택 공장 전체 생산실적의 juya 기준: 1=주간, 2=야간. 실적이 없는 조의 근무예정은 추정하지 않습니다.</p></section>
<section><h2>라인별 생산</h2>{byline.to_html(index=False,escape=True,border=0)}</section>
<section><h2>통합 그룹과 단독 LOT</h2>{groups.to_html(index=False,escape=True,border=0)}<p>p_grplot_list의 grp_lot_no로 묶으며, 연결표에 없는 LOT는 각각 단독 묶음으로 계산합니다. 그룹 {m['integrated_groups']}개 + 단독 LOT {m['standalone_lots']}개. 수량은 선택 날짜·라인의 실제 생산량이며, 여러 날짜에 걸친 그룹의 전체 생산량은 아닙니다. GRADE·COLOR·라인이 여러 개이면 모두 표시하고 그룹은 유지합니다.</p></section>
<section><h2>전력 집계 기준</h2><p>{escape(energy_note)}</p></section>
<section><h2>계산 기준</h2><p>평균 LOT SIZE = 일일 생산량 ÷ 라인·LOT 기준 중복 제거 LOT 수. 통합 LOT SIZE = 일일 생산량 ÷ (통합 그룹 수 + 단독 LOT 수). 금월 가동일 = 선택 라인에 생산 실적이 양수인 날짜 수. 금월 일평균 = 금월 총생산량 ÷ 가동일. 인당 생산량 = 일일 생산량 ÷ 생산일보 공장·교대별 (총원-결원) 합계 또는 직접 입력 인원. 단위는 명·교대이며 개인별 중복 제거 인원이 아닙니다. 업로드 DB RPM 계산 기준: HMI_feeder_list에서 p_date·line·times의 시간대별로 RPM(mval02) 250 초과인 원본 기록을 세고 시간당 최대 60분으로 제한합니다. 가동률은 가동 분 합계 ÷ (날짜별 가동 이력이 있는 호기 수의 합 × 1,440분) × 100입니다. 선택 호기 중 RPM 250 초과 가동 분이 있는 날짜·호기만 포함하며, 포함한 호기의 당일 정지 시간은 분모에 남습니다. 연속 5분 조건은 적용하지 않습니다. 기록 없는 구간은 별도로 표시하되 가동 분에는 더하지 않습니다. 주별·월별 집계는 DB의 일별 가동 분 합계 ÷ 일간 기준 분 합계 × 100으로 계산합니다. MES 서버에 접속하지 않습니다. 전력은 선택한 집계 기준을 따릅니다. 센서 실측은 생산일 08:30 기준, 청구 기준 추정 배분은 달력일 00시 기준입니다. 청구 기준은 2026년 1~10월만 월 청구량 ÷ 달력월 센서 합계를 계수로 적용하고 0.1 kWh 잔여량을 배분합니다. 전체 전력은 공용 설비를 포함하며 라인 필터와 무관합니다. 태양광 센서(217)는 제외합니다. 압출기·히터는 선택 호기의 센서에 대응하며 청구 기준에서는 동일 계수로 보정한 추정값입니다. 8호기는 히터(512)와 휴게실(510)을 포함합니다.</p></section></html>'''


def save_report(m, byline, groups):
    folder = ROOT / 'reports' / m['day']
    if m.get('period_kind', '일간') != '일간':
        folder = ROOT / 'reports' / m['period_kind'] / (m['range_start'] + '_' + m['range_end'])
    if m.get('energy_basis') == '청구 기준 추정 배분':
        folder = folder / '청구기준배분'
    if m['crew'] != '전체':
        folder = folder / (m['crew'] + '조')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'report.html').write_text(render_html(m, byline, groups), encoding='utf-8')
    pd.DataFrame(m.get('runtime_active_records',[]),columns=['생산일','호기','가동분','확보분','누락분','가동률_%']).to_csv(folder/'active_runtime_lines.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(m.get('crew_shift_schedule',[])).to_csv(folder/'crew_shift_schedule.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(m.get('people_mapping_records',[])).to_csv(folder/'headcount_crew_mapping.csv',index=False,encoding='utf-8-sig')
    byline.to_csv(folder / 'lines.csv', index=False, encoding='utf-8-sig')
    groups.to_csv(folder / 'lots.csv', index=False, encoding='utf-8-sig')
    (folder / 'metrics.json').write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')
    return folder


def main():
    parser = argparse.ArgumentParser(description='Generate a local production report without opening the website.')
    parser.add_argument('--date', default='yesterday', help='YYYY-MM-DD, yesterday (KST), or latest')
    parser.add_argument('--period', choices=['일간','주간','월간'], help='집계 단위. 기본은 저장된 조회 조건 또는 일간')
    parser.add_argument('--energy-basis', choices=['청구기준','실측'], help='전력 집계 기준. 기본은 저장된 조건 또는 청구기준(1~10월)')
    args = parser.parse_args()
    r, e = load_data()
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    if args.date == 'latest': day = r['day'].max().date()
    elif args.date == 'yesterday': day = datetime.now(KST).date() - timedelta(days=1)
    else: day = date.fromisoformat(args.date)
    # Daily headcount must be supplied anew; runtime is computed from sensor history.
    from period_reporting import build_period_report, save_period_report
    selected_lines = settings.get('lines', sorted(r['line'].unique()))
    result = build_period_report(r, e, load_runtime(), load_headcount(), day,
            selected_lines, crew=settings.get('crew','전체'),
            kind=args.period or settings.get('period','일간'))
    if (args.energy_basis or settings.get('energy_basis','청구기준')) == '청구기준':
        from billing_allocation import load_billing_allocation, apply_billing_basis
        allocation, bills = load_billing_allocation()
        result = apply_billing_basis(result, allocation, bills, selected_lines)
    m, byline, groups, _, ledger, weekly = result
    print(save_period_report(m, byline, groups, ledger, weekly))
    if m['runtime_holiday']: print('휴무: 전체 공장·전체 조 생산량이 0이므로 가동률 산정에서 제외했습니다.')
    elif not m['source_present']: print('주의: 해당 날짜 실적이 없습니다. 데이터 확인 경고를 포함해 저장했습니다.')


if __name__ == '__main__':
    main()
