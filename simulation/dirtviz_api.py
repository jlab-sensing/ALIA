"""Pull any DirtViz sensor stream into the CSV format `dirtviz_transformer.py`
reads.

Why this exists: the DirtViz web export returns **hourly means**, and averaging
destroys exactly the sample-to-sample redundancy ALIA is built to remove. The
API will serve the underlying series -- 5-minute on the GT BME280, 14-62 s on
some TEROS cells -- but only when asked for `resample=none`.

The API exposes streams two different ways, and this module covers both:

- **Dedicated endpoints.** `/power/<cell>` and `/teros/<cell>` each return
  several measurement columns in one response.
- **Named sensors.** `/sensor/` takes a sensor name plus a single measurement.

There is no endpoint that lists what a cell actually has, so availability is
discovered by probing (`--list` to just print it). Everything found is fetched
unless `--sources` or `--measurements` narrows it.

Known `ents` bug (2.3.11): `BackendClient.sensor_data` accepts a `resample`
argument and never puts it in the query string, so it silently returns the
hourly default. This module calls `client.get(...)` directly to work around
that; drop the workaround if upstream fixes it.

Example:
    python -m simulation.dirtviz_api --cell 1538 \\
        --start 2026-06-01 --end 2026-07-20 \\
        --output data/dirtviz-raw/gt_bme280.csv
"""

import argparse
import csv
import os
from datetime import datetime, timedelta

import pandas as pd
from ents.dirtviz import BackendClient

# Server-side aggregation. "none" is the raw series; anything else pre-averages
# and inflates the compression numbers downstream.
RESAMPLE_NONE = "none"

# Endpoints returning several measurement columns per response. Units are not
# in the payload, so they are recorded here. `/power/` reports v/i/p and the
# scaling is self-evident from the data: p == v*i/1000, which holds only for
# mV x uA -> uW.
ENDPOINT_SOURCES = {
    "power": {
        "path": "/power/{cell_id}",
        "units": {"v": "mV", "i": "uA", "p": "uW"},
    },
    "teros": {
        "path": "/teros/{cell_id}",
        # vwc is percent and ec is uS/cm here -- NOT the m3/m3 and dS/m of the
        # Haybarn TEROS-12 channels. `dirtviz_transformer` keeps them apart.
        "units": {"vwc": "%", "raw_vwc": "raw", "temp": "C", "ec": "uS/cm"},
    },
}

# Columns that are metadata rather than measurements.
_META_SUFFIX = "_unit"


def list_sensors(client, cell_id: int) -> list[dict]:
    """The cell's registered `/sensor/` streams, from `/cell/<id>/sensors`.

    This is the authoritative index: it returns the exact name+measurement pairs
    the endpoint expects, along with their units, so nothing has to be guessed.
    Rows look like:

        {"name": "WATERMARK200TS_SOIL_TEMPERATURE",
         "measurement": "Soil Temperature", "unit": "C", "data_type": "float"}

    It does NOT cover `/power/` and `/teros/` -- cells serving those return an
    empty list here, so those endpoints are probed separately. It is also
    time-independent, which makes it the right thing to show when a requested
    window comes back empty.
    """
    try:
        rows = client.get(f"/cell/{cell_id}/sensors")
    except Exception:
        return []
    return [r for r in rows if r.get("name")] if isinstance(rows, list) else []


def _endpoint_frame(client, cell_id, source, params) -> pd.DataFrame:
    """Raw response from a dedicated endpoint as a frame (may be empty)."""
    path = ENDPOINT_SOURCES[source]["path"].format(cell_id=cell_id)
    return pd.DataFrame(client.get(path, params=params))


def _named_frame(client, cell_id, sensor, measurement, params) -> pd.DataFrame:
    """Raw response from `/sensor/` as a frame (may be empty)."""
    query = dict(params)
    query |= {"cellId": cell_id, "name": sensor, "measurement": measurement}
    return pd.DataFrame(client.get("/sensor/", params=query))


