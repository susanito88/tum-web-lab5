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
import os
import hashlib
import pickle
import re
import urllib.parse
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, urljoin, parse_qs
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# HTTP Cache configuration
CACHE_DIR = Path.home() / '.go2web_cache'
CACHE_DIR.mkdir(exist_ok=True)

class HTTPClient:
    """Low-level HTTP client using TCP sockets (no urllib/requests)."""
    
    def __init__(self, enable_cache=True, enable_redirects=True):
        self.enable_cache = enable_cache
        self.enable_redirects = enable_redirects
        self.max_redirects = 5
        self.redirect_count = 0
    
    def _get_cache_path(self, url: str) -> Path:
        """Generate cache file path for a URL."""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return CACHE_DIR / f"{url_hash}.cache"
    
    def _load_cache(self, url: str) -> Optional[Tuple[str, Dict]]:
        """Load cached response if available."""
        if not self.enable_cache:
            return None
        
        cache_path = self._get_cache_path(url)
        if cache_path.exists():
            try:
                with open(cache_path, 'rb') as f:
                    return pickle.load(f)
            except Exception:
                return None
        return None
    
    def _save_cache(self, url: str, status_line: str, headers: Dict, body: str):
        """Save response to cache."""
        if not self.enable_cache:
            return
        
        cache_path = self._get_cache_path(url)
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump((status_line, headers, body), f)
        except Exception:
            pass
    
    def request(self, method: str, url: str, headers: Optional[Dict] = None) -> Tuple[int, Dict, str]:
        """
        Make an HTTP request using raw TCP sockets.
        Returns: (status_code, headers_dict, body)
        """
        # Check cache first
        cached = self._load_cache(url)
        if cached:
            status_line, headers, body = cached
            status_code = int(status_line.split()[1])
            return status_code, headers, body
        
        parsed = urlparse(url)
        host = parsed.netloc
        path = parsed.path or '/'
        if parsed.query:
            path += f'?{parsed.query}'
        
        # Default headers
        if headers is None:
            headers = {}
        
        default_headers = {
            'Host': host,
            'User-Agent': 'go2web/1.0',
            'Connection': 'close',
            'Accept': '*/*'
        }
        default_headers.update(headers)
        
        # Build HTTP request
        request_line = f"{method} {path} HTTP/1.1\r\n"
        header_lines = "\r\n".join(f"{k}: {v}" for k, v in default_headers.items())
        http_request = f"{request_line}{header_lines}\r\n\r\n"
        
        # Determine port
        port = 443 if parsed.scheme == 'https' else 80
        
        try:
            # Create socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            
            # Wrap with SSL for HTTPS
            if parsed.scheme == 'https':
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE  # Don't verify for lab purposes
                sock = context.wrap_socket(sock, server_hostname=host)
            
            sock.connect((host, port))
            sock.sendall(http_request.encode())
            
            # Receive response
            response = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            sock.close()
            
            # Parse response
            response_text = response.decode('utf-8', errors='ignore')
            parts = response_text.split('\r\n\r\n', 1)
            headers_text = parts[0]
            body = parts[1] if len(parts) > 1 else ''
            
            # Parse headers
            header_lines = headers_text.split('\r\n')
            status_line = header_lines[0]
            status_code = int(status_line.split()[1])
            
            response_headers = {}
            for line in header_lines[1:]:
                if ':' in line:
                    key, value = line.split(':', 1)
                    response_headers[key.strip()] = value.strip()
            
            # Handle redirects
            if self.enable_redirects and status_code in (301, 302, 303, 307, 308):
                if self.redirect_count < self.max_redirects:
                    redirect_url = response_headers.get('Location')
                    if redirect_url:
                        self.redirect_count += 1
                        if not redirect_url.startswith('http'):
                            redirect_url = urljoin(url, redirect_url)
                        return self.request(method if status_code != 303 else 'GET', redirect_url, headers)
            
            self.redirect_count = 0
            
            # Save to cache
            self._save_cache(url, status_line, response_headers, body)
            
            return status_code, response_headers, body
            
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
        elif tag.lower() in ('p', 'div', 'br', 'li'):
            self.text.append('\n')
    
    def handle_data(self, data):
        if not self.in_script and not self.in_style:
            self.text.append(data)
    
    def get_text(self):
        text = ''.join(self.text)
        # Clean up whitespace
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return '\n'.join(lines)


