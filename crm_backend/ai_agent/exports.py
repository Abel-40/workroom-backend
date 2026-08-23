"""Health-report .xlsx export (AI Workspace Health Check "Download report").

Reads real, already-computed data only -- analytics.services.get_project_stats
and the project's own tasks -- never invents anything. There is deliberately
no subtask-count column: subtasks aren't a modeled concept anywhere in
projects_and_tasks today, and the design brief explicitly requires the
report to never invent project information, so the column is omitted rather
than fabricated with zeros.

Sync, not async: openpyxl and the queryset iteration below are sync; the
caller (api/routers/ai.py) wraps this in sync_to_async, same reasoning as
projects_and_tasks.services.persist_ai_generated_tasks.
"""

import re
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from .models import AIProjectHealthSummary

_UNSAFE_FILENAME_CHARS = re.compile(r'[^A-Za-z0-9 _.-]')


def safe_filename(title: str, *, suffix: str = 'health-report.xlsx') -> str:
    """Strips a project title down to characters that are always safe inside
    a Content-Disposition header value, avoiding any header-injection risk
    from a title containing quotes/CRLF/exotic unicode."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub('', title).strip() or 'project'
    return f'{cleaned[:50]}-{suffix}'


def _assignee_label(task) -> str:
    if not task.assigned_to_id:
        return 'Unassigned'
    return task.assigned_to.first_name or task.assigned_to.username


def build_health_report_workbook(project, summary: AIProjectHealthSummary, stats: dict) -> BytesIO:
    workbook = Workbook()
    bold = Font(bold=True)

    summary_sheet = workbook.active
    summary_sheet.title = 'Summary'
    for row in [
        ('Project', project.title),
        ('Status', project.status),
        ('Risk level', summary.risk_level or 'unknown'),
        ('Completion %', stats['completion_percent']),
        ('Total tasks', stats['total_tasks']),
        ('Completed tasks', stats['completed_tasks']),
        ('In progress tasks', stats['in_progress_tasks']),
        ('To do tasks', stats['todo_tasks']),
        ('In review tasks', stats['in_review_tasks']),
        ('Overdue tasks', stats['overdue_tasks']),
        ('Unassigned tasks', stats['unassigned_tasks']),
        ('AI summary', summary.summary or ''),
    ]:
        summary_sheet.append(row)
    for cell in summary_sheet['A']:
        cell.font = bold
    summary_sheet.column_dimensions['A'].width = 20
    summary_sheet.column_dimensions['B'].width = 60

    tasks_sheet = workbook.create_sheet('Tasks')
    tasks_sheet.append(['Task', 'Assignee', 'Status', 'Due date'])
    for cell in tasks_sheet[1]:
        cell.font = bold
    tasks = project.tasks.filter(is_deleted=False).select_related('assigned_to').order_by('sequence', 'created_at')
    for task in tasks:
        tasks_sheet.append([
            task.title, _assignee_label(task), task.status,
            task.deadline.strftime('%Y-%m-%d') if task.deadline else '',
        ])
    tasks_sheet.column_dimensions['A'].width = 40
    tasks_sheet.column_dimensions['B'].width = 24
    tasks_sheet.column_dimensions['C'].width = 16
    tasks_sheet.column_dimensions['D'].width = 14

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
