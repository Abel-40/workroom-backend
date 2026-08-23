"""Phase 10: read-only, tenant-scoped aggregate queries.

No new models -- these are pure aggregations over Project/Task/
CompanyUserProfile, computed on demand rather than stored, so there's
nothing to keep in sync. Callers are responsible for the auth/tenant check
(api/routers/analytics.py reuses the same helpers every other project- and
company-scoped endpoint uses) before calling these.
"""

from departments_and_teams.models import Department
from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone
from projects_and_tasks.models import Project, Task
from users.models import CompanyUserProfile

User = get_user_model()


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


_ACTIVE_STATUS_FIELD = {
    Task.STATUS.TODO: 'todo_count',
    Task.STATUS.IN_PROGRESS: 'in_progress_count',
    Task.STATUS.IN_REVIEW: 'in_review_count',
}


async def get_company_workload(company) -> list[dict]:
    """Per-member workload snapshot: the owner plus every CompanyUserProfile
    in ``company``, each with their current active (assigned, not done, not
    deleted) task counts broken down by status -- what a dashboard "who's
    carrying what" view needs. One aggregate query for the counts rather
    than one query per member."""
    counts_by_user: dict[str, dict[str, int]] = {}
    counts_qs = (
        Task.objects.filter(project__company=company, is_deleted=False)
        .exclude(status=Task.STATUS.DONE)
        .exclude(assigned_to__isnull=True)
        .values('assigned_to', 'status')
        .annotate(count=Count('id'))
    )
    async for row in counts_qs:
        field = _ACTIVE_STATUS_FIELD.get(row['status'])
        if field is None:
            continue
        counts_by_user.setdefault(str(row['assigned_to']), {}).setdefault(field, 0)
        counts_by_user[str(row['assigned_to'])][field] = row['count']

    def workload_fields(user_id: str) -> dict:
        counts = counts_by_user.get(user_id, {})
        todo = counts.get('todo_count', 0)
        in_progress = counts.get('in_progress_count', 0)
        in_review = counts.get('in_review_count', 0)
        return {
            'active_task_count': todo + in_progress + in_review,
            'todo_count': todo,
            'in_progress_count': in_progress,
            'in_review_count': in_review,
        }

    # The owner has no CompanyUserProfile row (see get_company_stats above),
    # so they're assembled separately and always listed first.
    owner = await User.objects.aget(id=company.owner_id)
    members = [{
        'id': str(owner.id),
        'first_name': owner.first_name,
        'last_name': owner.last_name,
        'username': owner.username,
        'email': owner.email,
        'role': CompanyUserProfile.Role.Owner,
        'department': None,
        'is_active': True,
        **workload_fields(str(owner.id)),
    }]

    profiles = CompanyUserProfile.objects.filter(company=company).select_related('user', 'department')
    async for profile in profiles:
        members.append({
            'id': str(profile.user_id),
            'first_name': profile.user.first_name,
            'last_name': profile.user.last_name,
            'username': profile.user.username,
            'email': profile.user.email,
            'role': profile.role,
            'department': profile.department.name if profile.department_id else None,
            'is_active': profile.is_active,
            **workload_fields(str(profile.user_id)),
        })
    return members


async def get_department_stats(company) -> list[dict]:
    """Per-department project/task counts and member count -- a bounded,
    point-in-time breakdown for the company insights page. No trend/velocity
    data; that's explicitly out of V1 scope.

    Task.department is the task's own field, distinct from
    Task.project.department -- a task can be assigned to a different
    department than its parent project, so counting by the task's own field
    is deliberate here, not an oversight.
    """
    results = []
    departments = Department.objects.filter(company=company).order_by('name')
    async for department in departments:
        project_count = await Project.objects.filter(department=department, is_deleted=False).acount()
        tasks = Task.objects.filter(department=department, is_deleted=False)
        task_count = await tasks.acount()
        completed_task_count = await tasks.filter(status=Task.STATUS.DONE).acount()
        member_count = await CompanyUserProfile.objects.filter(department=department).acount()
        results.append({
            'id': str(department.id),
            'name': department.name,
            'project_count': project_count,
            'task_count': task_count,
            'completed_task_count': completed_task_count,
            'member_count': member_count,
        })
    return results


async def get_member_workload(company, user) -> dict:
    """Single-user variant of get_company_workload's active-task breakdown --
    for the employee detail page, which doesn't need the whole company's
    counts recomputed just to show one person's."""
    counts_by_status: dict[str, int] = {}
    counts_qs = (
        Task.objects.filter(project__company=company, is_deleted=False, assigned_to=user)
        .exclude(status=Task.STATUS.DONE)
        .values('status')
        .annotate(count=Count('id'))
    )
    async for row in counts_qs:
        field = _ACTIVE_STATUS_FIELD.get(row['status'])
        if field is not None:
            counts_by_status[field] = row['count']
    todo = counts_by_status.get('todo_count', 0)
    in_progress = counts_by_status.get('in_progress_count', 0)
    in_review = counts_by_status.get('in_review_count', 0)
    return {
        'active_task_count': todo + in_progress + in_review,
        'todo_count': todo,
        'in_progress_count': in_progress,
        'in_review_count': in_review,
    }
