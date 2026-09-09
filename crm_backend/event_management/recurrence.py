"""Recurrence as an RRULE string, and the bridge to the fields it replaces.

An event's recurrence is stored as one RFC 5545 ``RRULE`` value
(``FREQ=WEEKLY;BYDAY=MO,WE``) rather than as the three bespoke fields it used
to be (``is_recurring``/``recurrence_cadence``/``recurrence_days``). Those
three could say "weekly on Monday and Wednesday" and nothing else -- no
"every other Tuesday", no "last Friday of the month", no end date -- and every
one of those would have meant another column.

**Nothing expands a rule into occurrences.** There are no per-occurrence rows,
no generated series, and no calendar materialisation. The string is stored,
validated, and handed back; what it is *for* is the next decision, not this
one. That is deliberate -- see the OUT OF SCOPE list.

The legacy fields are still written, from the rule, for one release, so a
rollback and any client that has not moved yet keep working. They are never
read for authorization or scheduling.
"""

from datetime import UTC, datetime

from dateutil.rrule import rrulestr

# The legacy cadence values, and the RRULE FREQ each maps onto. Only these
# three were ever expressible, which is the point.
CADENCE_TO_FREQ = {
    'daily': 'DAILY',
    'weekly': 'WEEKLY',
    'monthly': 'MONTHLY',
}
FREQ_TO_CADENCE = {freq: cadence for cadence, freq in CADENCE_TO_FREQ.items()}

# The legacy day tokens the existing client sends ("Mon"), and the two-letter
# RFC 5545 BYDAY codes. Matched case-insensitively on the way in.
DAY_TO_BYDAY = {
    'mon': 'MO', 'tue': 'TU', 'wed': 'WE', 'thu': 'TH',
    'fri': 'FR', 'sat': 'SA', 'sun': 'SU',
}
BYDAY_TO_DAY = {byday: day.capitalize() for day, byday in DAY_TO_BYDAY.items()}

# A fixed anchor for validation only. Some rules (BYDAY without FREQ=WEEKLY,
# for instance) are only checkable against a start date; none of this is
# stored, and the real start_at is never used here so that validating the same
# rule twice can never disagree with itself.
_VALIDATION_DTSTART = datetime(2000, 1, 1, tzinfo=UTC)

MAX_RRULE_LENGTH = 500


class InvalidRecurrenceRule(ValueError):
    """The submitted string is not a single, storable RRULE."""


def normalize_rrule(value: str) -> str:
    """Return the canonical stored form of ``value``.

    One line, no ``RRULE:`` prefix, upper-cased. Every RFC 5545 keyword and
    value in a rule is case-insensitive and conventionally upper-case, so
    upper-casing is lossless and makes two spellings of the same rule compare
    equal.
    """
    return value.strip().removeprefix('RRULE:').strip().upper()


def validate_rrule(value: str) -> str:
    """Validate a submitted recurrence rule and return its stored form.

    Raises :class:`InvalidRecurrenceRule` with a short, safe reason. The
    parser's own message is not passed through: it describes dateutil's
    internals, not the request.
    """
    rule = normalize_rrule(value)
    if not rule:
        return ''
    if len(rule) > MAX_RRULE_LENGTH:
        raise InvalidRecurrenceRule('Recurrence rule is too long.')
    # A rule, not a calendar. Anything carrying its own DTSTART, EXDATE or a
    # second line is a VEVENT fragment, and accepting one would mean the
    # stored value no longer means what start_at says it means.
    if '\n' in rule or '\r' in rule:
        raise InvalidRecurrenceRule('Recurrence rule must be a single line.')
    for keyword in ('DTSTART', 'EXDATE', 'RDATE', 'EXRULE'):
        if keyword in rule:
            raise InvalidRecurrenceRule(f'Recurrence rule must not contain {keyword}.')
    if not rule.startswith('FREQ='):
        raise InvalidRecurrenceRule('Recurrence rule must start with FREQ=.')
    try:
        rrulestr(f'RRULE:{rule}', dtstart=_VALIDATION_DTSTART)
    except Exception as error:  # noqa: BLE001 -- dateutil raises bare ValueError/KeyError
        raise InvalidRecurrenceRule('Recurrence rule is not a valid RRULE.') from error
    return rule


def rrule_from_legacy(*, is_recurring: bool, cadence: str, days) -> str:
    """Translate the three legacy fields into the rule that means the same.

    Returns ``''`` for anything that was not actually a recurrence -- an
    ``is_recurring`` flag with no cadence said "repeats, somehow", which is
    not a rule and never was.
    """
    if not is_recurring:
        return ''
    freq = CADENCE_TO_FREQ.get((cadence or '').lower())
    if freq is None:
        return ''
    parts = [f'FREQ={freq}']
    if freq == 'WEEKLY':
        bydays = [DAY_TO_BYDAY[token] for day in days or [] if (token := str(day).strip()[:3].lower()) in DAY_TO_BYDAY]
        # Preserve calendar order rather than the order they were clicked in,
        # so the same selection always produces the same string.
        ordered = [code for code in ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU') if code in bydays]
        if ordered:
            parts.append(f'BYDAY={",".join(ordered)}')
    return ';'.join(parts)


def legacy_from_rrule(rule: str) -> dict:
    """Project a rule back onto the legacy fields, as far as they reach.

    A rule the old shape cannot express (``FREQ=YEARLY``, ``INTERVAL=2``, an
    ``UNTIL``) yields ``is_recurring=True`` with an empty cadence: the old
    fields say "this repeats" and decline to say how, which is the honest
    answer rather than a wrong one. This is what the dual-write writes; nothing
    reads it back.
    """
    rule = normalize_rrule(rule or '')
    if not rule:
        return {'is_recurring': False, 'recurrence_cadence': '', 'recurrence_days': []}
    parts = dict(
        piece.split('=', 1) for piece in rule.split(';') if '=' in piece
    )
    freq = parts.get('FREQ', '')
    cadence = FREQ_TO_CADENCE.get(freq, '')
    # INTERVAL other than 1 is exactly the case the legacy fields get wrong:
    # "every other week" would be stored as plain "weekly".
    if parts.get('INTERVAL', '1') != '1':
        cadence = ''
    days = []
    if cadence == 'weekly':
        days = [
            BYDAY_TO_DAY[code]
            for code in parts.get('BYDAY', '').split(',')
            if code in BYDAY_TO_DAY
        ]
    return {'is_recurring': True, 'recurrence_cadence': cadence, 'recurrence_days': days}
