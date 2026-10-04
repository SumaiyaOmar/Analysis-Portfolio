"""Coverage-based, fixed-market trend windows; descriptive, not causal."""
import pandas as pd

RULE = ('Within each market, include every maximal continuous run of at least six '
        'calendar months with at least 100 eligible orders in every month. Compare '
        'count-weighted first-three-month and last-three-month late rates. No '
        'market covers the full extract consistently; mode and region mix can still change.')
WARNING = ('Geographic coverage changes over time, limiting global comparisons. '
           'January 2018 contains only Pacific Asia; absent market-months are not zero late rates.')


def comparable_trends(orders):
    eligible = orders.loc[orders.eligible].copy()
    monthly_columns = ['market', 'run_id', 'order_month', 'eligible_orders', 'late_orders', 'late_rate']
    summary_columns = ['market', 'run_id', 'start_month', 'end_month', 'months', 'eligible_orders', 'late_orders',
                       'first_3m_eligible', 'first_3m_late', 'first_3m_rate', 'last_3m_eligible', 'last_3m_late', 'last_3m_rate', 'change_pp']
    if eligible.empty:
        return pd.DataFrame(columns=monthly_columns), pd.DataFrame(columns=summary_columns)
    eligible['order_month'] = pd.to_datetime(eligible.order_month)
    calendar = pd.date_range(eligible.order_month.min(), eligible.order_month.max(), freq='MS')
    months, summaries = [], []
    for market, group in eligible.groupby('market', sort=True):
        series = group.groupby('order_month').agg(eligible_orders=('order_id', 'size'), late_orders=('is_late', 'sum')).reindex(calendar, fill_value=0)
        valid = series.eligible_orders.ge(100)
        runs = valid.ne(valid.shift(fill_value=False)).cumsum()
        for _, run in series.loc[valid].groupby(runs[valid]):
            if len(run) < 6:
                continue
            run_id = f'{market} | {run.index[0]:%Y-%m} to {run.index[-1]:%Y-%m}'
            frame = run.rename_axis('order_month').reset_index()
            frame['market'], frame['run_id'] = market, run_id
            frame['late_rate'] = frame.late_orders / frame.eligible_orders
            months.append(frame[monthly_columns])
            first, last = run.iloc[:3].sum(), run.iloc[-3:].sum()
            first_rate, last_rate = first.late_orders / first.eligible_orders, last.late_orders / last.eligible_orders
            summaries.append([market, run_id, run.index[0], run.index[-1], len(run), int(run.eligible_orders.sum()), int(run.late_orders.sum()),
                              int(first.eligible_orders), int(first.late_orders), first_rate,
                              int(last.eligible_orders), int(last.late_orders), last_rate, (last_rate-first_rate)*100])
    return (pd.concat(months, ignore_index=True) if months else pd.DataFrame(columns=monthly_columns),
            pd.DataFrame(summaries, columns=summary_columns))
