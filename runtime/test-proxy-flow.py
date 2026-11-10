"""Exercise the exact pinned OAuth2 Proxy binary against the local disposable issuer."""
import http.cookiejar
import urllib.error
import urllib.request

for name in ['owner', 'other-user', 'wrong-issuer', 'wrong-audience', 'wrong-nonce', 'bad-signature']:
    urllib.request.urlopen('http://127.0.0.1:19000/case?name=' + name).close()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        response = opener.open('http://127.0.0.1:14181/upstream', timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    reached = 'OWNER_UPSTREAM_REACHED' in response.read().decode()
    assert reached == (name == 'owner'), f'{name}: unexpected upstream authorization (HTTP {response.code})'
    print(f'{name}: ' + ('allowed' if reached else 'denied'))
print('PROXY_IDENTITY_SIGNATURE_ISSUER_AUDIENCE_NONCE_TESTS_PASSED')
