# Workroom Backend API

**Base path:** `/api`

## Response format

Unless noted otherwise, every JSON response has this envelope. `errors` is present only when supplied by the endpoint.

```json
{
  "success": "boolean",
  "message": "string",
  "statusCode": "integer",
  "data": "object",
  "errors": "object | null"
}
```

`UUID` values are JSON strings. `datetime` values are ISO-8601 strings. List endpoints return `data.results: array` and `data.meta: { count: integer, page: integer, page_size: integer, has_next: boolean }`.

`User`: `{ id: integer, email: string, username: string, first_name: string, last_name: string }`

## Authentication

### POST `/api/auth/signup/`
Body: `{ email: string, username: string, password: string, first_name?: string, last_name?: string }`

Response `data`: `{ user: { id: integer, email: string, username: string, first_name: string, last_name: string } }`

Status: `201`, `400`

### POST `/api/auth/signin/`
Body: `{ email: string, password: string }`

Response `data`: `{ user: User, is_authenticated: boolean, access: string, role: "Owner" | "DL" | "DM" | null, company_id: UUID | null, company_name: string | null }`

`role`/`company_id`/`company_name` are `null` for a user who hasn't joined or created a company yet. `role` is `"Owner"` for the company creator, otherwise the caller's `CompanyUserProfile.role` (`"DL"` = Department Leader, `"DM"` = Department Member).

Status: `200`, `401`

### POST `/api/auth/refresh-token/`
Body: none

Response `data`: `{ access: string }`

Status: `200`, `401`

### POST `/api/auth/send_invite/`
Body: `{ email: string, department?: UUID | null, role?: "DL" | "DM" }`

Response `data`: `{ email: string, email_sent: boolean }`

Status: `200`, `400`, `403`

### POST `/api/emp/accept_invite/`
Body: `{ token: string, password: string, username: string }`

Response `data`: `{ user: User }`

Status: `201`, `400`

## Company and setup

### POST `/api/company/register/`
Body: `{ name: string, sector: UUID }`

Response `data`: `{ id: UUID, company_name: string, owner: string, sector: UUID }`

Status: `201`, `400`, `404`

### GET `/api/sectors/get_all_sectors/`
Body: none

Response `data`: `{ sectors: Array<{ id: UUID, name: string, description: string }> }`

Status: `200`

### POST `/api/department/create_departments_from_defaults/`
Body: `{ company_id: UUID, selected_types?: UUID[], use_all_default_departments?: boolean, use_all_default_task_types?: boolean }`

Response `data`: `{ company_id: UUID, created_departments: Array<{ name: string }>, total_departments_created: integer }`

Status: `201`, `403`, `404`

### GET `/api/department/{sector_id}/dept_types/`
Body: none

Response `data`: `{ department_types: Array<{ id: UUID, name: string, sector: UUID | null, description: string }> }`

Status: `200`, `404`

### POST `/api/default_task_type/default_task_type/`
Body: `{ company_id: UUID, selected_types?: UUID[], use_all_default_task_types?: boolean, use_all_default_departments?: boolean }`

Response `data`: `{ company_id: UUID, created_task_types: Array<{ name: string }>, total_task_types_created: integer }`

Status: `201`, `403`, `404`

### GET `/api/default_task_type/{sector_id}/default-tasktypes/`
Body: none

Response `data`: `{ tasktypes: Array<{ id: UUID, name: string, sector: UUID | null, description: string }> }`

Status: `200`, `404`

### GET `/api/departments/`
Lists the caller's own company's departments (not sector defaults/templates -- the departments actually created for this company). Any company member (Owner/DL/DM).

Body: none

Response `data`: `{ results: Array<{ id: UUID, name: string, description: string, leader_id: UUID | null, leader_name: string | null, member_count: integer }> }`

Status: `200`, `404`

### POST `/api/departments/`
Creates a department. Requires "managed company" standing: the company owner, or a department leader (same check used by `send_invite`) -- a department member is rejected with `403`.

Body: `{ name: string, description?: string, leader_id?: UUID | null }`

