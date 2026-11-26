# #41, fixed before computing: KXBTCD, 5-50 min, maker at 98-99c (taker band 01-02),
# same gate as #21-#24; reported beside bands 03-05 and 06-10 from the same tape so the
# method's under-read cancels in the comparison. Tape: the #40 run's KXBTCD control shard.
import sys
sys.path.insert(0, sys.argv[1])
import score_ladder as L, score_taker_flow as S
rows = [r for r in S.load(sys.argv[2]) if r["series"] == "KXBTCD"
        and r["ttc"] in ("5-10m", "10-15m", "15-30m", "30-50m")]
days = sorted({r["day"] for r in rows}); half = days[len(days)//2]
print(f"KXBTCD 5-50m, {days[0]}..{days[-1]} ({len(days)} days), H1 < {half}")
c = L.cells(rows, lambda r: r["band"])
for b in ("01-02", "03-05", "06-10"):
    ok, st = L.verdict(c[b], half)
    print(f"\nband {b} (maker at {100-int(b[3:])}-{100-int(b[:2])}c)  {'PASS' if ok else 'fail'}")
    for k in ("all", "H1", "H2"):
        print(f"  {k:<3} " + (S.fmt(st[k]) if st[k] else "none"))
