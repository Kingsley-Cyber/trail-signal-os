# Poison fixture for guard 10 — denylisted evasion dependency.
import rotating_proxies

def fetch(url):
    return rotating_proxies.get(url)
