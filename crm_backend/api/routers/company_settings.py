"""Company-wide settings an Owner controls.

Distinct from /company/default-config, which seeds departments and task types:
this router carries settings that change what the company *permits*, and there
is one of them so far. `allow_public_projects` decides whether a project may be
given `public` visibility, which puts it outside the tenant boundary and
readable by any authenticated user.

Read is open to any active member because the project form needs to know
whether `public` is on the menu. Write is Owner-only -- narrower than every
other admin endpoint here, and deliberately so: see
company.services.update_company_settings.
"""

from company import services
from ninja import Router, Schema
from utils.api_response import api_response as payload

from ..auth import JWTBearerAuth
from ..schemas import ApiResponse

router = Router(tags=['company-settings'])
auth = JWTBearerAuth()


class CompanySettingsIn(Schema):
    """Every field optional: a PATCH that names one setting must not reset the
    others to their defaults. `model_dump(exclude_unset=True)` in the endpoint
    is what makes "not sent" different from "sent as false"."""

    allow_public_projects: bool | None = None


def _settings_data(company) -> dict:
    return {'allow_public_projects': company.allow_public_projects}


@router.get('/', auth=auth, response={200: ApiResponse, 404: ApiResponse})
async def get_company_settings(request):
    company, error = await services.get_company_settings(request.auth)
    if error == 'not_found':
        return payload('You do not belong to a company.', 404, False)
    return payload('Company settings retrieved successfully.', 200, True, {'settings': _settings_data(company)})


@router.patch('/', auth=auth, response={200: ApiResponse, 403: ApiResponse})
async def update_company_settings(request, data: CompanySettingsIn):
    updates = data.model_dump(exclude_unset=True)
    company, error = await services.update_company_settings(request.auth, updates)
    if error == 'forbidden':
        return payload(
            'Only the company Owner can change company settings. Public visibility exposes a project '
            'outside the company, so switching it on is the Owner\'s decision.', 403, False,
        )
    return payload('Company settings updated successfully.', 200, True, {'settings': _settings_data(company)})
