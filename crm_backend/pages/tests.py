"""Folder/Page ("wiki") API: cross-tenant rejection and basic CRUD auth,
reusing the TwoCompanyTestCase fixture (api/tests.py) the same way every
other endpoint's tests check tenant isolation (DEVELOPMENT_RULES Rule 12).
"""

import json

from api.tests import TwoCompanyTestCase, auth_header

from pages.models import Page, PageFolder
from pages.services import blocks_to_text


class BlocksToTextTests(TwoCompanyTestCase):
    def test_flattens_headings_paragraphs_and_lists_only(self):
        text = blocks_to_text([
            {'type': 'heading', 'text': 'Title'},
            {'type': 'paragraph', 'text': 'Body text.'},
            {'type': 'list', 'items': ['one', 'two']},
            {'type': 'attachment', 'file_name': 'ignored.png'},
        ])
        self.assertEqual(text, 'Title\nBody text.\n- one\n- two')

    def test_caps_length(self):
        text = blocks_to_text([{'type': 'paragraph', 'text': 'x' * 5000}], max_chars=10)
        self.assertEqual(len(text), 10)


class PageFolderSecurityTests(TwoCompanyTestCase):
    def test_create_and_list_folder_scoped_to_own_company(self):
        response = self.client.post(
            '/api/page-folders/', json.dumps({'name': 'Research', 'color': 'emerald'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        folder_id = response.json()['data']['folder']['id']

        own_list = self.client.get('/api/page-folders/', **auth_header(self.owner_a))
        self.assertIn(folder_id, [f['id'] for f in own_list.json()['data']['results']])

        other_list = self.client.get('/api/page-folders/', **auth_header(self.owner_b))
        self.assertNotIn(folder_id, [f['id'] for f in other_list.json()['data']['results']])

    def test_member_of_the_company_can_create_folders(self):
        response = self.client.post(
            '/api/page-folders/', json.dumps({'name': 'Notes'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 201)


class PageSecurityTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.folder = PageFolder.objects.create(name='Docs', company=self.company_a, created_by=self.owner_a)

    def test_create_page_in_own_companys_folder(self):
        response = self.client.post(
            f'/api/page-folders/{self.folder.id}/pages/',
            json.dumps({'title': 'Spec', 'blocks': [{'type': 'paragraph', 'text': 'Hello'}]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['page']['title'], 'Spec')

    def test_other_company_cannot_add_pages_to_this_folder(self):
        response = self.client.post(
            f'/api/page-folders/{self.folder.id}/pages/', json.dumps({'title': 'Spec'}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)

    def test_other_company_cannot_view_or_edit_a_page(self):
        page = Page.objects.create(folder=self.folder, title='Private notes', created_by=self.owner_a)
        get_response = self.client.get(f'/api/pages/{page.id}/', **auth_header(self.owner_b))
        self.assertEqual(get_response.status_code, 403)
        patch_response = self.client.patch(
            f'/api/pages/{page.id}/', json.dumps({'title': 'Hijacked'}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(patch_response.status_code, 403)
        page.refresh_from_db()
        self.assertEqual(page.title, 'Private notes')

    def test_delete_is_a_soft_delete_and_hides_the_page_from_listings(self):
        page = Page.objects.create(folder=self.folder, title='Draft', created_by=self.owner_a)
        response = self.client.delete(f'/api/pages/{page.id}/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        page.refresh_from_db()
        self.assertTrue(page.is_deleted)
        listing = self.client.get('/api/pages/', **auth_header(self.owner_a))
        self.assertNotIn(str(page.id), [p['id'] for p in listing.json()['data']['results']])

    def test_picker_listing_excludes_other_companys_pages(self):
        Page.objects.create(folder=self.folder, title='Company A page', created_by=self.owner_a)
        other_folder = PageFolder.objects.create(name='B Docs', company=self.company_b, created_by=self.owner_b)
        Page.objects.create(folder=other_folder, title='Company B page', created_by=self.owner_b)

        response = self.client.get('/api/pages/', **auth_header(self.owner_a))
        titles = {p['title'] for p in response.json()['data']['results']}
        self.assertEqual(titles, {'Company A page'})
