"""Background jobs for projects and tasks.

Currently one: the dependency-graph integrity check.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


def find_dependency_cycles() -> list[list]:
    """Every cycle in the live `blocks` graph, as lists of task ids.

    The backstop `add_task_dependency` asks for. Creating an edge already
    refuses to close a loop, under an advisory lock, so in a correct system
    this finds nothing. It exists because "in a correct system" is the part
    worth verifying: a cycle can still arrive through a path that does not go
    past that check -- a data migration, a fixture load, a future bulk import,
    or a bug in the check itself -- and a cycle is silent. Nobody reports it;
    two tasks simply never start, and the reason is invisible from either one.

    Reported, never repaired. Breaking a cycle means deleting somebody's edge,
    and which edge is wrong is a judgment about the work, not something a
    nightly job should decide at 3am with no one watching.
    """
    from .models import Task, TaskDependency

    graph = {}
    rows = TaskDependency.objects.filter(
        kind=TaskDependency.Kind.BLOCKS,
        predecessor__is_deleted=False,
        successor__is_deleted=False,
    ).values_list('predecessor_id', 'successor_id')
    for predecessor_id, successor_id in rows:
        graph.setdefault(predecessor_id, []).append(successor_id)

    cycles = []
    # Iterative depth-first with an explicit path, rather than recursion: a
    # long dependency chain is exactly what a sequential plan looks like, and
    # it should not be able to exhaust the stack.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {}
    for root in list(graph):
        if colour.get(root, WHITE) != WHITE:
            continue
        stack = [(root, iter(graph.get(root, ())))]
        path = [root]
        colour[root] = GREY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                state = colour.get(child, WHITE)
                if state == GREY:
                    # Back edge: the cycle is the tail of the current path.
                    cycles.append(path[path.index(child):] + [child])
                    continue
                if state == WHITE:
                    colour[child] = GREY
                    path.append(child)
                    stack.append((child, iter(graph.get(child, ()))))
                    advanced = True
                    break
            if not advanced:
                colour[node] = BLACK
                stack.pop()
                path.pop()

    if cycles:
        titles = dict(
            Task.objects.filter(id__in={task_id for cycle in cycles for task_id in cycle})
            .values_list('id', 'title')
        )
        for cycle in cycles:
            logger.error(
                'task_dependency.cycle_detected tasks=%s',
                ' -> '.join(f'{titles.get(task_id, "?")} ({task_id})' for task_id in cycle),
            )
    return cycles


@shared_task
def check_dependency_graph_integrity_task():
    """Celery Beat schedule entry (see settings.CELERY_BEAT_SCHEDULE)."""
    cycles = find_dependency_cycles()
    return len(cycles)
