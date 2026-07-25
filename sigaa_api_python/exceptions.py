class SigaaException(Exception):
    """Base exception for Sigaa API."""
    pass
class SigaaSessionExpired(SigaaException):
    """Raised when the session is expired."""
    pass
class SigaaInvalidCredentials(SigaaException):
    """Raised when login fails due to invalid credentials."""
    pass
class SigaaConnectionError(SigaaException):
    """Raised when connection fails."""
    pass

class SigaaQuestionnaireError(SigaaException):
    """Raised when an unskippable questionnaire blocks access."""
    pass
