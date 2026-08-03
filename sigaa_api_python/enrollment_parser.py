import re
from .lexsoup import LexSoup

def parse_confirmation_result(html):
    soup = LexSoup(html)
    res_body_lower = html.lower()
    if soup.find('input', type='password') or 'senha incorreta' in res_body_lower or 'senha de confirmação inválida' in res_body_lower or ('inválida' in res_body_lower):
        error_elements = soup.find_all(class_='erros')
        if error_elements:
            msg = '; '.join([err.get_text(strip=True) for err in error_elements])
        else:
            msg = 'Senha incorreta ou erro de confirmação no SIGAA.'
        return (False, msg)
    return (True, None)

def parse_enrollment_page(html_content):
    soup = LexSoup(html_content)
    table = soup.find('table', id='lista-turmas-curriculo')
    if not table:
        table = soup.find('table', class_='listagem')
    if not table:
        return []
    levels = []
    current_level = None
    current_discipline = None
    tbody_rows = []
    for tb in table.find_all('tbody'):
        tbody_rows.extend(tb.find_all('tr'))
    rows = tbody_rows if tbody_rows else table.find_all('tr')
    for row in rows:
        row_classes = row.get('class', [])
        if 'periodo' in row_classes:
            level_text = row.get_text(strip=True)
            level_text = re.sub('\\s+', ' ', level_text).strip()
            current_level = {'level': level_text, 'disciplines': []}
            levels.append(current_level)
            current_discipline = None
            continue
        if 'disciplina' in row_classes:
            link = row.find('a', onclick=True)
            if not link:
                continue
            onclick_text = link['onclick']
            comp_match = re.search('PainelComponente\\.show\\((\\d+)', onclick_text)
            component_id = int(comp_match.group(1)) if comp_match else None
            equiv_link = row.find('a', class_='linkExpressoes')
            if not equiv_link:
                equiv_link = row.find('a', string=re.compile('equivalente'))
            equiv_onclick = equiv_link['onclick'] if equiv_link and equiv_link.has_attr('onclick') else None
            disp_text = link.get_text(strip=True)
            match = re.search('^\\s*\\*?\\s*([A-Z0-9]+)\\s*-\\s*(.+)$', disp_text)
            if match:
                code = match.group(1)
                name = match.group(2).strip()
            else:
                code = ''
                name = disp_text.strip()
            current_discipline = {'code': code, 'name': name, 'component_id': component_id, 'equiv_onclick': equiv_onclick, 'classes': []}
            if current_level is not None:
                current_level['disciplines'].append(current_discipline)
            else:
                if not levels:
                    current_level = {'level': 'Geral', 'disciplines': []}
                    levels.append(current_level)
                current_level['disciplines'].append(current_discipline)
            continue
        checkbox = row.find('input', attrs={'name': 'selecaoTurmas'})
        if checkbox and current_discipline is not None:
            class_id = checkbox.get('value')
            chk_id = checkbox.get('id')
            labels = row.find_all('label', attrs={'for': chk_id})
            class_code = ''
            teacher = ''
            description = ''
            schedule = ''
            location = ''
            if len(labels) >= 1:
                class_code = labels[0].get_text(strip=True)
            if len(labels) >= 2:
                detail_label = labels[1]
                strong_tag = detail_label.find('strong')
                description = strong_tag.get_text(strip=True) if strong_tag else ''
                label_copy = LexSoup(str(detail_label))
                if label_copy.find('strong'):
                    label_copy.find('strong').decompose()
                teacher = label_copy.get_text(strip=True).lstrip(' -').strip()
            if len(labels) >= 3:
                schedule = labels[2].get_text(strip=True)
            if len(labels) >= 4:
                location = labels[3].get_text(strip=True)
            current_discipline['classes'].append({'class_id': class_id, 'class_code': class_code, 'teacher': teacher, 'description': description, 'schedule': schedule, 'location': location})
    return levels