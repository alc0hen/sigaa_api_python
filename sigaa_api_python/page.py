import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .enums import HTTPMethod
from .exceptions import SigaaSessionExpired

class SigaaPage:

    def __init__(self, url, body, headers, method, status_code, request_headers=None):
        self.url = url
        self.body = body
        self.headers = headers
        self.method = method
        self.status_code = status_code
        self.request_headers = request_headers or {}
        self._soup = None
        self._view_state = None
        self.check_session_expired()

    @property
    def soup(self):
        if self._soup is None:
            self._soup = BeautifulSoup(self.body, 'lxml')
        return self._soup

    @property
    def view_state(self):
        if self._view_state is None:
            input_el = self.soup.find('input', attrs={'name': 'javax.faces.ViewState'})
            if input_el:
                self._view_state = input_el.get('value')
        return self._view_state

    def check_session_expired(self):
        if self.status_code == 302:
            location = self.headers.get('location') or self.headers.get('Location')
            if location and '/sigaa/expirada.jsp' in location:
                raise SigaaSessionExpired('SIGAA: Session expired.')
        if '/sigaa/expirada.jsp' in str(self.url):
            raise SigaaSessionExpired('SIGAA: Session expired.')

    def parse_jsfcljs(self, javascript_code):
        if 'getElementById' not in javascript_code:
            raise ValueError('SIGAA: Form not found in JS code.')
        form_query = re.search("document\\.getElementById\\('([^']+)'\\)", javascript_code)
        if not form_query:
            raise ValueError('SIGAA: Form without id in JS code.')
        form_id = form_query.group(1)
        form_el = self.soup.find(id=form_id)
        if not form_el:
            raise ValueError(f'SIGAA: Form with id {form_id} not found in page.')
        form_action = form_el.get('action')
        if not form_action:
            raise ValueError('SIGAA: Form without action.')
        action = urljoin(str(self.url), form_action)
        post_values = {}
        for input_el in form_el.find_all('input'):
            if input_el.get('type') == 'submit':
                continue
            name = input_el.get('name')
            value = input_el.get('value')
            if name is not None:
                post_values[name] = value
        match = re.search(',\\s*\\{(.*?)\\}\\s*,', javascript_code)
        if match:
            json_str = '{' + match.group(1) + '}'
            py_str = json_str.replace('true', 'True').replace('false', 'False').replace('null', 'None')
            try:
                import ast
                extra_values = ast.literal_eval(py_str)
                if isinstance(extra_values, dict):
                    post_values.update(extra_values)
            except (ValueError, SyntaxError):
                pass
        return {'action': action, 'post_values': post_values}