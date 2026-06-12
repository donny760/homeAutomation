import sqlite3, json
conn = sqlite3.connect('powerwall.db')
rows = conn.execute('''
    SELECT effective_date, winter_on_peak, winter_off_peak, winter_super_off_peak,
           summer_on_peak, summer_off_peak, summer_super_off_peak,
           base_services_charge_per_day, tou_periods_json
    FROM rate_history ORDER BY effective_date
''').fetchall()

def fmt_windows(ranges):
    if not ranges:
        return '—'
    return ', '.join('%d:00–%d:00' % (s, e) for s, e in ranges)

# Header
print('%-12s  %-6s %-6s %-6s  %-6s %-6s %-6s  %-7s  %-20s  %-20s' % (
    'Effective', 'W-On', 'W-Off', 'W-Sup',
    'S-On', 'S-Off', 'S-Sup', 'BSC/day',
    'Wkday super-off-pk', 'Wkend super-off-pk'))
print('-' * 120)

for r in rows:
    tou = json.loads(r[8]) if r[8] else {}
    wd  = tou.get('weekday', {})
    wk  = tou.get('weekend_holiday', {})
    wd_sup = fmt_windows(wd.get('super_off_peak', []))
    wk_sup = fmt_windows(wk.get('super_off_peak', []))
    print('%-12s  $%-5.4f $%-5.4f $%-5.4f  $%-5.4f $%-5.4f $%-5.4f  $%-6.4f  %-20s  %-20s' % (
        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], wd_sup, wk_sup))