def discover(
    client, cell, start: datetime, end: datetime, resample: str = RESAMPLE_NONE
) -> tuple[dict, dict]:
    """({source: [measurements]}, {(source, measurement): unit}) for this cell.

    Named sensors come from the registry, so any sensor works without this
    module knowing about it. `/power/` and `/teros/` are absent from the
    registry and are probed instead. Both are then checked against the requested
    window, so a source is reported only if you could actually fetch it.
    """
    params = BackendClient.time_to_params(start, end) | {"resample": resample}
    available, units = {}, {}

    for source in ENDPOINT_SOURCES:
        try:
            df = _endpoint_frame(client, cell.id, source, params)
        except Exception:
            continue
        if df.empty:
            continue
        measurements = [
            c
            for c in df.columns
            if c != "timestamp" and not c.endswith(_META_SUFFIX)
        ]
        # A column present but entirely null is not a stream.
        measurements = [m for m in measurements if df[m].notna().any()]
        if measurements:
            available[source] = measurements
            for m in measurements:
                units[(source, m)] = ENDPOINT_SOURCES[source]["units"].get(m, "")

    for entry in list_sensors(client, cell.id):
        sensor, measurement = entry["name"], entry.get("measurement", "")
        try:
            df = _named_frame(client, cell.id, sensor, measurement, params)
        except Exception:
            continue
        if df.empty or "data" not in df or not df["data"].notna().any():
            continue
        available.setdefault(sensor, []).append(measurement)
        units[(sensor, measurement)] = entry.get("unit", "")

    return available, units


def data_range(client, cell, resample: str = RESAMPLE_NONE):
    """(first, last) timestamp across every stream, or None if there is none.

    Only called when a requested window comes up empty, to turn "no data" into
    a window the user can actually use.
    """
    wide = BackendClient.time_to_params(
        datetime(2015, 1, 1), datetime(2100, 1, 1)
    ) | {"resample": resample}
    stamps = []

    for source in ENDPOINT_SOURCES:
        try:
            df = _endpoint_frame(client, cell.id, source, wide)
            if not df.empty:
                stamps.append(pd.to_datetime(df["timestamp"], format="mixed"))
        except Exception:
            pass
    for entry in list_sensors(client, cell.id):
        try:
            df = _named_frame(
                client, cell.id, entry["name"], entry.get("measurement", ""), wide
            )
            if not df.empty:
                stamps.append(pd.to_datetime(df["timestamp"], format="mixed"))
        except Exception:
            pass

    if not stamps:
        return None
    # Naive UTC, to compare against the naive datetimes the CLI parses.
    allts = pd.concat(stamps).map(
        lambda t: t.tz_convert(None) if t.tzinfo else t
    )
    return allts.min(), allts.max()


def _chunks(start: datetime, end: datetime, days: int):
    """Split a range into <=`days` windows: a multi-month pull at 14 s cadence
    is millions of points, and failures are cheaper to retry in pieces."""
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=days), end)
        yield cursor, stop
        cursor = stop


