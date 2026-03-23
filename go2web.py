#!/usr/bin/env python3
"""
go2web - HTTP over TCP Sockets
A command-line program that makes HTTP requests and displays human-readable responses.
"""

import socket
import ssl
import sys
import argparse
import json
import html.parser
import hashlib
import pickle
import re
import urllib.parse
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, urljoin
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# HTTP Cache configuration
CACHE_DIR = Path.home() / '.go2web_cache'
CACHE_DIR.mkdir(exist_ok=True)

# In-memory cache for the current session
_memory_cache: Dict[str, Tuple[str, Dict, str]] = {}


def decode_chunked(body: str) -> str:
    """Decode HTTP chunked transfer encoding."""
    result = []
    remaining = body

    while remaining:
        crlf_pos = remaining.find('\r\n')
        if crlf_pos == -1:
            result.append(remaining)
            break

        size_line = remaining[:crlf_pos].strip().split(';')[0].strip()

        if not size_line:
            remaining = remaining[crlf_pos + 2:]
            continue

        try:
            chunk_size = int(size_line, 16)
        except ValueError:
            return body  # Not chunked, return as-is

        if chunk_size == 0:
            break

        remaining = remaining[crlf_pos + 2:]
        chunk_data = remaining[:chunk_size]
        result.append(chunk_data)

        remaining = remaining[chunk_size:]
        if remaining.startswith('\r\n'):
            remaining = remaining[2:]

    return ''.join(result)


def decode_entities(text: str) -> str:
    """Decode common HTML entities."""
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")
    text = text.replace("&ndash;", "–").replace("&mdash;", "—").replace("&rsquo;", "'")
    text = re.sub(r'&#x([0-9A-Fa-f]+);', lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text


class HTTPClient:
    """Low-level HTTP client using TCP sockets (no urllib/requests)."""

    def __init__(self, enable_cache=True, enable_redirects=True):
        self.enable_cache = enable_cache
        self.enable_redirects = enable_redirects
        self.max_redirects = 5
        self.redirect_count = 0

    def _get_cache_path(self, url: str) -> Path:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return CACHE_DIR / f"{url_hash}.cache"

    def _load_cache(self, url: str) -> Optional[Tuple[str, Dict, str]]:
        if not self.enable_cache:
            return None
        # Check memory cache first
        if url in _memory_cache:
            return _memory_cache[url]
        # Then disk cache
        cache_path = self._get_cache_path(url)
        if cache_path.exists():
            try:
                with open(cache_path, 'rb') as f:
                    return pickle.load(f)
            except Exception:
                return None
        return None

    def _save_cache(self, url: str, status_line: str, headers: Dict, body: str):
        if not self.enable_cache:
            return
        entry = (status_line, headers, body)
        _memory_cache[url] = entry
        cache_path = self._get_cache_path(url)
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump(entry, f)
        except Exception:
            pass

    def request(self, method: str, url: str, headers: Optional[Dict] = None) -> Tuple[int, Dict, str]:
        """
        Make an HTTP request using raw TCP sockets.
        Returns: (status_code, headers_dict, body)
        """
        cached = self._load_cache(url)
        if cached:
            status_line, resp_headers, body = cached
            status_code = int(status_line.split()[1])
            return status_code, resp_headers, body

        parsed = urlparse(url)
        host = parsed.netloc
        path = parsed.path or '/'
        if parsed.query:
            path += f'?{parsed.query}'

        if headers is None:
            headers = {}

        default_headers = {
            'Host': host,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'identity',  # No compression — we handle raw text only
            'Connection': 'close',
        }
        default_headers.update(headers)

        request_line = f"{method} {path} HTTP/1.1\r\n"
        header_str = "\r\n".join(f"{k}: {v}" for k, v in default_headers.items())
        http_request = f"{request_line}{header_str}\r\n\r\n"

        port = 443 if parsed.scheme == 'https' else 80

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)

            if parsed.scheme == 'https':
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(sock, server_hostname=host)

            sock.connect((host, port))
            sock.sendall(http_request.encode())

            response = b''
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                except socket.timeout:
                    break
            sock.close()

            response_text = response.decode('utf-8', errors='ignore')
            parts = response_text.split('\r\n\r\n', 1)
            headers_text = parts[0]
            body = parts[1] if len(parts) > 1 else ''

            header_lines_list = headers_text.split('\r\n')
            status_line = header_lines_list[0]
            status_code = int(status_line.split()[1])

            response_headers = {}
            for line in header_lines_list[1:]:
                if ':' in line:
                    key, value = line.split(':', 1)
                    response_headers[key.strip().lower()] = value.strip()

            # Decode chunked encoding — check header OR body pattern
            transfer_enc = response_headers.get('transfer-encoding', '').lower()
            if 'chunked' in transfer_enc or re.match(r'^[0-9a-fA-F]+\r\n', body):
                body = decode_chunked(body)

            # Handle redirects
            if self.enable_redirects and status_code in (301, 302, 303, 307, 308):
                if self.redirect_count < self.max_redirects:
                    redirect_url = response_headers.get('location')
                    if redirect_url:
                        self.redirect_count += 1
                        if not redirect_url.startswith('http'):
                            redirect_url = urljoin(url, redirect_url)
                        next_method = 'GET' if status_code == 303 else method
                        return self.request(next_method, redirect_url, headers)

            self.redirect_count = 0
            self._save_cache(url, status_line, response_headers, body)
            return status_code, response_headers, body

        except ConnectionRefusedError:
            print(f"Error: connection refused for {host}", file=sys.stderr)
            sys.exit(1)
        except socket.gaierror as e:
            print(f"Error: could not resolve host {host} — {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error: Failed to connect to {host}: {e}", file=sys.stderr)
            sys.exit(1)


