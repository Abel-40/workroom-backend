"""Task-type directory API and project-collaborator handling: happy path,
membership requirement, and cross-tenant rejection, reusing the
TwoCompanyTestCase fixture (api/tests.py) so isolation is checked the same
way every other endpoint's tests check it.
"""

import json

from api.tests import TwoCompanyTestCase, auth_header
from django.core.files.uploadedfile import SimpleUploadedFile

from projects_and_tasks.models import Project, TaskType


class TaskTypeListTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        TaskType.objects.create(name='Bug Fix', company=self.company_a)
        TaskType.objects.create(name='Feature', company=self.company_a)
        TaskType.objects.create(name='Other Company Type', company=self.company_b)

    def list_task_types(self, user):
        return self.client.get('/api/task-types/', **auth_header(user))

    def test_owner_lists_their_company_task_types(self):
        response = self.list_task_types(self.owner_a)
        self.assertEqual(response.status_code, 200)
        names = {item['name'] for item in response.json()['data']['results']}
        self.assertEqual(names, {'Bug Fix', 'Feature'})

    def test_member_can_list_task_types(self):
        response = self.list_task_types(self.member_a)
        self.assertEqual(response.status_code, 200)

    def test_task_types_are_scoped_to_the_caller_own_company(self):
        response = self.list_task_types(self.owner_b)
        self.assertEqual(response.status_code, 200)
        names = {item['name'] for item in response.json()['data']['results']}
        self.assertEqual(names, {'Other Company Type'})

    def test_requires_authentication(self):
        response = self.client.get('/api/task-types/')
        self.assertEqual(response.status_code, 401)


class ProjectCollaboratorTests(TwoCompanyTestCase):
    def test_create_project_with_valid_collaborator(self):
        project = self.create_project(collaborator_ids=[str(self.member_a.id)])
        self.assertEqual(project['collaborator_ids'], [str(self.member_a.id)])
        stored = Project.objects.get(id=project['id'])
        self.assertEqual(list(stored.collaborators.values_list('id', flat=True)), [self.member_a.id])

    def test_create_project_rejects_collaborator_from_another_company(self):
        response = self.client.post(
            '/api/projects/',
            json.dumps({'title': 'Cross-tenant', 'collaborator_ids': [str(self.owner_b.id)]}),
            content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('collaborator_ids', response.json()['errors'])
        self.assertFalse(Project.objects.filter(title='Cross-tenant').exists())

    def test_update_project_replaces_collaborators(self):
        project = self.create_project(collaborator_ids=[str(self.member_a.id)])
        response = self.client.patch(
            f"/api/projects/{project['id']}/",
            json.dumps({'collaborator_ids': []}),
            content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['project']['collaborator_ids'], [])

    def test_update_project_rejects_collaborator_from_another_company(self):
        project = self.create_project()
        response = self.client.patch(
            f"/api/projects/{project['id']}/",
            json.dumps({'collaborator_ids': [str(self.owner_b.id)]}),
            content_type='application/json',
            **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        stored = Project.objects.get(id=project['id'])
        self.assertEqual(list(stored.collaborators.all()), [])


class ProjectImageTests(TwoCompanyTestCase):
    """A project's cover image is exactly one of an uploaded file or an
    external link at a time; uploads never go through a public /media/ path
    (see settings.py), only the authenticated GET .../image/ endpoint."""

    def tiny_png(self, name='cover.png'):
        # 1x1 transparent PNG.
        content = bytes.fromhex(
            '89504e470d0a1a0a0000000d4948445200000001000000010802000000907753'
            'de0000000c4944415478da6360000002000100e221bc330000000049454e44ae426082'
        )
        return SimpleUploadedFile(name, content, content_type='image/png')

    def set_link(self, project_id, image_url, user=None):
        return self.client.put(
            f'/api/projects/{project_id}/image/', json.dumps({'image_url': image_url}),
            content_type='application/json', **auth_header(user or self.owner_a),
        )

    def upload(self, project_id, upload=None, user=None):
        return self.client.post(
            f'/api/projects/{project_id}/image/', {'image': upload or self.tiny_png()},
            **auth_header(user or self.owner_a),
        )

    def test_owner_can_set_an_image_link(self):
        project = self.create_project()
        response = self.set_link(project['id'], 'https://example.com/cover.png')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['data']['project']['image'],
            {'kind': 'link', 'url': 'https://example.com/cover.png'},
        )

    def test_owner_can_upload_an_image_file(self):
        project = self.create_project()
        response = self.upload(project['id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['data']['project']['image'],
            {'kind': 'upload', 'url': f"/projects/{project['id']}/image/"},
        )

    def test_uploaded_image_can_be_downloaded(self):
        project = self.create_project()
        self.upload(project['id'])
        response = self.client.get(f"/api/projects/{project['id']}/image/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_setting_a_link_clears_a_previously_uploaded_file(self):
        project = self.create_project()
        self.upload(project['id'])
        self.set_link(project['id'], 'https://example.com/cover.png')
        response = self.client.get(f"/api/projects/{project['id']}/image/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 404)

    def test_uploading_a_file_clears_a_previously_set_link(self):
        project = self.create_project()
        self.set_link(project['id'], 'https://example.com/cover.png')
        response = self.upload(project['id'])
        self.assertEqual(
            response.json()['data']['project']['image']['kind'], 'upload',
        )

    def test_owner_can_remove_the_image(self):
        project = self.create_project()
        self.upload(project['id'])
        response = self.client.delete(f"/api/projects/{project['id']}/image/", **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        detail = self.client.get(f"/api/projects/{project['id']}/", **auth_header(self.owner_a))
        self.assertIsNone(detail.json()['data']['project']['image'])

    def test_rejects_disallowed_content_type(self):
        project = self.create_project()
        bad_file = SimpleUploadedFile('cover.txt', b'not an image', content_type='text/plain')
        response = self.upload(project['id'], upload=bad_file)
        self.assertEqual(response.status_code, 400)

    def test_rejects_oversized_file(self):
        project = self.create_project()
        big_file = SimpleUploadedFile('cover.png', b'\x00' * (5 * 1024 * 1024 + 1), content_type='image/png')
        response = self.upload(project['id'], upload=big_file)
        self.assertEqual(response.status_code, 400)

    def test_member_without_manage_rights_cannot_set_image(self):
        project = self.create_project()
        response = self.set_link(project['id'], 'https://example.com/cover.png', user=self.member_a)
        self.assertEqual(response.status_code, 403)

    def test_image_endpoints_are_scoped_to_the_caller_own_company(self):
        # The project exists but isn't visible to owner_b (company-visibility,
        # different company), so get_project_for_user reports 403, matching
        # every other project sub-resource endpoint's cross-tenant behavior.
        project = self.create_project()
        response = self.set_link(project['id'], 'https://example.com/cover.png', user=self.owner_b)
        self.assertEqual(response.status_code, 403)

    def test_requires_authentication(self):
        project = self.create_project()
        response = self.client.get(f"/api/projects/{project['id']}/image/")
        self.assertEqual(response.status_code, 401)
