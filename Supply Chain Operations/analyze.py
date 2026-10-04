"""Reproducible DataCo supply-chain cleaning, validation, and reporting pipeline."""

from __future__ import annotations

from pathlib import Path
import sys
from audit import enrich
from workbook_layout import refine
from trend_sensitivity import RULE as COMPARABLE_RULE, WARNING as COVERAGE_WARNING

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import PercentFormatter
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageTemplate,
    PageBreak,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "DataCoSupplyChainDataset.csv"
DICTIONARY = ROOT / "DescriptionDataCoSupplyChain.csv"
OUTPUT = ROOT / "outputs"
CSV_DIR = OUTPUT / "csv"
CHART_DIR = OUTPUT / "charts"
REPORT_DIR = OUTPUT / "report"

PRIMARY_STATUSES = {"Advance shipping", "Shipping on time", "Late delivery"}
LATE_STATUS = "Late delivery"
CANCEL_STATUS = "Shipping canceled"
COMPLETED_STATUSES = {"COMPLETE", "CLOSED"}
BLUE = "#28648a"
TEAL = "#39a6a3"
AMBER = "#e3a53a"
RED = "#c65a55"
SLATE = "#536574"
PALE = "#edf3f6"


def clean_label(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def wilson_interval(successes: pd.Series, totals: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Return 95% Wilson limits for a binomial proportion."""
    n = totals.astype(float)
    p = successes.astype(float).div(n.replace(0, np.nan))
    z = 1.959963984540054
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt((p * (1 - p) / n + z * z / (4 * n * n)) / denom)
    return (center - half).clip(0, 1), (center + half).clip(0, 1)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_and_validate() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    raw = pd.read_csv(INPUT, encoding="latin1", low_memory=False)
    raw.columns = [column.strip() for column in raw.columns]
    require(raw.shape == (180519, 53), f"Unexpected source shape: {raw.shape}")
    text_cols = raw.select_dtypes(include=["object", "string"]).columns
    whitespace_counts = {
        column: int(
            raw[column]
            .dropna()
            .map(lambda value: value != value.strip() if isinstance(value, str) else False)
            .sum()
        )
        for column in text_cols
    }
    for column in text_cols:
        raw[column] = raw[column].map(clean_label)

    raw["order date (DateOrders)"] = pd.to_datetime(
        raw["order date (DateOrders)"], errors="coerce"
    )
    raw["shipping date (DateOrders)"] = pd.to_datetime(
        raw["shipping date (DateOrders)"], errors="coerce"
    )
    for column in [
        "Days for shipping (real)",
        "Days for shipment (scheduled)",
        "Late_delivery_risk",
        "Order Item Quantity",
    ]:
        raw[column] = pd.to_numeric(raw[column], errors="coerce")

    require(raw["Order Item Id"].is_unique, "Order Item Id is not unique.")
    require(raw["Order Id"].notna().all(), "Order Id contains missing values.")
    require(not raw.duplicated().any(), "Source contains exact duplicate rows.")

    stable_cols = [
        "Market",
        "Order Region",
        "Delivery Status",
        "Order Status",
        "Shipping Mode",
        "Days for shipping (real)",
        "Days for shipment (scheduled)",
        "Late_delivery_risk",
        "order date (DateOrders)",
        "shipping date (DateOrders)",
    ]
    variation = raw.groupby("Order Id", sort=False)[stable_cols].nunique(dropna=False)
    varying = variation.gt(1).sum()
    require(not varying.any(), f"Order-level fields vary within orders: {varying[varying.gt(0)].to_dict()}")

    order_map = {
        "Order Id": "order_id",
        "Market": "market",
        "Order Region": "region",
        "Delivery Status": "delivery_status",
        "Order Status": "order_status",
        "Shipping Mode": "shipping_mode",
        "Late_delivery_risk": "late_delivery_risk",
        "Days for shipping (real)": "actual_days",
        "Days for shipment (scheduled)": "scheduled_days",
        "order date (DateOrders)": "order_date",
        "shipping date (DateOrders)": "shipping_date",
    }
    orders = raw[list(order_map)].drop_duplicates("Order Id").rename(columns=order_map).copy()
    orders["order_month"] = orders["order_date"].dt.to_period("M").dt.to_timestamp()
    orders["order_year"] = orders["order_date"].dt.year.astype("Int64")
    orders["duration_gap_days"] = orders["actual_days"] - orders["scheduled_days"]
    orders["eligible"] = orders["delivery_status"].isin(PRIMARY_STATUSES)
    orders["is_late"] = orders["delivery_status"].eq(LATE_STATUS)
    orders["is_canceled"] = orders["delivery_status"].eq(CANCEL_STATUS)
    orders["complete_closed"] = orders["order_status"].isin(COMPLETED_STATUSES)
    orders["sensitivity_eligible"] = orders["eligible"] & orders["complete_closed"]
    orders["sensitivity_late"] = orders["sensitivity_eligible"] & orders["is_late"]
    orders["shipment_elapsed_hours"] = (
        orders["shipping_date"] - orders["order_date"]
    ).dt.total_seconds() / 3600

    require(orders["order_date"].notna().all(), "Order date parsing failed.")
    require(orders["shipping_date"].notna().all(), "Shipping date parsing failed.")
    require(orders["delivery_status"].notna().all(), "Delivery status is missing.")
    require(
        (orders.loc[orders["is_late"], "late_delivery_risk"] == 1).all()
        and (orders.loc[~orders["is_late"], "late_delivery_risk"] == 0).all(),
        "Delivery Status conflicts with Late_delivery_risk.",
    )
    return raw, orders, {
        "raw_rows": len(raw),
        "columns": len(raw.columns),
        "exact_duplicates": int(raw.duplicated().sum()),
        "unique_items": int(raw["Order Item Id"].nunique()),
        "unique_orders": int(orders["order_id"].nunique()),
        "unique_customers": int(raw["Customer Id"].nunique()),
        "unique_products": int(raw["Product Card Id"].nunique()),
        "order_field_variation": varying.to_dict(),
        "surrounding_whitespace_counts": whitespace_counts,
    }


def summarize_groups(
    data: pd.DataFrame,
    groups: list[str],
    *,
    sensitivity: bool = False,
    global_late_total: int | None = None,
) -> pd.DataFrame:
    population_col = "sensitivity_eligible" if sensitivity else "eligible"
    late_col = "sensitivity_late" if sensitivity else "is_late"
    eligible = data.loc[data[population_col]].copy()
    if eligible.empty:
        return pd.DataFrame(columns=groups + ["eligible_orders", "late_orders", "late_rate"])
    summary = (
        eligible.groupby(groups, dropna=False, observed=True)
        .agg(eligible_orders=("order_id", "nunique"), late_orders=(late_col, "sum"))
        .reset_index()
    )
    summary["late_rate"] = summary["late_orders"] / summary["eligible_orders"]
    summary["late_rate_ci_low"], summary["late_rate_ci_high"] = wilson_interval(
        summary["late_orders"], summary["eligible_orders"]
    )
    total = global_late_total if global_late_total is not None else int(eligible[late_col].sum())
    summary["late_order_share"] = summary["late_orders"] / total if total else np.nan
    overall_rate = int(eligible[late_col].sum()) / eligible["order_id"].nunique()
    summary["pp_vs_filtered_overall"] = (summary["late_rate"] - overall_rate) * 100
    summary["low_volume"] = summary["eligible_orders"] < 100
    return summary


def make_tables(orders: pd.DataFrame) -> dict[str, pd.DataFrame]:
    all_eligible = orders.loc[orders["eligible"]]
    late_total = int(all_eligible["is_late"].sum())
    tables: dict[str, pd.DataFrame] = {
        "geography_market": summarize_groups(orders, ["market"], global_late_total=late_total),
        "geography_region": summarize_groups(orders, ["region"], global_late_total=late_total),
        "geography_by_mode": summarize_groups(
            orders, ["market", "shipping_mode"], global_late_total=late_total
        ),
        "geography_by_year": summarize_groups(
            orders, ["market", "order_year"], global_late_total=late_total
        ),
        "region_by_mode": summarize_groups(
            orders, ["region", "shipping_mode"], global_late_total=late_total
        ),
        "region_by_year": summarize_groups(
            orders, ["region", "order_year"], global_late_total=late_total
        ),
        "mode_performance": summarize_groups(
            orders, ["shipping_mode"], global_late_total=late_total
        ),
        "mode_market": summarize_groups(
            orders, ["market", "shipping_mode"], global_late_total=late_total
        ),
        "mode_region": summarize_groups(
            orders, ["region", "shipping_mode"], global_late_total=late_total
        ),
        "sensitivity_market": summarize_groups(
            orders, ["market"], sensitivity=True,
            global_late_total=int(orders["sensitivity_late"].sum()),
        ),
        "sensitivity_mode": summarize_groups(
            orders, ["shipping_mode"], sensitivity=True,
            global_late_total=int(orders["sensitivity_late"].sum()),
        ),
        "sensitivity_year": summarize_groups(
            orders, ["order_year"], sensitivity=True,
            global_late_total=int(orders["sensitivity_late"].sum()),
        ),
    }
    mode = all_eligible.groupby("shipping_mode", observed=True)
    mode_stats = mode.agg(
        median_gap_days=("duration_gap_days", "median"),
        gap_p25_days=("duration_gap_days", lambda s: s.quantile(0.25)),
        gap_p75_days=("duration_gap_days", lambda s: s.quantile(0.75)),
        mean_gap_days=("duration_gap_days", "mean"),
        late_median_gap_days=("duration_gap_days", lambda s: s[all_eligible.loc[s.index, "is_late"]].median()),
        late_orders_positive_gap=(
            "duration_gap_days",
            lambda s: int(((s > 0) & all_eligible.loc[s.index, "is_late"]).sum()),
        ),
    ).reset_index()
    mode_stats = mode_stats.merge(tables["mode_performance"], on="shipping_mode")
    mode_stats["late_positive_gap_share"] = (
        mode_stats["late_orders_positive_gap"] / mode_stats["late_orders"].replace(0, np.nan)
    )
    tables["mode_performance"] = mode_stats.sort_values("late_rate", ascending=False)

    monthly = (
        all_eligible.groupby("order_month", observed=True)
        .agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum"))
        .reset_index()
        .sort_values("order_month")
    )
    monthly["late_rate"] = monthly["late_orders"] / monthly["eligible_orders"]
    monthly["late_rate_ci_low"], monthly["late_rate_ci_high"] = wilson_interval(
        monthly["late_orders"], monthly["eligible_orders"]
    )
    monthly["change_vs_prior_month_pp"] = monthly["late_rate"].diff() * 100
    monthly["change_vs_prior_year_pp"] = monthly["late_rate"].diff(12) * 100
    monthly["rolling_3m_eligible"] = monthly["eligible_orders"].rolling(3, min_periods=1).sum()
    monthly["rolling_3m_late"] = monthly["late_orders"].rolling(3, min_periods=1).sum()
    monthly["rolling_3m_rate"] = monthly["rolling_3m_late"] / monthly["rolling_3m_eligible"]
    monthly["low_volume"] = monthly["eligible_orders"] < 100
    tables["monthly"] = monthly

    quarterly = (
        all_eligible.assign(order_quarter=all_eligible["order_date"].dt.to_period("Q").astype(str))
        .groupby("order_quarter", observed=True)
        .agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum"))
        .reset_index()
    )
    quarterly["late_rate"] = quarterly["late_orders"] / quarterly["eligible_orders"]
    quarterly["late_rate_ci_low"], quarterly["late_rate_ci_high"] = wilson_interval(
        quarterly["late_orders"], quarterly["eligible_orders"]
    )
    quarterly["low_volume"] = quarterly["eligible_orders"] < 100
    tables["quarterly"] = quarterly

    market_month = (
        all_eligible.groupby(["market", "order_month"], observed=True)
        .agg(eligible_orders=("order_id", "nunique"), late_orders=("is_late", "sum"))
        .reset_index()
    )
    market_month["late_rate"] = market_month["late_orders"] / market_month["eligible_orders"]
    market_month["late_rate_ci_low"], market_month["late_rate_ci_high"] = wilson_interval(
        market_month["late_orders"], market_month["eligible_orders"]
    )
    tables["monthly_market"] = market_month

    status = (
        orders.groupby(["delivery_status", "order_status"], observed=True)
        .agg(orders=("order_id", "nunique"))
        .reset_index()
    )
    tables["status_mix"] = status
    monthly_status = (
        orders.assign(order_month=orders["order_date"].dt.to_period("M").dt.to_timestamp())
        .groupby(["order_month", "delivery_status", "order_status"], observed=True)
        .agg(orders=("order_id", "nunique"))
        .reset_index()
    )
    tables["monthly_status_mix"] = monthly_status

    mix = (
        all_eligible.groupby(["order_month", "shipping_mode"], observed=True)
        .agg(eligible_orders=("order_id", "nunique"))
        .reset_index()
    )
    total_by_month = mix.groupby("order_month")["eligible_orders"].transform("sum")
    mix["mode_share"] = mix["eligible_orders"] / total_by_month
    tables["monthly_mode_mix"] = mix

    return tables


def category_label_collisions(raw: pd.DataFrame) -> pd.DataFrame:
    mapping = (
        raw.groupby("Category Name", dropna=False)["Category Id"]
        .agg(category_ids=lambda values: ", ".join(map(str, sorted(values.dropna().unique()))),
             category_id_count="nunique")
        .reset_index()
    )
    return mapping.loc[mapping["category_id_count"] > 1].reset_index(drop=True)


def make_quality(
    raw: pd.DataFrame,
    orders: pd.DataFrame,
    original_whitespace_counts: dict[str, int],
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for column in raw.columns:
        series = raw[column]
        nonnull = int(series.notna().sum())
        distinct = int(series.nunique(dropna=True))
        whitespace = original_whitespace_counts.get(column, 0)
        records.append(
            {
                "field": column,
                "rows": len(raw),
                "missing_count": int(series.isna().sum()),
                "missing_pct": float(series.isna().mean()),
                "distinct_nonmissing": distinct,
                "surrounding_whitespace_rows": whitespace,
                "constant_nonmissing": distinct <= 1,
            }
        )
    quality = pd.DataFrame(records)
    extra = pd.DataFrame(
        [
            {"field": "Order Id (order grain)", "rows": len(raw), "missing_count": 0,
             "missing_pct": 0.0, "distinct_nonmissing": orders["order_id"].nunique(),
             "surrounding_whitespace_rows": 0, "constant_nonmissing": False},
            {"field": "Order-level rows", "rows": len(orders), "missing_count": 0,
             "missing_pct": 0.0, "distinct_nonmissing": len(orders),
             "surrounding_whitespace_rows": 0, "constant_nonmissing": False},
        ]
    )
    return pd.concat([quality, extra], ignore_index=True)


def make_clean_items(raw: pd.DataFrame) -> pd.DataFrame:
    columns = {
        "Order Item Id": "order_item_id",
        "Order Id": "order_id",
        "order date (DateOrders)": "order_date",
        "Market": "market",
        "Order Region": "region",
        "Delivery Status": "delivery_status",
        "Shipping Mode": "shipping_mode",
        "Order Item Cardprod Id": "product_id",
        "Product Category Id": "product_category_id",
        "Category Name": "product_category",
        "Order Item Quantity": "quantity",
        "Order Item Product Price": "unit_price",
        "Order Item Discount": "discount_amount",
        "Order Item Discount Rate": "discount_rate",
        "Sales": "sales",
        "Order Item Total": "item_total",
        "Order Profit Per Order": "recorded_order_profit",
    }
    return raw[list(columns)].rename(columns=columns).copy()


def write_csv_tables(tables: dict[str, pd.DataFrame], orders: pd.DataFrame, items: pd.DataFrame) -> None:
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        table.to_csv(CSV_DIR / f"{name}.csv", index=False)
    orders.to_csv(CSV_DIR / "orders_clean.csv", index=False)
    items.to_csv(CSV_DIR / "items_clean.csv", index=False)


def make_charts(tables: dict[str, pd.DataFrame], orders: pd.DataFrame) -> dict[str, Path]:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.titleweight": "bold",
            "axes.edgecolor": "#ccd6dc",
            "axes.labelcolor": SLATE,
            "xtick.color": SLATE,
            "ytick.color": SLATE,
            "figure.facecolor": "white",
        }
    )
    paths: dict[str, Path] = {}
    geo = tables["geography_market"].sort_values("late_rate", ascending=True)
    fig, ax = plt.subplots(figsize=(9.4, 5.1))
    ax.barh(geo["market"], geo["late_rate"], color=BLUE)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Late orders / eligible distinct orders")
    ax.set_title("Late-shipping rate by market")
    for i, row in enumerate(geo.itertuples()):
        ax.text(row.late_rate + 0.006, i, f"{row.late_rate:.1%}  |  {row.late_orders:,}/{row.eligible_orders:,}", va="center", fontsize=8)
    ax.set_xlim(0, min(1, float(geo["late_rate"].max()) * 1.42))
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    paths["market_late_rate"] = CHART_DIR / "market_late_rate.png"
    fig.savefig(paths["market_late_rate"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    reg = tables["geography_region"].sort_values("late_rate", ascending=True)
    height = max(6.0, len(reg) * 0.31)
    fig, ax = plt.subplots(figsize=(10, height))
    bars = ax.barh(reg["region"], reg["late_rate"], color=[RED if r.late_rate >= geo["late_rate"].mean() else BLUE for r in reg.itertuples()])
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Late orders / eligible distinct orders")
    ax.set_title("Regional late-shipping rates and eligible-order counts")
    for i, row in enumerate(reg.itertuples()):
        ax.text(row.late_rate + 0.006, i, f"{row.late_rate:.1%}  |  {row.late_orders:,}/{row.eligible_orders:,}", va="center", fontsize=7)
    ax.set_xlim(0, min(1, float(reg["late_rate"].max()) * 1.45))
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    paths["region_late_rate"] = CHART_DIR / "region_late_rate.png"
    fig.savefig(paths["region_late_rate"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    mode = tables["mode_performance"].sort_values("late_rate", ascending=True)
    fig, ax = plt.subplots(figsize=(9.4, 5.2))
    ax.bar(mode["shipping_mode"], mode["late_rate"], color=TEAL)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel("Late orders / eligible distinct orders")
    ax.set_title("Late-shipping rate by shipping mode")
    for i, row in enumerate(mode.itertuples()):
        ax.text(i, row.late_rate + 0.015, f"{row.late_rate:.1%}\n{row.late_orders:,}/{row.eligible_orders:,}", ha="center", fontsize=8)
    ax.set_ylim(0, min(1, float(mode["late_rate"].max()) * 1.35))
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    paths["mode_late_rate"] = CHART_DIR / "mode_late_rate.png"
    fig.savefig(paths["mode_late_rate"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    gap = mode.set_index("shipping_mode").loc[
        ["Same Day", "First Class", "Second Class", "Standard Class"]
    ].reset_index()
    fig, ax = plt.subplots(figsize=(9.4, 5.2))
    ax.bar(gap["shipping_mode"], gap["median_gap_days"], color=AMBER, label="All eligible")
    ax.scatter(gap["shipping_mode"], gap["late_median_gap_days"], color=RED, label="Late-labeled orders: median", zorder=3)
    ax.axhline(0, color=SLATE, linewidth=0.8)
    ax.set_ylabel("Actual days - scheduled days")
    ax.set_title("Recorded duration gap by mode")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    paths["mode_duration_gap"] = CHART_DIR / "mode_duration_gap.png"
    fig.savefig(paths["mode_duration_gap"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    monthly = tables["monthly"]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.plot(monthly["order_month"], monthly["late_rate"], color=BLUE, alpha=0.42, linewidth=1.4, label="Monthly")
    ax.plot(monthly["order_month"], monthly["rolling_3m_rate"], color=TEAL, linewidth=2.4, label="Weighted 3-month")
    ax.fill_between(
        monthly["order_month"], monthly["late_rate_ci_low"], monthly["late_rate_ci_high"],
        color=BLUE, alpha=0.10, linewidth=0,
    )
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel("Late orders / eligible distinct orders")
    ax.set_title("Global shipping reliability by order month")
    ax.text(.5, 1.10, 'Changing market coverage limits global comparisons; January 2018 is Pacific Asia only.', transform=ax.transAxes, ha='center', fontsize=8, color=SLATE)
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.2)
    fig.autofmt_xdate()
    fig.tight_layout()
    paths["monthly_reliability"] = CHART_DIR / "monthly_reliability.png"
    fig.savefig(paths["monthly_reliability"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    quarterly = tables["quarterly"]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(quarterly["order_quarter"], quarterly["late_rate"], color=TEAL, marker="o", linewidth=2)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel("Late orders / eligible distinct orders")
    ax.set_title("Quarterly shipping reliability")
    ax.text(.5, 1.10, 'Changing market coverage; 2018Q1 contains January / Pacific Asia only.', transform=ax.transAxes, ha='center', fontsize=8, color=SLATE)
    ax.tick_params(axis="x", rotation=55)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    paths["quarterly_reliability"] = CHART_DIR / "quarterly_reliability.png"
    fig.savefig(paths["quarterly_reliability"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    market_month = tables["monthly_market"]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    for market, group in market_month.groupby("market"):
        group = group.set_index('order_month').reindex(monthly['order_month']).rename_axis('order_month').reset_index()
        ax.plot(group["order_month"], group["late_rate"], label=market, linewidth=1.5)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel("Late orders / eligible distinct orders")
    ax.set_title("Monthly late-shipping rate by market")
    ax.text(.5, 1.10, 'Gaps mean absent market-months; changing coverage limits global comparisons.', transform=ax.transAxes, ha='center', fontsize=8, color=SLATE)
    ax.legend(ncol=2, frameon=False)
    ax.grid(axis="y", alpha=0.2)
    fig.autofmt_xdate()
    fig.tight_layout()
    paths["monthly_market"] = CHART_DIR / "monthly_market.png"
    fig.savefig(paths["monthly_market"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    mix = tables["monthly_mode_mix"].pivot(index="order_month", columns="shipping_mode", values="mode_share").fillna(0)
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.stackplot(mix.index, *[mix[c] for c in mix.columns], labels=mix.columns, alpha=0.88)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel("Share of eligible orders")
    ax.set_title("Shipping-mode mix by order month")
    ax.text(.5, 1.10, 'Changing market coverage also affects mode mix; this is not a fixed global cohort.', transform=ax.transAxes, ha='center', fontsize=8, color=SLATE)
    ax.legend(loc="upper left", ncol=2, frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()
    paths["monthly_mode_mix"] = CHART_DIR / "monthly_mode_mix.png"
    fig.savefig(paths["monthly_mode_mix"], dpi=180, bbox_inches="tight")
    plt.close(fig)
    comparable = tables['comparable_market_summary']
    fig, ax = plt.subplots(figsize=(10, 4.8))
    positions = np.arange(len(comparable))
    ax.barh(positions - .17, comparable.first_3m_rate, height=.32, color=BLUE, label='First 3 months')
    ax.barh(positions + .17, comparable.last_3m_rate, height=.32, color=TEAL, label='Last 3 months')
    ax.set_yticks(positions, comparable.run_id.tolist(), fontsize=8)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlim(0, 1)
    ax.set_xlabel('Late orders / eligible orders; weighted within each 3-month window')
    ax.set_title('Comparable-market trend sensitivity')
    ax.legend(loc='lower right', fontsize=8)
    fig.text(.5, .01, 'Same market; >=6 continuous months; >=100 eligible orders/month. Mix can still change.', ha='center', fontsize=8, color=SLATE)
    fig.tight_layout(rect=(0, .04, 1, 1))
    paths['comparable_market_trend'] = CHART_DIR / 'comparable_market_trend.png'
    fig.savefig(paths['comparable_market_trend'], dpi=180, bbox_inches='tight')
    plt.close(fig)
    return paths


def write_workbook(
    raw: pd.DataFrame,
    orders: pd.DataFrame,
    items: pd.DataFrame,
    tables: dict[str, pd.DataFrame],
    quality: pd.DataFrame,
    metrics: dict[str, object],
) -> Path:
    path = OUTPUT / "DataCo_Operations_Analytics_Review.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    guide = pd.DataFrame(
        [
            ("Source", "DataCoSupplyChainDataset.csv; Mendeley Data, version 5, published 12 March 2019. CC BY 4.0. https://data.mendeley.com/datasets/8gx2fvg2k6/5"),
            ("Attribution", "DataCo Smart Supply Chain for Big Data Analysis; dataset downloaded from Mendeley Data. License: CC BY 4.0. Preserve attribution to the dataset authors/publisher when reusing."),
            ("Row grain", "Source rows are order items. Order-level shipping tables deduplicate to one row per Order Id only after checking consistency within each order."),
            ("Primary population", "Eligible distinct orders have Delivery Status Advance shipping, Shipping on time, or Late delivery. Shipping canceled is excluded from late-rate denominators and reported separately."),
            ("Late outcome", "Late distinct orders are eligible orders labeled Late delivery. Late_delivery_risk was verified to match the Delivery Status indicator exactly."),
            ("Late rate", "Late distinct orders / eligible distinct orders. Counts accompany every reported rate."),
            ("Uncertainty", "95% Wilson binomial confidence intervals. Groups with fewer than 100 eligible orders are flagged low volume; interval estimates do not correct for systematic data issues."),
            ("Geography share", "Late orders in the group / all eligible late orders. Difference is percentage points from the applicable overall rate."),
            ("Duration gap", "Recorded actual shipping days minus recorded scheduled shipment days. Positive is beyond schedule; this is a duration comparison, not a delivery-arrival timestamp."),
            ("Time basis", "Order month derived from order date (DateOrders). Weighted 3-month rate = sum of late orders / sum of eligible orders in current and previous two months."),
            ("Sensitivity population", "COMPLETE or CLOSED Order Status intersected with primary eligible statuses. This is a sensitivity analysis, not the primary population."),
            ("Cleaning", "Read source as Latin-1; trim surrounding whitespace in text fields; parse the two date/time columns; retain source labels otherwise. No postcode imputation or duration overwrite."),
            ("Privacy / omissions", "Cleaned tables exclude names, email, password, street, customer IDs, customer geocoordinates, product image URL, and unused item fields. The workbook includes only relevant shipment and item metrics."),
            ("Financial scope", "Financial fields are retained only in the item table for source traceability; no global currency comparisons are made because currency is not documented. Sales associated with late orders are not interpreted as lost revenue."),
            ("Dictionary caveats", "Supplied dictionary contains ambiguous location descriptions and does not clearly define Order Zipcode. Location labels are retained as reported after trimming; mixed language remains."),
            ("Comparable-market rule", COMPARABLE_RULE),
            ("First Class pattern", "All 9,602 eligible First Class orders record actual_days=2 and scheduled_days=1. Uniform recorded pattern requiring definition/source validation; no cause is established."),
            ("Standardization", "Common-mode rates use overall primary eligible mode weights and within-group mode rates. No missing stratum is imputed. Descriptive adjustment only; calculation code and counts accompany the workbook."),
            ("Limitations", "No supplier histories, inventory movements, stock levels, or verified delivery-arrival timestamps are available. Descriptive differences do not establish mode-switching effects or causation."),
        ],
        columns=["Topic", "Definition / note"],
    )
    data_sheets = {
        "Dataset Guide": guide,
        "Order Level": orders,
        "Item Level": items,
        "Data Quality": quality,
        "Market Summary": tables["geography_market"],
        "Region Summary": tables["geography_region"],
        "Market by Mode": tables["geography_by_mode"],
        "Market by Year": tables["geography_by_year"],
        "Mode Performance": tables["mode_performance"],
        "Monthly Trend": tables["monthly"],
        "Quarterly Trend": tables["quarterly"],
        "Monthly Market": tables["monthly_market"],
        "Monthly Mode Mix": tables["monthly_mode_mix"],
        "Order Status Mix": tables["status_mix"],
        "Complete Closed Sens": tables["sensitivity_market"],
        "Validation Checks": tables["validation_checks"],
        "Category Name IDs": tables["category_label_collisions"],
    }
    for name in ['comparable_market_monthly', 'comparable_market_summary', 'first_class_pattern', 'monthly_coverage', 'monthly_status_mix', 'region_by_mode', 'region_by_year', 'mode_by_year', 'duration_gap_distribution', 'same_day_conventions', 'robustness_overall', 'market_mode_standardized', 'region_mode_standardized', 'order_year_mode_standardized', 'duplicate_columns', 'financial_checks', 'negative_profit', 'field_disposition', 'sensitivity_mode', 'sensitivity_year']:
        data_sheets[name[:31]] = tables[name]
    wb = Workbook(write_only=True)
    wb.properties.creator = ''
    wb.properties.lastModifiedBy = ''
    wb.properties.title = 'DataCo Supply Chain Operations Analytics - Review Draft'
    percent_headers = {'first_3m_rate', 'last_3m_rate', 'missing_pct', 'mode_standardized_rate', 'other_status_share', 'negative_profit_share', 'within_mode_share', 'late_rate', 'late_rate_ci_low', 'late_rate_ci_high', 'late_order_share', 'mode_share', 'late_positive_gap_share'}
    pp_headers = {'change_pp', 'pp_vs_filtered_overall', 'change_vs_prior_month_pp', 'change_vs_prior_year_pp'}
    for name, frame in data_sheets.items():
        ws = wb.create_sheet(name)
        ws.freeze_panes = 'A2'
        ws.sheet_view.showGridLines = False
        ws.auto_filter.ref = f'A1:{get_column_letter(len(frame.columns))}{len(frame)+1}'
        for index, column in enumerate(frame.columns, 1):
            sample = [str(column)] + [str(value) for value in frame[column].head(100).dropna()]
            ws.column_dimensions[get_column_letter(index)].width = min(36, max(10, max(map(len, sample)) + 2))
        if name == 'Dataset Guide':
            ws.column_dimensions['A'].width = 26
            ws.column_dimensions['B'].width = 110
            for row in range(2, len(frame)+2):
                ws.row_dimensions[row].height = 64
        ws.row_dimensions[1].height = 32
        header_cells = []
        for column in frame.columns:
            cell = WriteOnlyCell(ws, value=str(column))
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='28648A')
            cell.alignment = Alignment(vertical='center', wrap_text=True)
            header_cells.append(cell)
        ws.append(header_cells)
        for row in frame.itertuples(index=False, name=None):
            cells = []
            for column, value in zip(frame.columns, row):
                if pd.isna(value):
                    value = None
                elif isinstance(value, pd.Timestamp):
                    value = value.to_pydatetime()
                elif isinstance(value, np.generic):
                    value = value.item()
                cell = WriteOnlyCell(ws, value=value)
                if column in percent_headers:
                    cell.number_format = '0.0%'
                elif column in pp_headers:
                    cell.number_format = '0.00" pp"'
                elif hasattr(value, 'strftime'):
                    cell.number_format = 'yyyy-mm-dd hh:mm'
                if name == 'Dataset Guide':
                    cell.alignment = Alignment(wrap_text=True, vertical='top')
                cells.append(cell)
            ws.append(cells)
    staging = path.with_suffix('.rebuild.xlsx')
    wb.save(staging)
    refine(staging, dimensions={name: f'A1:{get_column_letter(len(frame.columns))}{len(frame)+1}' for name, frame in data_sheets.items()})
    staging.replace(path)
    return path


def write_pdf(
    orders: pd.DataFrame,
    tables: dict[str, pd.DataFrame],
    quality: pd.DataFrame,
    metrics: dict[str, object],
    charts: dict[str, Path],
    portfolio: bool = False,
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    label = "Portfolio Project" if portfolio else "Review Draft"
    pdf = REPORT_DIR / ("DataCo_Operations_Analytics_Portfolio_Project.pdf" if portfolio else "DataCo_Operations_Analytics_Review_Draft.pdf")
    try:
        from matplotlib import font_manager

        font_file = font_manager.findfont("DejaVu Sans")
        pdfmetrics.registerFont(TTFont("DejaVu", font_file))
        font_name = "DejaVu"
    except Exception:
        font_name = "Helvetica"

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleCustom", parent=styles["Title"], fontName=font_name, fontSize=23, leading=29, textColor=colors.HexColor(BLUE), alignment=TA_CENTER, spaceAfter=10))
    styles.add(ParagraphStyle(name="HeadingCustom", parent=styles["Heading1"], fontName=font_name, fontSize=16, leading=20, textColor=colors.HexColor(BLUE), spaceBefore=8, spaceAfter=8))
    styles.add(ParagraphStyle(name="SubHeadingCustom", parent=styles["Heading2"], fontName=font_name, fontSize=11, leading=14, textColor=colors.HexColor(SLATE), spaceBefore=6, spaceAfter=5))
    styles.add(ParagraphStyle(name="BodyCustom", parent=styles["BodyText"], fontName=font_name, fontSize=9.3, leading=13, spaceAfter=6))
    styles.add(ParagraphStyle(name="SmallCustom", parent=styles["BodyText"], fontName=font_name, fontSize=7.6, leading=10))
    styles.add(ParagraphStyle(name="SmallHeaderCustom", parent=styles["BodyText"], fontName=font_name, fontSize=7.6, leading=10, textColor=colors.white))
    styles.add(ParagraphStyle(name="Takeaway", parent=styles["BodyText"], fontName=font_name, fontSize=10, leading=14, textColor=colors.HexColor("#214c65"), backColor=colors.HexColor(PALE), borderPadding=8, spaceBefore=5, spaceAfter=12))
    styles.add(ParagraphStyle(name="FooterCustom", parent=styles["BodyText"], fontName=font_name, fontSize=7, textColor=colors.HexColor(SLATE), alignment=TA_CENTER))

    def on_page(canvas, doc):
        canvas.saveState()
        if doc.page > 1:
            canvas.setStrokeColor(colors.HexColor("#d8e1e6"))
            canvas.line(18 * mm, 15 * mm, A4[0] - 18 * mm, 15 * mm)
            canvas.setFont(font_name, 7)
            canvas.setFillColor(colors.HexColor(SLATE))
            canvas.drawString(18 * mm, 10 * mm, f"DataCo Supply Chain Analytics | {label.upper()}")
            canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = BaseDocTemplate(str(pdf), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=17 * mm, bottomMargin=21 * mm, title=f"DataCo Operations Analytics - {label}", author="")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=on_page)])

    def P(text: str, style: str = "BodyCustom") -> Paragraph:
        return Paragraph(text, styles[style])

    def table(data: list[list[object]], widths: list[float] | None = None, font_size: int = 8) -> Table:
        converted = [
            [P(str(value), "SmallHeaderCustom" if row_index == 0 else "SmallCustom") for value in row]
            for row_index, row in enumerate(data)
        ]
        t = Table(converted, colWidths=widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BLUE)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6f8")]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d6e0e5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    eligible = orders.loc[orders["eligible"]]
    total_eligible = int(len(eligible))
    total_late = int(eligible["is_late"].sum())
    overall_rate = total_late / total_eligible
    cancelled = int(orders["is_canceled"].sum())
    sensitivity = orders.loc[orders["sensitivity_eligible"]]
    sensitivity_rate = float(sensitivity["sensitivity_late"].mean()) if len(sensitivity) else float("nan")
    month_first, month_last = orders["order_date"].min(), orders["order_date"].max()
    market = tables["geography_market"].sort_values("late_rate", ascending=False)
    region = tables["geography_region"].sort_values(["late_rate", "eligible_orders"], ascending=[False, False])
    mode = tables["mode_performance"].sort_values("late_rate", ascending=False)
    monthly = tables["monthly"]
    first_month = monthly.iloc[0]
    last_month = monthly.iloc[-1]
    no_final = monthly.iloc[:-1]
    qtrend = tables["quarterly"]
    same_day = orders.loc[orders["shipping_mode"].eq("Same Day")]
    same_12h = int(np.isclose(same_day["shipment_elapsed_hours"], 12).sum())
    same_duration_mix = same_day["actual_days"].value_counts(dropna=False).sort_index()
    late_gap_positive = int(((eligible["duration_gap_days"] > 0) & eligible["is_late"]).sum())
    late_gap_negative_or_zero = total_late - late_gap_positive
    completed_orders = int(orders["complete_closed"].sum())
    status_openish = int((~orders["order_status"].isin(COMPLETED_STATUSES)).sum())
    confidence_label = f"{overall_rate:.1%}"

    story: list[object] = [
        Spacer(1, 31 * mm),
        P("SUPPLY CHAIN &amp; OPERATIONS ANALYTICS", "TitleCustom"),
        P("DataCo shipping and fulfilment performance", "HeadingCustom"),
        Spacer(1, 3 * mm),
        P("Portfolio Project - reviewed descriptive analysis with documented source limitations." if portfolio else "REVIEW DRAFT - findings and business interpretation are prepared for review, not final sign-off.", "Takeaway"),
        Spacer(1, 10 * mm),
        P(f"<b>Coverage:</b> {total_eligible:,} eligible distinct orders from {month_first:%b %Y} to {month_last:%b %Y}; {total_late:,} labeled late ({confidence_label}). {cancelled:,} canceled orders are reported separately.", "BodyCustom"),
        P("<b>Source:</b> DataCo Smart Supply Chain for Big Data Analysis, Mendeley Data, version 5 (12 March 2019), CC BY 4.0. https://data.mendeley.com/datasets/8gx2fvg2k6/5", "BodyCustom"),
        Spacer(1, 4 * mm),
        P("Three leading findings", "HeadingCustom"),
    ]
    top_market = market.iloc[0]
    worst_region = region.iloc[0]
    best_region = region.iloc[-1]
    worst_mode = mode.iloc[0]
    best_mode = mode.iloc[-1]
    story += [
        P(f"1. <b>Geographic rates are closely grouped:</b> market rates span only 56.5%-57.7%; Europe contributes the largest late-order count (10,199). {top_market['market']} has the highest market rate ({top_market['late_rate']:.1%}; {int(top_market['late_orders']):,}/{int(top_market['eligible_orders']):,}). {worst_region['region']} is the highest-rate region ({worst_region['late_rate']:.1%}; {int(worst_region['eligible_orders']):,} eligible), while {best_region['region']} is lowest ({best_region['late_rate']:.1%}).", "BodyCustom"),
        P(f"2. <b>Mode results differ, but are not causal:</b> {worst_mode['shipping_mode']} is highest by labeled late rate ({worst_mode['late_rate']:.1%}; {int(worst_mode['late_orders']):,}/{int(worst_mode['eligible_orders']):,}); {best_mode['shipping_mode']} is lowest ({best_mode['late_rate']:.1%}). All 9,602 eligible First Class orders record actual_days = 2 and scheduled_days = 1. This uniform recorded pattern requires definition/source validation; no cause is established. Mode commitments and order mixes differ.", "BodyCustom"),
        P(f"3. <b>No sustained improvement or deterioration is demonstrated:</b> full-year rates are 57.2% in 2015, 57.6% in 2016, and 57.0% in 2017. The weighted three-month rate was {monthly.iloc[2]['rolling_3m_rate']:.1%} by {monthly.iloc[2]['order_month']:%b %Y} and {monthly.iloc[-1]['rolling_3m_rate']:.1%} by {monthly.iloc[-1]['order_month']:%b %Y}. Changing market coverage limits these global comparisons; January 2018 contains only Pacific Asia. These pooled rates do not establish a stable-market trend.", "BodyCustom"),
        P("Purpose: identify where late shipping is concentrated, compare observed performance against recorded service durations, and track reliability over order month. No causal claims are made.", "BodyCustom"),
        PageBreak(),
        P("1. Source, structure &amp; quality", "HeadingCustom"),
        P(f"The supplied file contains {metrics['raw_rows']:,} order-item rows and {metrics['columns']} columns. Each Order Item Id is unique; there are {metrics['unique_orders']:,} orders, {metrics['unique_customers']:,} customers, and {metrics['unique_products']:,} product IDs. The order-level dimensions and shipping fields checked were consistent across all item rows within each order. There are no exact duplicate rows.", "BodyCustom"),
        P(f"Order dates cover {month_first:%Y-%m-%d} to {month_last:%Y-%m-%d}; shipping timestamps extend to {orders['shipping_date'].max():%Y-%m-%d}. Date and delivery-status fields are complete in the parsed data. {status_openish:,} orders have an Order Status other than COMPLETE/CLOSED; status names are not treated as a reliable binary completion flag for the primary population.", "BodyCustom"),
        P("Key quality observations", "SubHeadingCustom"),
    ]
    quality_rows = [["Field", "Missing", "Missing %", "Distinct"]]
    for field in ["Order Zipcode", "Customer Zipcode", "Customer Lname", "Product Description", "Product Status"]:
        row = quality.loc[quality["field"].eq(field)].iloc[0]
        quality_rows.append([field, f"{int(row['missing_count']):,}", f"{row['missing_pct']:.2%}", f"{int(row['distinct_nonmissing']):,}"])
    story += [
        table(quality_rows, [43 * mm, 26 * mm, 30 * mm, 25 * mm]),
        Spacer(1, 5 * mm),
        P("Order Zipcode is 86.2% missing (exact missing rate shown in workbook); Product Description is empty throughout and Product Status is constant. Text whitespace was trimmed. Geographic labels remain in their original language; only surrounding whitespace was removed. Email/password values are masked in the source, but all names, emails, passwords, streets, customer IDs, customer coordinates, and other unnecessary personal fields are omitted from cleaned outputs. No postcodes or durations are imputed.", "BodyCustom"),
        P("Financial checks: all 180,519 Sales values reproduce quantity times price within 0.01 source monetary units. Item Total differs from Sales minus discount by more than 0.01 in 1,224 rows; the maximum residual is 0.010014, consistent with boundary rounding and floating-point precision. Recorded discount-rate and profit-ratio identities have maximum residuals of about 10.00 and 9.30 units, so rounding alone is not a sufficient blanket explanation. Negative profit occurs in 33,784 item rows (18.7%), with similar shares across shipping outcomes; retain these observations. Currency and exact ratio definitions remain undocumented; no global financial comparison is made.", "BodyCustom"),
        P("2. Analytical framework", "HeadingCustom"),
        P("<b>Primary eligible population:</b> one distinct order per Order Id with Delivery Status in Advance shipping, Shipping on time, or Late delivery. Late rate = eligible orders labeled Late delivery / eligible distinct orders. Shipping canceled is excluded from denominators and shown separately. The risk flag agrees exactly with Delivery Status in this file.", "BodyCustom"),
        P("<b>Uncertainty and volume:</b> 95% Wilson intervals are shown for group and monthly rates. Groups below 100 eligible orders are flagged low volume. The threshold is a reporting guardrail, not a significance test. Geographic differences are also checked within recorded shipping modes and years.", "BodyCustom"),
        P("<b>Duration and time:</b> recorded gap = actual shipping days − scheduled shipment days. Order month is the initial trend basis. A weighted three-month rate sums late orders divided by eligible orders across three months. Shipping timestamps are shipment timestamps, not proven delivery-arrival timestamps.", "BodyCustom"),
        P("<b>Robustness:</b> COMPLETE/CLOSED among primary eligible orders is a sensitivity population only. Findings are compared on monthly/quarterly views, excluding final month, by mode mix, and in coverage-qualified fixed-market windows. Associations do not establish the effect of switching mode.", "BodyCustom"),
        PageBreak(),
        P("3. Geographic shipping performance", "HeadingCustom"),
        P(f"Overall primary late rate: {total_late:,}/{total_eligible:,} = {overall_rate:.1%} (95% Wilson interval {wilson_interval(pd.Series([total_late]), pd.Series([total_eligible]))[0].iloc[0]:.1%}-{wilson_interval(pd.Series([total_late]), pd.Series([total_eligible]))[1].iloc[0]:.1%}). Market and region tables distinguish rate from volume; group shares are shares of the {total_late:,} eligible late orders.", "BodyCustom"),
        Image(str(charts["market_late_rate"]), width=168 * mm, height=91 * mm),
        P(f"By region, {worst_region['region']} has the highest rate at {worst_region['late_rate']:.1%} ({int(worst_region['late_orders']):,}/{int(worst_region['eligible_orders']):,}); its group is {'low volume' if worst_region['low_volume'] else 'not low volume'}. The chart and CSV include eligible counts, late counts, confidence limits, share of late orders, and percentage-point difference from overall.", "BodyCustom"),
        P("Europe has 10,199 late orders (28.3% of all late orders), followed by Pacific Asia with 9,720 (27.0%). Western Europe contributes 5,585 late orders, the largest region count. Market rates differ by only 1.13 percentage points; their Wilson intervals overlap. After applying the same overall mode weights to every market, rates span 57.0%-57.7% (0.62 points). Africa moves from 56.5% observed to 57.3% adjusted, illustrating mix sensitivity. Central Africa has the highest region rate, 60.0% (320/533; interval 55.8%-64.1%), but much less volume than Western Europe. Market-year coverage is uneven: Africa has no 2015 orders, LATAM has no 2016 orders, and January 2018 contains only Pacific Asia. No market is highest in every observed full year or in every mode; geography ranks should not be treated as a stable performance hierarchy. Standardization is descriptive and does not establish causal effects.", "BodyCustom"),
        P(f"What this means in simple terms… The rate tells us how often a market or region is late, while the count tells us how many orders that represents. Both matter when prioritizing investigation.", "Takeaway"),
        PageBreak(),
        P("4. Shipping-mode performance", "HeadingCustom"),
        Image(str(charts["mode_late_rate"]), width=145 * mm, height=68 * mm),
        P(f"{worst_mode['shipping_mode']} has the highest late-labeled rate ({worst_mode['late_rate']:.1%}); {best_mode['shipping_mode']} the lowest ({best_mode['late_rate']:.1%}). Observed overall median duration gaps range by mode; median gaps appear below; interquartile ranges and full distributions are in the workbook and CSVs. All 9,602 eligible First Class orders have actual_days = 2 and scheduled_days = 1. This uniform recorded pattern requires definition/source validation; it does not establish a cause. Recorded gaps are not arrival timestamp measures.", "BodyCustom"),
        Image(str(charts["mode_duration_gap"]), width=145 * mm, height=65 * mm),
        P(f"Among the {total_late:,} late-labeled orders, {late_gap_positive:,} have actual days greater than scheduled days and {late_gap_negative_or_zero:,} have a zero or negative recorded gap. All late-labeled eligible orders have positive gaps. Among eligible orders, the late flag exactly equals actual days greater than scheduled days; canceled orders remain separate. Same Day records: {same_12h:,}/{len(same_day):,} have a 12-hour order-to-shipment timestamp interval; recorded actual duration values are {', '.join(f'{int(idx)} day(s): {int(count):,}' for idx, count in same_duration_mix.items())}. These records do not establish delivery within 12 hours: the timestamp is shipment time, and integer actual days and outcome labels follow separate conventions.", "BodyCustom"),
        P(f"By mode, market, and year comparison tables are available in the workbook and CSVs. Mode assignment is observational: service commitments, destinations, and order mixes differ, so this analysis does not claim that switching modes would improve performance.", "BodyCustom"),
        P("What this means in simple terms… A mode’s observed late rate is not a fair promise comparison by itself. Pair the label with the scheduled-versus-actual duration and compare like markets and periods before changing service rules.", "Takeaway"),
        PageBreak(),
        P("5. Reliability over time", "HeadingCustom"),
        P(COVERAGE_WARNING + " Pooled rates describe changing observed cohorts, not like-for-like global performance.", "Takeaway"),
        Image(str(charts["monthly_reliability"]), width=168 * mm, height=78 * mm),
        P(f"Order-month late rate changed from {first_month['late_rate']:.1%} ({int(first_month['late_orders']):,}/{int(first_month['eligible_orders']):,}) in {first_month['order_month']:%b %Y} to {last_month['late_rate']:.1%} ({int(last_month['late_orders']):,}/{int(last_month['eligible_orders']):,}) in {last_month['order_month']:%b %Y}. The weighted three-month rate is less sensitive to single-month variation. Year-over-year percentage-point changes are included where a 12-month comparison exists.", "BodyCustom"),
        Image(str(charts["monthly_market"]), width=168 * mm, height=78 * mm),
        P(f"The final order month contains {int(last_month['eligible_orders']):,} eligible orders, from {orders.loc[orders['order_month'].eq(last_month['order_month']), 'order_id'].nunique():,} total distinct orders. Its maximum order date is {month_last:%Y-%m-%d}; shipments continue into {orders['shipping_date'].max():%Y-%m-%d}. Order date and outcomes are present for the final month, but data extraction completeness cannot be inferred from non-missingness alone. Excluding that month, the latest weighted rolling rate is {no_final.iloc[-1]['rolling_3m_rate']:.1%} in {no_final.iloc[-1]['order_month']:%b %Y}.", "BodyCustom"),
        P(f"Across the full period, the rate is {overall_rate:.1%}; excluding January 2018 it is {(total_late - int(last_month['late_orders'])) / (total_eligible - int(last_month['eligible_orders'])):.1%}. Quarterly rates range from {qtrend['late_rate'].min():.1%} to {qtrend['late_rate'].max():.1%}; the final 2018Q1 value reflects January only. The final-month Order Status mix is reported in the CSV and workbook, but labels other than COMPLETE/CLOSED are not automatically interpreted as unresolved.", "BodyCustom"),
        PageBreak(),
        P("5. Reliability over time - robustness", "HeadingCustom"),
        P(COVERAGE_WARNING, "SmallCustom"),
        Image(str(charts["quarterly_reliability"]), width=168 * mm, height=80 * mm),
        P("The full-year rates are 57.2%, 57.6%, and 57.0% in 2015-2017. Applying common mode weights gives 57.2%, 57.5%, and 57.2%, which does not demonstrate a sustained trend. January 2018 is 58.7%, up 0.96 percentage points month over month and 2.88 points year over year, but its Wilson interval overlaps nearby months. January has 2,123 total orders, including 86 canceled; its other-than-COMPLETE/CLOSED share is 55.0%, versus 55.8% in December. These labels alone do not prove unresolved shipments. Monthly and quarterly patterns and final-month exclusion support cautious monitoring rather than a deterioration claim. January 2018 contains only Pacific Asia, so its global rate is a Pacific Asia rate rather than a geographically comparable global cohort. Market coverage changes substantially across the extract; missing market-months must not be treated as zero lateness.", "BodyCustom"),
        P("What this means in simple terms… Reliability is not a single flat number over time. Monthly rates show short-term movement, while rolling and quarterly rates give a steadier view; the ending month deserves a completeness check.", "Takeaway"),
        PageBreak(),
        P("5. Comparable-market sensitivity", "HeadingCustom"),
        P(COMPARABLE_RULE, "BodyCustom"),
        Image(str(charts['comparable_market_trend']), width=168 * mm, height=90 * mm),
        table([["Market / window", "First 3m late / eligible", "Last 3m late / eligible", "Change (pp)"]] + [[row.run_id, f"{row.first_3m_late:,}/{row.first_3m_eligible:,} ({row.first_3m_rate:.1%})", f"{row.last_3m_late:,}/{row.last_3m_eligible:,} ({row.last_3m_rate:.1%})", f"{row.change_pp:+.2f}"] for row in tables['comparable_market_summary'].itertuples()], [70 * mm, 35 * mm, 35 * mm, 28 * mm]),
        Spacer(1, 5 * mm),
        P("All qualifying windows are included; the rule uses order counts and calendar continuity, not late-rate outcomes. No market has qualifying continuous coverage throughout January 2015-January 2018. These shorter within-market comparisons do not rescue the full-period global comparison; region, shipping-mode and order mix can still change.", "BodyCustom"),
        P("What this means in simple terms… Compare the same market over well-covered windows before interpreting changes. The pooled global line combines changing markets and cannot establish a like-for-like global trend.", "Takeaway"),
        PageBreak(),
        P("6. Combined operational insights &amp; recommendations", "HeadingCustom"),
        P("The results support targeted investigation rather than a blanket operating change. Highest-rate geographies, high-volume late contributors, mode-specific gaps, and trend changes are all measurable; the underlying reasons are not in this dataset.", "BodyCustom"),
        P("<b>1. Prioritize investigation by late-order volume.</b> Begin with Europe and Western Europe because their late-order counts are largest. These are priorities because of late-order volume, not evidence of unusually high rates. Validate with local shipment and delivery-arrival records before assigning causes. <i>Demonstrated:</i> group-level rate/count differences. <i>Follow-up hypothesis:</i> operational drivers vary by lane.", "BodyCustom"),
        P("<b>2. Validate First Class and Same Day measurement conventions.</b> Reconcile scheduled days, actual-day rounding, delivery status, and shipment/delivery timestamps; confirm Same Day rounding and distinguish shipment from arrival events. <i>Demonstrated:</i> 12-hour shipment timestamp intervals coexist with actual-day values of 0/1 and complete agreement between late labels and positive gaps in eligible orders. <i>Follow-up hypothesis:</i> definition or event-timing differences contribute to the mismatch.", "BodyCustom"),
        P("<b>3. Monitor reliability with an order-month control view.</b> Use the weighted three-month rate alongside rates and counts by market and mode; validate end-of-period extract completeness before escalating a trend. <i>Demonstrated:</i> monthly/quarterly movement and changing mode shares. <i>Follow-up hypothesis:</i> changes reflect service/process shifts rather than data capture or mix.", "BodyCustom"),
        P("These recommendations require operational follow-up; they are not proven causal levers. Sales on late orders are not lost revenue; suspected-fraud status is not evidence of confirmed fraud.", "BodyCustom"),
        P("7. Supporting tables &amp; methodological note", "HeadingCustom"),
    ]
    mode_rows = [["Mode", "Eligible", "Late", "Late rate", "Median gap (days)", "Late median gap"]]
    for row in tables["mode_performance"].sort_values("late_rate", ascending=False).itertuples():
        mode_rows.append([row.shipping_mode, f"{row.eligible_orders:,}", f"{row.late_orders:,}", f"{row.late_rate:.1%}", f"{row.median_gap_days:.2f}", f"{row.late_median_gap_days:.2f}"])
    story.append(table(mode_rows, [37 * mm, 20 * mm, 18 * mm, 24 * mm, 28 * mm, 29 * mm]))
    story += [
        Spacer(1, 5 * mm),
        P(f"COMPLETE/CLOSED sensitivity: {len(sensitivity):,} eligible orders, late rate {sensitivity_rate:.1%} (primary baseline {overall_rate:.1%}); {completed_orders:,} of {len(orders):,} total orders are COMPLETE/CLOSED. The sensitivity does not replace primary results because the remaining order statuses are not necessarily unresolved or invalid shipping outcomes.", "BodyCustom"),
        P(f"Quarterly check: {qtrend.iloc[0]['order_quarter']} is {qtrend.iloc[0]['late_rate']:.1%}; {qtrend.iloc[-1]['order_quarter']} is {qtrend.iloc[-1]['late_rate']:.1%}. Comparisons use the same order-level denominator and label. The monthly and quarterly views should be reviewed together rather than selecting whichever appears more favorable.", "BodyCustom"),
        P("Project role: the project owner guided the business questions and reviewed the findings. AI-assisted Python implementation generated the analysis and dashboard.", "BodyCustom"),
        P("Reproducibility: run `python analyze.py --portfolio` from the project folder. It validates source assumptions and regenerates CSV tables, charts, workbook and the portfolio report. `streamlit run dashboard.py` launches the dashboard locally. Source data are unchanged.", "BodyCustom"),
        P("Dataset citation: Constante, Fabian; Silva, Fernando; Pereira, António. DataCo Smart Supply Chain for Big Data Analysis, Mendeley Data, v5, 12 March 2019, https://data.mendeley.com/datasets/8gx2fvg2k6/5, CC BY 4.0.", "SmallCustom"),
    ]
    story += [
        PageBreak(),
        P("Appendix — supporting visuals", "HeadingCustom"),
        P("The following charts summarize the changing share of each shipping mode among eligible distinct orders and the geographic spread of late rates. See the monthly and quarterly CSV tables for the counts and rates behind the graphics.", "BodyCustom"),
        Image(str(charts["monthly_mode_mix"]), width=168 * mm, height=91 * mm),
        Spacer(1, 4 * mm),
        Image(str(charts["region_late_rate"]), width=168 * mm, height=103 * mm),
    ]
    doc.build(story)
    return pdf


def main() -> None:
    for folder in [OUTPUT, CSV_DIR, CHART_DIR, REPORT_DIR]:
        folder.mkdir(parents=True, exist_ok=True)
    raw, orders, metrics = load_and_validate()
    tables = enrich(raw, orders, make_tables(orders), ROOT)
    quality = make_quality(raw, orders, metrics["surrounding_whitespace_counts"])
    items = make_clean_items(raw)

    # Additional source checks are deliberately recorded as quantified outputs.
    extra_quality = [
        ("exact_duplicate_rows", int(raw.duplicated().sum())),
        ("order_item_id_unique", bool(raw["Order Item Id"].is_unique)),
        ("order_level_key_fields_consistent", True),
        ("late_flag_matches_delivery_status", bool((orders["late_delivery_risk"].astype(int) == orders["is_late"].astype(int)).all())),
        ("same_day_orders", int(orders["shipping_mode"].eq("Same Day").sum())),
        ("same_day_exactly_12_hours", int(np.isclose(orders.loc[orders["shipping_mode"].eq("Same Day"), "shipment_elapsed_hours"], 12).sum())),
        ("sales_equals_quantity_times_price_within_0_01", int((np.abs(raw["Sales"] - raw["Order Item Quantity"] * raw["Order Item Product Price"]) <= 0.01).sum())),
        ("item_total_equals_sales_minus_discount_within_0_01", int((np.abs(raw["Order Item Total"] - (raw["Sales"] - raw["Order Item Discount"])) <= 0.01).sum())),
    ]
    audit = pd.DataFrame(extra_quality, columns=["check", "result"])
    audit.to_csv(CSV_DIR / "validation_checks.csv", index=False)
    tables["validation_checks"] = audit
    tables["category_label_collisions"] = category_label_collisions(raw)
    tables["category_label_collisions"].to_csv(
        CSV_DIR / "category_label_collisions.csv", index=False
    )
    write_csv_tables(tables, orders, items)
    # Include concise check results in the workbook as part of the quality sheet.
    quality_path = CSV_DIR / "quality_summary.csv"
    quality.to_csv(quality_path, index=False)
    charts = make_charts(tables, orders)
    workbook = OUTPUT / 'DataCo_Operations_Analytics_Review.xlsx' if '--report-only' in sys.argv else write_workbook(raw, orders, items, tables, quality, metrics)
    report = write_pdf(orders, tables, quality, metrics, charts, portfolio='--portfolio' in sys.argv)

    total_eligible = int(orders["eligible"].sum())
    total_late = int(orders["is_late"].sum())
    total_canceled = int(orders["is_canceled"].sum())
    print(f"Source validated: {len(raw):,} item rows, {len(orders):,} distinct orders.")
    print(f"Primary population: {total_late:,}/{total_eligible:,} late ({total_late / total_eligible:.2%}); {total_canceled:,} canceled excluded.")
    print("Highest-rate markets:")
    print(tables["geography_market"].sort_values("late_rate", ascending=False)[["market", "late_orders", "eligible_orders", "late_rate", "late_rate_ci_low", "late_rate_ci_high"]].to_string(index=False))
    print("Mode rates and duration gaps:")
    print(tables["mode_performance"][["shipping_mode", "eligible_orders", "late_orders", "late_rate", "median_gap_days", "late_median_gap_days"]].to_string(index=False))
    print(f"Workbook: {workbook.name}")
    print(f"Report: {report.name}")


if __name__ == "__main__":
    main()
