# sp500_constituents.csv — provenance

- **Source:** https://github.com/fja05680/sp500
- **File:** `S&P 500 Historical Components & Changes (Updated).csv`, vendored
  here unmodified as `sp500_constituents.csv`.
- **License:** MIT (Copyright (c) 2019-2020 Farrell J. Aultman).
- **Fetched:** 2026-09-02.
- **Format:** one row per date on which index membership changed; `tickers` is
  the full comma-separated constituent list effective from that date until the
  next row. Tickers are point-in-time (e.g. `FB` before 2022, `META` after;
  `RTN` before the 2020 Raytheon/UTX merger).
- **Coverage in this repo's backtest:** 1996-01-02 .. 2026-06-30.

## Known limitations
- Membership dates are end-of-day accurate but not intraday; a name added or
  dropped mid-week is modelled as effective on the listed date.
- Class-share tickers use `.` here (`BRK.B`, `BF.B`); the loader maps these to
  Yahoo's `-` convention for price downloads.
- This file fixes *selection* bias (which names were in the index), not *data*
  availability: free Yahoo Finance history is missing for most members that
  left via merger or bankruptcy, so those names still drop out of the price
  panel. `run_backtest.py` prints how many requested tickers returned usable
  data so the residual gap is visible.
