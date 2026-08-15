"""Authentication adapters for Django Ninja."""

from ninja.security import HttpBearer
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class JWTBearerAuth(HttpBearer):
    """Authenticate a Ninja route with the existing SimpleJWT access tokens."""

    def authenticate(self, request, token):
        try:
            jwt_auth = JWTAuthentication()
            validated_token = jwt_auth.get_validated_token(token)
            return jwt_auth.get_user(validated_token)
        except (InvalidToken, TokenError):
            return None
