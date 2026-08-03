from urllib.parse import urljoin
from .exceptions import SigaaInvalidCredentials

def parse_continue_form(page):
    btn_continuar = page.soup.find('input', attrs={'value': lambda v: v and 'Continuar' in v and ('>>' in v)})
    if not btn_continuar:
        return None
    form = btn_continuar.find_parent('form')
    if not form or not form.get('action'):
        return None
    submit_url = urljoin(str(page.url), form.get('action'))
    submit_data = {}
    for input_el in form.find_all('input'):
        name = input_el.get('name')
        if name:
            if input_el.get('type') in ['submit', 'button'] and input_el != btn_continuar:
                continue
            submit_data[name] = input_el.get('value', '')
    if btn_continuar.get('name'):
        submit_data[btn_continuar.get('name')] = btn_continuar.get('value')
    return (submit_url, submit_data)

class SigaaLogin:

    def __init__(self, session):
        self.session = session
        self.login_status = False

    async def login(self, username, password):
        raise NotImplementedError

class SigaaLoginImpl(SigaaLogin):

    def __init__(self, session):
        super().__init__(session)

    async def get_login_form(self):
        page = await self.session.get('/sigaa/verTelaLogin.do')
        return self._parse_login_form(page)

    def _parse_login_form(self, page):
        form = page.soup.find('form', attrs={'name': 'loginForm'})
        if not form:
            raise ValueError('SIGAA: No login form found.')
        action = form.get('action')
        if not action:
            raise ValueError('SIGAA: No action in login form.')
        full_action_url = urljoin(str(page.url), action)
        post_values = {}
        for input_el in form.find_all('input'):
            name = input_el.get('name')
            value = input_el.get('value')
            if name:
                post_values[name] = value if value is not None else ''
        return (full_action_url, post_values)

    async def login(self, username, password):
        action_url, post_values = await self.get_login_form()
        post_values['user.login'] = username
        post_values['user.senha'] = password
        page = await self.session.post(action_url, data=post_values)
        max_skips = 3
        while max_skips > 0:
            if '/sigaa/questionarios.jsf' in str(page.url):
                page = await self.session.get('/sigaa/verPortalDiscente.do')
                max_skips -= 1
                continue
            continue_form = parse_continue_form(page)
            if continue_form:
                submit_url, submit_data = continue_form
                page = await self.session.post(submit_url, data=submit_data)
                max_skips -= 1
                continue
            break
        if 'Questionários de Avaliação' in page.soup.text or '/sigaa/questionarios.jsf' in str(page.url):
            from .exceptions import SigaaQuestionnaireError
            raise SigaaQuestionnaireError('Acesso bloqueado por Questionário de Avaliação obrigatório no SIGAA.')
        if 'Entrar no Sistema' in page.body or 'Usuário e/ou senha inválidos' in page.body:
            if 'Usuário e/ou senha inválidos' in page.body:
                raise SigaaInvalidCredentials('SIGAA: Invalid credentials.')
            raise ValueError('SIGAA: Invalid response after login attempt (Check credentials or system status).')
        self.login_status = True
        return page