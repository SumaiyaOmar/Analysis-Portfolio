"""Local interactive dashboard for the DataCo shipping review."""

from pathlib import Path
from textwrap import wrap

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from trend_sensitivity import comparable_trends, RULE, WARNING


ROOT = Path(__file__).resolve().parent
ORDERS_PATH = ROOT / "outputs" / "csv" / "orders_clean.csv"
BLUE = "#28648a"
TEAL = "#39a6a3"
RED = "#c65a55"
SLATE = "#536574"

st.set_page_config(
    page_title="DataCo | Shipping reliability",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data
def load_orders() -> pd.DataFrame:
    if not ORDERS_PATH.exists():
        raise FileNotFoundError("Run `python analyze.py` first to generate outputs/csv/orders_clean.csv.")
    df = pd.read_csv(ORDERS_PATH, parse_dates=["order_date", "shipping_date", "order_month"])
    df["order_year"] = df["order_year"].astype("Int64")
    return df


try:
    data = load_orders()
except FileNotFoundError as error:
    st.error(str(error))
    st.stop()

st.markdown(
    """
    <style>
    .stApp { background: #f5f7f9; color: #263746; }
    [data-testid="stSidebar"] { background: #edf2f5; }
    .hero { padding: 1.05rem 1.35rem; border-radius: 14px; background: linear-gradient(110deg,#244d67,#347d91); color: white; margin-bottom: 1rem; }
    .hero h1 { color: white; font-size: 1.8rem; margin: 0 0 .25rem; }
    .hero p { color: #e9f3f5; margin: 0; }
    .context { font-size: .88rem; color: #536574; padding: .55rem .2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="hero"><h1>Shipping reliability | DataCo</h1>'
    '<p>Order-level view of geography, shipping mode, and reliability over time · Portfolio Project</p></div>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Filter the analysis")
    markets = st.multiselect("Markets", sorted(data["market"].dropna().unique()), default=sorted(data["market"].dropna().unique()))
    regions = st.multiselect("Regions", sorted(data["region"].dropna().unique()), default=sorted(data["region"].dropna().unique()))
    modes = st.multiselect("Shipping modes", sorted(data["shipping_mode"].dropna().unique()), default=sorted(data["shipping_mode"].dropna().unique()))
    statuses = st.multiselect("Order statuses", sorted(data["order_status"].dropna().unique()), default=sorted(data["order_status"].dropna().unique()))
    years = sorted(data["order_year"].dropna().astype(int).unique())
    selected_years = st.multiselect("Order years", years, default=years)
    all_months = sorted(data["order_month"].dropna().unique())
    month_range = st.select_slider(
        "Order month range",
        options=all_months,
        value=(all_months[0], all_months[-1]),
        format_func=lambda value: pd.Timestamp(value).strftime("%b %Y"),
    )
    st.caption("All charts and rates use distinct orders and the same filters.")
    st.divider()
    st.markdown("**Population**  \nAdvance shipping + Shipping on time + Late delivery. Shipping canceled is reported separately.")
    st.markdown("**Late rate**  \nLate-labeled eligible orders ÷ eligible distinct orders.")

filtered = data.loc[
    data["market"].isin(markets)
    & data["region"].isin(regions)
    & data["shipping_mode"].isin(modes)
    & data["order_status"].isin(statuses)
    & data["order_year"].astype("Int64").isin(selected_years)
    & data["order_month"].between(pd.Timestamp(month_range[0]), pd.Timestamp(month_range[1]))
].copy()
eligible = filtered.loc[filtered["eligible"]].copy()
canceled_n = int(filtered["is_canceled"].sum())
eligible_n = int(eligible["order_id"].nunique())
late_n = int(eligible["is_late"].sum())
late_rate = late_n / eligible_n if eligible_n else 0
st.markdown(
    f'<div class="context">Filtered coverage: {filtered["order_date"].min().strftime("%d %b %Y") if not filtered.empty else "—"} – {filtered["order_date"].max().strftime("%d %b %Y") if not filtered.empty else "—"} · '
    f'{eligible_n:,} eligible distinct orders · {canceled_n:,} canceled orders excluded · '
    f'{filtered["order_id"].nunique():,} total distinct orders</div>',
    unsafe_allow_html=True,
)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Eligible orders", f"{eligible_n:,}")
k2.metric("Late orders", f"{late_n:,}")
k3.metric("Late rate", f"{late_rate:.1%}" if eligible_n else "—")
k4.metric("Shipping canceled", f"{canceled_n:,}")
st.caption('Coverage caution: January 2018 contains only Pacific Asia. Markets are absent in some earlier periods; missing cohorts are not zero late rates. These historical records do not establish extract completeness.')

if eligible.empty:
    st.warning("No eligible orders match these filters. Broaden the filters to view shipment performance.")
    st.stop()


def selection_label(label, selected, available):
    if len(selected) == len(available):
        return f'All {label}'
    return ', '.join(map(str, selected)) if len(selected) <= 2 else f'{len(selected)} {label} selected'


scope = ' | '.join([
    selection_label('markets', markets, data.market.unique()),
    selection_label('regions', regions, data.region.unique()),
    selection_label('modes', modes, data.shipping_mode.unique()),
    selection_label('statuses', statuses, data.order_status.unique()),
    selection_label('years', selected_years, years),
    f'{pd.Timestamp(month_range[0]):%b %Y} - {pd.Timestamp(month_range[1]):%b %Y}',
])


def plot_chart(fig):
    title = fig.layout.title.text or 'Selected orders'
    title_scope = '<br>'.join(wrap(scope, width=60))
    fig.update_layout(title=dict(text=f'{title}<br><sup>{title_scope}</sup>', font=dict(size=15)), margin=dict(t=110))
    st.plotly_chart(fig, width='stretch')

z = 1.959963984540054

def grouped_rate(frame: pd.DataFrame, dimension: str) -> pd.DataFrame:
    result = (
        frame.groupby(dimension, dropna=False, observed=True)
        .agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum"))
        .reset_index()
    )
    result["late_rate"] = result["late_orders"] / result["eligible_orders"]
    result["share_of_late"] = result["late_orders"] / max(late_n, 1)
    result["low_volume"] = result["eligible_orders"] < 100
    n = result["eligible_orders"].astype(float)
    p = result["late_rate"]
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) / den).pow(0.5)
    result["ci_low"] = (center - half).clip(0, 1)
    result["ci_high"] = (center + half).clip(0, 1)
    result["rate_delta_pp"] = (result["late_rate"] - late_rate) * 100
    return result

geo_tab, mode_tab, time_tab = st.tabs(["Geographic performance", "Shipping-mode performance", "Reliability over time"])

with geo_tab:
    st.subheader(f"Geographic performance | {scope}")
    g1, g2 = st.columns([1, 1.15])
    market_rates = grouped_rate(eligible, "market").sort_values("late_rate", ascending=False)
    region_rates = grouped_rate(eligible, "region").sort_values("late_rate", ascending=False)
    with g1:
        fig = px.bar(
            market_rates.sort_values("late_rate"),
            x="late_rate",
            y="market",
            orientation="h",
            color="late_rate",
            color_continuous_scale=["#8cb8cb", BLUE],
            hover_data={"late_orders": True, "eligible_orders": True, "share_of_late": ":.1%", "ci_low": ":.1%", "ci_high": ":.1%", "rate_delta_pp": ":.1f", "late_rate": ":.1%"},
            labels={"late_rate": "Late rate", "market": "Market"},
            title="Late rate by market",
        )
        fig.update_layout(coloraxis_showscale=False, height=390, margin=dict(l=10, r=12, t=50, b=15))
        fig.update_xaxes(tickformat=".0%")
        plot_chart(fig)
    with g2:
        fig = px.scatter(
            region_rates,
            x="eligible_orders",
            y="late_rate",
            size="late_orders",
            color="late_rate",
            color_continuous_scale=["#8cb8cb", RED],
            hover_name="region",
            hover_data={"late_orders": True, "eligible_orders": True, "share_of_late": ":.1%", "ci_low": ":.1%", "ci_high": ":.1%", "rate_delta_pp": ":.1f", "late_rate": ":.1%"},
            labels={"eligible_orders": "Eligible distinct orders", "late_rate": "Late rate"},
            title="Region rate vs volume",
        )
        fig.update_layout(height=390, margin=dict(l=10, r=12, t=50, b=15))
        fig.update_yaxes(tickformat=".0%")
        plot_chart(fig)
    st.caption("Hover for late/eligible counts, share of filtered late orders, Wilson 95% interval, and percentage-point difference from the filtered overall rate. Groups below 100 eligible orders are low-volume.")
    st.dataframe(region_rates.assign(late_rate=region_rates["late_rate"].map(lambda x: f"{x:.1%}"), share_of_late=region_rates["share_of_late"].map(lambda x: f"{x:.1%}"), rate_delta_pp=region_rates["rate_delta_pp"].map(lambda x: f"{x:+.1f} pp"), confidence_interval=region_rates.apply(lambda r: f"{r.ci_low:.1%}–{r.ci_high:.1%}", axis=1))[["region", "eligible_orders", "late_orders", "late_rate", "share_of_late", "rate_delta_pp", "confidence_interval", "low_volume"]], width='stretch', hide_index=True)
    st.markdown("**Context check:** geography rates can change with the shipping-mode and year mix. Use the shared filters to compare like-for-like combinations; observed differences are descriptive, not causal.")
    year_mode = eligible.groupby(["order_year", "shipping_mode"], observed=True).agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum")).reset_index()
    year_mode["late_rate"] = year_mode["late_orders"] / year_mode["eligible_orders"]
    if not year_mode.empty:
        fig = px.line(year_mode, x="order_year", y="late_rate", color="shipping_mode", markers=True, hover_data=["eligible_orders", "late_orders"], labels={"order_year": "Order year", "late_rate": "Late rate", "shipping_mode": "Mode"}, title="Late rate by mode and order year")
        fig.update_yaxes(tickformat=".0%")
        plot_chart(fig)

with mode_tab:
    st.subheader(f"Shipping-mode performance | {scope}")
    first_class = eligible.loc[eligible.shipping_mode.eq('First Class')]
    uniform_first = first_class.actual_days.eq(2) & first_class.scheduled_days.eq(1)
    st.info(f'First Class source-pattern check (selected orders): {int(uniform_first.sum()):,}/{len(first_class):,} eligible orders record actual_days = 2 and scheduled_days = 1. In the full dataset, all 9,602 eligible First Class orders share this pattern. Validate definitions and source records; no cause is established.')
    mode_rates = grouped_rate(eligible, "shipping_mode")
    gaps = eligible.groupby("shipping_mode", observed=True).agg(
        median_gap_days=("duration_gap_days", "median"),
        gap_p25_days=("duration_gap_days", lambda x: x.quantile(.25)),
        gap_p75_days=("duration_gap_days", lambda x: x.quantile(.75)),
        late_median_gap_days=("duration_gap_days", lambda x: x[eligible.loc[x.index, "is_late"]].median()),
        late_positive_gap_orders=("duration_gap_days", lambda x: int(((x > 0) & eligible.loc[x.index, "is_late"]).sum())),
    ).reset_index()
    mode_rates = mode_rates.merge(gaps, on="shipping_mode", how="left")
    fig = px.bar(mode_rates.sort_values("late_rate"), x="shipping_mode", y="late_rate", color="late_rate", color_continuous_scale=["#9cc7d2", RED], hover_data={"late_orders": True, "eligible_orders": True, "ci_low": ":.1%", "ci_high": ":.1%", "median_gap_days": ":.2f", "late_median_gap_days": ":.2f", "late_rate": ":.1%"}, labels={"late_rate": "Late rate", "shipping_mode": "Shipping mode"}, title="Late rate by shipping mode")
    fig.update_layout(coloraxis_showscale=False, height=390, margin=dict(l=10, r=10, t=50, b=10))
    fig.update_yaxes(tickformat=".0%")
    plot_chart(fig)
    mode_display = mode_rates.copy()
    for column in ['late_rate', 'ci_low', 'ci_high']:
        mode_display[column] = mode_display[column].map(lambda value: f'{value:.1%}')
    st.dataframe(mode_display[["shipping_mode", "eligible_orders", "late_orders", "late_rate", "ci_low", "ci_high", "median_gap_days", "gap_p25_days", "gap_p75_days", "late_median_gap_days", "late_positive_gap_orders"]], width='stretch', hide_index=True)
    same = filtered.loc[filtered["shipping_mode"].eq("Same Day")]
    same_12 = int((same["shipment_elapsed_hours"].sub(12).abs() < 1e-8).sum())
    same_mix = same["actual_days"].value_counts(dropna=False).sort_index()
    same_mix_text = ", ".join(f"{int(day)} day(s): {int(count):,}" for day, count in same_mix.items())
    st.info(f"Same Day timestamp check (filtered): {same_12:,}/{len(same):,} orders have exactly 12 hours between order and shipment timestamps. Recorded actual duration: {same_mix_text or 'no Same Day orders'}. Shipment time is not confirmed delivery arrival.")
    st.caption("Recorded duration gap = actual shipping days − scheduled shipment days; positive means the recorded actual duration exceeds schedule. Mode assignments are observational; mix and commitments differ.")
    by_market_mode = eligible.groupby(["market", "shipping_mode"], observed=True).agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum")).reset_index()
    by_market_mode["late_rate"] = by_market_mode["late_orders"] / by_market_mode["eligible_orders"]
    fig = px.line(by_market_mode, x="market", y="late_rate", color="shipping_mode", markers=True, hover_data=["eligible_orders", "late_orders"], labels={"market": "Market", "late_rate": "Late rate", "shipping_mode": "Mode"}, title="Mode comparison within market")
    fig.update_yaxes(tickformat=".0%")
    plot_chart(fig)

with time_tab:
    st.subheader(f"Reliability over time | {scope}")
    st.warning(WARNING + ' The selected pooled trend describes a changing observed population, not a fixed global cohort.')
    trend = (
        eligible.groupby("order_month", observed=True)
        .agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum"))
        .reset_index()
        .sort_values("order_month")
    )
    calendar = pd.date_range(pd.Timestamp(month_range[0]), pd.Timestamp(month_range[1]), freq='MS')
    trend = trend.set_index('order_month').reindex(calendar).rename_axis('order_month').reset_index()
    trend[['eligible_orders', 'late_orders']] = trend[['eligible_orders', 'late_orders']].fillna(0)
    trend["late_rate"] = trend["late_orders"] / trend["eligible_orders"].replace(0, float('nan'))
    trend["rolling_3m_orders"] = trend["eligible_orders"].rolling(3, min_periods=1).sum()
    trend["rolling_3m_late"] = trend["late_orders"].rolling(3, min_periods=1).sum()
    trend["rolling_3m_rate"] = trend["rolling_3m_late"] / trend["rolling_3m_orders"].replace(0, float('nan'))
    trend["mom_pp"] = trend["late_rate"].diff() * 100
    trend["yoy_pp"] = trend["late_rate"].diff(12) * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=trend["order_month"], y=trend["late_rate"], mode="lines+markers", name="Monthly", line=dict(color=BLUE, width=1.5), hovertemplate="%{x|%b %Y}<br>Rate %{y:.1%}<extra></extra>"))
    fig.add_trace(go.Scatter(x=trend["order_month"], y=trend["rolling_3m_rate"], mode="lines", name="Weighted 3-month", line=dict(color=TEAL, width=3), customdata=trend[["rolling_3m_late", "rolling_3m_orders"]], hovertemplate="%{x|%b %Y}<br>Rolling rate %{y:.1%}<br>Late / eligible %{customdata[0]:,.0f}/%{customdata[1]:,.0f}<extra></extra>"))
    fig.update_layout(title="Selected-order reliability by order month", height=420, margin=dict(l=10, r=10, t=50, b=10), yaxis_tickformat=".0%", yaxis_title="Late rate", xaxis_title="Order month", legend=dict(orientation="h", y=1.05))
    plot_chart(fig)
    st.dataframe(trend.assign(late_rate=trend.late_rate.map(lambda x: '' if pd.isna(x) else f'{x:.1%}'), rolling_3m_rate=trend.rolling_3m_rate.map(lambda x: '' if pd.isna(x) else f'{x:.1%}'), mom_pp=trend["mom_pp"].map(lambda x: "" if pd.isna(x) else f"{x:+.1f} pp"), yoy_pp=trend["yoy_pp"].map(lambda x: "" if pd.isna(x) else f"{x:+.1f} pp"))[["order_month", "eligible_orders", "late_orders", "late_rate", "rolling_3m_rate", "mom_pp", "yoy_pp"]], width='stretch', hide_index=True)
    market_trend = eligible.groupby(["order_month", "market"], observed=True).agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum")).reset_index()
    st.caption(WARNING)
    market_trend["late_rate"] = market_trend["late_orders"] / market_trend["eligible_orders"]
    market_calendar = pd.MultiIndex.from_product([calendar, sorted(eligible.market.unique())], names=['order_month', 'market'])
    market_trend = market_trend.set_index(['order_month', 'market']).reindex(market_calendar).reset_index()
    fig = px.line(market_trend, x="order_month", y="late_rate", color="market", labels={"order_month": "Order month", "late_rate": "Late rate", "market": "Market"}, title="Monthly late rate by market")
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=50, b=10))
    fig.update_yaxes(tickformat=".0%")
    plot_chart(fig)
    mode_mix = eligible.groupby(["order_month", "shipping_mode"], observed=True).agg(eligible_orders=("order_id", "nunique")).reset_index()
    st.caption('Mode shares also reflect changing market coverage; they do not isolate changes in service performance.')
    mode_mix["mode_share"] = mode_mix["eligible_orders"] / mode_mix.groupby("order_month")["eligible_orders"].transform("sum")
    fig = px.area(mode_mix, x="order_month", y="mode_share", color="shipping_mode", labels={"order_month": "Order month", "mode_share": "Share of eligible orders", "shipping_mode": "Mode"}, title="Shipping-mode mix over time")
    fig.update_layout(height=370, margin=dict(l=10, r=10, t=50, b=10))
    fig.update_yaxes(tickformat=".0%")
    plot_chart(fig)
    latest = trend.loc[trend.eligible_orders.gt(0)].iloc[-1]
    no_final = trend.iloc[:-1]
    prior_text = f"{no_final.iloc[-1]['rolling_3m_rate']:.1%} ({no_final.iloc[-1]['order_month']:%b %Y})" if not no_final.empty else 'unavailable for a single-month selection'
    st.caption(f"Coverage check: latest eligible month {latest['order_month']:%b %Y} has {int(latest['eligible_orders']):,} eligible orders; orders through {filtered['order_date'].max():%d %b %Y}. Prior ending-window rate: {prior_text}. Calendar months with no selected orders have no monthly rate. Rolling windows use calendar months; gaps and partial windows require care. Non-missing records alone do not prove extract completeness.")
    coverage = filtered.groupby('order_month').agg(total_orders=('order_id', 'size'), eligible_orders=('eligible', 'sum'), canceled_orders=('is_canceled', 'sum'), complete_closed_orders=('complete_closed', 'sum')).reset_index()
    coverage['other_order_status_orders'] = coverage.total_orders - coverage.complete_closed_orders
    st.dataframe(coverage, hide_index=True, width='stretch')
    st.markdown('**Comparable-market trend sensitivity (selected orders)**')
    st.caption(RULE)
    comparable_monthly, comparable_summary = comparable_trends(filtered)
    if comparable_summary.empty:
        st.info('No selected market has a qualifying six-month coverage window. No comparable trend is inferred.')
    else:
        fig = px.line(comparable_monthly, x='order_month', y='late_rate', color='run_id', markers=True,
                      title='Fixed-market coverage windows', color_discrete_sequence=[BLUE, TEAL, SLATE, '#8ca5b6', '#5f8799'], labels={'late_rate': 'Late rate', 'order_month': 'Order month', 'run_id': 'Market / window'})
        fig.update_yaxes(tickformat='.0%')
        plot_chart(fig)
        comparable_display = comparable_summary.copy()
        for column in ['first_3m_rate', 'last_3m_rate']:
            comparable_display[column] = comparable_display[column].map(lambda value: f'{value:.1%}')
        comparable_display['change_pp'] = comparable_display.change_pp.map(lambda value: f'{value:+.2f} pp')
        st.dataframe(comparable_display, hide_index=True, width='stretch')

with st.expander("Definitions, status sensitivity, and limitations"):
    sensitivity = filtered.loc[filtered["sensitivity_eligible"]]
    sensitivity_rate = sensitivity["sensitivity_late"].sum() / len(sensitivity) if len(sensitivity) else float("nan")
    st.markdown(
        f"""
        - **Primary population:** {len(eligible):,} distinct orders with Advance shipping, Shipping on time, or Late delivery.
        - **Late rate:** {late_n:,} orders labeled Late delivery divided by {eligible_n:,} eligible orders ({late_rate:.1%}).
        - **Canceled:** {canceled_n:,} Shipping canceled orders, outside the primary denominator.
        - **Status sensitivity:** COMPLETE/CLOSED intersected with eligible labels gives {len(sensitivity):,} orders and a {sensitivity_rate:.1%} late rate. This is not the definitive population.
        - **Uncertainty:** displayed 95% Wilson intervals; groups below 100 eligible orders are flagged as low volume.
        - **Limits:** mode/geography associations are descriptive; no inventory, supplier, stock, or verified delivery-arrival data are supplied. Currency is undocumented.
        - **Attribution:** DataCo Smart Supply Chain for Big Data Analysis, Mendeley Data v5, 12 March 2019, CC BY 4.0.
        """
    )
