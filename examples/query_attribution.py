"""_common.query_attribution — attribute contacts to orders.

Step 5 of the contact-classification pipeline. Two public attribution rules:

  * `attribute_contacts_to_orders` — legacy "most-recent-prior order" `join_asof`
    (the CLAUDE.md default rule). Use when ERC is unknown or for back-compat.

  * `attribute_by_erc` — ERC-aware dispatch. For WISMO ERCs (4101/4102) it picks
    the OLDEST order in the last N days where the parcel was still in transit at
    enquiry time (delivery_dt IS NULL OR delivery_dt > enquiry_dt). This corrects
    the well-documented asof misrouting where customers chase an OLDER not-yet-
    delivered order while asof picks a newer one. For other ERCs it falls back
    to the legacy asof.

Validation that motivated the new rule:
  scripts/archeived/WISMO_v2/wismo_timeline_analysis.ipynb §§10.1–10.3 — 21% of WISMO DOR
  refunds book on a DIFFERENT order from the one asof picks (32% when restricted
  to single-parcel orders, isolating the cross-order mismatch from multi-parcel
  noise).

Library usage:
    from _common.query_attribution import (
        attribute_contacts_to_orders, attribute_by_erc,
        WISMO_ERCS, RETURNS_ERCS,
    )

CLI usage (legacy asof):
    python -m _common.query_attribution \\
        --contacts contacts.parquet --orders orders.parquet \\
        --contact-date enquiry_dt --order-date order_date \\
        --by trading_code account_number \\
        --out attributed.parquet

CLI usage (ERC-aware):
    python -m _common.query_attribution --strategy by_erc \\
        --contacts contacts.parquet --orders orders.parquet \\
        --contact-date enquiry_dt --order-date order_date \\
        --delivery-date delivery_dt --erc-col ENQUIRY_REASON_CODE \\
        --by trading_code account_number \\
        --wismo-window-days 30 \\
        --out attributed.parquet
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import polars as pl


# ERC buckets recognised by attribute_by_erc. Sourced from CLAUDE.md "Data
# Conventions → ERC" — keep these in sync if CLAUDE.md changes.
WISMO_ERCS: frozenset[int]   = frozenset({4101, 4102})
RETURNS_ERCS: frozenset[int] = frozenset({4301, 4302, 4401, 4403, 4603, 5301, 5302})

WISMO_WINDOW_DAYS_DEFAULT             = 30
# Check 1 (code-match) gets a wider window than Checks 2/3 — the diary code is
# the identity signal, so proximity to enquiry matters less. Empirically, 30d
# misses ~24% of recoverable mismatches whose code-matching order sits 30–60d
# before enquiry. 60d catches most without polluting Checks 2/3.
WISMO_CODE_MATCH_WINDOW_DAYS_DEFAULT  = 60
RETURNS_WINDOW_DAYS_DEFAULT           = 60

# attrib_strategy column values
# WISMO branch labels — Check 1 (code-match) → Check 2 (GNR/DOR last delivered)
# → Check 3 (oldest in-flight); each has a within-bucket fallback to "latest
# order in the look-back window" so a contact always picks something when ANY
# order exists in the window.
STRAT_WISMO_CODE_MATCH        = 'wismo_code_match'                # Check 1 primary
STRAT_WISMO_CODE_MATCH_FB     = 'wismo_code_match_fb_latest'      # Check 1 fallback
STRAT_WISMO_GNR_DOR_DELIVERED = 'wismo_gnr_dor_last_delivered'    # Check 2 primary
STRAT_WISMO_GNR_DOR_FB        = 'wismo_gnr_dor_fb_latest'         # Check 2 fallback
STRAT_WISMO_OLDEST            = 'wismo_in_flight_oldest'          # Check 3 primary
STRAT_WISMO_OLDEST_FB         = 'wismo_in_flight_fb_latest'       # Check 3 fallback
STRAT_ASOF                    = 'asof_fallback'                   # no order in window
STRAT_NO_MATCH                = 'no_match'                        # no order at all

# Default intention labels that flag a "Goods Not Received / Denial of Receipt"
# WISMO contact (where delivery_dt < enquiry_dt and not null). Pulled from the
# `sub_category` output of the WISMO keyword classifier — adjust via the
# `gnr_dor_intentions` kwarg if the classifier vocabulary changes.
GNR_DOR_INTENTIONS_DEFAULT: frozenset[str] = frozenset({
    'Marked delivered but not received',
    'Gift card not received',
})


def attribute_contacts_to_orders(
    contacts: pl.DataFrame,
    orders: pl.DataFrame,
    contact_date_col: str,
    order_date_col: str,
    by_cols: list[str],
    *,
    strategy: str = 'backward',
    drop_null_dates: bool = True,
) -> pl.DataFrame:
    """Attribute each contact row to its most recent prior order.

    Implementation: `polars.join_asof` with `by=by_cols` and `strategy=backward`,
    which matches each contact to the largest order_date_col that is ≤ the
    contact_date_col (within the same group defined by by_cols).

    Both inputs are sorted by (by_cols + asof_col) before joining (required by
    polars' asof semantics).

    Per CLAUDE.md timestamp policy: existence is asserted and null rates are
    logged for both date columns before the join. Null asof keys break
    `join_asof`, so by default they are dropped (with a log line); set
    `drop_null_dates=False` to error instead.

    Contacts with no prior order get null values for the order columns.
    """
    if strategy not in ('backward', 'forward', 'nearest'):
        raise ValueError(f'strategy must be backward/forward/nearest, got {strategy!r}')

    # Timestamp policy: assert existence, log null rate, handle explicitly.
    assert contact_date_col in contacts.columns, (
        f'contact_date_col {contact_date_col!r} not in contacts: {contacts.columns}')
    assert order_date_col in orders.columns, (
        f'order_date_col {order_date_col!r} not in orders: {orders.columns}')

    n_contacts_null = contacts[contact_date_col].is_null().sum()
    n_orders_null = orders[order_date_col].is_null().sum()
    print(f'  null {contact_date_col}: {n_contacts_null:,}/{contacts.height:,} '
          f'({n_contacts_null / max(contacts.height, 1) * 100:.2f}%)')
    print(f'  null {order_date_col}:   {n_orders_null:,}/{orders.height:,} '
          f'({n_orders_null / max(orders.height, 1) * 100:.2f}%)')

    if n_contacts_null or n_orders_null:
        if not drop_null_dates:
            raise ValueError(
                'null asof keys present; set drop_null_dates=True to drop them.')
        contacts = contacts.filter(pl.col(contact_date_col).is_not_null())
        orders   = orders.filter(pl.col(order_date_col).is_not_null())
        print(f'  dropped null-date rows → contacts={contacts.height:,} orders={orders.height:,}')

    contacts_sorted = contacts.sort(by_cols + [contact_date_col])
    orders_sorted   = orders.sort(by_cols + [order_date_col])

    return contacts_sorted.join_asof(
        orders_sorted,
        left_on=contact_date_col,
        right_on=order_date_col,
        by=by_cols,
        strategy=strategy,
    )


def _attribute_wismo(
    contacts: pl.DataFrame,
    orders: pl.DataFrame,
    *,
    contact_date_col: str,
    order_date_col: str,
    delivery_date_col: str,
    by_cols: list[str],
    window_days: int,
    code_match_window_days: int,
    diary_code_col: str | None,
    intention_col: str | None,
    gnr_dor_intentions: frozenset[str],
    order_adj_codes_col: str | None,
) -> pl.DataFrame:
    """Three-check WISMO attribution + within-bucket fallback.

    Window policy: Check 1 (code-match) considers orders in
    [enquiry_dt − code_match_window_days, enquiry_dt] (the wider window);
    Checks 2 and 3 and the within-bucket "latest in window" fallback all use the
    narrower [enquiry_dt − window_days, enquiry_dt]. The wider Check-1 window
    catches cases where the agent applied an adjustment to a slightly older
    order (e.g. customer called 37 days after order, asof would route to a
    newer placeholder order instead). Pass `code_match_window_days = window_days`
    to restore single-window behaviour.

    Buckets (MECE on the pair `(has_diary_code, intention ∈ gnr_dor_intentions)`):

      A. has diary code:
         primary  → latest order in window whose adj-code list contains the diary
                    code (strat=wismo_code_match)
         fallback → latest order in window (strat=wismo_code_match_fb_latest)
      B. no diary code AND intention in gnr_dor_intentions:
         primary  → latest order in window with delivery_dt < enquiry_dt
                    (strat=wismo_gnr_dor_last_delivered)
         fallback → latest order in window (strat=wismo_gnr_dor_fb_latest)
      C. no diary code AND intention not in gnr_dor_intentions:
         primary  → oldest order in window with delivery_dt IS NULL OR
                    delivery_dt > enquiry_dt (strat=wismo_in_flight_oldest)
         fallback → latest order in window (strat=wismo_in_flight_fb_latest)

    Contacts with NO order in window get null order columns and null
    attrib_strategy — the caller falls those rows back to legacy asof.

    Back-compat: when diary_code_col is None and intention_col is None, every
    contact lands in bucket C — recovering the previous oldest-in-flight rule.
    """
    if code_match_window_days < window_days:
        raise ValueError(
            f'code_match_window_days ({code_match_window_days}) must be >= '
            f'window_days ({window_days}) — Check 1 widens the look-back, '
            f'never narrows it.'
        )

    has_diary  = diary_code_col is not None and diary_code_col in contacts.columns
    has_intent = intention_col  is not None and intention_col  in contacts.columns
    has_adj    = order_adj_codes_col is not None and order_adj_codes_col in orders.columns

    # Per-contact row id so we can group correctly even if (by_cols,
    # contact_date_col) is not unique on the contacts side.
    contacts_indexed = contacts.with_row_index('_contact_idx')
    pairs = contacts_indexed.join(orders, on=by_cols, how='inner')

    # Step 1 — pre-filter to the WIDER window (Check 1 reach). Within-window flags
    # `_in_window_main` carry the narrower window for Checks 2/3 and the bucket
    # fallback tier.
    window_lo_main = pl.col(contact_date_col) - pl.duration(days=window_days)
    window_lo_code = pl.col(contact_date_col) - pl.duration(days=code_match_window_days)
    in_window_main = pl.col(order_date_col).is_between(window_lo_main, pl.col(contact_date_col))
    in_window_code = pl.col(order_date_col).is_between(window_lo_code, pl.col(contact_date_col))
    candidates = pairs.filter(in_window_code).with_columns(
        in_window_main.alias('_in_window_main'),
    )

    if candidates.is_empty():
        # No WISMO contact had any order in window — return contacts shaped like
        # the join output (null order cols, null strategy) so the caller can
        # left-join and fall back to asof.
        order_only_cols = [c for c in orders.columns if c not in by_cols]
        return contacts_indexed.with_columns(
            [pl.lit(None).cast(orders.schema[c]).alias(c) for c in order_only_cols]
            + [pl.lit(None).cast(pl.String).alias('attrib_strategy')]
        ).drop('_contact_idx')

    # Step 2 — annotate per-row booleans the three checks care about.
    candidates = candidates.with_columns(
        (pl.col(delivery_date_col).is_null()
         | (pl.col(delivery_date_col) > pl.col(contact_date_col))).alias('_in_flight'),
        (pl.col(delivery_date_col).is_not_null()
         & (pl.col(delivery_date_col) < pl.col(contact_date_col))).alias('_delivered_before'),
    )

    # Normalised order adj-code list (strip CHAR padding) for the code-match join.
    if has_adj:
        candidates = candidates.with_columns(
            pl.col(order_adj_codes_col).list.eval(pl.element().str.strip_chars())
              .alias('_adj_codes_norm'),
        )
    else:
        candidates = candidates.with_columns(
            pl.lit(None).cast(pl.List(pl.String)).alias('_adj_codes_norm'),
        )

    if has_diary and has_adj:
        candidates = candidates.with_columns(
            (pl.col(diary_code_col).is_not_null()
             & pl.col('_adj_codes_norm').is_not_null()
             & pl.col('_adj_codes_norm').list.contains(pl.col(diary_code_col)))
              .alias('_adj_matches'),
        )
    else:
        candidates = candidates.with_columns(pl.lit(False).alias('_adj_matches'))

    # Step 3 — bucket per row (constant for a given _contact_idx).
    if has_diary:
        bucket_expr = pl.when(pl.col(diary_code_col).is_not_null()).then(pl.lit('A'))
    else:
        # No diary col → bucket A is empty. Use a never-true clause to make the
        # type checker / Polars happy.
        bucket_expr = pl.when(pl.lit(False)).then(pl.lit('A'))

    if has_intent:
        bucket_expr = bucket_expr.when(
            pl.col(intention_col).is_in(list(gnr_dor_intentions))
        ).then(pl.lit('B'))

    candidates = candidates.with_columns(bucket_expr.otherwise(pl.lit('C')).alias('_bucket'))

    # Step 4 — tier (0=primary, 1=within-bucket fallback) and a single
    # ascending tiebreak score (negate epoch where "latest = better").
    # Bucket A Tier 0 (code-match) uses the WIDER window — _in_window_main not
    # required. All other tiers require _in_window_main (the narrow window).
    # Rows outside the narrow window that don't satisfy bucket A Tier 0 get
    # tier 99 → filtered out before picking.
    candidates = candidates.with_columns(
        pl.when((pl.col('_bucket') == 'A') & pl.col('_adj_matches')).then(0)
          .when((pl.col('_bucket') == 'B') & pl.col('_delivered_before')
                & pl.col('_in_window_main')).then(0)
          .when((pl.col('_bucket') == 'C') & pl.col('_in_flight')
                & pl.col('_in_window_main')).then(0)
          .when(pl.col('_in_window_main')).then(1)
          .otherwise(99)
          .alias('_tier'),
    ).filter(pl.col('_tier') < 99).with_columns(
        pl.when((pl.col('_bucket') == 'C') & (pl.col('_tier') == 0))
          .then(pl.col(order_date_col).dt.epoch('s'))                 # OLDEST order_dt
          .when((pl.col('_bucket') == 'B') & (pl.col('_tier') == 0))
          .then(-pl.col(delivery_date_col).dt.epoch('s'))             # LATEST delivery_dt
          .otherwise(-pl.col(order_date_col).dt.epoch('s'))           # LATEST order_dt
          .alias('_tiebreak_score'),
    )

    sort_keys = ['_contact_idx', '_tier', '_tiebreak_score']
    if 'ORDER_SERIAL_NUMBER' in candidates.columns:
        sort_keys.append('ORDER_SERIAL_NUMBER')
    picked = (
        candidates.sort(sort_keys)
                  .group_by('_contact_idx', maintain_order=True)
                  .first()
    )

    # Step 5 — map (bucket, tier) to the public strategy label, then drop temps.
    picked = picked.with_columns(
        pl.when((pl.col('_bucket') == 'A') & (pl.col('_tier') == 0))
            .then(pl.lit(STRAT_WISMO_CODE_MATCH))
          .when((pl.col('_bucket') == 'A') & (pl.col('_tier') == 1))
            .then(pl.lit(STRAT_WISMO_CODE_MATCH_FB))
          .when((pl.col('_bucket') == 'B') & (pl.col('_tier') == 0))
            .then(pl.lit(STRAT_WISMO_GNR_DOR_DELIVERED))
          .when((pl.col('_bucket') == 'B') & (pl.col('_tier') == 1))
            .then(pl.lit(STRAT_WISMO_GNR_DOR_FB))
          .when((pl.col('_bucket') == 'C') & (pl.col('_tier') == 0))
            .then(pl.lit(STRAT_WISMO_OLDEST))
          .otherwise(pl.lit(STRAT_WISMO_OLDEST_FB))
          .alias('attrib_strategy'),
    ).drop('_in_flight', '_delivered_before', '_adj_codes_norm',
            '_adj_matches', '_bucket', '_tier', '_tiebreak_score')

    # Step 6 — left-join back so contacts with no in-window order are preserved
    # with null order cols + null strategy (caller routes those to asof).
    order_only_cols = [c for c in orders.columns if c not in by_cols]
    picked_slim = picked.select(['_contact_idx'] + order_only_cols + ['attrib_strategy'])
    return (
        contacts_indexed.join(picked_slim, on='_contact_idx', how='left')
                        .drop('_contact_idx')
    )


def attribute_by_erc(
    contacts: pl.DataFrame,
    orders: pl.DataFrame,
    *,
    contact_date_col: str,
    order_date_col: str,
    delivery_date_col: str,
    erc_col: str,
    by_cols: list[str],
    diary_code_col: str | None = None,
    intention_col: str | None = None,
    gnr_dor_intentions: frozenset[str] = GNR_DOR_INTENTIONS_DEFAULT,
    order_adj_codes_col: str | None = None,
    wismo_window_days: int = WISMO_WINDOW_DAYS_DEFAULT,
    wismo_code_match_window_days: int = WISMO_CODE_MATCH_WINDOW_DAYS_DEFAULT,
    returns_window_days: int = RETURNS_WINDOW_DAYS_DEFAULT,
    return_indicator_col: str | None = None,
) -> pl.DataFrame:
    """ERC-aware contact-to-order attribution.

    Routes each contact through the rule that fits its intent:

      WISMO (ERC ∈ {4101, 4102}):
        Three-check dispatch over the look-back window
        order_dt ∈ [enquiry_dt − wismo_window_days, enquiry_dt]:

          Check 1 — `diary_code_col` is not null on the contact:
            primary  → latest order within the WIDER look-back
                       [enquiry_dt − wismo_code_match_window_days, enquiry_dt]
                       whose `order_adj_codes_col` list (CHAR-trimmed) contains
                       the diary code
            fallback → latest order in the narrower main window
          Check 2 — diary code is null AND `intention_col` ∈ `gnr_dor_intentions`:
            primary  → latest order in window with `delivery_dt < enquiry_dt`
            fallback → latest order in window
          Check 3 — diary code is null AND intention ∉ `gnr_dor_intentions`:
            primary  → oldest order in window with
                       `delivery_dt IS NULL OR delivery_dt > enquiry_dt`
            fallback → latest order in window

        Bucket assignment is MECE on `(has_diary_code, intention ∈ gnr_dor_set)`.
        Contacts with no order in window fall to legacy asof; with no order at
        all to `no_match`. Tie-breaks: smallest `ORDER_SERIAL_NUMBER`.

        Back-compat — if `diary_code_col` and `intention_col` are both omitted,
        every WISMO contact lands in bucket C, recovering the previous
        "oldest in-flight" rule unchanged.

      RETURNS (ERC ∈ {4301, 4302, 4401, 4403, 4603, 5301, 5302}):
        SCOPE HOOK ONLY. Kwargs are accepted (`returns_window_days`,
        `return_indicator_col`) but the branch falls back to legacy asof for
        now. Wire the actual logic in a follow-up plan.

      OTHER ERCs: legacy `attribute_contacts_to_orders` (most-recent-prior asof).

    Output: contacts × orders columns, plus:
      `attrib_strategy` — one of:
        - ``"wismo_code_match"``               (Check 1 primary)
        - ``"wismo_code_match_fb_latest"``     (Check 1 fallback)
        - ``"wismo_gnr_dor_last_delivered"``   (Check 2 primary)
        - ``"wismo_gnr_dor_fb_latest"``        (Check 2 fallback)
        - ``"wismo_in_flight_oldest"``         (Check 3 primary)
        - ``"wismo_in_flight_fb_latest"``      (Check 3 fallback)
        - ``"asof_fallback"``                  (no order in window OR Returns/Other)
        - ``"no_match"``                       (no order at all)

    Caller responsibilities (NOT enforced here):
      - `TRADING_CODE` already stripped of CHAR padding.
      - `TRADING_CODE == 'E'` already excluded from both inputs.
      - `Promised_Date` / `Del_Date_Time` already parsed to Date/Datetime
        (this function asserts the dtype is not String).
    """
    assert contact_date_col in contacts.columns, (
        f'contact_date_col {contact_date_col!r} not in contacts: {contacts.columns}')
    assert order_date_col in orders.columns, (
        f'order_date_col {order_date_col!r} not in orders: {orders.columns}')
    assert delivery_date_col in orders.columns, (
        f'delivery_date_col {delivery_date_col!r} not in orders: {orders.columns}')
    assert erc_col in contacts.columns, (
        f'erc_col {erc_col!r} not in contacts: {contacts.columns}')

    # Don't accept String dtype for date columns — callers must parse upstream
    # (so we don't silently silently sort strings lexically).
    if str(orders.schema[delivery_date_col]) == 'String':
        raise TypeError(
            f"orders[{delivery_date_col!r}] is String — parse to Datetime upstream "
            f'(e.g. .str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)).'
        )
    if str(orders.schema[order_date_col]) == 'String':
        raise TypeError(
            f"orders[{order_date_col!r}] is String — parse to Datetime upstream."
        )

    # Log null rates for the three timestamps (per CLAUDE.md timestamp policy).
    for df, col, label in [
        (contacts, contact_date_col, 'contacts.contact_date'),
        (orders, order_date_col, 'orders.order_date'),
        (orders, delivery_date_col, 'orders.delivery_date'),
    ]:
        n_null = df[col].is_null().sum()
        print(f'  null {label} ({col}): {n_null:,}/{df.height:,} '
              f'({n_null / max(df.height, 1) * 100:.2f}%)')
    # Note: null delivery_date is meaningful here — "never delivered" qualifies
    # as in-transit, so we do NOT drop these orders.

    # Route contacts by ERC bucket. WISMO and Returns go to bucket-specific
    # branches; everything else (and contacts with null ERC) goes through asof.
    is_wismo   = pl.col(erc_col).is_in(list(WISMO_ERCS))
    is_returns = pl.col(erc_col).is_in(list(RETURNS_ERCS))

    wismo_contacts   = contacts.filter(is_wismo)
    returns_contacts = contacts.filter(is_returns)
    other_contacts   = contacts.filter(~(is_wismo | is_returns) | pl.col(erc_col).is_null())

    print(f'\n  WISMO contacts:   {wismo_contacts.height:,}')
    print(f'  Returns contacts: {returns_contacts.height:,}  (SCOPE: falls back to asof)')
    print(f'  Other contacts:   {other_contacts.height:,}')

    parts: list[pl.DataFrame] = []

    # WISMO branch: three-check dispatch, then asof for the residue.
    if wismo_contacts.height > 0:
        wismo_matched = _attribute_wismo(
            wismo_contacts, orders,
            contact_date_col=contact_date_col,
            order_date_col=order_date_col,
            delivery_date_col=delivery_date_col,
            by_cols=by_cols,
            window_days=wismo_window_days,
            code_match_window_days=wismo_code_match_window_days,
            diary_code_col=diary_code_col,
            intention_col=intention_col,
            gnr_dor_intentions=gnr_dor_intentions,
            order_adj_codes_col=order_adj_codes_col,
        )
        # Contacts that DID get an in-window candidate carry a non-null strategy
        # set by _attribute_wismo. Residue (no order in window) carries null.
        wismo_hit  = wismo_matched.filter(pl.col('attrib_strategy').is_not_null())
        wismo_miss = wismo_matched.filter(pl.col('attrib_strategy').is_null())
        print(f'    WISMO matched by 3-check dispatcher: {wismo_hit.height:,}  '
              f'(asof fallback: {wismo_miss.height:,})')
        if wismo_hit.height > 0:
            print('    WISMO strategy mix:')
            for r in (
                wismo_hit.group_by('attrib_strategy').len()
                         .sort('len', descending=True).iter_rows(named=True)
            ):
                print(f"      {r['attrib_strategy']:36s}  {r['len']:>8,}")
        parts.append(wismo_hit)

        # WISMO contacts with no in-flight candidate fall through to asof on
        # the SAME orders pool (the legacy fn re-uses contact/order_date_col).
        if wismo_miss.height > 0:
            # Strip the order-side columns we just nulled out before re-attributing
            order_only_cols = [c for c in orders.columns if c not in by_cols]
            wismo_fallback_in = wismo_miss.drop(order_only_cols + ['attrib_strategy']
                                                if 'attrib_strategy' in wismo_miss.columns
                                                else order_only_cols)
            fallback_attrib = attribute_contacts_to_orders(
                wismo_fallback_in, orders,
                contact_date_col=contact_date_col,
                order_date_col=order_date_col,
                by_cols=by_cols,
            )
            fallback_attrib = fallback_attrib.with_columns(
                pl.when(pl.col(order_date_col).is_not_null())
                  .then(pl.lit(STRAT_ASOF))
                  .otherwise(pl.lit(STRAT_NO_MATCH))
                  .alias('attrib_strategy'))
            parts.append(fallback_attrib)

    # Returns branch (SCOPE hook): for now, just asof.
    _ = return_indicator_col, returns_window_days  # accepted but unused this phase
    if returns_contacts.height > 0:
        returns_attrib = attribute_contacts_to_orders(
            returns_contacts, orders,
            contact_date_col=contact_date_col,
            order_date_col=order_date_col,
            by_cols=by_cols,
        )
        returns_attrib = returns_attrib.with_columns(
            pl.when(pl.col(order_date_col).is_not_null())
              .then(pl.lit(STRAT_ASOF))
              .otherwise(pl.lit(STRAT_NO_MATCH))
              .alias('attrib_strategy'))
        parts.append(returns_attrib)

    # Other / unknown ERCs.
    if other_contacts.height > 0:
        other_attrib = attribute_contacts_to_orders(
            other_contacts, orders,
            contact_date_col=contact_date_col,
            order_date_col=order_date_col,
            by_cols=by_cols,
        )
        other_attrib = other_attrib.with_columns(
            pl.when(pl.col(order_date_col).is_not_null())
              .then(pl.lit(STRAT_ASOF))
              .otherwise(pl.lit(STRAT_NO_MATCH))
              .alias('attrib_strategy'))
        parts.append(other_attrib)

    if not parts:
        # Empty contacts: return an empty frame with the same shape we'd otherwise produce.
        empty_template = attribute_contacts_to_orders(
            contacts, orders,
            contact_date_col=contact_date_col,
            order_date_col=order_date_col,
            by_cols=by_cols,
        ).with_columns(pl.lit(None).cast(pl.String).alias('attrib_strategy'))
        return empty_template

    # vertical_relaxed because branch outputs may have slightly different
    # null-vs-typed schema after column drops above.
    return pl.concat(parts, how='diagonal_relaxed')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--contacts', type=Path, required=True)
    ap.add_argument('--orders', type=Path, required=True)
    ap.add_argument('--contact-date', required=True,
                    help='Date column on the contacts side (e.g. enquiry_dt).')
    ap.add_argument('--order-date', required=True,
                    help='Date column on the orders side (e.g. order_date).')
    ap.add_argument('--by', nargs='+', required=True,
                    help='Group-by columns shared between contacts and orders '
                         '(e.g. trading_code account_number).')
    ap.add_argument('--strategy', default='backward',
                    choices=('backward', 'forward', 'nearest', 'by_erc'),
                    help='backward/forward/nearest = legacy asof. by_erc = '
                         'ERC-aware dispatch (WISMO oldest-in-flight + asof for rest).')
    ap.add_argument('--out', type=Path, required=True)
    # by_erc-only flags
    ap.add_argument('--erc-col', default='ENQUIRY_REASON_CODE',
                    help='ERC column on the contacts side. Required when --strategy=by_erc.')
    ap.add_argument('--delivery-date', default=None,
                    help='Delivery timestamp column on orders side. Required when '
                         '--strategy=by_erc.')
    ap.add_argument('--wismo-window-days', type=int, default=WISMO_WINDOW_DAYS_DEFAULT,
                    help='Narrow look-back window for WISMO Checks 2/3 and the '
                         'within-bucket fallback. Default 30.')
    ap.add_argument('--wismo-code-match-window-days', type=int,
                    default=WISMO_CODE_MATCH_WINDOW_DAYS_DEFAULT,
                    help='Wider look-back window for WISMO Check 1 (code-match). '
                         'Default 60. Must be >= --wismo-window-days.')
    ap.add_argument('--returns-window-days', type=int, default=RETURNS_WINDOW_DAYS_DEFAULT,
                    help='Look-back window for Returns rule (hook only). Default 60.')
    ap.add_argument('--diary-code-col', default=None,
                    help='Diary-extracted adjustment-code column on contacts side '
                         '(e.g. "adjustment_code"). When set, enables Check 1.')
    ap.add_argument('--intention-col', default=None,
                    help='Intention/sub-category column on contacts side '
                         '(e.g. "sub_category"). When set, enables Check 2 routing.')
    ap.add_argument('--order-adj-codes-col', default=None,
                    help='List-of-strings adj-code column on orders side '
                         '(e.g. "order_adj_codes"). Required for Check 1.')
    ap.add_argument('--gnr-dor-intentions', nargs='+', default=None,
                    help='Intention labels routed to Check 2 (GNR/DOR last delivered). '
                         'Defaults to the WISMO classifier vocabulary in '
                         'GNR_DOR_INTENTIONS_DEFAULT.')
    args = ap.parse_args()

    contacts = pl.read_parquet(args.contacts)
    orders   = pl.read_parquet(args.orders)
    print(f'contacts: {contacts.height:,}  orders: {orders.height:,}')

    if args.strategy == 'by_erc':
        if args.delivery_date is None:
            ap.error('--delivery-date is required when --strategy=by_erc')
        gnr_dor = (
            frozenset(args.gnr_dor_intentions)
            if args.gnr_dor_intentions else GNR_DOR_INTENTIONS_DEFAULT
        )
        out = attribute_by_erc(
            contacts, orders,
            contact_date_col=args.contact_date,
            order_date_col=args.order_date,
            delivery_date_col=args.delivery_date,
            erc_col=args.erc_col,
            by_cols=args.by,
            diary_code_col=args.diary_code_col,
            intention_col=args.intention_col,
            gnr_dor_intentions=gnr_dor,
            order_adj_codes_col=args.order_adj_codes_col,
            wismo_window_days=args.wismo_window_days,
            wismo_code_match_window_days=args.wismo_code_match_window_days,
            returns_window_days=args.returns_window_days,
        )
        print('\nattrib_strategy distribution:')
        print(out.group_by('attrib_strategy').len().sort('len', descending=True))
    else:
        out = attribute_contacts_to_orders(
            contacts, orders,
            contact_date_col=args.contact_date,
            order_date_col=args.order_date,
            by_cols=args.by,
            strategy=args.strategy,
        )

    n_matched = out.filter(pl.col(args.order_date).is_not_null()).height
    print(f'attributed: {n_matched:,}/{out.height:,} '
          f'({n_matched / out.height * 100:.1f}%) contacts matched to an order')

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_parquet(args.out)
    print(f'wrote {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
