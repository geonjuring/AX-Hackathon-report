import json

import streamlit as st
import pandas as pd
import altair as alt
from chart_reporting import aggregate_chart_data, period_labels, line_energy_efficiency, build_chart_history
from calendar import monthrange
from billing_allocation import load_billing_allocation, apply_billing_basis, allocation_by_line
from period_reporting import build_period_report, period_bounds, save_period_report, render_period_html, period_energy_by_line

from production_report import (DATA, SETTINGS, load_data, summarize, number, render_html, save_report, configure_schedule, energy_by_line, load_runtime, load_headcount, utilization_number)

st.set_page_config(page_title='결산 리포트 | SLP', page_icon='📊', layout='wide')
st.markdown('''<style>
.block-container{max-width:1680px;padding:1rem 2rem 1.5rem}
h1{font-size:1.7rem!important;letter-spacing:-1px;padding:0 0 .35rem!important}
h2,h3{font-size:1.05rem!important;padding:.3rem 0!important}
[data-testid="stVerticalBlock"]{gap:.55rem}
[data-testid="stMetric"]{padding:10px 12px;border:1px solid #2a4057;border-radius:9px;background:#101e30}
[data-testid="stMetricValue"]{font-size:1.25rem!important;line-height:1.35}
[data-testid="stMetricLabel"] p{font-size:.76rem!important}
[data-testid="stCaptionContainer"] p{font-size:.74rem;line-height:1.45}
[data-testid="stAlert"]{padding:.45rem .7rem}
[data-testid="stAlert"] p{font-size:.8rem;line-height:1.4}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p{font-size:.78rem}
[data-testid="stSidebar"] [data-baseweb="tag"]{font-size:.7rem}
[data-testid="stSidebar"] [data-baseweb="select"]>div{max-height:110px;overflow:auto}
@media(max-width:700px){.block-container{padding:1rem}.st-key-metrics [data-testid="stMetricValue"]{font-size:1.1rem!important}}
@media print{[data-testid="stSidebar"],button{display:none}}
</style>''', unsafe_allow_html=True)
st.title('결산 리포트')
st.caption('날짜를 선택하면 생산 · LOT · 근무조 · 전력 지표와 리포트를 자동으로 작성합니다.')


@st.cache_data(show_spinner='생산·통합 LOT·전력 데이터를 읽고 있습니다…')
def cached_load(stamp, calculation_version):
    r, e = load_data()
    allocation, billing_summary = load_billing_allocation()
    return r, e, load_runtime(), load_headcount(), allocation, billing_summary



paths = [DATA / '03_에이전트용' / n for n in ['p_prod_result.csv', 'p_prod_plan.csv']]
paths.extend(DATA / '01_분석용' / n for n in ['p_grplot_list.csv', 'HMI_feeder_list.csv', 'EMS_전력계_15min집계.csv', 'p_prod_rpt.csv', '전력요금_월별청구_2026.csv'])
try:
    stamp = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    r, e, runtime, headcount, allocation, billing_summary = cached_load(stamp, 'runtime-mes-hourcap60-crew-juya-line8-heater-v2')
except (OSError, ValueError) as ex:
    st.error(f'데이터를 읽을 수 없습니다: {ex}')
    st.stop()

