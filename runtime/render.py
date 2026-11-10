"""Deterministic runtime rendering; private values are supplied only in memory."""
import json
import re

def require(value, message):
    if not value: raise ValueError(message)

def proxy_config(tenant, client, owner_oid, hostname, secret, cookie_secret):
    # CoerceClaim in the pinned OAuth2 Proxy supports a scalar OIDC claim as a group
    # list. Using oid makes authorization depend on an immutable tenant object ID.
    settings = {
        'provider': 'oidc',
        'oidc_issuer_url': f'https://login.microsoftonline.com/{tenant}/v2.0',
        'client_id': client,
        'client_secret': secret,
        'redirect_url': f'https://{hostname}/oauth2/callback',
        'scope': 'openid profile',
        'user_id_claim': 'oid',
        'oidc_email_claim': 'oid',
        'oidc_groups_claim': 'oid',
        'allowed_groups': [owner_oid],
        'email_domains': ['*'],
        'skip_claims_from_profile_url': True,
        'insecure_oidc_skip_nonce': False,
        'code_challenge_method': 'S256',
        'http_address': '127.0.0.1:4180',
        'upstreams': ['http://127.0.0.1:7681/'],
        'reverse_proxy': True,
        'pass_host_header': True,
        'proxy_websockets': True,
        'skip_provider_button': True,
        'cookie_name': '__Host-codex_terminal',
        'cookie_secret': cookie_secret,
        'cookie_secure': True,
        'cookie_httponly': True,
        'cookie_samesite': 'lax',
        'cookie_expire': '4h',
        'cookie_refresh': '0',
        'pass_access_token': False,
        'pass_authorization_header': False,
        'set_authorization_header': False,
        'pass_basic_auth': False,
        'pass_user_headers': False,
        'request_logging': False,
        'auth_logging': True,
        'standard_logging': True,
    }
    return '\n'.join(f'{key} = {json.dumps(value)}' for key, value in settings.items()) + '\n'


def compose(images, size):
    require(size in ['Standard_B1ms', 'Standard_B2als_v2'], 'Unsupported VM size.')
    small = size == 'Standard_B1ms'
    return {
        'name': 'codex-terminal',
        'services': {
            'terminal': {
                'image': images['terminal'], 'user': '1000:1000', 'restart': 'unless-stopped',
                'ports': ['127.0.0.1:7681:7681'],
                'command': ['node', '/opt/codex-session.mjs'],
                'stop_grace_period': '30s',
                'volumes': ['/srv/codex-terminal/workspace:/workspace',
                            '/srv/codex-terminal/codex-config:/home/node/.codex',
                            '/etc/codex-terminal/session-manager.mjs:/opt/codex-session.mjs:ro',
                            '/etc/codex-terminal/session-hooks.toml:/etc/codex/requirements.toml:ro'],
                'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true', 'seccomp=/etc/codex-terminal/seccomp.json',
                                 'apparmor=codex-terminal'],
                'mem_limit': '1280m' if small else '3g',
                'memswap_limit': '1280m' if small else '3g',
                'cpus': 0.9 if small else 1.8,
                'pids_limit': 512,
            },
            'auth': {
                'image': images['auth'], 'user': '65532:65532', 'restart': 'unless-stopped',
                'network_mode': 'host', 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
                'command': ['--config=/etc/oauth2-proxy.cfg'],
                'volumes': ['/etc/codex-terminal/oauth2-proxy.cfg:/etc/oauth2-proxy.cfg:ro'],
                'tmpfs': ['/tmp:rw,noexec,nosuid,size=16m,mode=1777'], 'mem_limit': '128m',
            },
            'https': {
                'image': images['https'], 'restart': 'unless-stopped', 'network_mode': 'host',
                'read_only': True, 'cap_drop': ['ALL'], 'cap_add': ['NET_BIND_SERVICE'],
                'security_opt': ['no-new-privileges:true'],
                'volumes': ['/etc/codex-terminal/Caddyfile:/etc/caddy/Caddyfile:ro',
                            '/srv/codex-terminal/caddy-data:/data', '/srv/codex-terminal/caddy-config:/config'],
                'mem_limit': '128m',
            },
        },
    }


def public_hostname(config, state):
    host = config.get('customDomain') or state['vm']['hostName']
    require(isinstance(host, str) and len(host) <= 253 and '.' in host and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                for part in host.split('.')), 'Supply a lowercase DNS hostname without a scheme, port or path.')
    return host


def caddy_config(host, azure_host, email):
    config = f'''{{
    admin off
    email {email}
    servers {{
        protocols h1 h2
    }}
}}
{host} {{
    tls {{
        protocols tls1.2 tls1.3
    }}
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:4180 {{
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {{host}}
        header_up -X-Auth-Request-User
        header_up -X-Auth-Request-Email
        header_up -X-Forwarded-User
    }}
}}
'''
    if azure_host != host:
        config += f'''{azure_host} {{
    tls {{
        protocols tls1.2 tls1.3
    }}
    redir https://{host}{{uri}} permanent
}}
'''
    return config

