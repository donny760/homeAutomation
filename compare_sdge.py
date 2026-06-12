"""Compare SDG&E 15-min interval CSV against daily_costs DB by month."""
import csv
import sqlite3
import sys
import os
import io
from datetime import datetime
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

CSV_CANDIDATES = [
    r'C:\Users\Don\Downloads\Electric_15_Minute_1-1-2026_6-6-2026_20260607.csv',
    r'd:\projects\homeAutomation\sdge_2026.csv',
]
if len(sys.argv) > 1:
    CSV_CANDIDATES = [sys.argv[1]]

CSV_PATH = next((p for p in CSV_CANDIDATES if os.path.exists(p)), None)
if not CSV_PATH:
    print("CSV not found. Pass path as argument: py compare_sdge.py <path_to_csv>")
    sys.exit(1)

DB_PATH = r'd:\projects\homeAutomation\powerwall.db'
print(f"CSV:  {CSV_PATH}")
print(f"DB:   {DB_PATH}\n")

# ── Parse SDG&E CSV ────────────────────────────────────────────────────────────
sdge_monthly = defaultdict(lambda: {'consumption': 0.0, 'generation': 0.0})
sdge_daily   = defaultdict(lambda: {'consumption': 0.0, 'generation': 0.0})

with open(CSV_PATH, newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            d = datetime.strptime(row['Date'].strip(), '%m/%d/%Y').date()
        except ValueError:
            continue
        cons = float(row['Consumption'] or 0)
        gen  = float(row['Generation']  or 0)
        mk   = d.strftime('%Y-%m')
        dk   = d.isoformat()
        sdge_monthly[mk]['consumption'] += cons
        sdge_monthly[mk]['generation']  += gen
        sdge_daily[dk]['consumption']   += cons
        sdge_daily[dk]['generation']    += gen

# ── Query DB ───────────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
rows = conn.execute(
    "SELECT date, import_kwh, export_kwh FROM daily_costs "
    "WHERE date >= '2026-01-01' AND date <= '2026-06-05' ORDER BY date"
).fetchall()
conn.close()

db_monthly = defaultdict(lambda: {'import': 0.0, 'export': 0.0})
db_daily   = {}
for date, imp, exp in rows:
    db_monthly[date[:7]]['import']  += imp
    db_monthly[date[:7]]['export']  += exp
    db_daily[date] = (imp, exp)

# ── Monthly comparison ─────────────────────────────────────────────────────────
all_months = sorted(set(list(sdge_monthly) + list(db_monthly)))
print(f"{'Month':<8} | {'SDGE Imp':>10} {'DB Imp':>10} {'D Imp':>9} {'D%':>6} | "
      f"{'SDGE Exp':>10} {'DB Exp':>10} {'D Exp':>9} {'D%':>6}")
print('-' * 88)
ytd_sdge_imp = ytd_db_imp = ytd_sdge_exp = ytd_db_exp = 0.0
for m in all_months:
    si = sdge_monthly[m]['consumption']
    sg = sdge_monthly[m]['generation']
    di = db_monthly[m]['import']
    de = db_monthly[m]['export']
    di_pct = (di - si) / si * 100 if si else 0
    de_pct = (de - sg) / sg * 100 if sg else 0
    ytd_sdge_imp += si; ytd_db_imp += di
    ytd_sdge_exp += sg; ytd_db_exp += de
    print(f"{m:<8} | {si:>10.1f} {di:>10.1f} {di-si:>+9.1f} {di_pct:>+5.1f}% | "
          f"{sg:>10.1f} {de:>10.1f} {de-sg:>+9.1f} {de_pct:>+5.1f}%")

print('-' * 88)
ytd_imp_pct = (ytd_db_imp - ytd_sdge_imp) / ytd_sdge_imp * 100 if ytd_sdge_imp else 0
ytd_exp_pct = (ytd_db_exp - ytd_sdge_exp) / ytd_sdge_exp * 100 if ytd_sdge_exp else 0
print(f"{'YTD':<8} | {ytd_sdge_imp:>10.1f} {ytd_db_imp:>10.1f} {ytd_db_imp-ytd_sdge_imp:>+9.1f} "
      f"{ytd_imp_pct:>+5.1f}% | {ytd_sdge_exp:>10.1f} {ytd_db_exp:>10.1f} "
      f"{ytd_db_exp-ytd_sdge_exp:>+9.1f} {ytd_exp_pct:>+5.1f}%")

# ── Per-day deltas (flag days > 2 kWh off) ────────────────────────────────────
print("\nDays with import or export delta > 2.0 kWh:")
print(f"{'Date':<12} {'SDGE Imp':>10} {'DB Imp':>10} {'D Imp':>8} | "
      f"{'SDGE Exp':>10} {'DB Exp':>10} {'D Exp':>8}")
flagged = 0
for dk in sorted(sdge_daily):
    if dk > '2026-06-05':
        continue
    si = sdge_daily[dk]['consumption']
    sg = sdge_daily[dk]['generation']
    di, de = db_daily.get(dk, (0.0, 0.0))
    if abs(di - si) > 2.0 or abs(de - sg) > 2.0:
        print(f"{dk:<12} {si:>10.2f} {di:>10.2f} {di-si:>+8.2f} | "
              f"{sg:>10.2f} {de:>10.2f} {de-sg:>+8.2f}")
        flagged += 1
if flagged == 0:
    print("  None — all days within 2.0 kWh on both import and export.")