`name` must be unique within the company (case-insensitive); `leader_id`, if given, must be an existing member of the company.

Response `data`: `{ department: { id: UUID, name: string, description: string, leader_id: UUID | null, leader_name: string | null, member_count: 0 } }`

Status: `201`, `400`, `403`

### GET `/api/teams/`
Lists the caller's own company's teams. A team is a cross-department grouping of members assembled for a specific project or initiative (unlike a Department, which is a fixed org unit). Any company member (Owner/DL/DM).

Body: none

Response `data`: `{ results: Array<{ id: UUID, name: string, description: string, leader_id: UUID | null, leader_name: string | null, member_ids: UUID[] }> }`

Status: `200`, `404`

### POST `/api/teams/`
Creates a team. Requires the same "managed company" standing as creating a department.

Body: `{ name: string, description?: string, leader_id?: UUID | null, member_ids?: UUID[] }`

`name` must be unique within the company; `leader_id` and every id in `member_ids`, if given, must be an existing member of the company (an unknown or cross-company id is rejected with `400`).

Response `data`: `{ team: { id: UUID, name: string, description: string, leader_id: UUID | null, leader_name: string | null, member_ids: UUID[] } }`

Status: `201`, `400`, `403`

### GET `/api/task-types/`
Lists the caller's own company's task types (not sector defaults/templates). Any company member (Owner/DL/DM).

Body: none

Response `data`: `{ results: Array<{ id: UUID, name: string, description: string }> }`

Status: `200`, `404`

## Projects

### POST `/api/projects/`
Body: `{ title: string, description?: string, department_id?: UUID | null, team_id?: UUID | null, visibility?: "public" | "company" | "department" | "private", priority?: "low" | "medium" | "high", start_date?: datetime | null, deadline?: datetime | null, collaborator_ids?: UUID[] }`

`collaborator_ids` must be company members (owner or any `CompanyUserProfile`); an unknown or cross-company id is rejected with `400`.

Response `data`: `{ project: Project }`

Status: `201`, `400`

### GET `/api/projects/`
Body: none

Response `data`: `{ results: Project[], meta: Pagination }`

Status: `200`

### GET `/api/projects/{project_id}/`
Body: none

Response `data`: `{ project: Project }`

Status: `200`, `403`, `404`

### PATCH `/api/projects/{project_id}/`
Body: any subset of `{ title: string, description: string, department_id: UUID | null, team_id: UUID | null, visibility: "public" | "company" | "department" | "private", priority: "low" | "medium" | "high", status: "Active" | "Inactive" | "Done", start_date: datetime | null, deadline: datetime | null, collaborator_ids: UUID[] }`

Response `data`: `{ project: Project }`

Status: `200`, `400`, `403`, `404`

### DELETE `/api/projects/{project_id}/`
Body: none

Response `data`: `{}`

Status: `200`, `403`, `404`

`Project`: `{ id: UUID, title: string, description: string, company_id: UUID, department_id: UUID | null, team_id: UUID | null, visibility: string, status: string, priority: string, start_date: datetime, deadline: datetime, created_by: UUID | null, created_at: datetime, updated_at: datetime, total_tasks: integer, active_tasks: integer, completion_percent: number, collaborator_ids: UUID[], image: { kind: "upload" | "link", url: string } | null }`

### Project cover image
A project's cover image is exactly one of an uploaded file or an external link at a time -- setting one clears the other. Requires manage rights on the project (creator, company owner, or the leader of the project's own department), same as `PATCH`/`DELETE` above.

#### PUT `/api/projects/{project_id}/image/`
Sets the cover image to an external link.

Body: `{ image_url: string (URL) }`

Response `data`: `{ project: Project }`

Status: `200`, `400`, `403`, `404`

#### POST `/api/projects/{project_id}/image/`
Uploads a cover image file. Multipart form with a single field `image`. Max 5MB; `image/png`, `image/jpeg`, `image/gif`, or `image/webp` only.

Response `data`: `{ project: Project }`

Status: `200`, `400`, `403`, `404`

