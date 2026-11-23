/**
 * Field rules for the cron expressions accepted by EventBridge Scheduler and AWS Glue:
 * cron(minutes hours day-of-month month day-of-week year), all in UTC.
 */
export interface CronField {
  readonly name: string;
  readonly min: number;
  readonly max: number;
  /** Upper-case names for 1..n, accepted wherever a number is. */
  readonly names?: readonly string[];
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'] as const;
const WEEKDAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'] as const;

export const MINUTES: CronField = { name: 'minutes', min: 0, max: 59 };
export const HOURS: CronField = { name: 'hours', min: 0, max: 23 };
export const DAY_OF_MONTH: CronField = { name: 'day-of-month', min: 1, max: 31 };
export const MONTH: CronField = { name: 'month', min: 1, max: 12, names: MONTHS };
export const DAY_OF_WEEK: CronField = { name: 'day-of-week', min: 1, max: 7, names: WEEKDAYS };
export const YEAR: CronField = { name: 'year', min: 1970, max: 2199 };

/** Maximum occurrence N in day-of-week "D#N" (the Nth weekday D of the month). */
const MAX_WEEKDAY_OCCURRENCE = 5;

class FieldError extends Error {}

function expectedValues(field: CronField): string {
  const range = `${String(field.min)}-${String(field.max)}`;
  const { names } = field;
  return names === undefined ? range : `${range} or ${String(names[0])}-${String(names[names.length - 1])}`;
}

function parseValue(field: CronField, token: string): number {
  if (/^\d+$/.test(token)) {
    const value = Number(token);
    if (value >= field.min && value <= field.max) {
      return value;
    }
  } else {
    const index = field.names?.indexOf(token.toUpperCase()) ?? -1;
    if (index >= 0) {
      return index + 1;
    }
  }
  throw new FieldError(`expected ${expectedValues(field)}, got "${token}"`);
}

/** value, value-value, (* | value | value-value)/increment, or a lone * when it is the whole field. */
function checkElement(field: CronField, element: string, alone: boolean): void {
  const [base = '', increment, ...extra] = element.split('/');
  if (extra.length > 0) {
    throw new FieldError(`expected at most one "/", got "${element}"`);
  }
  if (increment !== undefined) {
    const span = field.max - field.min + 1;
    if (!/^\d+$/.test(increment) || Number(increment) < 1 || Number(increment) > span) {
      throw new FieldError(`expected an increment of 1-${String(span)}, got "${increment}"`);
    }
  }
  if (base === '*') {
    if (!alone && increment === undefined) {
      throw new FieldError('"*" cannot be combined with other values');
    }
    return;
  }
  const [from = '', to, ...more] = base.split('-');
  if (more.length > 0) {
    throw new FieldError(`expected a single range, got "${base}"`);
  }
  parseValue(field, from);
  if (to !== undefined) {
    parseValue(field, to);
  }
}

/**
 * Every value a list already accepted by checkList selects, in ascending order. A range whose start
 * is after its end wraps past the field maximum.
 */
export function expandList(field: CronField, value: string): number[] {
  const span = field.max - field.min + 1;
  const selected = new Set<number>();
  for (const element of value.split(',')) {
    const [base = '', increment] = element.split('/');
    const step = increment === undefined ? 1 : Number(increment);
    let from = field.min;
    let to = field.max;
    if (base !== '*') {
      const [start = '', end] = base.split('-');
      from = parseValue(field, start);
      to = end !== undefined ? parseValue(field, end) : increment === undefined ? from : field.max;
    }
    const length = (to - from + span) % span;
    for (let offset = 0; offset <= length; offset += step) {
      selected.add(field.min + ((from - field.min + offset) % span));
    }
  }
  return [...selected].sort((a, b) => a - b);
}

function checkList(field: CronField, value: string): void {
  const elements = value.split(',');
  for (const element of elements) {
    checkElement(field, element, elements.length === 1);
  }
}

/** Day-of-month also accepts L (last day), LW (last weekday) and nW (weekday nearest day n). */
function checkDayOfMonth(value: string): void {
  if (value === 'L' || value === 'LW') {
    return;
  }
  const nearest = /^(.+)W$/.exec(value);
  if (nearest !== null) {
    parseValue(DAY_OF_MONTH, nearest[1] ?? '');
    return;
  }
  checkList(DAY_OF_MONTH, value);
}

/** Day-of-week also accepts L (last day of the week), nL (last weekday n of the month) and n#k (kth weekday n). */
function checkDayOfWeek(value: string): void {
  if (value === 'L') {
    return;
  }
  const last = /^(.+)L$/.exec(value);
  if (last !== null) {
    parseValue(DAY_OF_WEEK, last[1] ?? '');
    return;
  }
  const nth = /^([^#]+)#(.*)$/.exec(value);
  if (nth !== null) {
    parseValue(DAY_OF_WEEK, nth[1] ?? '');
    const occurrence = nth[2] ?? '';
    if (!/^[1-9]$/.test(occurrence) || Number(occurrence) > MAX_WEEKDAY_OCCURRENCE) {
      throw new FieldError(`expected an occurrence of 1-${String(MAX_WEEKDAY_OCCURRENCE)}, got "${occurrence}"`);
    }
    return;
  }
  checkList(DAY_OF_WEEK, value);
}

const FIELDS: readonly [CronField, (value: string) => void][] = [
  [MINUTES, (value) => { checkList(MINUTES, value); }],
  [HOURS, (value) => { checkList(HOURS, value); }],
  [DAY_OF_MONTH, checkDayOfMonth],
  [MONTH, (value) => { checkList(MONTH, value); }],
  [DAY_OF_WEEK, checkDayOfWeek],
  [YEAR, (value) => { checkList(YEAR, value); }],
];

/**
 * Problems with a `cron(...)` expression whose overall shape (six space-separated fields) the schema
 * has already checked; an empty result means EventBridge Scheduler and AWS Glue accept every field.
 */
export function cronExpressionIssues(expression: string): string[] {
  const fields = expression.slice('cron('.length, -1).split(' ');
  const issues: string[] = [];
  FIELDS.forEach(([field, check], index) => {
    const value = fields[index] ?? '';
    const questionMarkAllowed = field === DAY_OF_MONTH || field === DAY_OF_WEEK;
    try {
      if (value === '?' && questionMarkAllowed) {
        return;
      }
      if (value.includes('?')) {
        throw new FieldError('"?" is allowed only in day-of-month or day-of-week');
      }
      check(value);
    } catch (error) {
      if (!(error instanceof FieldError)) {
        throw error;
      }
      issues.push(`${field.name} field "${value}" is invalid: ${error.message}`);
    }
  });
  const [, , dayOfMonth, , dayOfWeek] = fields;
  if ((dayOfMonth === '?') === (dayOfWeek === '?')) {
    issues.push('must use "?" in exactly one of day-of-month or day-of-week');
  }
  return issues;
}
