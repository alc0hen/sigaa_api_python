from .session import SigaaSession
from .login import SigaaLoginImpl
from .account import Account
from .enums import InstitutionType

class Sigaa:

    def __init__(self, url, institution=InstitutionType.IFAL, cookies=None):
        self.url = url
        self.institution = institution
        self.session = SigaaSession(url, cookies=cookies)
        if institution in [InstitutionType.IFSC, InstitutionType.IFAL, InstitutionType.UFAL, InstitutionType.UFPE]:
            self.login_controller = SigaaLoginImpl(self.session)
        else:
            raise NotImplementedError(f'Institution {institution} not implemented yet.')

    async def login(self, username, password):
        page = await self.login_controller.login(username, password)
        return Account(self.session, page)

    async def close(self):
        await self.session.close()