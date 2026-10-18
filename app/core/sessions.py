"""Cookie names shared by the session layer, the login routes and the CSRF middleware."""

SESSION_COOKIE_NAME = "envmanager_session"

# Holds the single-use token of a login that passed the password and awaits its second factor.
PENDING_LOGIN_COOKIE_NAME = "envmanager_pending_login"

# Holds the sealed, not yet confirmed secret of a second factor setup, for the account screens.
TOTP_SETUP_COOKIE_NAME = "envmanager_totp_setup"
TOTP_SETUP_COOKIE_PATH = "/account/totp"
