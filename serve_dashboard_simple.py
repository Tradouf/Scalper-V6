#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import http.server
import os
import socketserver

PORT = 8080
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(BASE_DIR, '..'))
os.chdir(PROJECT_DIR)

class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

if __name__ == '__main__':
    with socketserver.TCPServer(('', PORT), NoCacheHandler) as httpd:
        print(f'Dashboard servi sur http://localhost:{PORT}/output/salledesmarches-dashboard-live.html')
        print('Ctrl+C pour arrêter.')
        httpd.serve_forever()
