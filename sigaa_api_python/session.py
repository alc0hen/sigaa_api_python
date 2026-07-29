import os
import aiohttp
import asyncio
from .enums import HTTPMethod
from .page import SigaaPage
from .exceptions import SigaaConnectionError, SigaaQuestionnaireError
from urllib.parse import urljoin, urlparse


def _origin(url) -> tuple:
    parsed = urlparse(str(url))
    return (parsed.scheme.lower(), parsed.netloc.lower())


def _same_origin(url, base_origin: tuple) -> bool:
    scheme, netloc = _origin(url)
    if netloc != base_origin[1]:
        return False
    if base_origin[0] == 'https' and scheme != 'https':
        return False
    return True


class SigaaSession:
    def __init__(self, url, cookies=None):
        self.base_url = url
        self._session = None
        self.headers = {
            'User-Agent': 'SIGAA-Api/1.0',
            'Accept-Encoding': 'br, gzip, deflate',
            'Accept': '*/*',
            'Cache-Control': 'max-age=0',
            'DNT': '1'
        }
        self._initial_cookies = cookies
        self.last_url = None

    async def _get_session(self):
        if self._session is None:
            cookie_jar = aiohttp.CookieJar()
            if self._initial_cookies:
                cookie_jar.update_cookies(self._initial_cookies)

            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                cookie_jar=cookie_jar,
                timeout=timeout
            )
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def request(self, method, path, data=None, json=None, retry_count=0, redirect_count=0, **kwargs):
        session = await self._get_session()

        # Enforce manual redirect handling for security check
        kwargs['allow_redirects'] = False

        proxy_url = os.environ.get('SIGAA_PROXY')
        if proxy_url:
            kwargs['proxy'] = proxy_url

        current_method = method
        current_path = path
        current_data = data
        current_json = json
        current_redirect_count = redirect_count

        while True:
            if current_path.startswith('http'):
                url = current_path
            else:
                url = urljoin(self.base_url, current_path)

            base_origin = _origin(self.base_url)
            base_netloc = base_origin[1]

            if not _same_origin(url, base_origin):
                req_netloc = urlparse(url).netloc
                raise ValueError(
                    f"Security Alert: Potential SSRF attempt blocked. Request to {req_netloc} not allowed.")

            # Construct request-specific headers with Referer
            req_headers = self.headers.copy()
            if self.last_url:
                req_headers['Referer'] = self.last_url
            if 'headers' in kwargs:
                req_headers.update(kwargs['headers'])

            # Pass req_headers to kwargs
            request_kwargs = kwargs.copy()
            request_kwargs['headers'] = req_headers

            try:
                async with session.request(current_method, url, data=current_data, json=current_json,
                                           **request_kwargs) as response:
                    # Handle Redirects Manually
                    if response.status in (301, 302, 303, 307, 308):
                        if current_redirect_count >= 10:
                            raise SigaaConnectionError("Too many redirects")

                        location = response.headers.get('Location')
                        if not location:
                            # Treat as final response if location is missing
                            body = await response.text()
                            self.last_url = str(response.url)
                            page = SigaaPage(
                                url=response.url,
                                body=body,
                                headers=dict(response.headers),
                                method=current_method,
                                status_code=response.status,
                                request_headers=dict(response.request_info.headers)
                            )
                            break

                        new_url = urljoin(str(response.url), location)

                        # Validate Redirect Target Domain
                        if not _same_origin(new_url, base_origin):
                            new_netloc = urlparse(new_url).netloc
                            raise ValueError(
                                f"Security Alert: External redirect blocked. Redirect to {new_netloc} not allowed.")

                        # Determine method for next request
                        next_method = current_method
                        if response.status in (301, 302, 303):
                            next_method = HTTPMethod.GET.value
                            current_data = None
                            current_json = None

                        self.last_url = str(response.url)
                        current_method = next_method
                        current_path = new_url
                        current_redirect_count += 1
                        continue

                    # Process Final Response
                    if not _same_origin(response.url, base_origin):
                        final_netloc = urlparse(str(response.url)).netloc
                        raise ValueError(
                            f"Security Alert: External redirect blocked. Redirect to {final_netloc} not allowed.")

                    body = await response.text()
                    self.last_url = str(response.url)
                    page = SigaaPage(
                        url=response.url,
                        body=body,
                        headers=dict(response.headers),
                        method=current_method,
                        status_code=response.status,
                        request_headers=dict(response.request_info.headers)
                    )

                    if page.soup.find(id='btnNaoResponderContinuarSigaa'):
                        if retry_count >= 3:
                            return page
                        await self._handle_questionnaire(page)
                        # Retry the ORIGINAL request (resetting redirect count)
                        return await self.request(method, path, data=data, json=json, retry_count=retry_count + 1,
                                                  **kwargs)

                    return page

            except aiohttp.ServerDisconnectedError as e:
                if retry_count < 3:
                    await asyncio.sleep(1.5)
                    return await self.request(method, path, data=data, json=json, retry_count=retry_count + 1,
                                              redirect_count=redirect_count, **kwargs)
                raise SigaaConnectionError(f"Connection error: {e}")
            except aiohttp.ClientError as e:
                raise SigaaConnectionError(f"Connection error: {e}")

    async def _handle_questionnaire(self, page):
        """
        Submits the form to skip the questionnaire.
        """
        skip_button = page.soup.find(id='btnNaoResponderContinuarSigaa')
        if not skip_button:
            return
        form = skip_button.find_parent('form')
        if not form:
            return
        action = form.get('action')
        form_id = form.get('id')
        if not action or not form_id:
            return

        action_url = urljoin(str(page.url), action)

        if not _same_origin(action_url, _origin(self.base_url)):
            return

        view_state = page.view_state
        post_values = {
            form_id: form_id,
            'btnNaoResponderContinuarSigaa': 'btnNaoResponderContinuarSigaa'
        }

        if view_state:
            post_values['javax.faces.ViewState'] = view_state

        session = await self._get_session()
        async with session.post(action_url, data=post_values, allow_redirects=False) as resp:
            await resp.text()

    async def get(self, path, **kwargs):
        return await self.request(HTTPMethod.GET.value, path, **kwargs)

    async def post(self, path, data=None, **kwargs):
        return await self.request(HTTPMethod.POST.value, path, data=data, **kwargs)

    async def follow_all_redirects(self, page):
        return page
