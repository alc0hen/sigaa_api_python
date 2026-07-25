import re
from urllib.parse import urljoin
from .bond import StudentBond, TeacherBond
from .exceptions import SigaaConnectionError
class Account:
    def __init__(self, session, homepage):
        self.session = session
        self.homepage = homepage
        self._name = None
        self._emails = None
        self.active_bonds = []
        self.inactive_bonds = []
        self._parse_homepage(homepage)
    def _parse_homepage(self, homepage):
        if 'Questionários de Avaliação' in homepage.soup.text or '/sigaa/questionarios.jsf' in str(homepage.url) or 'Question&#225;rios de Avalia&#231;&#227;o' in homepage.body:
            from .exceptions import SigaaQuestionnaireError
            raise SigaaQuestionnaireError("Acesso bloqueado por Questionário de Avaliação obrigatório no SIGAA.")

        if 'O sistema comportou-se de forma inesperada' in homepage.body:
             raise ValueError('SIGAA: Invalid homepage, system error.')
        url_str = str(homepage.url)
        if '/portais/discente/discente.jsf' in url_str:
            self._parse_student_homepage(homepage)
        elif '/sigaa/vinculos.jsf' in url_str or '/sigaa/escolhaVinculo.do' in url_str:
            self._parse_bond_page(homepage)
    def _parse_bond_page(self, page):
        rows = page.soup.select('table.subFormulario tbody tr')
        for row in rows:
            cells = row.find_all('td')
            if not cells:
                continue
            type_cell = row.find(id='tdTipo')
            bond_type = type_cell.get_text(strip=True) if type_cell else ""
            if len(cells) < 4:
                continue
            status = cells[3].get_text(strip=True)
            bond = None
            if 'Discente' in bond_type:
                registration = cells[2].get_text(strip=True)
                program = cells[4].get_text(strip=True).replace('Curso: ', '')
                link = row.find('a', href=True)
                switch_url = None
                if link:
                    switch_url = urljoin(str(page.url), link['href'])
                bond = StudentBond(self.session, registration, program, switch_url)
            elif 'Docente' in bond_type:
                bond = TeacherBond()
            if bond:
                if status == 'Sim':
                    self.active_bonds.append(bond)
                elif status == 'Não':
                    self.inactive_bonds.append(bond)
    def _parse_student_homepage(self, page):
        profile_div = page.soup.find(id='perfil-docente')
        if not profile_div:
            return
        table = profile_div.find('table')
        if not table:
            return
        rows = table.find_all('tr')
        registration = None
        program = None
        status = None
        for row in rows:
            cells = row.find_all('td')
            if len(cells) != 2:
                continue
            key = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if 'Matrícula:' in key:
                registration = value
            elif 'Curso:' in key:
                program = re.sub(r' - [MTN]$', '', value)
            elif 'Status:' in key:
                status = value
        if registration and program:
            bond = StudentBond(self.session, registration, program, None)
            if status in ['CURSANDO', 'CONCLUINTE', 'ATIVO']:
                self.active_bonds.append(bond)
            else:
                self.inactive_bonds.append(bond)
    async def get_name(self):
        if self._name:
            return self._name
        if '/portais/discente/discente.jsf' in str(self.homepage.url):
             name_el = self.homepage.soup.select_one('p.usuario > span')
             if name_el:
                 self._name = name_el.get_text(strip=True)
                 return self._name
        page = await self.session.get('/sigaa/portais/discente/discente.jsf')
        name_el = page.soup.select_one('p.usuario > span')
        if name_el:
            self._name = name_el.get_text(strip=True)
            return self._name
        return None
