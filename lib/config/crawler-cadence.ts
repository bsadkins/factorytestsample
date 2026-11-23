import { DAY_OF_MONTH, DAY_OF_WEEK, HOURS, MINUTES, MONTH, YEAR, expandList } from './cron';

/** AWS Glue rejects crawler schedules that run more often than this. */
export const GLUE_CRAWLER_MINIMUM_MINUTES = 5;

const MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000;

function clock(hour: number, minute: number): string {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

/**
 * Whether the full calendar selects two consecutive UTC days. Day-of-month L, LW and nW and
 * day-of-week nL and n#k pick at most one day a month, and day-of-week L picks only Saturdays, so
 * none can select adjacent dates. The other forms are enumerated over the supported year range,
 * which makes month lengths, leap years and month/year filters part of the adjacency decision.
 */
function selectsConsecutiveDays(dayOfMonth: string, month: string, dayOfWeek: string, year: string): boolean {
  if ((dayOfMonth !== '?' && /[LW]/.test(dayOfMonth)) || (dayOfMonth === '?' && /[L#]/.test(dayOfWeek))) {
    return false;
  }
  const months = expandList(MONTH, month);
  const years = expandList(YEAR, year);
  const days = new Set(dayOfMonth === '?' ? [] : expandList(DAY_OF_MONTH, dayOfMonth));
  const weekdays = new Set(dayOfMonth === '?' ? expandList(DAY_OF_WEEK, dayOfWeek) : []);
  let previousDay: number | undefined;

  for (const selectedYear of years) {
    for (const selectedMonth of months) {
      const monthLength = new Date(Date.UTC(selectedYear, selectedMonth, 0)).getUTCDate();
      for (let day = 1; day <= monthLength; day += 1) {
        const timestamp = Date.UTC(selectedYear, selectedMonth - 1, day);
        const selected = dayOfMonth === '?' ? weekdays.has(new Date(timestamp).getUTCDay() + 1) : days.has(day);
        if (selected && previousDay !== undefined && timestamp - previousDay === MILLISECONDS_PER_DAY) {
          return true;
        }
        if (selected) {
          previousDay = timestamp;
        }
      }
    }
  }
  return false;
}

/**
 * Problems with how often a Glue crawler would run on a `cron(...)` expression that
 * cronExpressionIssues has already accepted. EventBridge Scheduler allows one-minute schedules, so
 * this applies only to crawler schedules. Reports the earliest pair of runs under the minimum.
 */
export function glueCrawlerCadenceIssues(expression: string): string[] {
  const [minuteField = '', hourField = '', dayOfMonth = '', month = '', dayOfWeek = '', year = ''] = expression
    .slice('cron('.length, -1)
    .split(' ');
  const minutes = expandList(MINUTES, minuteField);
  const hours = expandList(HOURS, hourField);
  const first = minutes[0] ?? MINUTES.min;
  const last = minutes[minutes.length - 1] ?? MINUTES.min;
  const earliestHour = hours[0] ?? HOURS.min;
  const latestHour = hours[hours.length - 1] ?? HOURS.min;
  const issue = (gap: number, earlier: string, later: string): string[] => [
    `runs ${String(gap)} minute${gap === 1 ? '' : 's'} apart (${earlier} and ${later}); ` +
      `AWS Glue crawler schedules need at least ${String(GLUE_CRAWLER_MINIMUM_MINUTES)} minutes between runs`,
  ];

  for (let index = 1; index < minutes.length; index += 1) {
    const previous = minutes[index - 1] ?? first;
    const current = minutes[index] ?? first;
    if (current - previous < GLUE_CRAWLER_MINIMUM_MINUTES) {
      return issue(current - previous, clock(earliestHour, previous), `${clock(earliestHour, current)} UTC`);
    }
  }
  const acrossHours = 60 - last + first;
  if (acrossHours >= GLUE_CRAWLER_MINIMUM_MINUTES) {
    return [];
  }
  const nextHour = hours.find((hour) => hours.includes(hour + 1));
  if (nextHour !== undefined) {
    return issue(acrossHours, clock(nextHour, last), `${clock(nextHour + 1, first)} UTC`);
  }
  if (latestHour === HOURS.max && earliestHour === HOURS.min && selectsConsecutiveDays(dayOfMonth, month, dayOfWeek, year)) {
    return issue(acrossHours, clock(latestHour, last), `${clock(earliestHour, first)} UTC the next day`);
  }
  return [];
}
