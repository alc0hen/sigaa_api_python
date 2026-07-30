from urllib.parse import urljoin
from .exceptions import SigaaConnectionError
from .course import Course
import re
import logging
logger = logging.getLogger(__name__)

class StudentBond:

    def __init__(self, session, registration, program, switch_url=None):
        self.session = session
        self.registration = registration
        self.program = program
        self.switch_url = switch_url
        self.courses = []

    async def get_courses(self):
        page = None
        if self.switch_url:
            page = await self.session.get(self.switch_url)
        else:
            page = await self.session.get('/sigaa/portais/discente/discente.jsf')
        self.courses = self._parse_courses(page)
        return self.courses

    def _parse_courses(self, page):
        courses = []
        try:
            tables = page.soup.find_all('table')
            for table in tables:
                headers = table.find_all('th')
                header_texts = [h.get_text(strip=True) for h in headers]
                is_course_table = any(('Componente' in h or 'Disciplina' in h for h in header_texts))
                title_idx = -1
                for i, h in enumerate(header_texts):
                    if 'Componente' in h or 'Disciplina' in h:
                        title_idx = i
                        break
                if not is_course_table:
                    first_row = table.find('tr')
                    if first_row:
                        row_text = first_row.get_text(strip=True)
                        if 'Componente' in row_text or 'Disciplina' in row_text:
                            is_course_table = True
                if not is_course_table:
                    continue
                tbody = table.find('tbody')
                rows = tbody.find_all('tr') if tbody else table.find_all('tr')
                for row in rows:
                    if 'periodo' in row.get('class', []):
                        continue
                    row_text_clean = row.get_text(strip=True)
                    if 'Componente Curricular' in row_text_clean or 'Disciplina' in row_text_clean:
                        continue
                    cells = row.find_all('td')
                    if not cells:
                        continue
                    name_cell = None
                    if title_idx != -1 and title_idx < len(cells):
                        name_cell = cells[title_idx]
                    if not name_cell:
                        for cell in cells:
                            if cell.find('span', class_='tituloDisciplina'):
                                name_cell = cell
                                break
                    if not name_cell and len(cells) > 1:
                        if title_idx == -1:
                            text1 = cells[1].get_text(strip=True)
                            if 'Campus' in text1 or 'Sala' in text1:
                                name_cell = cells[0]
                            else:
                                name_cell = cells[1]
                    if not name_cell:
                        continue
                    if name_cell.find('span', class_='tituloDisciplina'):
                        title = name_cell.find('span', class_='tituloDisciplina').get_text(strip=True)
                    else:
                        title = name_cell.get_text(strip=True)
                    access_link = row.find('a', onclick=True)
                    if not access_link:
                        for cell in cells:
                            link = cell.find('a', onclick=True)
                            if link and ('discente' in str(link.get('title', '')).lower() or 'acessar' in link.get_text(strip=True).lower()):
                                access_link = link
                                break
                    if access_link:
                        js_code = access_link['onclick']
                        try:
                            form_data = page.parse_jsfcljs(js_code)
                            schedule_code = ''
                            info_td = row.find('td', class_='info')
                            if info_td:
                                schedule_code = info_td.get_text(strip=True)
                            courses.append(Course(self.session, title, form_data, schedule_code=schedule_code))
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f'Error parsing courses: {e}')
        return courses
    MAX_CONCURRENT_SESSIONS = 5
    MAX_BATCH_SIZE = 4
    LOGIN_COST = 7
    SCRAPE_COST = 4

    def _compute_optimal_strategy(self, n_classes):
        import math
        S = self.MAX_CONCURRENT_SESSIONS
        if n_classes <= S:
            return (1, n_classes, 1, self.LOGIN_COST + self.SCRAPE_COST)
        best_b = 1
        best_time = float('inf')
        for b in range(1, min(n_classes, self.MAX_BATCH_SIZE) + 1):
            n_batches = math.ceil(n_classes / b)
            n_waves = math.ceil(n_batches / S)
            estimated = n_waves * (self.LOGIN_COST + b * self.SCRAPE_COST)
            if estimated < best_time:
                best_time = estimated
                best_b = b
        n_batches = math.ceil(n_classes / best_b)
        n_waves = math.ceil(n_batches / S)
        return (best_b, n_batches, n_waves, best_time)

    async def get_history(self, cached_history=None, credentials=None):
        try:
            logger.info('SIGAA: Starting get_history combining Official History and Turmas Anteriores...')
            page = None
            if self.switch_url:
                logger.info(f'SIGAA: Switching context via URL: {self.switch_url}')
                page = await self.session.get(self.switch_url)
            else:
                logger.info('SIGAA: Accessing discente.jsf to ensure session context.')
                page = await self.session.get('/sigaa/portais/discente/discente.jsf')
            official_history = {}
            action_data = self._extract_jscook_action_by_jsf_method(page, 'portalDiscente.boletim')
            if not action_data:
                action_data = self._extract_jscook_action_by_jsf_method(page, 'relatorioNotasAluno.gerarRelatorio')
            if not action_data:
                action_data = self._extract_jscook_action(page, 'Boletim')
            if not action_data:
                action_data = self._extract_jscook_action(page, 'Consultar Minhas Notas')
            if not action_data:
                action_data = self._extract_jscook_action_by_jsf_method(page, 'portalDiscente.historico')
            if not action_data:
                action_data = self._extract_jscook_action(page, 'Consultar Histórico Escolar')
            if not action_data:
                action_data = self._extract_jscook_action(page, 'Emitir Histórico')
            if action_data:
                logger.info(f"SIGAA: Found Official History action: {action_data['action_url']}")
                try:
                    history_page = await self.session.post(action_data['action_url'], data=action_data['post_values'])
                    official_history = self._parse_bulletin(history_page)
                    logger.info(f'SIGAA: Parsed Official History with {len(official_history)} semesters.')
                except Exception as e:
                    logger.error(f'SIGAA: Failed to fetch Official History: {e}')
            page = await self.session.get('/sigaa/portais/discente/discente.jsf')
            active_courses = self._parse_courses(page)
            active_course_titles = {c.title for c in active_courses}
            actual_current_semester = None
            if page and hasattr(page, 'body') and page.body:
                import re
                actual_semester_match = re.search('Semestre atual:\\s*<strong[^>]*>(\\d{4}\\.\\d)</strong>', page.body)
                if actual_semester_match:
                    actual_current_semester = actual_semester_match.group(1)
            turmas_page = await self.session.get('/sigaa/portais/discente/turmas.jsf')
            failed_official = not official_history
            detailed_history = await self._parse_previous_classes(turmas_page, cached_history, credentials, active_course_titles, actual_current_semester, fetch_grades=failed_official)
            for sem, subjects in detailed_history.items():
                if sem in official_history:
                    for subj in subjects:
                        for off_subj in official_history[sem]:
                            off_name = off_subj['name'].lower().strip()
                            det_name = subj['name'].lower().strip()
                            if off_name == det_name or off_name in det_name or det_name in off_name:
                                if off_subj.get('final_grade') is not None:
                                    subj['final_grade'] = off_subj['final_grade']
                                if off_subj.get('status'):
                                    subj['status'] = off_subj['status']
                                if off_subj.get('absences') is not None:
                                    subj['absences'] = off_subj['absences']
                                break
            final_history = detailed_history if detailed_history else official_history or {}
            overlap_sem = None
            if active_course_titles:
                for sem, courses in final_history.items():
                    overlap = [c for c in courses if c['name'].lower().strip() in active_course_titles]
                    has_final = any((any((st in (c.get('status') or '').lower() for st in ['aprovado', 'reprovado', 'concluído', 'cancelado', 'dispensado', 'trancado'])) for c in courses))
                    if overlap and (not has_final):
                        overlap_sem = sem
                        break
            if overlap_sem:
                del final_history[overlap_sem]
            return final_history
        except Exception as e:
            logger.error(f'Get history error: {e}', exc_info=True)
            return {}

    async def _parse_previous_classes(self, page, cached_history=None, credentials=None, active_course_titles=None, actual_current_semester=None, fetch_grades=False):
        history = {}
        classes_to_fetch = []
        try:
            tables = page.soup.find_all('table', class_='listagem')
            if not tables:
                logger.info("SIGAA: No 'listagem' tables found, trying 'tabelaRelatorio'.")
                tables = page.soup.find_all('table', class_='tabelaRelatorio')
            logger.info(f'SIGAA: Found {len(tables)} tables to parse for Turmas Anteriores.')
            latest_semester = None
            for table_idx, table in enumerate(tables):
                rows = table.find_all('tr')
                current_semester = 'Unknown'
                logger.info(f'SIGAA: Table {table_idx + 1} has {len(rows)} rows.')
                for row in rows:
                    text = row.get_text(strip=True)
                    if 'Ano' in text or 'Período' in text or len(row.find_all('td')) == 1:
                        sem_match = re.search('(\\d{4}\\.\\d)', text)
                        if sem_match:
                            current_semester = sem_match.group(1)
                            if latest_semester is None:
                                latest_semester = current_semester
                            logger.info(f'SIGAA: Detected semester grouping: {current_semester}')
                            continue
                    avancar_img = row.find('img', src=re.compile('avancar\\.gif'))
                    if not avancar_img:
                        continue
                    link = avancar_img.find_parent('a')
                    if not link or not link.get('onclick'):
                        continue
                    js_code = link['onclick']
                    try:
                        form_data = page.parse_jsfcljs(js_code)
                    except Exception as e:
                        logger.warning(f'SIGAA: Failed to parse jsfcljs for class link: {e}')
                        continue
                    cells = row.find_all('td')
                    title = 'Desconhecido'
                    schedule_code = ''
                    row_status = None
                    cell_texts = [c.get_text(strip=True) for c in cells]
                    for i, t in enumerate(cell_texts):
                        if '-' in t and len(t) > 5 and (not t.replace('.', '').isdigit()):
                            if title == 'Desconhecido':
                                title = t
                        t_upper = t.upper()
                        if 'APROVADO' in t_upper or 'REPROVADO' in t_upper or 'TRANCADO' in t_upper or ('MATRICULADO' in t_upper) or ('DISPENSADO' in t_upper) or ('CANCELADO' in t_upper) or ('CONCLUÍDO' in t_upper) or ('CONCLUIDO' in t_upper):
                            row_status = t.title()
                    logger.info(f"SIGAA: Row '{title}' [{current_semester}] → cells={cell_texts} | status={row_status!r}")
                    if actual_current_semester and current_semester == actual_current_semester:
                        logger.info(f"SIGAA: Skipping '{title}' — it belongs to the actual current semester ({actual_current_semester}).")
                        continue
                    CURRENT_STATUSES = {'matriculado', 'cursando', 'em andamento', 'em curso', 'em progresso', 'ativo'}
                    if row_status and row_status.strip().lower() in CURRENT_STATUSES:
                        logger.info(f"SIGAA: Skipping '{title}' ({row_status!r}) — detected as current-semester, not history.")
                        continue
                    if row_status is None:
                        row_status = 'Concluído'
                    can_reuse = False
                    if cached_history and current_semester in cached_history:
                        for c_subj in cached_history[current_semester]:
                            if c_subj.get('name') == title:
                                if row_status not in ['Matriculado', 'Cursando', 'Indefinido']:
                                    if current_semester not in history:
                                        history[current_semester] = []
                                    history[current_semester].append(c_subj)
                                    can_reuse = True
                                    break
                    if can_reuse:
                        logger.info(f"SIGAA: Reusing cached details for '{title}' in {current_semester}.")
                        continue
                    classes_to_fetch.append({'title': title, 'js_code': js_code, 'schedule_code': schedule_code, 'row_status': row_status, 'semester': current_semester})
            if classes_to_fetch:
                if credentials:
                    import asyncio
                    n = len(classes_to_fetch)
                    batch_size, n_batches, n_waves, est_time = self._compute_optimal_strategy(n)
                    batches = [classes_to_fetch[i:i + batch_size] for i in range(0, n, batch_size)]
                    logger.info(f'SIGAA: Strategy computed → {n} classes, batch_size={batch_size}, batches={n_batches}, waves={n_waves}, est_time≈{est_time}s')
                    semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_SESSIONS)

                    async def bounded_fetch_batch(batch):
                        async with semaphore:
                            return await self._fetch_batch_parallel(credentials, batch)
                    tasks = [bounded_fetch_batch(b) for b in batches]
                    batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                    for batch, result in zip(batches, batch_results):
                        if isinstance(result, Exception):
                            logger.error(f'SIGAA: Batch fetch failed: {result}')
                            for c_info in batch:
                                sem = c_info['semester']
                                if sem not in history:
                                    history[sem] = []
                                history[sem].append({'name': c_info['title'], 'final_grade': 0.0, 'absences': 0, 'status': c_info['row_status'], 'grades': [], 'professor': 'Desconhecido'})
                        else:
                            for c_info, subj_result in result:
                                sem = c_info['semester']
                                if sem not in history:
                                    history[sem] = []
                                if isinstance(subj_result, Exception):
                                    logger.error(f"SIGAA: Parallel fetch failed for '{c_info['title']}': {subj_result}")
                                    history[sem].append({'name': c_info['title'], 'final_grade': 0.0, 'absences': 0, 'status': c_info['row_status'], 'grades': [], 'professor': 'Desconhecido'})
                                else:
                                    history[sem].append(subj_result)
                else:
                    logger.warning('SIGAA: No credentials provided, falling back to sequential fetch.')
                    for c_info in classes_to_fetch:
                        sem = c_info['semester']
                        title = c_info['title']
                        try:
                            form_data = page.parse_jsfcljs(c_info['js_code'])
                            subj = await self._process_course_sync(title, form_data, c_info['schedule_code'], c_info['row_status'])
                            if sem not in history:
                                history[sem] = []
                            history[sem].append(subj)
                        except Exception as e:
                            logger.error(f"SIGAA: Sequential fetch failed for '{title}': {e}")
                        import asyncio
                        await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f'Parse previous classes error: {e}')
        return history

    async def _fetch_batch_parallel(self, credentials, batch, fetch_grades=False):
        from .sigaa import Sigaa
        username = credentials['username']
        password = credentials['password']
        url = credentials['url']
        inst_type = credentials['inst_type']
        titles = [c['title'] for c in batch]
        sigaa = Sigaa(url, inst_type)
        results = []
        try:
            logger.info(f'Worker: Logging in for batch {titles}...')
            await sigaa.login(username, password)
            if self.switch_url:
                await sigaa.session.get(self.switch_url)
            for idx, class_info in enumerate(batch):
                try:
                    turmas_page = await sigaa.session.get('/sigaa/portais/discente/turmas.jsf')
                    target_js_code = None
                    tables = turmas_page.soup.find_all('table', class_=['listagem', 'tabelaRelatorio'])
                    for table in tables:
                        rows = table.find_all('tr')
                        current_sem = 'Unknown'
                        for row in rows:
                            text = row.get_text(strip=True)
                            sem_match = re.search('(\\d{4}\\.\\d)', text)
                            if sem_match:
                                current_sem = sem_match.group(1)
                                continue
                            if current_sem != class_info['semester']:
                                continue
                            cells = row.find_all('td')
                            row_title = 'Desconhecido'
                            for cell in cells:
                                t = cell.get_text(strip=True)
                                if '-' in t and len(t) > 5 and (not t.replace('.', '').isdigit()):
                                    row_title = t
                                    break
                            if row_title == class_info['title']:
                                avancar_img = row.find('img', src=re.compile('avancar\\.gif'))
                                if avancar_img:
                                    link = avancar_img.find_parent('a')
                                    if link and link.get('onclick'):
                                        target_js_code = link['onclick']
                                break
                        if target_js_code:
                            break
                    if not target_js_code:
                        raise ValueError(f"Class '{class_info['title']}' not found in worker session.")
                    form_data = turmas_page.parse_jsfcljs(target_js_code)
                    subj = await self._process_course_sync(class_info['title'], form_data, class_info['schedule_code'], class_info['row_status'], sigaa_session=sigaa.session, fetch_grades=fetch_grades)
                    results.append((class_info, subj))
                    if idx < len(batch) - 1:
                        logger.info(f'Worker: Re-navigating for next class in batch (done {idx + 1}/{len(batch)}).')
                except Exception as e:
                    logger.error(f"Worker: Failed to fetch '{class_info['title']}' in batch: {e}")
                    results.append((class_info, e))
        finally:
            await sigaa.close()
        return results

    async def _process_course_sync(self, title, form_data, schedule_code, row_status, sigaa_session=None, fetch_grades=False):
        session = sigaa_session or self.session
        course = Course(session, title, form_data, schedule_code)
        if not fetch_grades:
            grades, frequency, professor = await course.get_professor_only()
            logger.info(f"SIGAA: Fetched professor for '{title}'.")
            return {'name': title, 'final_grade': None, 'absences': None, 'status': row_status, 'grades': [], 'professor': professor}
        else:
            grades, frequency, professor = await course.get_all_details()
            logger.info(f"SIGAA: Fetched all details for '{title}' (fallback mode).")
            final_grade = None
            for g in grades:
                if g['type'] == 'single' and any((n in g['name'].lower() for n in ['média', 'nota final', 'resultado'])):
                    final_grade = g['value']
                elif g['type'] == 'group':
                    for sg in g['grades']:
                        if 'média' in sg['name'].lower() or 'final' in sg['name'].lower():
                            final_grade = sg['value']
            if final_grade is None:
                valid_vals = []
                for g in grades:
                    if g['type'] == 'single':
                        if g['value'] is not None:
                            valid_vals.append(g['value'])
                    elif g['type'] == 'group':
                        for sg in g['grades']:
                            if sg['value'] is not None:
                                valid_vals.append(sg['value'])
                if valid_vals:
                    final_grade = round(sum(valid_vals) / len(valid_vals), 1)
                else:
                    final_grade = 0.0
            absences = frequency.get('total_faltas', 0) if frequency else 0
            detailed_grades = []
            for g in grades:
                if g['type'] == 'single':
                    detailed_grades.append({'name': g['name'], 'value': g['value']})
                elif g['type'] == 'group':
                    for sg in g['grades']:
                        detailed_grades.append({'name': sg['name'], 'value': sg['value']})
            return {'name': title, 'final_grade': final_grade, 'absences': absences, 'status': row_status, 'grades': detailed_grades, 'professor': professor}

    def _extract_jscook_action(self, page, label):
        try:
            form = page.soup.find('form', id=re.compile('menu:form_menu_discente|menuForm'))
            if not form:
                form = page.soup.find('input', attrs={'name': 'jscook_action'})
                if form:
                    form = form.find_parent('form')
            if not form:
                return None
            post_values = {}
            for inp in form.find_all('input'):
                if inp.get('name'):
                    post_values[inp.get('name')] = inp.get('value', '')
            scripts = page.soup.find_all('script')
            action = None
            for s in scripts:
                if s.string and (f"'{label}'" in s.string or f'"{label}"' in s.string):
                    matches = re.findall(f"""['\\"]{label}['\\"]\\s*,\\s*['\\"]([^'\\"]+)['\\"]""", s.string)
                    if matches:
                        action = matches[0]
                        break
            if action:
                post_values['jscook_action'] = action
                url = form.get('action')
                return {'action_url': urljoin(str(page.url), url), 'post_values': post_values}
        except:
            pass
        return None

    def _extract_jscook_action_by_jsf_method(self, page, jsf_method):
        import re
        try:
            form = page.soup.find('form', id=re.compile('menu:form_menu_discente|menuForm'))
            if not form:
                form = page.soup.find('input', attrs={'name': 'jscook_action'})
                if form:
                    form = form.find_parent('form')
            if not form:
                return None
            post_values = {}
            for inp in form.find_all('input'):
                if inp.get('name'):
                    post_values[inp.get('name')] = inp.get('value', '')
            scripts = page.soup.find_all('script')
            action = None
            for s in scripts:
                if s.string and jsf_method in s.string:
                    match = re.search('[\'\\"]([^\'\\"]*' + re.escape(jsf_method) + '[^\'\\"]*)[\'\\"]', s.string)
                    if match:
                        action = match.group(1)
                        break
            if action:
                post_values['jscook_action'] = action
                url = form.get('action')
                from urllib.parse import urljoin
                return {'action_url': urljoin(str(page.url), url), 'post_values': post_values}
        except Exception as e:
            logger.error(f'Error extracting by jsf_method {jsf_method}: {e}')
        return None

    def _parse_bulletin(self, page):
        history = {}
        try:
            tables = page.soup.find_all('table', class_='tabelaRelatorio')
            for table in tables:
                caption = table.find('caption')
                semester = caption.get_text(strip=True) if caption else 'Unknown'
                subjects = []
                rows = table.find_all('tr')
                headers_row = table.find('th').parent if table.find('th') else None
                if not headers_row:
                    continue
                raw_headers = [th.get_text(strip=True) for th in headers_row.find_all('th')]
                headers = [h.lower() for h in raw_headers]
                idx_name = -1
                idx_final_grade = -1
                idx_status = -1
                idx_absences = -1
                for i, h in enumerate(headers):
                    h_lower = h.lower()
                    if 'componente' in h_lower or 'disciplina' in h_lower:
                        idx_name = i
                    elif 'situação' in h_lower or 'status' in h_lower:
                        idx_status = i
                    elif 'faltas' in h_lower:
                        idx_absences = i
                    elif 'resultado' in h_lower or 'média' in h_lower or 'nota' in h_lower:
                        idx_final_grade = i
                grade_indices = []
                for i, h in enumerate(headers):
                    if i == idx_name or i == idx_status or i == idx_absences:
                        continue
                    if i == idx_final_grade and ('resultado' in h.lower() or 'média' in h.lower() or 'nota final' in h.lower()):
                        continue
                    h_lower = h.lower()
                    if h_lower in ['créditos', 'ch', 'turma', 'tipo', 'código', 'ano', 'período']:
                        continue
                    grade_indices.append((i, raw_headers[i]))
                if idx_name == -1:
                    continue
                for row in rows:
                    if 'class' in row.attrs and ('agrupador' in row['class'] or 'titulo' in row['class']):
                        continue
                    cells = row.find_all('td')
                    if not cells:
                        continue
                    try:
                        if idx_name >= len(cells):
                            continue
                        name = cells[idx_name].get_text(strip=True)
                        status = cells[idx_status].get_text(strip=True) if idx_status != -1 and idx_status < len(cells) else ''
                        final_grade = None
                        if idx_final_grade != -1 and idx_final_grade < len(cells):
                            txt = cells[idx_final_grade].get_text(strip=True).replace(',', '.')
                            if txt and txt != '-' and (txt != '--'):
                                try:
                                    final_grade = float(txt)
                                except:
                                    pass
                        absences = 0
                        if idx_absences != -1 and idx_absences < len(cells):
                            txt = cells[idx_absences].get_text(strip=True)
                            try:
                                absences = int(txt)
                            except:
                                pass
                        detailed_grades = []
                        for idx, label in grade_indices:
                            if idx < len(cells):
                                val_txt = cells[idx].get_text(strip=True).replace(',', '.')
                                if val_txt and val_txt != '-' and (val_txt != '--'):
                                    try:
                                        val = float(val_txt)
                                        detailed_grades.append({'name': label, 'value': val})
                                    except:
                                        pass
                        subjects.append({'name': name, 'final_grade': final_grade, 'absences': absences, 'status': status, 'grades': detailed_grades})
                    except:
                        continue
                if subjects:
                    history[semester] = subjects
        except Exception as e:
            logger.error(f'Parse bulletin error: {e}')
        return history

    async def get_enrollment_disciplines(self):
        from .enrollment_parser import parse_enrollment_page
        if self.switch_url:
            page = await self.session.get(self.switch_url)
        else:
            page = await self.session.get('/sigaa/portais/discente/discente.jsf')
        action, form_id = self._extract_enrollment_action(page)
        if not action:
            raise ValueError('SIGAA: Realizar Matrícula menu action not found.')
        post_values = {form_id: form_id, 'jscook_action': action}
        if page.view_state:
            post_values['javax.faces.ViewState'] = page.view_state
        form_el = page.soup.find('form', id=form_id)
        action_url = '/sigaa/portais/discente/discente.jsf'
        if form_el and form_el.get('action'):
            action_url = urljoin(str(page.url), form_el.get('action'))
        instrucoes_page = await self.session.post(action_url, data=post_values)
        form_el = instrucoes_page.soup.find('form', id='form')
        if not form_el:
            form_el = instrucoes_page.soup.find('form')
        if not form_el:
            raise ValueError('SIGAA: Instructions form not found.')
        form_id = form_el.get('id', 'form')
        post_values = {form_id: form_id}
        for inp in form_el.find_all('input'):
            name = inp.get('name')
            val = inp.get('value', '')
            itype = inp.get('type')
            if not name:
                continue
            if itype == 'submit':
                continue
            if itype == 'checkbox':
                post_values[name] = 'on'
                continue
            post_values[name] = val
        btn = form_el.find('input', id=re.compile('btnIniciarSolicit'))
        if btn:
            btn_name = btn.get('name', 'form:btnIniciarSolicit')
            post_values[btn_name] = btn.get('value', 'Iniciar seleção de turmas')
        else:
            post_values[f'{form_id}:btnIniciarSolicit'] = 'Iniciar seleção de turmas'
        if 'javax.faces.ViewState' not in post_values and instrucoes_page.view_state:
            post_values['javax.faces.ViewState'] = instrucoes_page.view_state
        action = form_el.get('action', '/sigaa/graduacao/matricula/instrucoes/instrucoes_regular.jsf')
        action_url = urljoin(str(instrucoes_page.url), action)
        selecao_page = await self.session.post(action_url, data=post_values)
        levels = parse_enrollment_page(selecao_page.body)
        return {'levels': levels, 'view_state': selecao_page.view_state, 'action_url': urljoin(str(selecao_page.url), '/sigaa/graduacao/matricula/turmas_curriculo.jsf')}

    async def submit_enrollment(self, selected_class_ids, view_state, action_url=None):
        if not action_url:
            action_url = '/sigaa/graduacao/matricula/turmas_curriculo.jsf'
        data = [('formSelecionarTurmas', 'formSelecionarTurmas'), ('formSelecionarTurmas:btaoSelecionarTurmas', 'formSelecionarTurmas:btaoSelecionarTurmas'), ('javax.faces.ViewState', view_state)]
        for cid in selected_class_ids:
            data.append(('selecaoTurmas', str(cid)))
        response_page = await self.session.post(action_url, data=data)
        return response_page

    async def request_confirmation_page(self, view_state, action_url=None):
        if not action_url:
            action_url = '/sigaa/graduacao/matricula/turmas_selecionadas.jsf'
        data = [('formBotoesSuperiores', 'formBotoesSuperiores'), ('formBotoesSuperiores:linkSubmissao', 'formBotoesSuperiores:linkSubmissao'), ('javax.faces.ViewState', view_state)]
        response_page = await self.session.post(action_url, data=data)
        return response_page

    async def confirm_enrollment(self, password, view_state, confirmation_page_html):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(confirmation_page_html, 'lxml')
        form = soup.find('form', id=re.compile('form|confirm'))
        if not form:
            form = soup.find('form')
        if not form:
            raise ValueError('SIGAA: Confirmation form not found.')
        form_id = form.get('id', 'form')
        post_values = {form_id: form_id}
        for inp in form.find_all('input'):
            name = inp.get('name')
            val = inp.get('value', '')
            itype = inp.get('type')
            if not name:
                continue
            if itype == 'submit':
                continue
            post_values[name] = val
        pwd_field = form.find('input', type='password')
        if pwd_field:
            post_values[pwd_field.get('name')] = password
        btn = form.find('input', type='submit')
        if btn:
            post_values[btn.get('name')] = btn.get('value', '')
        else:
            btn_confirm = form.find(id=re.compile('confirmar|gravar|enviar'))
            if btn_confirm and btn_confirm.get('name'):
                post_values[btn_confirm.get('name')] = btn_confirm.get('value', '')
        if 'javax.faces.ViewState' not in post_values and view_state:
            post_values['javax.faces.ViewState'] = view_state
        action = form.get('action')
        action_url = urljoin(str(self.session.base_url), action) if action else str(self.session.base_url)
        final_page = await self.session.post(action_url, data=post_values)
        return final_page

    def _extract_enrollment_action(self, page):
        scripts = page.soup.find_all('script')
        for s in scripts:
            if s.string and 'matriculaGraduacao.telaInstrucoes' in s.string:
                match = re.search('[\'\\"]([^\'\\"]*matriculaGraduacao\\.telaInstrucoes[^\'\\"]*)[\'\\"]', s.string)
                if match:
                    action = match.group(1)
                    form_match = re.search('menu:form_menu_discente|menuForm', s.string)
                    form_id = form_match.group(0) if form_match else 'menu:form_menu_discente'
                    return (action, form_id)
        return (None, None)

    def __repr__(self):
        return f"<StudentBond registration='{self.registration}' program='{self.program}'>"

class TeacherBond:

    def __repr__(self):
        return '<TeacherBond>'