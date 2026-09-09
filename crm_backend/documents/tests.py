"""One document model, four scopes, one access rule.

The rule worth being most careful about is `personal`: §7 says no
administrative override, ever, "including Owner-over-CM or CM-over-Owner".
That is the one rule here with no exceptions, so it gets tested from both
directions rather than once.
"""

import json
from datetime import timedelta

from api.tests import TwoCompanyTestCase, auth_header
from asgiref.sync import async_to_sync
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from pages.models import FolderShare, PageFolder
from projects_and_tasks.models import Project, ProjectMembership
from users.models import CompanyUserProfile, User

from documents import services
from documents.models import Document, DocumentShare
from documents.services import purge_expired_documents, validate_upload

PASSWORD = 'Kx9#mQ2vLp8Z'


def a_file(name='notes.pdf', content=b'%PDF-1.4 hello', content_type='application/pdf'):
    return SimpleUploadedFile(name, content, content_type=content_type)


class DocumentWorldMixin:
    def setUp(self):
        super().setUp()
        self.cm = self._member('cm', CompanyUserProfile.Role.COMPANY_MANAGER)
        self.other_member = self._member('other')
        now = timezone.now()
        self.project = Project.objects.create(
            title='Website Revamp', company=self.company_a, department=self.department_a,
            visibility=Project.VISIBILITY.COMPANY, start_date=now, deadline=now + timedelta(days=90),
            created_by=self.owner_a, current_owner=self.owner_a,
        )

    def _member(self, name, role=CompanyUserProfile.Role.DEPARTMENT_MEMBER):
        user = User.objects.create_user(email=f'{name}@example.com', username=name, password=PASSWORD)
        CompanyUserProfile.objects.create(
            user=user, company=self.company_a, department=self.department_a, role=role,
        )
        return user

    def make_document(self, *, scope, owner=None, project=None, folder=None, uploaded_by=None, size=1024):
        return Document.objects.create(
            company=self.company_a, scope=scope, owner=owner, project=project, folder=folder,
            file=a_file(), name='notes.pdf', content_type='application/pdf', size=size,
            uploaded_by=uploaded_by or owner or self.owner_a,
        )

    def access(self, user, document):
        return async_to_sync(services.resolve_document_access)(user, document)


