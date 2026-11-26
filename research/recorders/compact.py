"""Merge one UTC day of recorder snapshots into one file per venue.

Input: a directory holding one sub-directory per snapshot (the unpacked artifacts, named
snap-YYYYMMDD-HHMM-<run>), each with <venue>.csv.gz. Output: <out>/<venue>-YYYYMMDD.csv.gz,
rows in snapshot order, one header. A snapshot missing a venue (that venue failed that run) is
skipped for that venue and counted. A header that differs from the first one is a schema change
mid-day and fails the merge rather than writing misaligned columns.
"""
import argparse, csv, gzip, os, sys


def merge(src, day, out):
    snaps = sorted(d for d in os.listdir(src) if d.startswith(f"snap-{day}-"))
    venues = sorted({f[:-7] for d in snaps for f in os.listdir(os.path.join(src, d)) if f.endswith(".csv.gz")})
    os.makedirs(out, exist_ok=True)
    report = {}
    for v in venues:
        header, n, missing = None, 0, 0
        path = os.path.join(out, f"{v}-{day}.csv.gz")
        with gzip.open(path, "wt", newline="") as fo:
            w = csv.writer(fo)
            for d in snaps:
                p = os.path.join(src, d, f"{v}.csv.gz")
                if not os.path.exists(p):
                    missing += 1
                    continue
                with gzip.open(p, "rt", newline="") as fi:
                    r = csv.reader(fi)
                    h = next(r, None)
                    if h is None:
                        continue
                    if header is None:
                        header = h
                        w.writerow(h)
                    elif h != header:
                        raise ValueError(f"{v}: header changed in {d}")
                    for row in r:
                        w.writerow(row); n += 1
        report[v] = (n, len(snaps) - missing, missing)
    return snaps, report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--day", required=True, help="YYYYMMDD")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    snaps, rep = merge(a.src, a.day, a.out)
    print(f"{a.day}: {len(snaps)} snapshots")
    for v, (n, have, miss) in rep.items():
        print(f"  {v}: {n} rows from {have} snapshots, {miss} missing")
    return 0 if snaps else 1


if __name__ == "__main__":
    sys.exit(main())