def strip_html(html_content: str) -> str:
    """Strip HTML tags from content."""
    stripper = HTMLStripper()
    stripper.feed(html_content)
    return stripper.get_text()


def search_google(query: str, client: HTTPClient) -> List[str]:
    """Search using Google and extract top 10 results."""
    search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num=10"
    
    try:
        # Add additional headers to appear more like a real browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        status, resp_headers, body = client.request('GET', search_url, headers)
        
        if status != 200:
            print(f"Error: Google search returned status {status}", file=sys.stderr)
            return []
        
        # Extract search results (simplified parsing)
        results = []
        
        # Look for result containers
        import re
        
        # More comprehensive patterns to handle different Google result formats  
        patterns = [
            # Pattern 1: Standard result with hX tag
            r'<a\s+href="(/url\?q=([^"&]+)[^"]*)"\s+[^>]*><div[^>]*><span>[^<]*</span></div><div[^>]*><h\d[^>]*>([^<]+)</h\d>',
            # Pattern 2: Result with direct link
            r'<a\s+href="([^"]*(?:https?://[^"]*)?)"[^>]*>(?:<h\d[^>]*>)?([^<]+)(?:</h\d>)?</a>',
            # Pattern 3: Data-sokoban-container pattern
            r'href="([^"]*?)"[^>]*>([^<]+)</a>.*?(?=<a href|$)',
        ]
        
        for pattern in patterns:
            if len(results) >= 10:
                break
            matches = re.findall(pattern, body, re.DOTALL | re.IGNORECASE)
            for match in matches[:10]:
                if len(results) >= 10:
                    break
                    
                # Extract URL and title from different pattern groups
                if len(match) == 3:
                    url = match[1] if match[1] else match[0]
                    title = match[2]
                elif len(match) == 2:
                    url = match[0]
                    title = match[1]
                else:
                    continue
                
                # Clean up URL if it's Google's redirect URL
                if '/url?q=' in url:
                    import urllib.parse
                    try:
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                        url = parsed.get('q', [url])[0]
                    except:
                        pass
                
                # Filter out invalid URLs and Google's own URLs
                if url.startswith('http') and 'google.com' not in url and 'webcache' not in url:
                    title_clean = title.strip()[:100]
                    if title_clean:  # Only add if title exists
                        result_entry = f"{title_clean}\n{url}"
                        if result_entry not in results:  # Avoid duplicates
                            results.append(result_entry)
        
        return results[:10]
    
    except Exception as e:
        print(f"Error during search: {e}", file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(
        description='go2web - HTTP over TCP Sockets',
        prog='go2web',
        epilog='Examples:\n'
               '  %(prog)s -u https://example.com\n'
               '  %(prog)s -s machine learning\n'
               '  %(prog)s -h',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('-u', '--url', metavar='URL', help='Make HTTP request to specified URL')
    group.add_argument('-s', '--search', metavar='SEARCH_TERM', nargs='+', 
                      help='Search using Google and print top 10 results')
    
    parser.add_argument('--no-cache', action='store_true', help='Disable caching')
    parser.add_argument('--no-redirects', action='store_true', help='Disable redirect following')
    
    args = parser.parse_args()
    
    # Check if no arguments provided
    if not args.url and not args.search:
        parser.print_help()
        return
    
    client = HTTPClient(
        enable_cache=not args.no_cache,
        enable_redirects=not args.no_redirects
    )
    
    if args.url:
        # Handle URL request
        if not args.url.startswith('http'):
            args.url = 'http://' + args.url
        
        # Validate URL format
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
        
        if status == 200:
            # Assume HTML content, strip tags
            content_type = headers.get('Content-Type', '')
            if 'json' in content_type.lower():
                try:
                    parsed = json.loads(body)
                    print(json.dumps(parsed, indent=2))
                except:
                    print(body)
            else:
                readable = strip_html(body)
                print(readable)
        else:
            print(f"Error: HTTP {status}", file=sys.stderr)
            sys.exit(1)
    
    elif args.search:
        # Handle search request
        search_term = ' '.join(args.search)
        print(f"Searching for: {search_term}\n")
        
        results = search_google(search_term, client)
        
        if results:
            for i, result in enumerate(results, 1):
                print(f"{i}. {result}\n")
        else:
            print("No results found.", file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