class PersonalDocumentsHaveNoOverrideTests(DocumentWorldMixin, TwoCompanyTestCase):
    """The strict scope. §7 is emphatic and it is the rule most likely to be
    eroded by a well-meaning "but the Owner should be able to see everything"."""

    def setUp(self):
        super().setUp()
        self.mine = self.make_document(scope=Document.Scope.PERSONAL, owner=self.other_member)

    def test_the_owner_of_the_document_manages_it(self):
        self.assertEqual(self.access(self.other_member, self.mine), 'manage')

    def test_the_company_owner_cannot_read_it(self):
        self.assertIsNone(self.access(self.owner_a, self.mine))

    def test_a_company_manager_cannot_read_it(self):
        self.assertIsNone(self.access(self.cm, self.mine))

    def test_a_company_manager_cannot_read_the_owners_personal_document(self):
        """The other direction, which §7 calls out separately -- CM-over-Owner
        is refused exactly as Owner-over-CM is."""
        owners = self.make_document(scope=Document.Scope.PERSONAL, owner=self.owner_a)
        self.assertIsNone(self.access(self.cm, owners))

    def test_another_company_cannot_read_it(self):
        self.assertIsNone(self.access(self.owner_b, self.mine))

    def test_an_explicit_share_grants_read_and_not_manage(self):
        DocumentShare.objects.create(document=self.mine, user=self.cm, shared_by=self.other_member)
        self.assertEqual(self.access(self.cm, self.mine), 'read')

    def test_a_shared_recipient_cannot_delete_it(self):
        DocumentShare.objects.create(document=self.mine, user=self.cm, shared_by=self.other_member)
        ok, error = async_to_sync(services.delete_document)(self.cm, self.mine)
        self.assertEqual(error, 'forbidden')
        self.assertFalse(ok)

    def test_a_shared_recipient_cannot_share_it_onward(self):
        DocumentShare.objects.create(document=self.mine, user=self.cm, shared_by=self.other_member)
        response = self.client.post(
            f'/api/v1/documents/{self.mine.id}/share/', json.dumps({'user_id': str(self.owner_a.id)}),
            content_type='application/json', **auth_header(self.cm),
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_it_is_not_found_rather_than_forbidden(self):
        """A personal document must not confirm its own existence to somebody
        outside it."""
        response = self.client.get(f'/api/v1/documents/{self.mine.id}/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 404, response.content)


class ProjectDocumentsTests(DocumentWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.document = self.make_document(
            scope=Document.Scope.PROJECT, project=self.project, uploaded_by=self.other_member,
        )

    def test_project_view_reads(self):
        self.assertEqual(self.access(self.member_a, self.document), 'read')

    def test_project_manage_manages(self):
        self.assertEqual(self.access(self.owner_a, self.document), 'manage')

    def test_the_uploader_can_delete_their_own(self):
        """The person who filed it can take it back, even without MANAGE."""
        self.assertEqual(self.access(self.other_member, self.document), 'manage')

    def test_another_company_gets_nothing(self):
        self.assertIsNone(self.access(self.owner_b, self.document))

    def test_a_private_project_hides_its_documents(self):
        self.project.visibility = Project.VISIBILITY.PRIVATE
        self.project.save(update_fields=['visibility'])
        self.assertIsNone(self.access(self.member_a, self.document))

    def test_contribute_is_required_to_upload(self):
        """VIEW reads but does not add. A viewer putting files on a project is
        the same shape of defect as a viewer adding tasks to it."""
        document, error = async_to_sync(services.create_document)(
            self.member_a, self.company_a, a_file(), scope=Document.Scope.PROJECT, project=self.project,
        )
        self.assertEqual(error, 'forbidden')

    def test_a_contributor_can_upload(self):
        ProjectMembership.objects.create(
            project=self.project, user=self.member_a,
            role=ProjectMembership.Role.CONTRIBUTOR, added_by=self.owner_a,
        )
        document, error = async_to_sync(services.create_document)(
            self.member_a, self.company_a, a_file(), scope=Document.Scope.PROJECT, project=self.project,
        )
        self.assertIsNone(error)
        self.assertEqual(document.scope, Document.Scope.PROJECT)


class CompanyDocumentsTests(DocumentWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.document = self.make_document(scope=Document.Scope.COMPANY)

    def test_any_active_member_reads(self):
        self.assertEqual(self.access(self.member_a, self.document), 'read')

    def test_owner_and_company_manager_manage(self):
        self.assertEqual(self.access(self.owner_a, self.document), 'manage')
        self.assertEqual(self.access(self.cm, self.document), 'manage')

    def test_a_department_member_cannot_manage(self):
        self.assertEqual(self.access(self.member_a, self.document), 'read')

    def test_another_company_gets_nothing(self):
        self.assertIsNone(self.access(self.owner_b, self.document))

    def test_a_deactivated_member_gets_nothing(self):
        profile = CompanyUserProfile.objects.get(user=self.member_a, company=self.company_a)
        profile.is_active = False
        profile.save(update_fields=['is_active'])
        self.assertIsNone(self.access(self.member_a, self.document))

    def test_only_owner_or_cm_may_upload(self):
        _, error = async_to_sync(services.create_document)(
            self.member_a, self.company_a, a_file(), scope=Document.Scope.COMPANY,
        )
        self.assertEqual(error, 'forbidden')
        _, error = async_to_sync(services.create_document)(
            self.cm, self.company_a, a_file(), scope=Document.Scope.COMPANY,
        )
        self.assertIsNone(error)


class FolderDocumentsTests(DocumentWorldMixin, TwoCompanyTestCase):
    """The Info Portal rule, unchanged: creator plus FolderShare recipients."""

    def setUp(self):
        super().setUp()
        self.folder = PageFolder.objects.create(
            company=self.company_a, name='Handbook', created_by=self.other_member,
        )
        self.document = self.make_document(
            scope=Document.Scope.FOLDER, folder=self.folder, uploaded_by=self.other_member,
        )

    def test_the_folder_creator_manages(self):
        self.assertEqual(self.access(self.other_member, self.document), 'manage')

    def test_a_non_recipient_gets_nothing_even_as_company_owner(self):
        self.assertIsNone(self.access(self.owner_a, self.document))

    def test_a_share_recipient_reads(self):
        FolderShare.objects.create(folder=self.folder, user=self.member_a)
        self.assertEqual(self.access(self.member_a, self.document), 'read')

    def test_a_deleted_folder_hides_its_documents(self):
        self.folder.is_deleted = True
        self.folder.save(update_fields=['is_deleted'])
        self.assertIsNone(self.access(self.other_member, self.document))


class UploadValidationTests(DocumentWorldMixin, TwoCompanyTestCase):
    """§7 asks for content-type and size validation on every upload path. One
    validator serves them all, with per-path limits passed in."""

    def test_an_empty_file_is_refused(self):
        empty = SimpleUploadedFile('empty.pdf', b'', content_type='application/pdf')
        self.assertEqual(validate_upload(empty), 'empty_file')

    def test_an_oversized_file_is_refused(self):
        big = SimpleUploadedFile('big.pdf', b'x' * (services.MAX_DOCUMENT_SIZE_BYTES + 1),
                                 content_type='application/pdf')
        self.assertEqual(validate_upload(big), 'too_large')

    def test_a_disallowed_type_is_refused(self):
        script = SimpleUploadedFile('run.sh', b'#!/bin/sh', content_type='application/x-sh')
        self.assertEqual(validate_upload(script), 'invalid_content_type')

    def test_a_good_file_passes(self):
        self.assertIsNone(validate_upload(a_file()))

    def test_the_limits_are_arguments_not_fixed(self):
        """A résumé and a profile picture have their own smaller caps. The
        point of the shared validator is one implementation, not one policy."""
        image = SimpleUploadedFile('photo.png', b'x' * 2048, content_type='image/png')
        self.assertEqual(validate_upload(image, max_bytes=1024), 'too_large')
        self.assertEqual(
            validate_upload(image, allowed_types={'application/pdf'}), 'invalid_content_type',
        )
        self.assertIsNone(validate_upload(image, max_bytes=4096, allowed_types={'image/png'}))


class RetentionTests(DocumentWorldMixin, TwoCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.document = self.make_document(scope=Document.Scope.PERSONAL, owner=self.other_member)

    def test_deleting_is_soft_and_starts_the_clock(self):
        async_to_sync(services.delete_document)(self.other_member, self.document)
        self.document.refresh_from_db()
        self.assertTrue(self.document.is_deleted)
        self.assertIsNotNone(self.document.deleted_at)

    def test_a_deleted_document_can_be_restored_inside_the_window(self):
        """Without this, the retention window is only a delay before permanent
        loss, which helps nobody."""
        async_to_sync(services.delete_document)(self.other_member, self.document)
        ok, error = async_to_sync(services.restore_document)(self.other_member, self.document)
        self.assertIsNone(error)
        self.document.refresh_from_db()
        self.assertFalse(self.document.is_deleted)
        self.assertIsNone(self.document.deleted_at)

    def test_the_purge_leaves_documents_inside_the_window_alone(self):
        async_to_sync(services.delete_document)(self.other_member, self.document)
        self.assertEqual(purge_expired_documents(), 0)
        self.assertTrue(Document.objects.filter(id=self.document.id).exists())

    def test_the_purge_removes_documents_past_the_window(self):
        async_to_sync(services.delete_document)(self.other_member, self.document)
        Document.objects.filter(id=self.document.id).update(
            deleted_at=timezone.now() - timedelta(days=services.RETENTION_DAYS + 1),
        )
        self.assertEqual(purge_expired_documents(), 1)
        self.assertFalse(Document.objects.filter(id=self.document.id).exists())

    def test_the_purge_never_touches_live_documents(self):
        live = self.make_document(scope=Document.Scope.COMPANY)
        purge_expired_documents()
        self.assertTrue(Document.objects.filter(id=live.id).exists())


class StorageAccountingTests(DocumentWorldMixin, TwoCompanyTestCase):
    def test_it_sums_live_documents(self):
        self.make_document(scope=Document.Scope.COMPANY, size=1000)
        self.make_document(scope=Document.Scope.COMPANY, size=2500)
        self.assertEqual(async_to_sync(services.company_storage_bytes)(self.company_a), 3500)

    def test_a_deleted_document_stops_counting(self):
        """Recoverable, but a company should not be charged for something they
        have already deleted."""
        document = self.make_document(scope=Document.Scope.COMPANY, size=1000)
        async_to_sync(services.delete_document)(self.owner_a, document)
        self.assertEqual(async_to_sync(services.company_storage_bytes)(self.company_a), 0)

    def test_it_is_scoped_to_one_company(self):
        self.make_document(scope=Document.Scope.COMPANY, size=1000)
        Document.objects.create(
            company=self.company_b, scope=Document.Scope.COMPANY, file=a_file(),
            name='theirs.pdf', size=9999, uploaded_by=self.owner_b,
        )
        self.assertEqual(async_to_sync(services.company_storage_bytes)(self.company_a), 1000)

    def test_the_endpoint_reports_it(self):
        self.make_document(scope=Document.Scope.COMPANY, size=4096)
        response = self.client.get('/api/v1/documents/storage/', **auth_header(self.owner_a))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['used_bytes'], 4096)


class DocumentListingTests(DocumentWorldMixin, TwoCompanyTestCase):
    """The listing filters in the query, not after it -- a listing that fetches
    everything and drops what the caller may not see is one forgotten call away
    from being a leak."""

    def setUp(self):
        super().setUp()
        self.mine = self.make_document(scope=Document.Scope.PERSONAL, owner=self.other_member)
        self.theirs = self.make_document(scope=Document.Scope.PERSONAL, owner=self.member_a)
        self.company_doc = self.make_document(scope=Document.Scope.COMPANY)

    def listed(self, user):
        response = self.client.get('/api/v1/documents/', **auth_header(user))
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()['data']['results']}

    def test_a_member_sees_their_own_and_company_documents(self):
        listed = self.listed(self.other_member)
        self.assertIn(str(self.mine.id), listed)
        self.assertIn(str(self.company_doc.id), listed)

    def test_a_member_never_sees_someone_elses_personal_document(self):
        self.assertNotIn(str(self.theirs.id), self.listed(self.other_member))

    def test_not_even_the_company_owner(self):
        listed = self.listed(self.owner_a)
        self.assertNotIn(str(self.mine.id), listed)
        self.assertNotIn(str(self.theirs.id), listed)

    def test_a_shared_personal_document_appears(self):
        DocumentShare.objects.create(document=self.theirs, user=self.other_member, shared_by=self.member_a)
        self.assertIn(str(self.theirs.id), self.listed(self.other_member))


class ProjectDocumentEndpointTests(DocumentWorldMixin, TwoCompanyTestCase):
    """The project-scoped URLs are unchanged; only what backs them moved."""

    def upload(self, actor):
        return self.client.post(
            f'/api/v1/projects/{self.project.id}/documents/',
            {'file': a_file(), 'label': 'Spec'}, **auth_header(actor),
        )

    def test_a_manager_can_upload_and_list(self):
        response = self.upload(self.owner_a)
        self.assertEqual(response.status_code, 201, response.content)

        listing = self.client.get(
            f'/api/v1/projects/{self.project.id}/documents/', **auth_header(self.owner_a),
        )
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(len(listing.json()['data']['results']), 1)

    def test_downloading_still_works(self):
        document_id = self.upload(self.owner_a).json()['data']['document']['id']
        response = self.client.get(
            f'/api/v1/documents/{document_id}/download/', **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 200)

    def test_another_company_cannot_download(self):
        document_id = self.upload(self.owner_a).json()['data']['document']['id']
        response = self.client.get(
            f'/api/v1/documents/{document_id}/download/', **auth_header(self.owner_b),
        )
        self.assertEqual(response.status_code, 404)

    def test_an_oversized_upload_is_refused(self):
        big = SimpleUploadedFile(
            'big.pdf', b'x' * (services.MAX_DOCUMENT_SIZE_BYTES + 1), content_type='application/pdf',
        )
        response = self.client.post(
            f'/api/v1/projects/{self.project.id}/documents/', {'file': big}, **auth_header(self.owner_a),
        )
        self.assertEqual(response.status_code, 400, response.content)