def fetch_source(
    client,
    cell,
    source: str,
    measurements: list[str],
    start: datetime,
    end: datetime,
    resample: str = RESAMPLE_NONE,
    chunk_days: int = 30,
) -> tuple[pd.DataFrame, dict]:
    """One source's measurements as a wide frame, plus {measurement: unit}."""
    frames = []
    units = {}

    for window_start, window_end in _chunks(start, end, chunk_days):
        params = BackendClient.time_to_params(window_start, window_end)
        params["resample"] = resample

        if source in ENDPOINT_SOURCES:
            df = _endpoint_frame(client, cell.id, source, params)
            if df.empty:
                continue
            keep = [m for m in measurements if m in df.columns]
            frames.append(df[["timestamp"] + keep])
            units |= {
                m: ENDPOINT_SOURCES[source]["units"].get(m, "") for m in keep
            }
        else:
            # `/sensor/` is one request per measurement; join them on timestamp.
            wide = None
            for measurement in measurements:
                df = _named_frame(client, cell.id, source, measurement, params)
                if df.empty:
                    continue
                if "unit" in df and len(df):
                    units[measurement] = str(df["unit"].iloc[0])
                part = df[["timestamp", "data"]].rename(
                    columns={"data": measurement}
                )
                wide = part if wide is None else wide.merge(
                    part, on="timestamp", how="outer"
                )
            if wide is not None:
                frames.append(wide)

    if not frames:
        return pd.DataFrame(columns=["timestamp"]), units

    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], format="mixed", utc=True)
    # Chunk boundaries are inclusive on both ends, so seam samples repeat.
    out = (
        out.drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    return out, units


def describe_empty(
    client, cell, start: datetime, end: datetime, resample: str = RESAMPLE_NONE
) -> str:
    """Explain an empty window in terms the caller can act on.

    "No data" has two very different causes -- the cell has no streams at all,
    or it has streams but not in the window you asked for -- and the fix differs,
    so say which and name a window that would work.
    """
    registered = list_sensors(client, cell.id)
    lines = [f"no data for cell {cell.id} in {start.date()}..{end.date()}"]

    if registered:
        lines.append("  registered streams:")
        for entry in registered:
            unit = entry.get("unit", "")
            lines.append(
                f"    {entry['name']} / {entry.get('measurement', '')}"
                + (f" ({unit})" if unit else "")
            )

    span = data_range(client, cell, resample)
    if span is None:
        lines.append("  this cell has no data at any date.")
    else:
        first, last = span
        lines.append(f"  data covers {first:%Y-%m-%d} .. {last:%Y-%m-%d}")
        if start > last:
            lines.append("  -> the window you asked for is AFTER the last reading.")
        elif end < first:
            lines.append("  -> the window you asked for is BEFORE the first reading.")
        else:
            lines.append("  -> that range overlaps yours; try widening it.")
    return "\n".join(lines)


def fetch_cell(
    cell_id: int,
    start: datetime,
    end: datetime,
    sources: list[str] | None = None,
    measurements: list[str] | None = None,
    resample: str = RESAMPLE_NONE,
    base_url: str | None = None,
) -> tuple[pd.DataFrame, dict, dict, str]:
    """Wide frame across every requested source, {column: unit},
    {column: source}, and the cell name.

    Columns are prefixed with their source only on collision, so the common
    single-source case keeps the API's own names.
    """
    client = BackendClient(base_url) if base_url else BackendClient()
    cell = client.cell_from_id(cell_id)
    if cell is None:
        raise SystemExit(f"cell {cell_id} not found")

    print(f"cell {cell.id} '{cell.name}' ({cell.location})")
    available, discovered_units = discover(client, cell, start, end, resample)
    if not available:
        raise SystemExit(describe_empty(client, cell, start, end, resample))

    if sources:
        unknown = [s for s in sources if s not in available]
        for s in unknown:
            print(f"  [warning] source {s!r} has no data here; skipping")
        available = {s: m for s, m in available.items() if s in sources}
    if measurements:
        available = {
            s: [m for m in ms if m in measurements]
            for s, ms in available.items()
        }
        available = {s: ms for s, ms in available.items() if ms}
    if not available:
        raise SystemExit("nothing left to fetch after --sources/--measurements")

    # Only qualify names that would otherwise clash between sources.
    seen = [m for ms in available.values() for m in ms]
    clashing = {m for m in seen if seen.count(m) > 1}

    wide, units, devices = None, {}, {}
    for source, wanted in available.items():
        df, source_units = fetch_source(
            client, cell, source, wanted, start, end, resample
        )
        if df.empty or len(df.columns) < 2:
            continue
        renames = {m: f"{source}_{m}" for m in wanted if m in clashing}
        df = df.rename(columns=renames)
        # Registry units beat payload units: the registry is declared metadata,
        # the payload copy is whatever the last row happened to carry.
        units |= {
            renames.get(m, m): discovered_units.get((source, m))
            or source_units.get(m, "")
            for m in wanted
        }
        devices |= {renames.get(m, m): source for m in wanted}
        got = [c for c in df.columns if c != "timestamp"]
        print(f"  {source:8s} {len(df):7d} rows  {', '.join(got)}")
        wide = df if wide is None else wide.merge(df, on="timestamp", how="outer")

    if wide is None:
        raise SystemExit("no data returned")
    wide = wide.sort_values("timestamp").reset_index(drop=True)
    return wide, units, devices, cell.name


def write_csv(df: pd.DataFrame, units: dict, devices: dict, path: str) -> None:
    """Write the stacked-header CSV `dirtviz_transformer.py` parses: column
    names, units, source, then epoch-second rows."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    channels = [c for c in df.columns if c != "timestamp"]

    # Positional iteration, not `itertuples()._asdict()`: registry measurement
    # names contain spaces ("Soil Temperature"), which itertuples renames to
    # positional `_1`/`_2` fields, so lookup by column name would fail.
    ordered = df[["timestamp"] + channels]

    with open(path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["timestamp"] + channels)
        w.writerow(["s"] + [units.get(c, "") for c in channels])
        w.writerow(["TIME"] + [devices.get(c, "").upper() for c in channels])
        for row in ordered.itertuples(index=False, name=None):
            w.writerow(
                [int(row[0].timestamp())]
                + [
                    "" if pd.isna(v) else repr(float(v))
                    for v in row[1:]
                ]
            )


def _parse_date(text: str) -> datetime:
    return datetime.fromisoformat(text)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cell", required=True, type=int, help="DirtViz cell id.")
    p.add_argument("--start", required=True, type=_parse_date, help="ISO date.")
    p.add_argument("--end", required=True, type=_parse_date, help="ISO date.")
    p.add_argument(
        "--sources",
        default="",
        help="Comma-separated subset of sources (e.g. power,teros,bme280). "
        "Default: every source found with data.",
    )
    p.add_argument(
        "--measurements",
        default="",
        help="Comma-separated subset of measurements. Default: all of them.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Print what this cell has in the window, then exit.",
    )
    p.add_argument(
        "--resample",
        default=RESAMPLE_NONE,
        help="Server-side aggregation. Keep 'none' — anything else pre-averages "
        "the data and inflates the compression numbers.",
    )
    p.add_argument("-o", "--output", help="CSV path to write.")
    args = p.parse_args()

    if args.list:
        client = BackendClient()
        cell = client.cell_from_id(args.cell)
        if cell is None:
            raise SystemExit(f"cell {args.cell} not found")
        print(f"cell {cell.id} '{cell.name}' ({cell.location})")
        found, units = discover(client, cell, args.start, args.end, args.resample)
        if not found:
            print(describe_empty(client, cell, args.start, args.end, args.resample))
            return
        for source, measurements in found.items():
            labelled = [
                m + (f" ({units[(source, m)]})" if units.get((source, m)) else "")
                for m in measurements
            ]
            print(f"  {source:34s} {', '.join(labelled)}")
        return

    if not args.output:
        p.error("-o/--output is required unless --list is given")

    def split(text):
        return [x.strip() for x in text.split(",") if x.strip()]

    df, units, devices, _cell_name = fetch_cell(
        args.cell,
        args.start,
        args.end,
        sources=split(args.sources) or None,
        measurements=split(args.measurements) or None,
        resample=args.resample,
    )
    write_csv(df, units, devices, args.output)

    span = df["timestamp"].max() - df["timestamp"].min()
    gaps = df["timestamp"].diff().dt.total_seconds().dropna()
    print(
        f"\nwrote {args.output}: {len(df)} rows, {span.days} days, "
        f"median cadence {gaps.median():.0f}s"
    )
    if args.resample != RESAMPLE_NONE:
        print(
            f"WARNING: resample={args.resample!r} returns pre-averaged data; "
            f"compression measured against it will be optimistic."
        )


if __name__ == "__main__":
    main()
