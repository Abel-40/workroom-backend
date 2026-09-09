"""A health summary must never name an individual.

§5's hard rule. A project health summary is generated from analytics figures
-- counts, percentages, overdue totals -- and is visible to everyone with
project VIEW. That combination is only safe while the summary talks about
*work*. The moment it says "Alice is behind on three tasks" it becomes a
performance statement about a named person, broadcast to the whole project,
written by a model that was guessing.

§8 already forbids building per-person performance metrics deliberately. This
is the same prohibition arriving by accident, through prose, and it is checked
here rather than trusted to the prompt: the prompt asks the model not to, and
validation is what makes it true.

The check runs **before the summary is persisted**. A summary that names
somebody is regenerated, not saved and cleaned up afterwards -- once it is in
the table it has been readable.
"""

import re
import unicodedata

# Names shorter than this are skipped. "Al", "Bo" and especially initials
# appear inside ordinary words and would make every summary unsaveable.
# Two-character names exist; a false negative on one is a far smaller harm
# than a validator that rejects every summary and gets switched off.
MIN_NAME_LENGTH = 3


def _normalize(text: str) -> str:
    """Casefold and strip accents so "José" matches "Jose".

    A model paraphrasing a name without its diacritics is the likeliest way
    this check gets bypassed by accident rather than by intent.
    """
    decomposed = unicodedata.normalize('NFKD', text or '')
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def candidate_names(members) -> set[str]:
    """Every token a member could be referred to by.

    Takes first name, last name, full name, username, and the local part of
    the email -- because "ask j.smith about it" names somebody just as surely
    as "ask John Smith". The email domain is excluded; matching on it would
    reject every summary at a company whose domain is a word.
    """
    names = set()
    for member in members:
        parts = [
            (member.first_name or '').strip(),
            (member.last_name or '').strip(),
            f'{(member.first_name or "").strip()} {(member.last_name or "").strip()}'.strip(),
            (member.username or '').strip(),
            (member.email or '').split('@')[0].strip(),
        ]
        for part in parts:
            if len(part) >= MIN_NAME_LENGTH:
                names.add(_normalize(part))
    return names


def names_found_in(summary_text: str, members) -> list[str]:
    """Which member names appear in the summary. Empty means it is safe.

    Matching is on word boundaries so "Rob" does not fire on "robust" and
    "Sam" does not fire on "same" -- a validator that cries wolf is one
    somebody eventually disables, which is worse than not having it.

    Separators inside a candidate (a space in "John Smith", a dot in
    "j.smith") are treated as flexible, so "John  Smith" and "john-smith"
    are still caught.
    """
    if not summary_text:
        return []
    haystack = _normalize(summary_text)
    found = []
    for name in candidate_names(members):
        pattern = r'[\s._\-]+'.join(re.escape(part) for part in re.split(r'[\s._\-]+', name) if part)
        if not pattern:
            continue
        if re.search(rf'(?<![\w]){pattern}(?![\w])', haystack):
            found.append(name)
    return sorted(found)


def summary_is_anonymous(summary_text: str, members) -> bool:
    return not names_found_in(summary_text, members)
