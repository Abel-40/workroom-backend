"""Folder/Page ("wiki") API: cross-tenant rejection and basic CRUD auth,
reusing the TwoCompanyTestCase fixture (api/tests.py) the same way every
other endpoint's tests check tenant isolation (DEVELOPMENT_RULES Rule 12).
"""

import json

from api.tests import TwoCompanyTestCase, auth_header
from notifications_and_activity.models import Notification
from users.models import CompanyUserProfile, User

from pages.markdown import markdown_to_blocks
from pages.models import FolderShare, Page, PageFolder
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


class MarkdownToBlocksTests(TwoCompanyTestCase):
    def test_atx_heading_becomes_a_heading_block(self):
        blocks = markdown_to_blocks('## Project Overview\nSome body text.')
        self.assertEqual(blocks[0], {'type': 'heading', 'text': 'Project Overview'})
        self.assertEqual(blocks[1], {'type': 'paragraph', 'text': 'Some body text.'})

    def test_bold_only_line_becomes_a_heading_block(self):
        blocks = markdown_to_blocks('**Project Overview**\n\nBody paragraph.')
        self.assertEqual(blocks[0], {'type': 'heading', 'text': 'Project Overview'})
        self.assertEqual(blocks[1], {'type': 'paragraph', 'text': 'Body paragraph.'})

    def test_bold_lead_in_list_item_stays_a_list_item_not_a_heading(self):
        blocks = markdown_to_blocks('- **Title:** Research the best stack\n- **Goal:** Ship it')
        self.assertEqual(blocks, [{
            'type': 'list',
            'items': ['**Title:** Research the best stack', '**Goal:** Ship it'],
        }])

    def test_numbered_list_items_are_collected_into_one_list_block(self):
        blocks = markdown_to_blocks('1. First\n2. Second\n3. Third')
        self.assertEqual(blocks, [{'type': 'list', 'items': ['First', 'Second', 'Third']}])

    def test_consecutive_paragraph_lines_join_into_one_block(self):
        blocks = markdown_to_blocks('Line one.\nLine two.\n\nA new paragraph.')
        self.assertEqual(blocks, [
            {'type': 'paragraph', 'text': 'Line one.\nLine two.'},
            {'type': 'paragraph', 'text': 'A new paragraph.'},
        ])

    def test_blank_or_plain_text_falls_back_to_a_single_paragraph(self):
        self.assertEqual(markdown_to_blocks('Just one line.'), [{'type': 'paragraph', 'text': 'Just one line.'}])
        self.assertEqual(markdown_to_blocks(''), [{'type': 'paragraph', 'text': ''}])


