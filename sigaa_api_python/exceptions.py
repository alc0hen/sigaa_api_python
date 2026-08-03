class SigaaException(Exception):
    pass

class SigaaSessionExpired(SigaaException):
    pass

class SigaaInvalidCredentials(SigaaException):
    pass

class SigaaConnectionError(SigaaException):
    pass

class SigaaQuestionnaireError(SigaaException):
    pass