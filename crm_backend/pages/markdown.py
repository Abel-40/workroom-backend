"""Turn a flat, markdown-ish AI response into structured page blocks
(heading/paragraph/list) -- the same schema pages/services.py::create_page
already accepts and the Info Portal editor already renders (see
PageBlock in pagesStore.ts). Used so a saved AI answer reads like the rest
of the wiki instead of landing as one raw, unstructured text dump.

Deliberately narrow: this recognizes ATX headings (#/##/...), a line that's
ENTIRELY bold as a pseudo-heading (a common AI-response convention, e.g.
"**Project Overview**" on its own line), and -/*/N. list items -- not full
CommonMark. Nested lists are flattened to one level, since PageBlock.items
is a flat list of strings, not a tree. Inline styling (bold/italic/code
*within* a line) is left as-is in the block text -- that's a display-time
concern for the frontend (src/lib/markdown.ts), not a storage concern here.
"""

import re

_HEADING_RE = re.compile(r'^#{1,6}\s+(.*)$')
_BOLD_ONLY_LINE_RE = re.compile(r'^\*\*(.+?)\*\*:?$')
_LIST_ITEM_RE = re.compile(r'^(?:[-*]|\d+\.)\s+(.*)$')


def markdown_to_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    paragraph_lines: list[str] = []
    list_items: list[str] = []

    def flush_paragraph():
        if paragraph_lines:
            blocks.append({'type': 'paragraph', 'text': '\n'.join(paragraph_lines).strip()})
            paragraph_lines.clear()

    def flush_list():
        if list_items:
            blocks.append({'type': 'list', 'items': list(list_items)})
            list_items.clear()

    for raw_line in (text or '').splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue

        heading_match = _HEADING_RE.match(line)
        bold_only_match = _BOLD_ONLY_LINE_RE.match(line)
        list_match = _LIST_ITEM_RE.match(line)

        if heading_match:
            flush_paragraph()
            flush_list()
            blocks.append({'type': 'heading', 'text': heading_match.group(1).strip()})
        elif bold_only_match:
            flush_paragraph()
            flush_list()
            blocks.append({'type': 'heading', 'text': bold_only_match.group(1).strip()})
        elif list_match:
            flush_paragraph()
            list_items.append(list_match.group(1).strip())
        else:
            flush_list()
            paragraph_lines.append(line)

    flush_paragraph()
    flush_list()
    return blocks or [{'type': 'paragraph', 'text': (text or '').strip()}]