all_lines = sorted(r['line'].unique())
with st.sidebar:
    st.header('리포트 설정')
    period_names = {'일간':'일별','주간':'주별','월간':'월별'}
    period = st.radio('집계 단위', ['일간','주간','월간'], format_func=period_names.get, horizontal=True)
    period_label = period_names[period]
    reference_date = st.session_state.get('_report_reference_date', r['day'].max().date())
    if period == '일간':
        report_date = st.date_input('기준일', value=reference_date, min_value=r['day'].min().date())
    else:
        months = pd.period_range(r['day'].min(), r['day'].max(), freq='M').astype(str).tolist()
        reference_month = reference_date.strftime('%Y-%m')
        selected_month = st.selectbox('기준 연월', months,
            index=months.index(reference_month) if reference_month in months else len(months)-1,
            format_func=lambda value: f'{value[:4]}년 {int(value[5:])}월')
        report_date = pd.Timestamp(selected_month + '-01').date()
        if reference_month == selected_month:
            report_date = report_date.replace(day=reference_date.day)
    week_number = None
    if period == '주간':
        last_day = monthrange(report_date.year, report_date.month)[1]
        week_number = st.selectbox('월내 주차', list(range(1, (last_day-1)//7+2)),
                                  index=(report_date.day-1)//7,
                                  format_func=lambda w: f'{w}주차 · {(w-1)*7+1}~{min(w*7,last_day)}일',
                                  key=f'week-{report_date.year}-{report_date.month}')
    if week_number is not None:
        report_date = report_date.replace(day=(week_number-1)*7+1)
    st.session_state['_report_reference_date'] = report_date
    range_start, range_end = period_bounds(report_date, period, week_number)
    energy_basis = '청구기준'
    factory = st.selectbox('공장', ['전체 공장', '1공장', '2공장'])
    available = [x for x in all_lines if factory == '전체 공장' or (factory == '1공장' and int(x) <= 8) or (factory == '2공장' and int(x) >= 11)]
    line_view = st.selectbox(
        '생산 라인', ['통합'] + available,
        format_func=lambda value: value if value == '통합' else f'{int(value)}호기',
        key=f'line-view-{factory}',
        help='통합은 선택한 공장의 전체 라인을 합산합니다. 개별 호기를 선택하면 해당 라인만 표시합니다.',
    )
    lines = available if line_view == '통합' else [line_view]
    crew_view = st.radio('근무조', ['전체', 'A조', 'B조'], horizontal=True)
    crew = '전체' if crew_view == '전체' else crew_view[0]
    people = 0
    if st.button('예약 리포트 조회 조건 저장', use_container_width=True):
        SETTINGS.write_text(json.dumps({'lines': lines, 'crew': crew, 'period': period, 'energy_basis': energy_basis}, ensure_ascii=False, indent=2), encoding='utf-8')
        st.success('저장했습니다. 예약 및 명령어 리포트 생성에 적용됩니다.')
    if st.button('원본 데이터 새로 읽기', use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if not lines:
    st.info('생산 라인을 하나 이상 선택하세요.')
    st.stop()

m, byline, groups, trend, ledger, weekly = build_period_report(r, e, runtime, headcount, report_date, lines, crew=crew, kind=period, week_number=week_number, people=people)
m, byline, groups, trend, ledger, weekly = apply_billing_basis((m, byline, groups, trend, ledger, weekly), allocation, billing_summary, lines)
billing_applied = m.get('energy_basis') == '청구 기준 추정 배분'
latest = r['day'].max().date()
scope = f'{factory} 통합' if line_view == '통합' else f'{int(line_view)}호기'
st.caption(f'조회 {scope} · {crew_view} · {period_label} {m['range_label']} · 생산 {m['period_start']} ~ {m['period_end']} · 최종 생산일 {latest} · {len(lines)}개 라인')
with st.expander('가동률에 포함된 가동 호기 목록'):
    active_runtime = pd.DataFrame(m['runtime_active_records'],columns=['생산일','호기','가동분','확보분','누락분','가동률_%'])
    st.dataframe(active_runtime,hide_index=True,use_container_width=True)
    st.download_button('가동 호기 목록 CSV',active_runtime.to_csv(index=False).encode('utf-8-sig'),f'active_lines_{period}_{m["range_start"]}_{m["range_end"]}.csv','text/csv')

for cells in [
    [((period_label + ' 생산량' if crew == '전체' else crew_view + ' ' + period_label + ' 생산량'), m['total'], ' kg'), ('A조 생산량 · ' + ((m['crew_shift_labels']['A'] + (' 근무' if m['crew_shift_labels']['A'] in ['주간','야간'] else '')) if period=='일간' else '기간 비교'), m['a'], ' kg'), ('B조 생산량 · ' + ((m['crew_shift_labels']['B'] + (' 근무' if m['crew_shift_labels']['B'] in ['주간','야간'] else '')) if period=='일간' else '기간 비교'), m['b'], ' kg'), (('전력량 · 한전 청구 기준' if billing_applied else '전력량 · DB 센서 기준'), m['energy'], ' kWh')],
    [('평균 LOT SIZE', m['average_lot'], ' kg/LOT'), ('통합 LOT SIZE', m['integrated_lot'], ' kg/LOT'), (('가동률 · 가동 호기 기준' if period == '일간' else '가동률 · 가동 호기 합산') + (' · 잠정' if m['utilization_status'] == '잠정' else ''), m['utilization'], ' %'), ('압출기 전력량', m['extruder_energy'], ' kWh')],
    [('금월 총생산량', m['month_total'], ' kg'), (('금월 가동일' if period == '일간' else '기간 가동일'), m['working_days'], ' 일'), (('금월 일평균' if period == '일간' else '기간 일평균'), m['daily_average'], ' kg/일'), ('인당 생산량 · 명·교대 기준', m['per_person'], ' kg/명·교대')],
]:
    for col, (label, value, unit) in zip(st.columns(4), cells):
        col.metric(label, '휴무 · 산정 제외' if label.startswith('가동률') and m['runtime_holiday'] else m['utilization_status'] if label.startswith('가동률') and m['utilization_status'] in ['가동 이력 없음','기록 없음'] else utilization_number(value) if label.startswith('가동률') else number(value, unit))

@st.cache_data(show_spinner='시각화 데이터를 집계하고 있습니다…')
def chart_history(source_stamp, selected_lines, selected_crew, year):
    return build_chart_history(r, e, runtime, allocation, billing_summary,
                               list(selected_lines), selected_crew, year)


@st.cache_data(show_spinner=False)
def chart_line_energy(source_stamp, selected_lines, start, end):
    month_bill = billing_summary[billing_summary['청구월'] == start[:7]]
    if not month_bill.empty and month_bill.iloc[0]['상태'] == '청구 기준 추정 배분':
        return allocation_by_line(allocation, start, end, list(selected_lines)), '한전 청구 기준 추정 배분'
    return period_energy_by_line(e, start, end, list(selected_lines)), 'DB 센서 기준'


def render_visualizations(unit):
    chart_ledger, active_records = chart_history(stamp, tuple(lines), crew, report_date.year)
    if unit == '월별':
        st.caption(f'{scope} · {crew_view} · {report_date.year}년 월별 비교')
    else:
        month = f'{report_date:%Y-%m}'
        chart_ledger = chart_ledger[chart_ledger['생산일'].str.startswith(month)].copy()
        active_records = [row for row in active_records if row['생산일'].startswith(month)]
        st.caption(f'{scope} · {crew_view} · {month} · ' +
                   ('매월 1~7일·8~14일·15~21일·22~28일·나머지 날짜' if unit == '주별' else '날짜별 비교'))
    daily, active = aggregate_chart_data(chart_ledger, active_records, unit)
    production_tab, runtime_tab, lot_chart_tab, energy_tab, efficiency_tab, comparison_tab = st.tabs(
        ['생산량', '호기별 가동률', 'LOT SIZE', '전력량', '전력 효율', '생산량·전력 효율'])
    with production_tab:
        columns = ['A조_kg', 'B조_kg'] if crew == '전체' else [crew + '조_kg']
        if crew == '전체':
            columns = ['생산량_kg'] + columns
        production = daily[['기간'] + columns].rename(
            columns={'생산량_kg':'전체', 'A조_kg':'A조', 'B조_kg':'B조'}).melt(
                id_vars='기간', var_name='구분', value_name='생산량')
        base = alt.Chart(production).encode(
            x=alt.X('기간:O', title='기간', sort=sorted(daily['기간'].tolist())),
            y=alt.Y('생산량:Q', title='생산량 (kg)', scale=alt.Scale(zero=True)),
            color=alt.Color('구분:N', scale=alt.Scale(
                domain=['전체','A조','B조'], range=['#dbe5f1','#69e4cd','#5d8ef0'])),
            tooltip=['기간:O','구분:N',alt.Tooltip('생산량:Q', format=',.1f')])
        bars = base.transform_filter(alt.datum.구분 != '전체').mark_bar().encode(
            xOffset=alt.XOffset('구분:N', sort=['A조','B조']))
        chart = bars
        if crew == '전체':
            total_line = base.transform_filter(alt.datum.구분 == '전체').mark_line(point=True, strokeWidth=2)
            chart = bars + total_line
        st.altair_chart(chart.properties(height=320), use_container_width=True)
    with runtime_tab:
        periods = daily['기간'].tolist()
        if not periods:
            st.caption('선택 범위에 가동률을 계산할 기간이 없습니다.')
        else:
            preferred = period_labels(pd.Series([report_date]), unit).iloc[0]
            selected_period = st.selectbox('가동률 비교 기간', periods,
                index=periods.index(preferred) if preferred in periods else len(periods)-1,
                key=f'runtime-bar-period-{unit}-{report_date.year}-{report_date.month}')
            ranking = active[active['기간'] == selected_period].copy()
            if ranking.empty:
                st.caption('선택 기간에 가동 이력이 확인된 호기가 없습니다.')
            else:
                ranking['호기명'] = ranking['호기'].astype(str).str.replace(r'^0', '', regex=True) + '호기'
                ranking['호기번호'] = pd.to_numeric(ranking['호기'], errors='coerce')
                ranking = ranking.sort_values('호기번호')
                ranking['표시'] = ranking['가동률_%'].map(lambda value: f'{value:.1f}%')
                base = alt.Chart(ranking).encode(
                    y=alt.Y('호기명:N', title=None, sort=ranking['호기명'].tolist()),
                    x=alt.X('가동률_%:Q', title='가동률 (%)',
                        scale=alt.Scale(domain=[0,110]), axis=alt.Axis(values=[0,20,40,60,80,100])),
                    tooltip=['기간:O','호기명:N',alt.Tooltip('가동률_%:Q',format='.2f'),'가동분:Q','누락분:Q'])
                bars = base.mark_bar(color='#69e4cd', cornerRadiusEnd=4, size=18)
                labels = base.mark_text(align='left', dx=6, color='#dbe5f1').encode(text='표시:N')
                st.altair_chart((bars + labels).properties(height=max(160,len(ranking)*29)), use_container_width=True)
            measured = set(ranking['호기'].astype(str).str.zfill(2))
            excluded = [f'{int(line)}호기' for line in lines if line not in measured]
            if excluded:
                st.caption('가동 이력 미확인 · 미산출: ' + ', '.join(excluded))
    with lot_chart_tab:
        lot_data = daily[['기간']].copy()
        lot_data['평균 LOT SIZE'] = daily['생산량_kg'].div(daily['LOT수'].where(daily['LOT수'] > 0))
        lot_data['통합 LOT SIZE'] = daily['생산량_kg'].div(daily['통합묶음수'].where(daily['통합묶음수'] > 0))
        lot_bars = lot_data.melt(id_vars='기간', var_name='지표', value_name='LOT SIZE')
        lot_bars['단위'] = lot_bars['지표'].map({'평균 LOT SIZE':'kg/LOT', '통합 LOT SIZE':'kg/LOT'})
        st.altair_chart(alt.Chart(lot_bars).mark_bar().encode(
            x=alt.X('기간:O', title='기간', sort=sorted(daily['기간'].tolist())),
            xOffset=alt.XOffset('지표:N', sort=['평균 LOT SIZE','통합 LOT SIZE']),
            y=alt.Y('LOT SIZE:Q', title='LOT SIZE (kg/LOT)', scale=alt.Scale(zero=True)),
            color=alt.Color('지표:N', scale=alt.Scale(
                domain=['평균 LOT SIZE','통합 LOT SIZE'], range=['#69e4cd','#5d8ef0'])),
            tooltip=['기간:O','지표:N',alt.Tooltip('LOT SIZE:Q',format=',.1f'),'단위:N']
        ).properties(height=300), use_container_width=True)
        st.caption('평균 LOT SIZE: kg/LOT · 통합 LOT SIZE: kg/LOT')
    with energy_tab:
        st.subheader('공장 전체 전력량 추이')
        st.caption('월별로 한전 청구 자료가 있으면 청구 기준 추정 배분, 없으면 DB 센서 합계를 사용합니다.')
        st.bar_chart(daily.set_index('기간')[['전력량_kWh']], height=260)
        st.subheader('호기별 전력사용량')
        periods = daily['기간'].tolist()
        if periods:
            preferred = period_labels(pd.Series([report_date]), unit).iloc[0]
            energy_period = st.selectbox('전력량 비교 기간', periods,
                index=periods.index(preferred) if preferred in periods else len(periods)-1,
                key=f'energy-bar-period-{unit}-{report_date.year}-{report_date.month}')
            dates = chart_ledger.loc[period_labels(chart_ledger['생산일'], unit) == energy_period, '생산일']
            power, power_basis = chart_line_energy(stamp, tuple(lines), dates.min(), dates.max())
            st.caption(f'{energy_period} · {power_basis}')
            power = power.copy()
            power['호기번호'] = pd.to_numeric(power['호기'])
            power = power.sort_values('호기번호')
            power['호기명'] = power['호기번호'].astype(str) + '호기'
            valid = power.dropna(subset=['압출기_kWh','히터·부속_kWh','호기합계_kWh']).copy()
            if not valid.empty:
                segments = valid.melt(id_vars=['호기명'], value_vars=['압출기_kWh','히터·부속_kWh'],
                    var_name='설비', value_name='전력량_kWh')
                segments['설비'] = segments['설비'].str.replace('_kWh','',regex=False)
                bars = alt.Chart(segments).mark_bar(size=18).encode(
                    y=alt.Y('호기명:N', title=None, sort=valid['호기명'].tolist()),
                    x=alt.X('전력량_kWh:Q', title='전력사용량 (kWh)', stack='zero', scale=alt.Scale(zero=True)),
                    color=alt.Color('설비:N', scale=alt.Scale(
                        domain=['압출기','히터·부속'], range=['#69e4cd','#5d8ef0'])),
                    tooltip=['호기명:N','설비:N',alt.Tooltip('전력량_kWh:Q',format=',.1f')])
                valid['합계표시'] = valid['호기합계_kWh'].map(lambda value: f'{value:,.1f} kWh')
                labels = alt.Chart(valid).mark_text(align='left',dx=5,color='#dbe5f1').encode(
                    y=alt.Y('호기명:N',title=None,sort=valid['호기명'].tolist()),
                    x=alt.X('호기합계_kWh:Q',scale=alt.Scale(domain=[0,max(1.,valid['호기합계_kWh'].max()*1.3)])),
                    text='합계표시:N')
                st.altair_chart((bars + labels).properties(height=max(160,len(valid)*29)),use_container_width=True)
            missing = power.loc[~power.index.isin(valid.index),'호기명'].tolist()
            if missing:
                st.caption('계측값 미확인 · 미산출: ' + ', '.join(missing))
            st.caption('호기별 합계는 압출기 + 히터·부속입니다. 공용 설비는 공장 전체 전력량에만 포함하며, 8호기는 히터와 휴게실을 포함합니다.')
    with efficiency_tab:
        st.subheader('호기별 생산 1톤당 전력량')
        periods = daily['기간'].tolist()
        if periods:
            preferred = period_labels(pd.Series([report_date]), unit).iloc[0]
            efficiency_period = st.selectbox('효율 비교 기간', periods,
                index=periods.index(preferred) if preferred in periods else len(periods)-1,
                key=f'efficiency-period-{unit}-{report_date.year}-{report_date.month}')
            dates = chart_ledger.loc[period_labels(chart_ledger['생산일'], unit) == efficiency_period, '생산일']
            power, efficiency_basis = chart_line_energy(stamp, tuple(lines), dates.min(), dates.max())
            efficiency = line_energy_efficiency(power, r, dates.min(), dates.max())
            valid = efficiency.dropna(subset=['톤당전력량']).copy()
            st.caption(f'{efficiency_period} · {efficiency_basis} · 전체 조 생산량 기준')
            if not valid.empty:
                valid['표시'] = valid['톤당전력량'].map(lambda value: f'{value:,.1f} kWh/톤')
                base = alt.Chart(valid).encode(
                    y=alt.Y('호기명:N', title=None, sort=valid['호기명'].tolist()),
                    x=alt.X('톤당전력량:Q', title='생산 1톤당 전력량 (kWh/톤)',
                        scale=alt.Scale(domain=[0,max(1.,valid['톤당전력량'].max()*1.4)])),
                    tooltip=['호기명:N',alt.Tooltip('톤당전력량:Q',title='톤당 전력량 (kWh/톤)',format=',.1f'),
                        alt.Tooltip('호기합계_kWh:Q',title='전력량 (kWh)',format=',.1f'),
                        alt.Tooltip('생산량_톤:Q',title='생산량 (톤)',format=',.2f')])
                bars = base.mark_bar(color='#69e4cd',size=18,cornerRadiusEnd=4)
                labels = base.mark_text(align='left',dx=5,color='#dbe5f1').encode(text='표시:N')
                st.altair_chart((bars+labels).properties(height=max(160,len(valid)*29)),use_container_width=True)
            missing = efficiency.loc[efficiency['톤당전력량'].isna(),'호기명'].tolist()
            if missing:
                st.caption('생산실적 또는 전력량 부족으로 미산출: ' + ', '.join(missing))
            st.caption('호기별 (압출기 + 히터·부속 전력량) ÷ 해당 호기 전체 조 생산량(톤). 공용 설비는 제외합니다. 값이 낮을수록 톤당 전력 사용이 적습니다.')
            st.caption('제품·LOT 규모 차이도 함께 비교하세요. 청구 기준은 추정값이며 전력의 달력일과 생산일(08:30 기준) 경계가 다릅니다.')
            with st.expander('호기별 전력 효율 계산 내역'):
                table = efficiency[['호기명','호기합계_kWh','생산량_톤','톤당전력량']].rename(
                    columns={'호기명':'호기','호기합계_kWh':'전력량_kWh','톤당전력량':'톤당전력량_kWh_톤'})
                st.dataframe(table,hide_index=True,use_container_width=True)

    with comparison_tab:
        st.subheader('호기별 생산량·전력 효율 비교')
        periods = daily['기간'].tolist()
        if periods:
            preferred = period_labels(pd.Series([report_date]), unit).iloc[0]
            comparison_period = st.selectbox('생산량·효율 비교 기간', periods,
                index=periods.index(preferred) if preferred in periods else len(periods)-1,
                key=f'production-efficiency-period-{unit}-{report_date.year}-{report_date.month}')
            dates = chart_ledger.loc[period_labels(chart_ledger['생산일'], unit) == comparison_period, '생산일']
            power, comparison_basis = chart_line_energy(stamp, tuple(lines), dates.min(), dates.max())
            comparison = line_energy_efficiency(power, r, dates.min(), dates.max())
            st.caption(f'{comparison_period} · {comparison_basis} · 전체 조 생산량 기준')
            if comparison['생산량_톤'].notna().any():
                base = alt.Chart(comparison).encode(
                    x=alt.X('호기명:N',title='호기',sort=comparison['호기명'].tolist()),
                    tooltip=['호기명:N',
                        alt.Tooltip('생산량_톤:Q',title='생산량 (톤)',format=',.2f'),
                        alt.Tooltip('톤당전력량:Q',title='톤당 전력량 (kWh/톤)',format=',.1f'),
                        alt.Tooltip('호기합계_kWh:Q',title='전력량 (kWh)',format=',.1f')])
                bars = base.mark_bar(color='#69e4cd',opacity=.8).encode(
                    y=alt.Y('생산량_톤:Q', title='생산량 (톤)',scale=alt.Scale(zero=True),
                        axis=alt.Axis(orient='left',titleColor='#69e4cd')))
                line = base.mark_line(color='#ffbd69',point=True,strokeWidth=2).encode(
                    y=alt.Y('톤당전력량:Q',title='톤당 전력량 (kWh/톤)',scale=alt.Scale(zero=True),
                        axis=alt.Axis(orient='right',titleColor='#ffbd69',grid=False)))
                st.altair_chart((bars+line).resolve_scale(y='independent').properties(height=360),use_container_width=True)
                st.caption('민트색 막대·왼쪽 축: 생산량(톤) / 주황색 꺾은선·오른쪽 축: 톤당 전력량(kWh/톤).')
            else:
                st.caption('선택 기간에 비교할 생산실적이 없습니다.')
            st.caption('생산량이 많고 톤당 전력량이 적은 호기를 함께 확인할 수 있습니다. 제품·LOT 규모·가동 조건 차이를 고려해 비교하세요.')
            st.caption('생산량이 0이거나 전력값이 없는 호기는 효율을 산출하지 않습니다. 청구 기준 효율은 추정값이며 전력과 생산 집계의 시간 경계가 다릅니다.')


shift_table = pd.DataFrame(m['crew_shift_schedule'],columns=['생산일','공장','A조','B조','확인사항'])
with st.expander('날짜별 A/B조 주야 근무 확인'):
    st.caption('선택한 공장의 전체 생산실적에 기록된 juya 코드 기준입니다. 1=주간(08:30~20:30), 2=야간(20:30~다음 날 08:30). 선택한 호기·조의 생산 유무로 근무조를 추정하지 않습니다.')
    st.dataframe(shift_table.replace({'주간':'주간 근무','야간':'야간 근무'}),hide_index=True,use_container_width=True)
    st.download_button('날짜별 근무조 주야간 CSV',shift_table.to_csv(index=False).encode('utf-8-sig'),f'crew_shifts_{m["range_start"]}_{m["range_end"]}.csv','text/csv')
    st.caption('생산실적 없음은 휴무·근무예정 여부를 확정하는 뜻이 아닙니다. 인원 계산에서는 원본을 보존하고 2공장 D→A·E→B 통일 후 같은 날짜·주야간 생산실적 기준으로 보정합니다.')

st.caption('생산량·LOT·금월 지표는 선택 조 기준입니다. A/B조 비교 카드·압출기 전력량은 선택 라인의 조회 기간 전체 기준입니다. 가동률은 선택 라인 중 날짜별 가동 이력이 있는 호기만 포함합니다. 전체 사용 전력량은 공장 전체이며, 전력과 가동률을 조별로 배분하지 않습니다.')

overview, details, lot_tab, basis = st.tabs(['생산 현황', '기간 리포트', '통합 LOT 검토', '계산 기준 · 자동화'])
with overview:
    st.subheader(f'시각화 · {crew_view}')
    st.caption('차트의 생산량이 없는 날짜는 0으로 표시합니다. 가동률은 생산실적 유무와 관계없이 해당 날짜의 RPM 기록으로 계산합니다.')
    chart_unit = st.radio('시각화 집계 단위', ['일별','주별','월별'], index=['일간','주간','월간'].index(period), horizontal=True)
    render_visualizations(chart_unit)
    with st.expander('날짜별 조별 생산량 표'):
        st.dataframe(trend.reset_index(), hide_index=True, use_container_width=True)
    st.download_button('생산 추이 CSV 다운로드', trend.to_csv().encode('utf-8-sig'), f'trend_{m["day"]}_{crew_view}.csv', 'text/csv')
    st.subheader('라인별 생산량과 LOT SIZE')
    st.dataframe(byline, hide_index=True, use_container_width=True)
    st.download_button('라인별 CSV 다운로드', byline.to_csv(index=False).encode('utf-8-sig'), f'production_{m["day"]}_{crew_view}.csv', 'text/csv')

with details:
    st.subheader(f'{m["range_label"]} {period_label} 리포트 · {crew_view}')
    if m['source_present']:
        st.write(f"선택 라인·{crew_view}의 {period_label} 생산량은 **{m['total']:,.0f} kg**, 같은 라인의 기간 비교값은 A조 **{m['a']:,.0f} kg**, B조 **{m['b']:,.0f} kg**입니다. 금월 누적 생산량은 **{m['month_total']:,.0f} kg**이며, 가동일 **{m['working_days']}일** 기준 일평균은 **{number(m['daily_average'], ' kg')}**입니다.")
    elif m['runtime_holiday']:
        st.info('전체 공장의 생산량 합계가 0인 휴무일입니다.')
    else:
        st.warning('원본 실적이 없어 일일 생산 요약을 확정할 수 없습니다.')
    st.caption(f"DB 가동률: 가동 분 {number(m['operating_minutes'], '분')} / 기준 {m['expected_minutes']:,}분. 기록 누락 {m['missing_runtime_minutes']:,}분은 가동 분에 더하지 않습니다.")
    st.caption('가동 분은 RPM 250 초과 원본 기록을 시간당 최대 60분으로 집계합니다. 주별·월별 집계는 일별 분자와 분모를 합산합니다.')
    st.caption(f"인원 {m['people']}명·교대 · {m['people_source']} · {m['people_note']}")
    if not m['people']: st.info('인당 생산량: 자동 계산에 필요한 생산일보 인원 기록을 확인하세요.')
    with st.expander('인당 생산량 · 인원 조 코드 연결 근거'):
        mapping = pd.DataFrame(m.get('people_mapping_records',[]))
        st.caption('2공장 D→A·E→B를 기본 적용하고, 같은 날짜·공장·주야간의 생산실적 조가 다르면 보정합니다. 적용인원은 총원-결원이며, 원본 CSV의 현원 값은 변경하지 않습니다. 1공장의 반대 조 기록도 같은 생산실적 기준으로 연결합니다.')
        if not mapping.empty:
            st.dataframe(mapping,hide_index=True,use_container_width=True)
            st.download_button('인원 조 코드 연결 CSV',mapping.to_csv(index=False).encode('utf-8-sig'),f'headcount_crew_mapping_{m["range_start"]}_{m["range_end"]}.csv','text/csv')
        else:
            st.caption('개별 호기에는 일보 인원을 자동 배분하지 않습니다.')

    if m['energy'] is None: st.info('전체 전력량: DB에서 해당 기간의 센서 사용량을 확인할 수 없습니다.')
    html = render_period_html(m, byline, groups, ledger, weekly)
    c1, c2 = st.columns(2)
    c1.download_button('리포트 HTML 다운로드 · 인쇄/PDF', html, f'report_{period}_{m["range_start"]}_{m["range_end"]}_{crew_view}.html', 'text/html', use_container_width=True)
    if c2.button('현재 리포트를 서버에 저장', use_container_width=True):
        folder = save_period_report(m, byline, groups, ledger, weekly)
        st.success(f'저장 완료: {folder}')
    st.caption('HTML 파일을 브라우저에서 열고 인쇄를 누르면 PDF로 저장할 수 있습니다. 개인 급여·이름은 포함하지 않습니다.')
with lot_tab:
    st.subheader('통합 그룹과 단독 LOT')
    st.write('p_grplot_list 연결표의 그룹 번호로 LOT를 합칩니다. 연결표에 없는 LOT는 정상적인 단독 LOT로 계산합니다. 스케줄 순번이나 GRADE·COLOR로 그룹을 추정하지 않습니다.')
    st.info(f"통합 그룹 {m['integrated_groups']}개 + 단독 LOT {m['standalone_lots']}개 = 총 {len(groups)}개 묶음")
    st.caption('표의 수량은 선택 날짜·라인의 실제 생산량입니다. 여러 날짜에 걸친 그룹의 전체 생산량은 아닙니다. 그룹에 서로 다른 라인·GRADE·COLOR가 있으면 모두 표시하며, 연결표의 그룹은 유지합니다.')
    st.dataframe(groups, hide_index=True, use_container_width=True)
    st.download_button('LOT 묶음 CSV 다운로드', groups.to_csv(index=False).encode('utf-8-sig'), f'lots_{m["day"]}_{crew_view}.csv', 'text/csv')
with basis:
    st.markdown('''
| 지표 | 계산 기준 |
|---|---|
| 일일/A조/B조 생산량 | 생산실적 `pr_qty` 합계 / `jo`별 합계 (kg) |
| 평균 LOT SIZE | 당일 생산량 ÷ 라인·LOT 기준 중복 제거 LOT 수 |
| 통합 LOT SIZE | 당일 생산량 ÷ (통합 그룹 수 + 단독 LOT 수). `p_grplot_list` 기준, 미등록 LOT는 단독 처리 |
| 가동률 | 업로드 DB `HMI_feeder_list`의 `p_date`·`line`·시간대별 RPM(`mval02`) 250 초과 기록 수, 시간당 최대 60분. 날짜별 RPM 가동 분이 양수인 호기만 포함. 가동 분 ÷ (가동 호기·일 수 × 1,440분) × 100. 포함한 호기의 당일 정지·누락 시간은 분모에 유지. 주별·월별 집계는 일별 분자·분모 합산. MES 서버에 접속하지 않음 |
| 생산 라인 비율 | 당일 생산량이 양수인 라인 수 ÷ 선택 라인 수. 센서 기반 가동률과 다름 |
| 금월 가동일 | 해당 월 1일부터 기준일까지 선택 라인 합계 생산량이 양수인 날짜 수 |
| 금월 일평균 | 금월 누적 생산량 ÷ 금월 가동일 |
| 인당 생산량 | 선택 조 생산량 ÷ 일보의 (tot_cnt-abs_cnt) 합계(명·교대). 2공장 D→A·E→B 통일, 같은 날짜·공장·주야간 생산실적 조로 보정. 공장 전체 라인 선택 시 자동 적용. 개별 호기는 배분 기준이 없어 미산출 |
| 전체 사용 전력량 | 한전 청구 자료가 있는 월: 달력일 센서 합계에 월 청구량/월 센서 합계 계수 적용, 0.1 kWh 잔여량 배분. 청구 자료가 없는 월: DB 센서 사용량 합계(생산일 08:30~다음 날 08:30). 공장·라인 필터와 무관, 태양광(217) 제외 |
| 압출기 전력량 | 선택 호기 센서 합계. 청구 자료가 있는 월은 월 계수로 보정한 추정값, 없는 월은 DB 센서 사용량 합계 |
''')
    st.caption('주별 집계는 월내 1~7·8~14·15~21·22~28·29~말일입니다. 생산·전력은 일간 원장의 합이며, LOT 평균·인당 생산량은 합산한 분자와 분모로 재계산합니다. 가동률은 DB 일간 가동 분 합계 ÷ 일간 기준 분 합계로 계산합니다. 일별 비율을 단순 평균하지 않습니다. 기간 LOT/통합 묶음 수는 일별 건수의 합입니다.')
    st.caption('가동 분은 MES 재현식과 같이 원본 행을 세고 시간당 60분으로 제한합니다. 기록 확보율은 별도로 중복 분을 제거해 산출합니다. RPM 250의 경계 처리는 당시 MES 검증 자료에 없어 초과 조건으로 재현합니다.')
    st.subheader('자동 작성과 예약 실행')
    st.write('날짜·라인을 변경하면 지표와 리포트가 자동 갱신됩니다. 웹페이지를 열지 않고도 아래 명령으로 파일을 생성할 수 있습니다.')
    st.code('/data/hackathon/.venv/bin/python /data/hackathon/production_report.py --date yesterday', language='bash')
    st.caption('yesterday는 한국시간 전일입니다. 가동률은 업로드 DB RPM 기록으로 계산합니다. 인원은 해당 날짜의 생산일보를 다시 조회합니다.')
    st.write('한국시간으로 실행 시각을 정한 뒤 예약을 등록하면, 웹페이지가 닫혀 있어도 서버가 전일 보고서를 생성합니다. 먼저 왼쪽에서 예약 리포트 조회 조건(라인·근무조)을 저장하세요.')
    st.caption('생산일은 다음 날 08:30에 끝나므로 예약 시각은 한국시간 09:00 이후로 설정합니다.')
    with st.form('daily_schedule'):
        hour = st.number_input('매일 실행 시각 (한국시간 · 시)', min_value=9, max_value=23, value=9)
        minute = st.number_input('분', min_value=0, max_value=59, value=0)
        submitted = st.form_submit_button('매일 전일 리포트 예약 등록 / 변경')
    if submitted:
        try:
            configure_schedule(hour, minute)
            st.success(f'매일 한국시간 {hour:02d}:{minute:02d} 예약을 등록했습니다. 서버 cron 서비스가 실행 중이어야 동작합니다.')
        except (OSError, ValueError, RuntimeError) as ex:
            st.error(str(ex))
    if st.button('이 리포트의 예약 해제'):
        try:
            configure_schedule(0, 0, enabled=False)
            st.success('이 리포트의 예약을 해제했습니다.')
        except (OSError, ValueError, RuntimeError) as ex:
            st.error(str(ex))
    st.write('직접 등록할 경우: 매일 한국시간 09:00은 UTC 서버에서 다음과 같습니다.')
    st.code('0 0 * * * /data/hackathon/.venv/bin/python /data/hackathon/production_report.py --date yesterday >> /data/hackathon/reports/automation.log 2>&1', language='bash')
    st.caption('등록 버튼을 누르기 전에는 예약하지 않습니다. 실적 CSV가 갱신되지 않으면 데이터 없음 경고 리포트가 생성됩니다. 결과: reports/YYYY-MM-DD/report.html')
