# SkybyDay standing-route source coverage

This report measures the route-reference source against the two saved ADSB.lol
captures. It is an evidence package for the local release gate. It does not
claim that a standing route is the current flight's dispatch plan, and it does
not select a departure/destination leg from a multi-airport chain.

## Source snapshot and method

The source is the official VRS standing-data repository at commit
[`ba4454ca8b96c0302a0e037a644d7504d9b5beb0`](https://github.com/vradarserver/standing-data/tree/ba4454ca8b96c0302a0e037a644d7504d9b5beb0),
tree `09a7d864d71ff1cc6134aff723bd08dd9675c280`. The schema is the upstream
[schema-01 README](https://raw.githubusercontent.com/vradarserver/standing-data/main/routes/schema-01/README.md).
The 12 required `*-all.csv` files were fetched serially from that commit from
19:46:51.449978Z through 19:46:53.710982Z on 21 October 2026. The downloaded
CSV bytes total 1,365,528, well below the 30 MB budget. File sizes and SHA-256
digests are recorded in
[`data/qa/skybyday-routes/reports/manifest.json`](../data/qa/skybyday-routes/reports/manifest.json).

The reproducible analyzer is
[`data/qa/skybyday-routes/analyze.py`](../data/qa/skybyday-routes/analyze.py),
and the row-level result is in
[`coverage.tsv`](../data/qa/skybyday-routes/reports/coverage.tsv). Re-run it
from the repository root with:

```text
python3 data/qa/skybyday-routes/analyze.py
```

Only the saved normalized captures were read. No telemetry request, route API
request, bulk mirror request, or current-flight lookup was made.

The analyzer applies the schema's exact normalization: uppercase and trim the
callsign, strip leading flight-number zeroes, add a zero when the remaining
number starts with a letter, and accept only the documented number shape. A
two-character or mixed airline code is unsupported unless a verified alias is
provided; no alias was supplied for this measurement. A present normalized key
with no row in the pinned source is `unmatched`. A missing callsign is `missing`.
An exact row with two airport codes is `two_endpoint`; an exact row with three
or more codes is `multileg`. The full ordered chain is retained.

## Coverage result

Capture rows are counted independently because they are separate observations.
The combined deduplicated view uses the normalized callsign as its key and
retains the first observed row only for denominator reporting. Percentages
below use normalized candidates as the denominator; unsupported and missing
observations are shown separately.

| Capture | Aircraft | Callsign present | Normalized candidates | 2-endpoint | Multileg | Any exact reference | Unmatched | Unsupported | Missing | Any reference / candidate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `2026-10-18-den-phase2` (provider timestamp `2026-10-19T03:57:32.001Z`) | 55 | 55 | 44 | 31 | 8 | 39 | 5 | 11 | 0 | 88.64% |
| `den-check` (provider timestamp `2026-10-20T05:28:00Z`) | 29 | 28 | 23 | 17 | 3 | 20 | 3 | 5 | 1 | 86.96% |
| Combined records | 84 | 83 | 67 | 48 | 11 | 59 | 8 | 16 | 1 | 88.06% |
| Combined deduplicated normalized callsigns | 65 | 65 | 65 | 47 | 10 | 57 | 8 | 0 | 0 | 87.69% |

For the combined record denominator, exact two-airport references cover 48/67
(71.64%) and all exact references cover 59/67 (88.06%). After deduplication,
two-airport references cover 47/65 (72.31%) and all exact references cover
57/65 (87.69%). The two captures therefore show useful standing-reference
coverage for this Denver sample, while 36/84 records do not provide a
source-backed two-endpoint reference: 11 are multi-leg references and 25 are
unsupported, unmatched, or missing a callsign.

The unmatched rows are validly normalizable callsigns whose airline code has no
route file in this pinned tree: `OPS9`, `EJA220`, `HER348`, `JRE749`, `KOW971`,
`EMD24`, `EJA808`, and `FLC84`. Unsupported examples include registrations,
`OPS 6`, `REDCHIEF`, and the unverified mixed-code `F9MX650`. The single missing
row has no callsign. These classifications are listed with ICAO24 and source
details in `coverage.tsv`.

## Interpretation and release boundary

The VRS data is standing reference data keyed by normalized callsign. It does
not include a flight date, a dispatch record, aircraft identity, or proof that
the observed aircraft is flying the listed route now. A multi-leg row such as
`KSBA-KSFO-KLAX-KDEN` remains a chain; SkybyDay must not silently label its
first and last airports as the current departure and destination.

This closes the question “what coverage does the measured standing source have
for these saved captures?” It does not close the product requirement that every
live aircraft have accurate departure and destination data. The application
must continue to display `Unknown` when no source-supported reference exists and
must keep `current_flight_confirmed` false. A future current-flight provider
requires a separately authorized source, licensing review, a dated coverage
study, and a new acceptance gate.
