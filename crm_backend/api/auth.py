"""Authentication adapters for Django Ninja."""

from ninja.security import HttpBearer
from asgiref.sync import sync_to_async
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class JWTBearerAuth(HttpBearer):
    """Authenticate a Ninja route with the existing SimpleJWT access tokens."""

    async def authenticate(self, request, token):
        try:
            jwt_auth = JWTAuthentication()
            validated_token = jwt_auth.get_validated_token(token)
            return await sync_to_async(jwt_auth.get_user, thread_sensitive=True)(validated_token)
        except (InvalidToken, TokenError):
            return None
