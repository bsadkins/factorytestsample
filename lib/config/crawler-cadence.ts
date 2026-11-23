import { DAY_OF_MONTH, DAY_OF_WEEK, HOURS, MINUTES, expandList } from './cron';

/** AWS Glue rejects crawler schedules that run more often than this. */
export const GLUE_CRAWLER_MINIMUM_MINUTES = 5;

/** Shortest month length; a day at or past it can be the last day of some month. */
const SHORTEST_MONTH_DAYS = 28;

function clock(hour: number, minute: number): string {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

/**
 * Whether two consecutive calendar days can both be selected. Day-of-month L, LW and nW and
 * day-of-week nL and n#k pick at most one day a month, and day-of-week L picks only Saturdays. Any
 * selected day of month from the 28th with the 1st counts as consecutive, whatever the month field.
 */
function selectsConsecutiveDays(dayOfMonth: string, dayOfWeek: string): boolean {
  if (dayOfMonth !== '?') {
    if (/[LW]/.test(dayOfMonth)) {
      return false;
    }
    const days = new Set(expandList(DAY_OF_MONTH, dayOfMonth));
    return [...days].some((day) => days.has(day + 1) || (day >= SHORTEST_MONTH_DAYS && days.has(DAY_OF_MONTH.min)));
  }
  if (/[L#]/.test(dayOfWeek)) {
    return false;
  }
  const weekdays = new Set(expandList(DAY_OF_WEEK, dayOfWeek));
  return [...weekdays].some((day) => weekdays.has((day % DAY_OF_WEEK.max) + 1));
}

/**
 * Problems with how often a Glue crawler would run on a `cron(...)` expression that
 * cronExpressionIssues has already accepted. EventBridge Scheduler allows one-minute schedules, so
 * this applies only to crawler schedules. Reports the earliest pair of runs under the minimum.
 */
export function glueCrawlerCadenceIssues(expression: string): string[] {
  const [minuteField = '', hourField = '', dayOfMonth = '', , dayOfWeek = ''] = expression.slice('cron('.length, -1).split(' ');
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
  if (latestHour === HOURS.max && earliestHour === HOURS.min && selectsConsecutiveDays(dayOfMonth, dayOfWeek)) {
    return issue(acrossHours, clock(latestHour, last), `${clock(earliestHour, first)} UTC the next day`);
  }
  return [];
}
