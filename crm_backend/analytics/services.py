"""Phase 10: read-only, tenant-scoped aggregate queries.

No new models -- these are pure aggregations over Project/Task/
CompanyUserProfile, computed on demand rather than stored, so there's
nothing to keep in sync. Callers are responsible for the auth/tenant check
(api/routers/analytics.py reuses the same helpers every other project- and
company-scoped endpoint uses) before calling these.
"""

from django.utils import timezone
from projects_and_tasks.models import Project, Task
from users.models import CompanyUserProfile


async def get_project_stats(project: Project) -> dict:
    tasks = project.tasks.filter(is_deleted=False)
    total = await tasks.acount()
    completed = await tasks.filter(status=Task.STATUS.DONE).acount()
    in_progress = await tasks.filter(status=Task.STATUS.IN_PROGRESS).acount()
    todo = await tasks.filter(status=Task.STATUS.TODO).acount()
    in_review = await tasks.filter(status=Task.STATUS.IN_REVIEW).acount()
    overdue = await tasks.exclude(status=Task.STATUS.DONE).filter(deadline__lt=timezone.now()).acount()
    unassigned = await tasks.filter(assigned_to__isnull=True).acount()
    return {
        'total_tasks': total,
        'completed_tasks': completed,
        'in_progress_tasks': in_progress,
        'todo_tasks': todo,
        'in_review_tasks': in_review,
        'overdue_tasks': overdue,
        'unassigned_tasks': unassigned,
        'completion_percent': round((completed / total) * 100, 2) if total else 0,
    }


async def get_company_stats(company) -> dict:
    projects = Project.objects.filter(company=company, is_deleted=False)
    project_count = await projects.acount()
    active_projects = await projects.filter(status=Project.STATUS.ACTIVE).acount()
    completed_projects = await projects.filter(status=Project.STATUS.DONE).acount()

    tasks = Task.objects.filter(project__company=company, is_deleted=False)
    task_count = await tasks.acount()
    completed_tasks = await tasks.filter(status=Task.STATUS.DONE).acount()

    # The company owner has no CompanyUserProfile row (see company/models.py
    # -- Company.owner is a direct OneToOneField), so member_count is every
    # profile in this company plus the owner themself.
    profile_count = await CompanyUserProfile.objects.filter(company=company).acount()

    return {
        'project_count': project_count,
        'active_projects': active_projects,
        'completed_projects': completed_projects,
        'member_count': profile_count + 1,
        'task_count': task_count,
        'completed_tasks': completed_tasks,
    }
