import asyncio
import logging
import socket
import ssl
import aiohttp
import config
logger = logging.getLogger(__name__)
CF_SITEKEY = '0x4AAAAAAAJel0iaAR3mgkjp'
ACTION_URLS = {'signup': 'https://dash.cloudflare.com/sign-up', 'login': 'https://dash.cloudflare.com/login', 'onboarding': 'https://dash.cloudflare.com/profile/api-tokens'}
async def _solve_capsolver(website_url: str, website_key: str, action: str, session: aiohttp.ClientSession) -> str:
    """Решить Turnstile через capsolver.com API."""
    api_key = config.CAPTCHA_API_KEY
    base = 'https://api.capsolver.com'
    from cloudflare_api import CHROME_UA
    create_payload = {'clientKey': api_key, 'task': {'type': 'AntiTurnstileTaskProxyLess', 'websiteURL': website_url, 'websiteKey': website_key, 'metadata': {'action': action}, 'userAgent': CHROME_UA}}
    async with session.post(f'{base}/createTask', json=create_payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await r.json()
    if data.get('errorId', 0) != 0:
        raise RuntimeError(f"Capsolver createTask error: {data.get('errorDescription')}")
    task_id = data['taskId']
    logger.debug(f'Capsolver taskId={task_id}')
    get_payload = {'clientKey': api_key, 'taskId': task_id}
    for attempt in range(30):
        await asyncio.sleep(3 if attempt == 0 else 2)
        async with session.post(f'{base}/getTaskResult', json=get_payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
            result = await r.json()
        status = result.get('status')
        if status == 'ready':
            token = result['solution']['token']
            logger.debug(f'Capsolver token: {token[:30]}...')
            return token
        elif status in ('processing', 'idle'):
            continue
        else:
            raise RuntimeError(f'Capsolver ошибка: {result}')
    raise TimeoutError('Capsolver: не решил за 60 сек')
async def _solve_2captcha(website_url: str, website_key: str, action: str, session: aiohttp.ClientSession) -> str:
    """Решить Turnstile через 2captcha.com API."""
    api_key = config.CAPTCHA_API_KEY
    base = 'https://api.2captcha.com'
    from cloudflare_api import CHROME_UA
    create_payload = {'clientKey': api_key, 'task': {'type': 'TurnstileTaskProxyless', 'websiteURL': website_url, 'websiteKey': website_key, 'action': action, 'userAgent': CHROME_UA}}
    async with session.post(f'{base}/createTask', json=create_payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await r.json()
    if data.get('errorId', 0) != 0:
        raise RuntimeError(f"2captcha createTask error: {data.get('errorDescription')}")
    task_id = data['taskId']
    logger.debug(f'2captcha taskId={task_id}')
    get_payload = {'clientKey': api_key, 'taskId': task_id}
    for _ in range(30):
        await asyncio.sleep(4)
        async with session.post(f'{base}/getTaskResult', json=get_payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
            result = await r.json()
        status = result.get('status')
        if status == 'ready':
            token = result['solution']['token']
            logger.debug(f'2captcha token: {token[:30]}...')
            return token
        elif status in ('processing', 'idle'):
            continue
        else:
            raise RuntimeError(f'2captcha ошибка: {result}')
    raise TimeoutError('2captcha: не решил за 120 сек')
async def solve_turnstile(action: str, proxy: str | None=None) -> str:
 
    if not config.CAPTCHA_API_KEY:
        raise RuntimeError('CAPTCHA_API_KEY не задан в config.py! Зарегись на capsolver.com или 2captcha.com и вставь ключ.')
    website_url = ACTION_URLS.get(action, ACTION_URLS['onboarding'])
    service = config.CAPTCHA_SERVICE.lower()
    for attempt in range(1, 4):
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                if service == 'capsolver':
                    return await _solve_capsolver(website_url, CF_SITEKEY, action, session)
                elif service == '2captcha':
                    return await _solve_2captcha(website_url, CF_SITEKEY, action, session)
                else:
                    raise ValueError(f'Неизвестный сервис капчи: {service}')
        except Exception as e:
            logger.warning(f'⚠️ Попытка {attempt}/3 решения Turnstile ({action}) завершилась ошибкой: {e}')
            if attempt == 3:
                raise e
            await asyncio.sleep(2)
async def solve_cf_challenge(url: str, proxy: str) -> dict:

    if not config.CAPTCHA_API_KEY:
        raise RuntimeError('CAPTCHA_API_KEY не задан!')
    base = 'https://api.capsolver.com'
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        from urllib.parse import urlparse
        import urllib.request
        proxy_ip = proxy
        proxy_to_parse = proxy if '://' in proxy else f'http://{proxy}'
        parsed = urlparse(proxy_to_parse)
        try:
            ip = socket.gethostbyname(parsed.hostname)
            auth = ''
            if parsed.username:
                auth = f'{parsed.username}:{parsed.password}@' if parsed.password else f'{parsed.username}@'
            scheme = parsed.scheme or 'http'
            proxy_ip = f'{scheme}://{auth}{ip}:{parsed.port or 80}'
        except Exception as e:
            logger.warning(f'Failed to resolve proxy hostname: {e}')
        payload = {'clientKey': config.CAPTCHA_API_KEY, 'task': {'type': 'AntiCloudflareTask', 'websiteURL': url, 'proxy': proxy_ip}}
        async with session.post(f'{base}/createTask', json=payload, timeout=30) as r:
            data = await r.json()
        if data.get('errorId', 0) != 0:
            raise RuntimeError(f"Capsolver AntiCF error: {data.get('errorDescription')}")
        task_id = data['taskId']
        logger.debug(f'Capsolver AntiCF taskId={task_id}')
        for attempt in range(40):
            await asyncio.sleep(4)
            async with session.post(f'{base}/getTaskResult', json={'clientKey': config.CAPTCHA_API_KEY, 'taskId': task_id}, timeout=30) as r:
                result = await r.json()
            status = result.get('status')
            if status == 'ready':
                token = result['solution']['token']
                ua = result['solution']['userAgent']
                logger.debug(f'Capsolver AntiCF ready!')
                return {'cf_clearance': token, 'user_agent': ua}
            elif status in ('processing', 'idle'):
                continue
            else:
                raise RuntimeError(f'Capsolver AntiCF error: {result}')
        raise TimeoutError('Capsolver AntiCF: timeout')