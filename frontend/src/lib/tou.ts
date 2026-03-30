type Range = [number, number];

export interface TouPeriods {
  weekday: {
    on_peak?: Range[];
    super_off_peak?: Range[];
    super_off_peak_winter_mar_apr?: Range[];
  };
  weekend_holiday: {
    on_peak?: Range[];
    super_off_peak?: Range[];
  };
}

function inRanges(h: number, ranges?: Range[]): boolean {
  return !!ranges && ranges.some(([s, e]) => h >= s && h < e);
}

export function touPeriod(
  h: number,
  mon: number,
  isSummer: boolean,
  isWeekendOrHoliday: boolean,
  touCfg?: TouPeriods,
): string {
  const defaultCfg: TouPeriods = {
    weekday: {
      on_peak: [[16, 21]],
      super_off_peak: [[0, 6]],
      super_off_peak_winter_mar_apr: [[10, 14]],
    },
    weekend_holiday: {
      on_peak: [[16, 21]],
      super_off_peak: [[0, 14]],
    },
  };
  const p = touCfg || defaultCfg;
  const dayType = isWeekendOrHoliday ? 'weekend_holiday' : 'weekday';
  const rules = p[dayType] || {};

  if (inRanges(h, rules.on_peak)) return 'on_peak';
  if (inRanges(h, rules.super_off_peak)) return 'super_off_peak';
  if (
    dayType === 'weekday' &&
    !isSummer &&
    [3, 4].includes(mon) &&
    'super_off_peak_winter_mar_apr' in rules &&
    inRanges(h, (rules as TouPeriods['weekday']).super_off_peak_winter_mar_apr)
  )
    return 'super_off_peak';
  return 'off_peak';
}
