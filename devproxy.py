# Local stand-in for nginx: static files from cwd, /api/* forwarded to :8000. Never deploy this.
import http.server, urllib.request, urllib.error
class H(http.server.SimpleHTTPRequestHandler):
    def _proxy(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0)) or None
        req = urllib.request.Request('http://127.0.0.1:8000'+self.path, data=body, method=self.command,
                                     headers={'Content-Type': self.headers.get('Content-Type','application/json')})
        try: r = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e: r = e
        data = r.read(); self.send_response(r.status)
        self.send_header('Content-Type', r.headers.get('Content-Type','')); self.send_header('Content-Length', len(data))
        self.end_headers(); self.wfile.write(data)
    def do_GET(self): self._proxy() if self.path.startswith('/api/') else super().do_GET()
    def do_POST(self): self._proxy()
http.server.ThreadingHTTPServer(('127.0.0.1', 8765), H).serve_forever()
