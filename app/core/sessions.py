"""Cookie names shared by the session layer, the login routes and the CSRF middleware."""

SESSION_COOKIE_NAME = "envmanager_session"

# Holds the single-use token of a login that passed the password and awaits its second factor.
PENDING_LOGIN_COOKIE_NAME = "envmanager_pending_login"
