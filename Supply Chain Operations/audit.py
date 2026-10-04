"""Source audits and descriptive robustness checks; source files stay unchanged."""
import hashlib
import json
from itertools import combinations
import numpy as np
import pandas as pd
from trend_sensitivity import comparable_trends


def enrich(raw, orders, tables, root):
    e = orders.loc[orders.eligible]
    tables['comparable_market_monthly'], tables['comparable_market_summary'] = comparable_trends(orders)
    tables['first_class_pattern'] = e.loc[e.shipping_mode.eq('First Class')].groupby(['actual_days', 'scheduled_days', 'delivery_status']).size().rename('eligible_orders').reset_index()
    tables['duplicate_columns'] = pd.DataFrame([(a, b) for a, b in combinations(raw.columns, 2) if raw[a].equals(raw[b])], columns=['field_a', 'field_b'])
    checks = []
    for label, residual in [
        ('sales_minus_quantity_times_price', raw.Sales - raw['Order Item Quantity'] * raw['Order Item Product Price']),
        ('total_minus_sales_minus_discount', raw['Order Item Total'] - (raw.Sales - raw['Order Item Discount'])),
        ('discount_minus_sales_times_discount_rate', raw['Order Item Discount'] - raw.Sales * raw['Order Item Discount Rate']),
        ('profit_minus_total_times_profit_ratio', raw['Order Profit Per Order'] - raw['Order Item Total'] * raw['Order Item Profit Ratio']),
    ]:
        checks.append((label, len(residual), int(residual.abs().le(.01).sum()), .01, residual.abs().median(), residual.abs().max()))
    tables['financial_checks'] = pd.DataFrame(checks, columns=['identity_test', 'item_rows', 'within_tolerance', 'tolerance', 'median_absolute_residual', 'max_absolute_residual'])
    tables['negative_profit'] = raw.assign(negative_profit=raw['Order Profit Per Order'].lt(0)).groupby('Delivery Status').agg(item_rows=('Order Item Id', 'size'), negative_profit_items=('negative_profit', 'sum')).reset_index()
    tables['negative_profit']['negative_profit_share'] = tables['negative_profit'].negative_profit_items / tables['negative_profit'].item_rows
    tables['duration_gap_distribution'] = e.groupby(['shipping_mode', 'duration_gap_days']).agg(eligible_orders=('order_id', 'size'), late_orders=('is_late', 'sum')).reset_index()
    tables['duration_gap_distribution']['within_mode_share'] = tables['duration_gap_distribution'].eligible_orders / tables['duration_gap_distribution'].groupby('shipping_mode').eligible_orders.transform('sum')
    tables['same_day_conventions'] = orders.loc[orders.shipping_mode.eq('Same Day')].groupby(['actual_days', 'scheduled_days', 'shipment_elapsed_hours', 'delivery_status']).size().rename('orders').reset_index()
    c = orders.groupby('order_month').agg(total_orders=('order_id', 'size'), eligible_orders=('eligible', 'sum'), late_orders=('is_late', 'sum'), canceled_orders=('is_canceled', 'sum'), complete_closed_orders=('complete_closed', 'sum'), first_order=('order_date', 'min'), last_order=('order_date', 'max'), last_shipment=('shipping_date', 'max')).reset_index()
    c['other_order_status_orders'] = c.total_orders - c.complete_closed_orders
    c['other_status_share'] = c.other_order_status_orders / c.total_orders
    tables['monthly_coverage'] = c
    weights = e.shipping_mode.value_counts(normalize=True)
    for dimension in ['market', 'region', 'order_year']:
        s = e.groupby([dimension, 'shipping_mode']).is_late.agg(['sum', 'count']).reset_index()
        s['contribution'] = s['sum'] / s['count'] * s.shipping_mode.map(weights)
        adjusted = s.groupby(dimension).agg(mode_standardized_rate=('contribution', 'sum'), modes_present=('shipping_mode', 'nunique'), smallest_mode_orders=('count', 'min')).reset_index()
        adjusted.loc[adjusted.modes_present.ne(len(weights)), 'mode_standardized_rate'] = np.nan
        actual = e.groupby(dimension).is_late.agg(['sum', 'count']).reset_index()
        actual['late_rate'] = actual['sum'] / actual['count']
        tables[f'{dimension}_mode_standardized'] = actual.merge(adjusted, on=dimension)
    tables['mode_by_year'] = e.groupby(['order_year', 'shipping_mode']).is_late.agg(['sum', 'count']).reset_index()
    tables['mode_by_year']['late_rate'] = tables['mode_by_year']['sum'] / tables['mode_by_year']['count']
    populations = {'primary': e, 'exclude_final_month': e.loc[e.order_month.ne(orders.order_month.max())], 'complete_closed': e.loc[e.complete_closed]}
    tables['robustness_overall'] = pd.DataFrame([(name, len(f), int(f.is_late.sum()), f.is_late.mean()) for name, f in populations.items()], columns=['population', 'eligible_orders', 'late_orders', 'late_rate'])
    retained = {'Order Item Id', 'Order Id', 'order date (DateOrders)', 'Market', 'Order Region', 'Delivery Status', 'Order Status', 'Shipping Mode', 'Late_delivery_risk', 'Days for shipping (real)', 'Days for shipment (scheduled)', 'shipping date (DateOrders)', 'Order Item Cardprod Id', 'Product Category Id', 'Category Name', 'Order Item Quantity', 'Order Item Product Price', 'Order Item Discount', 'Order Item Discount Rate', 'Sales', 'Order Item Total', 'Order Profit Per Order'}
    tables['field_disposition'] = pd.DataFrame([(col, 'retained' if col in retained else 'omitted', 'Relevant analysis field' if col in retained else 'Unnecessary for shipping analysis; personal fields excluded') for col in raw.columns], columns=['source_field', 'disposition', 'reason'])
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ['DataCoSupplyChainDataset.csv', 'DescriptionDataCoSupplyChain.csv']}
    (root / 'outputs' / 'source_manifest.json').write_text(json.dumps({'sha256': hashes, 'source': 'https://data.mendeley.com/datasets/8gx2fvg2k6/5', 'contributors': ['Fabian Constante', 'Fernando Silva', 'António Pereira'], 'license': 'CC BY 4.0', 'version': 5, 'publication_date': '2019-03-12', 'status': 'review draft'}, indent=2), encoding='utf-8')
    for f in [tables['geography_market'], tables['geography_region'], tables['mode_performance'], tables['monthly'], tables['quarterly']]:
        assert f.eligible_orders.sum() == len(e)
        assert f.late_orders.sum() == e.is_late.sum()
    return tables