class HTMLStripper(html.parser.HTMLParser):
    """Strip HTML tags and extract readable text."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = []
        self.in_script = False
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ('script', 'style'):
            setattr(self, f'in_{tag.lower()}', True)

    def handle_endtag(self, tag):
        if tag.lower() in ('script', 'style'):
            setattr(self, f'in_{tag.lower()}', False)
        elif tag.lower() in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'tr'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.in_script and not self.in_style:
            self.text.append(data)

    def get_text(self):
        text = ''.join(self.text)
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return '\n'.join(lines)


def strip_html(html_content: str) -> str:
    stripper = HTMLStripper()
    stripper.feed(html_content)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Search result parsers
# ---------------------------------------------------------------------------

def _parse_yahoo_results(html: str) -> List[Tuple[str, str, str]]:
    """Extract results from Yahoo search HTML."""
    results = []
    seen_urls = set()

    titles = re.findall(
        r'class="[^"]*compTitle[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    )
    snippets = re.findall(
        r'class="[^"]*compText[^"]*"[^>]*>(.*?)</(?:p|div|span)>',
        html, re.DOTALL
    )

    snippet_index = 0
    for href, title_html in titles:
        title = decode_entities(re.sub(r'<[^>]+>', '', title_html).strip())
        if not title or title.lower() == "ads":
            continue

        # Extract real URL from Yahoo redirect wrapper
        url_match = re.search(r'RU=([^/]+)', href)
        if url_match:
            url = urllib.parse.unquote(url_match.group(1))
        else:
            url = href

        if not url.startswith('http') or 'yahoo.com' in url or url in seen_urls:
            continue
        if any(ad in url for ad in ['googlesyndication', 'doubleclick', 'bing.com/aclick']):
            continue

        snippet = ""
        if snippet_index < len(snippets):
            snippet = decode_entities(re.sub(r'<[^>]+>', '', snippets[snippet_index]).strip())
            snippet_index += 1

        seen_urls.add(url)
        results.append((title, url, snippet))
        if len(results) == 10:
            break

    return results





def search(query: str, client: HTTPClient, debug: bool = False) -> List[Tuple[str, str, str]]:
    """Search Yahoo for results."""
    encoded = urllib.parse.quote_plus(query)

    yahoo_url = f"https://search.yahoo.com/search?p={encoded}&ei=UTF-8&nojs=1"
    if debug:
        sys.stderr.write(f"[DEBUG] Search URL: {yahoo_url}\n")

    status, headers, body = client.request(
        'GET', yahoo_url,
        headers={'Cookie': 'sB=v=1&pstaid=undefined'}
    )

    if debug:
        sys.stderr.write(f"[DEBUG] Status: {status}, body length: {len(body)}\n")

    if status == 200:
        results = _parse_yahoo_results(body)
        if results:
            return results

    return []


def main():
    parser = argparse.ArgumentParser(
        description='go2web - HTTP over TCP Sockets',
        prog='go2web',
        epilog='Examples:\n'
               '  %(prog)s -u https://example.com\n'
               '  %(prog)s -s machine learning\n'
               '  %(prog)s -s python 3          # fetch result #3 directly\n'
               '  %(prog)s -h',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('-u', '--url', metavar='URL', help='Make HTTP request to specified URL')
    group.add_argument('-s', '--search', metavar='SEARCH_TERM', nargs='+',
                       help='Search and print top 10 results. Append a number to fetch that result.')

    parser.add_argument('--no-cache', action='store_true', help='Disable caching')
    parser.add_argument('--no-redirects', action='store_true', help='Disable redirect following')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')

    args = parser.parse_args()

    if not args.url and not args.search:
        parser.print_help()
        return

    client = HTTPClient(
        enable_cache=not args.no_cache,
        enable_redirects=not args.no_redirects
    )

    # -----------------------------------------------------------------------
    # -u: fetch a URL
    # -----------------------------------------------------------------------
    if args.url:
        if not args.url.startswith('http'):
            args.url = 'https://' + args.url

        try:
            parsed = urlparse(args.url)
            if not parsed.netloc:
                print(f"Error: Invalid URL format: {args.url}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"Error: Invalid URL: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Fetching: {args.url}\n")
        status, headers, body = client.request('GET', args.url)

        if args.debug:
            sys.stderr.write(f"[DEBUG] Status: {status}\n")
            sys.stderr.write(f"[DEBUG] Headers: {headers}\n")
            sys.stderr.write(f"[DEBUG] Body length: {len(body)}\n")

        if status == 200:
            content_type = headers.get('content-type', '')
            if 'json' in content_type.lower():
                try:
                    parsed_json = json.loads(body)
                    print(json.dumps(parsed_json, indent=2))
                except Exception:
                    print(body)
            else:
                print(strip_html(body))
        else:
            print(f"Error: HTTP {status}", file=sys.stderr)
            sys.exit(1)

    # -----------------------------------------------------------------------
    # -s: search
    # -----------------------------------------------------------------------
    elif args.search:
        tokens = args.search
        index = None

        # If last token is a number, treat it as result index to fetch
        if tokens[-1].isdigit():
            index = int(tokens[-1])
            tokens = tokens[:-1]

        search_term = ' '.join(tokens)
        print(f"Searching for: {search_term}\n")

        results = search(search_term, client, debug=args.debug)

        if not results:
            print("No results found.", file=sys.stderr)
            sys.exit(1)

        if index is not None:
            # Fetch specific result by number
            if 1 <= index <= len(results):
                title, url, snippet = results[index - 1]
                print(f"Fetching result {index}: {title}")
                print(f"URL: {url}\n")
                status, headers, body = client.request('GET', url)
                print(strip_html(body))
            else:
                print(f"Error: index {index} out of range (1–{len(results)})", file=sys.stderr)
                sys.exit(1)
        else:
            for i, (title, url, snippet) in enumerate(results, 1):
                print(f"{i}. {title}")
                print(f"   {url}")
                if snippet:
                    print(f"   {snippet}")
                print()


if __name__ == '__main__':
    main()