class PageFolderSecurityTests(TwoCompanyTestCase):
    def test_create_and_list_folder_scoped_to_own_company(self):
        response = self.client.post(
            '/api/v1/page-folders/', json.dumps({'name': 'Research', 'color': 'emerald'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        folder_id = response.json()['data']['folder']['id']

        own_list = self.client.get('/api/v1/page-folders/', **auth_header(self.owner_a))
        self.assertIn(folder_id, [f['id'] for f in own_list.json()['data']['results']])

        other_list = self.client.get('/api/v1/page-folders/', **auth_header(self.owner_b))
        self.assertNotIn(folder_id, [f['id'] for f in other_list.json()['data']['results']])

    def test_member_of_the_company_can_create_folders(self):
        response = self.client.post(
            '/api/v1/page-folders/', json.dumps({'name': 'Notes'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 201)

    def test_delete_folder_removes_it_from_listing(self):
        folder = PageFolder.objects.create(name='Old Notes', company=self.company_a, created_by=self.owner_a)
        response = self.client.delete(f'/api/v1/page-folders/{folder.id}/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        listing = self.client.get('/api/v1/page-folders/', **auth_header(self.owner_a))
        self.assertNotIn(str(folder.id), [f['id'] for f in listing.json()['data']['results']])

    def test_other_company_cannot_delete_this_folder(self):
        folder = PageFolder.objects.create(name='Docs', company=self.company_a, created_by=self.owner_a)
        response = self.client.delete(f'/api/v1/page-folders/{folder.id}/', **auth_header(self.owner_b))
        self.assertEqual(response.status_code, 403)
        folder.refresh_from_db()
        self.assertFalse(folder.is_deleted)

    def test_deleting_a_folder_cascades_to_its_pages(self):
        folder = PageFolder.objects.create(name='Docs', company=self.company_a, created_by=self.owner_a)
        page = Page.objects.create(folder=folder, title='Spec', created_by=self.owner_a)
        response = self.client.delete(f'/api/v1/page-folders/{folder.id}/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        page.refresh_from_db()
        self.assertTrue(page.is_deleted)
        # Cascaded pages must also drop out of the cross-folder picker used
        # by the AI Assistant's page-context feature.
        picker = self.client.get('/api/v1/pages/', **auth_header(self.owner_a))
        self.assertNotIn(str(page.id), [p['id'] for p in picker.json()['data']['results']])


class PageSecurityTests(TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.folder = PageFolder.objects.create(name='Docs', company=self.company_a, created_by=self.owner_a)

    def test_create_page_in_own_companys_folder(self):
        response = self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/pages/',
            json.dumps({'title': 'Spec', 'blocks': [{'type': 'paragraph', 'text': 'Hello'}]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['page']['title'], 'Spec')

    def test_other_company_cannot_add_pages_to_this_folder(self):
        response = self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/pages/', json.dumps({'title': 'Spec'}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)

    def test_other_company_cannot_view_or_edit_a_page(self):
        page = Page.objects.create(folder=self.folder, title='Private notes', created_by=self.owner_a)
        get_response = self.client.get(f'/api/v1/pages/{page.id}/', **auth_header(self.owner_b))
        self.assertEqual(get_response.status_code, 403)
        patch_response = self.client.patch(
            f'/api/v1/pages/{page.id}/', json.dumps({'title': 'Hijacked'}),
            content_type='application/json', **auth_header(self.owner_b),
        )
        self.assertEqual(patch_response.status_code, 403)
        page.refresh_from_db()
        self.assertEqual(page.title, 'Private notes')

    def test_delete_is_a_soft_delete_and_hides_the_page_from_listings(self):
        page = Page.objects.create(folder=self.folder, title='Draft', created_by=self.owner_a)
        response = self.client.delete(f'/api/v1/pages/{page.id}/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200)
        page.refresh_from_db()
        self.assertTrue(page.is_deleted)
        listing = self.client.get('/api/v1/pages/', **auth_header(self.owner_a))
        self.assertNotIn(str(page.id), [p['id'] for p in listing.json()['data']['results']])

    def test_picker_listing_excludes_other_companys_pages(self):
        Page.objects.create(folder=self.folder, title='Company A page', created_by=self.owner_a)
        other_folder = PageFolder.objects.create(name='B Docs', company=self.company_b, created_by=self.owner_b)
        Page.objects.create(folder=other_folder, title='Company B page', created_by=self.owner_b)

        response = self.client.get('/api/v1/pages/', **auth_header(self.owner_a))
        titles = {p['title'] for p in response.json()['data']['results']}
        self.assertEqual(titles, {'Company A page'})


class FolderPrivacyTests(TwoCompanyTestCase):
    """A folder defaults to creator-only visibility within its own company;
    FolderShare is the only way another member gets access. member_a is a
    real second user of company_a (not its owner), so it's the right
    fixture for "same company, not the creator, not shared" -- the actual
    new rule under test, as distinct from the pre-existing cross-company
    checks above."""

    def setUp(self):
        super().setUp()
        self.folder = PageFolder.objects.create(name='Founder notes', company=self.company_a, created_by=self.owner_a)
        self.page = Page.objects.create(folder=self.folder, title='Runway plan', created_by=self.owner_a)

    def test_creator_can_list_view_and_edit_their_own_folder(self):
        listing = self.client.get('/api/v1/page-folders/', **auth_header(self.owner_a))
        self.assertIn(str(self.folder.id), [f['id'] for f in listing.json()['data']['results']])

        get_response = self.client.get(f'/api/v1/pages/{self.page.id}/', **auth_header(self.owner_a))
        self.assertEqual(get_response.status_code, 200)

        patch_response = self.client.patch(
            f'/api/v1/pages/{self.page.id}/', json.dumps({'title': 'Updated plan'}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(patch_response.status_code, 200)

    def test_same_company_non_shared_member_cannot_see_or_open_the_folder(self):
        listing = self.client.get('/api/v1/page-folders/', **auth_header(self.member_a))
        self.assertNotIn(str(self.folder.id), [f['id'] for f in listing.json()['data']['results']])

        pages_response = self.client.get(f'/api/v1/page-folders/{self.folder.id}/pages/', **auth_header(self.member_a))
        self.assertEqual(pages_response.status_code, 403)

        page_response = self.client.get(f'/api/v1/pages/{self.page.id}/', **auth_header(self.member_a))
        self.assertEqual(page_response.status_code, 403)

    def test_sharing_grants_view_and_edit_but_not_delete(self):
        share_response = self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/share/', json.dumps({'user_ids': [str(self.member_a.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(share_response.status_code, 200)
        self.assertTrue(FolderShare.objects.filter(folder=self.folder, user=self.member_a).exists())

        listing = self.client.get('/api/v1/page-folders/', **auth_header(self.member_a))
        self.assertIn(str(self.folder.id), [f['id'] for f in listing.json()['data']['results']])

        patch_response = self.client.patch(
            f'/api/v1/pages/{self.page.id}/', json.dumps({'title': 'Edited by shared member'}),
            content_type='application/json', **auth_header(self.member_a),
        )
        self.assertEqual(patch_response.status_code, 200)

        # Shared access is not ownership: no delete, no re-sharing.
        delete_response = self.client.delete(f'/api/v1/page-folders/{self.folder.id}/', **auth_header(self.member_a))
        self.assertEqual(delete_response.status_code, 403)

    def test_only_the_creator_can_share_the_folder(self):
        response = self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/share/', json.dumps({'user_ids': [str(self.member_a.id)]}),
            content_type='application/json', **auth_header(self.member_a),
        )
        # member_a has no access at all yet, so this 403s before even
        # reaching the "only the creator may share" check -- get_folder_for_user
        # runs first, matching every other folder endpoint.
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FolderShare.objects.filter(folder=self.folder).exists())

    def test_cannot_share_with_someone_outside_the_company(self):
        response = self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/share/', json.dumps({'user_ids': [str(self.owner_b.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(FolderShare.objects.filter(folder=self.folder).exists())


class FolderShareManagementTests(TwoCompanyTestCase):
    """Sharing is only half a Google-Docs-style flow without the other half:
    seeing who currently has access, taking it back, and telling the person
    they were given it in the first place."""

    def setUp(self):
        super().setUp()
        self.folder = PageFolder.objects.create(name='Founder notes', company=self.company_a, created_by=self.owner_a)
        # A second ordinary member of company_a -- the shared fixture only
        # provides one, and "a collaborator may not revoke someone else"
        # needs two distinct non-owners.
        self.other_member_a = User.objects.create_user(
            email='other-member-a@example.com', username='other-member-a', password='Kx9#mQ2vLp8Z',
        )
        CompanyUserProfile.objects.create(
            user=self.other_member_a, company=self.company_a, department=self.department_a,
            role=CompanyUserProfile.Role.DEPARTMENT_MEMBER,
        )

    def _share_with(self, user):
        return self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/share/', json.dumps({'user_ids': [str(user.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )

    def test_sharing_notifies_the_recipient_exactly_once(self):
        self.assertEqual(self._share_with(self.member_a).status_code, 200)
        notifications = Notification.objects.filter(
            recipient=self.member_a, type=Notification.Type.FOLDER_SHARED,
        )
        self.assertEqual(notifications.count(), 1)
        self.assertEqual(notifications.first().related_object_id, self.folder.id)

        # Re-sharing with someone who already has access is a no-op, not a
        # second notification (retry-safety, Rule 8).
        self.assertEqual(self._share_with(self.member_a).status_code, 200)
        self.assertEqual(notifications.count(), 1)

    def test_owner_and_shared_member_can_both_see_the_access_list(self):
        self._share_with(self.member_a)
        for caller in (self.owner_a, self.member_a):
            response = self.client.get(
                f'/api/v1/page-folders/{self.folder.id}/shares/', **auth_header(caller),
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()['data']
            self.assertEqual(data['owner_id'], str(self.owner_a.id))
            self.assertEqual([s['user_id'] for s in data['results']], [str(self.member_a.id)])

    def test_a_member_with_no_access_cannot_see_the_access_list(self):
        response = self.client.get(
            f'/api/v1/page-folders/{self.folder.id}/shares/', **auth_header(self.member_a),
        )
        self.assertEqual(response.status_code, 403)

    def test_another_company_cannot_see_the_access_list(self):
        response = self.client.get(
            f'/api/v1/page-folders/{self.folder.id}/shares/', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 403)

    def test_creator_can_revoke_access_and_the_folder_disappears_again(self):
        self._share_with(self.member_a)
        response = self.client.delete(
            f'/api/v1/page-folders/{self.folder.id}/shares/{self.member_a.id}/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FolderShare.objects.filter(folder=self.folder, user=self.member_a).exists())

        listing = self.client.get('/api/v1/page-folders/', **auth_header(self.member_a))
        self.assertNotIn(str(self.folder.id), [f['id'] for f in listing.json()['data']['results']])

    def test_a_shared_member_can_remove_themselves_but_not_anyone_else(self):
        self._share_with(self.member_a)
        self._share_with(self.owner_a)  # no-op: creator already has access

        other = self.other_member_a
        self.client.post(
            f'/api/v1/page-folders/{self.folder.id}/share/', json.dumps({'user_ids': [str(other.id)]}),
            content_type='application/json', **auth_header(self.owner_a),
        )

        # member_a may not revoke someone else's access...
        forbidden = self.client.delete(
            f'/api/v1/page-folders/{self.folder.id}/shares/{other.id}/', **auth_header(self.member_a),
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertTrue(FolderShare.objects.filter(folder=self.folder, user=other).exists())

        # ...but may leave the folder themselves.
        allowed = self.client.delete(
            f'/api/v1/page-folders/{self.folder.id}/shares/{self.member_a.id}/', **auth_header(self.member_a),
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(FolderShare.objects.filter(folder=self.folder, user=self.member_a).exists())

    def test_revoking_access_nobody_has_is_reported_not_silently_accepted(self):
        response = self.client.delete(
            f'/api/v1/page-folders/{self.folder.id}/shares/{self.member_a.id}/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 404)

    def test_listing_marks_which_folders_the_caller_owns(self):
        self._share_with(self.member_a)
        response = self.client.get('/api/v1/page-folders/', **auth_header(self.member_a))
        folder = next(f for f in response.json()['data']['results'] if f['id'] == str(self.folder.id))
        self.assertFalse(folder['is_owner'])

        response = self.client.get('/api/v1/page-folders/', **auth_header(self.owner_a))
        folder = next(f for f in response.json()['data']['results'] if f['id'] == str(self.folder.id))
        self.assertTrue(folder['is_owner'])