#### GET `/api/projects/{project_id}/image/`
Streams the uploaded image file (not a raw `/media/` path -- there isn't one; see settings.py). Requires the same view permission as the project itself. `404` if the project's cover image is an external link or unset.

Status: `200` (binary image response), `403`, `404`

#### DELETE `/api/projects/{project_id}/image/`
Removes the cover image (uploaded file or link, whichever is set).

Response `data`: `{}`

Status: `200`, `403`, `404`

## Tasks

### POST `/api/projects/{project_id}/tasks/`
Body: `{ title: string, description?: string, department_id?: UUID | null, task_type_id?: UUID | null, assigned_to_id?: UUID | null, priority?: "low" | "medium" | "high", deadline?: datetime | null, estimated_time_hours?: number | null }`

Response `data`: `{ task: Task }`

Status: `201`, `400`, `403`, `404`

### GET `/api/projects/{project_id}/tasks/`
Body: none

Response `data`: `{ results: Task[], meta: Pagination }`

Status: `200`, `403`, `404`

### GET `/api/tasks/{task_id}/`
Body: none

Response `data`: `{ task: Task }`

Status: `200`, `403`, `404`

### PATCH `/api/tasks/{task_id}/`
Body: any subset of `{ title: string, description: string, department_id: UUID | null, task_type_id: UUID | null, priority: "low" | "medium" | "high", deadline: datetime | null, estimated_time_hours: number | null, spent_time_hours: number | null }`

Response `data`: `{ task: Task }`

Status: `200`, `400`, `403`, `404`

### PATCH `/api/tasks/{task_id}/status/`
Body: `{ status: "To Do" | "In Progress" | "In Review" | "Done" }`

Response `data`: `{ task: Task }`

Status: `200`, `400`, `403`, `404`

### POST `/api/tasks/{task_id}/assign/`
Body: `{ assigned_to_id?: UUID | null }`

Response `data`: `{ task: Task }`

Status: `200`, `400`, `403`, `404`

### DELETE `/api/tasks/{task_id}/`
Body: none

Response `data`: `{}`

Status: `200`, `403`, `404`

`Task`: `{ id: UUID, project_id: UUID | null, department_id: UUID | null, task_type_id: UUID | null, created_by: UUID | null, assigned_to: UUID | null, title: string, description: string, status: string, priority: string, source: string, deadline: datetime, estimated_time_hours: number | null, spent_time_hours: number | null, created_at: datetime, updated_at: datetime }`

## Documents

### POST `/api/projects/{project_id}/documents/`
Body (`multipart/form-data`): `{ file: File, label?: string, task_id?: UUID | null }`

Response `data`: `{ document: Document }`

Status: `201`, `400`, `403`, `404`

### GET `/api/projects/{project_id}/documents/`
Body: none

Response `data`: `{ results: Document[], meta: Pagination }`

Status: `200`, `403`, `404`

### GET `/api/documents/{document_id}/`
Body: none

Response `data`: `{ document: Document }`

Status: `200`, `403`, `404`

### GET `/api/documents/{document_id}/download/`
Body: none

Response: file stream (`Content-Type` is the document content type)

Status: `200`, `403`, `404`

### DELETE `/api/documents/{document_id}/`
Body: none

Response `data`: `{}`

Status: `200`, `403`, `404`

`Document`: `{ id: UUID, project_id: UUID, task_id: UUID | null, uploaded_by: UUID | null, type: string, name: string, label: string, content_type: string, size: integer, created_at: datetime }`

## AI

### POST `/api/projects/{project_id}/ai-plan/`
Body: none

Response `data`: `{ generation: Generation }`

Status: `202`, `403`, `404`, `500`

### GET `/api/ai/generations/{generation_id}/`
Body: none

Response `data`: `{ generation: Generation }`

Status: `200`, `403`, `404`

### GET `/api/projects/{project_id}/ai-generations/`
Body: none

Response `data`: `{ results: Generation[], meta: Pagination }`

Status: `200`, `403`, `404`

### POST `/api/projects/{project_id}/ai-assistant/`
Body: `{ question: string, reference_url?: URL | null }`

Response `data`: `{ assistant_query: AssistantQuery }`

Status: `202`, `403`, `404`, `500`

### GET `/api/ai/assistant-queries/{query_id}/`
Body: none

Response `data`: `{ assistant_query: AssistantQuery }`

Status: `200`, `403`, `404`

### GET `/api/projects/{project_id}/ai-assistant-queries/`
Body: none

Response `data`: `{ results: AssistantQuery[], meta: Pagination }`

Status: `200`, `403`, `404`

### POST `/api/projects/{project_id}/ai-health-summary/`
Body: none

Response `data`: `{ health_summary: HealthSummary }`

Status: `202`, `403`, `404`, `500`

### GET `/api/ai/health-summaries/{summary_id}/`
Body: none

Response `data`: `{ health_summary: HealthSummary }`

Status: `200`, `403`, `404`

### GET `/api/projects/{project_id}/ai-health-summaries/`
Body: none

Response `data`: `{ results: HealthSummary[], meta: Pagination }`

Status: `200`, `403`, `404`

`Generation`: `{ id: UUID, project_id: UUID, requested_by: UUID | null, status: string, provider: string, model: string, requested_at: datetime, started_at: datetime | null, completed_at: datetime | null, task_count: integer, error_message: string | null }`

`AssistantQuery`: `{ id: UUID, project_id: UUID, requested_by: UUID | null, question: string, reference_url: string | null, status: string, provider: string, model: string, answer: string | null, refused: boolean, requested_at: datetime, started_at: datetime | null, completed_at: datetime | null, error_message: string | null }`

`HealthSummary`: `{ id: UUID, project_id: UUID, requested_by: UUID | null, status: string, provider: string, model: string, summary: string | null, risk_level: string | null, requested_at: datetime, started_at: datetime | null, completed_at: datetime | null, error_message: string | null }`

## Notifications

### GET `/api/notifications/`
Body: none

Response `data`: `{ results: Notification[], meta: Pagination, unread_count: integer }`

Status: `200`

### POST `/api/notifications/{notification_id}/read/`
Body: none

Response `data`: `{ notification: Notification }`

Status: `200`, `404`

### POST `/api/notifications/mark-all-read/`
Body: none

Response `data`: `{ updated_count: integer }`

Status: `200`

`Notification`: `{ id: UUID, type: string, title: string, message: string, related_object_type: string, related_object_id: UUID | null, is_read: boolean, created_at: datetime }`

## Analytics, subscription, and health

### GET `/api/analytics/projects/{project_id}/`
Body: none

Response `data`: `{ total_tasks: integer, completed_tasks: integer, in_progress_tasks: integer, todo_tasks: integer, in_review_tasks: integer, overdue_tasks: integer, unassigned_tasks: integer, completion_percent: number }`

Status: `200`, `403`, `404`

### GET `/api/analytics/company/`
Body: none

Response `data`: `{ project_count: integer, active_projects: integer, completed_projects: integer, member_count: integer, task_count: integer, completed_tasks: integer }`

Status: `200`, `404`

### GET `/api/analytics/company/members/`
Body: none

Per-member workload snapshot: the company owner plus every `CompanyUserProfile`, each with their current active (assigned, not done, not deleted) task counts, broken down by status.

Response `data`: `{ members: Array<{ id: UUID, first_name: string, last_name: string, username: string, email: string, role: "Owner" | "DL" | "DM", department: string | null, active_task_count: integer, todo_count: integer, in_progress_count: integer, in_review_count: integer }> }`

Status: `200`, `404`

### POST `/api/subscriptions/start-checkout/`
Body: `{ plan_id: UUID }`

Response `data`: `{ checkout_url: string }`

Status: `200`, `400`, `500`

### GET `/api/subscriptions/my-subscription/`
Body: none

Response `data`: `{ id: UUID, company: UUID, plan: UUID, status: string, is_trial: boolean, start_date: datetime, current_period_end: datetime | null, canceled_at: datetime | null, is_active: boolean, on_trial: boolean }`

Status: `200`, `404`

### GET `/api/health/`
Body: none

Response `data`: `{ database: "up" | "down" }`

Status: `200`, `503`
