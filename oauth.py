# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####


import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import requests

from . import global_vars


class PortsBlockedException(Exception):
    pass


class SimpleOAuthAuthenticator(object):
    def __init__(self, server_url, client_id, ports):
        self.server_url = server_url
        self.client_id = client_id
        self.ports = ports

    def _get_tokens(self, authorization_code=None, refresh_token=None, grant_type="authorization_code"):
        data = {
            "grant_type": grant_type,
            "state": "random_state_string",
            "client_id": self.client_id,
            "scopes": "read write",
        }
        if hasattr(self, 'redirect_uri'):
            data["redirect_uri"] = self.redirect_uri
        if authorization_code:
            data['code'] = authorization_code
        if refresh_token:
            data['refresh_token'] = refresh_token

        session = requests.Session()
        proxy_which = global_vars.PREFS.get('proxy_which')
        proxy_address = global_vars.PREFS.get('proxy_address')
        if proxy_which == 'NONE':
            session.trust_env = False
        elif proxy_which == 'CUSTOM':
            session.trust_env = False
            session.proxies = {'https': proxy_address}
        else:
            session.trust_env = True
        response = session.post(
            '%s/o/token/' % self.server_url,
            data=data
        )
        if response.status_code != 200:
            print("error retrieving refresh tokens %s" % response.status_code)
            print(response.content)
            return None, None, None

        response_json = json.loads(response.content)
        refresh_token = response_json['refresh_token']
        access_token = response_json['access_token']
        return access_token, refresh_token, response_json

    def get_new_token(self, register=True, redirect_url=None):
        class HTTPServerHandler(BaseHTTPRequestHandler):
            html_template = '<html>%(head)s<h1>%(message)s</h1></html>'

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                if 'code' in self.path:
                    self.auth_code = self.path.split('=')[1]
                    # Display to the user that they no longer need the browser window
                    if redirect_url:
                        redirect_string = (
                            '<head><meta http-equiv="refresh" content="0;url=%(redirect_url)s"></head>'
                            '<script> window.location.href="%(redirect_url)s"; </script>' % {'redirect_url': redirect_url}
                        )
                    else:
                        redirect_string = ""
                    self.wfile.write(bytes(self.html_template % {'head': redirect_string, 'message': 'You may now close this window.'}, 'utf-8'))
                    qs = parse_qs(urlparse(self.path).query)
                    self.server.authorization_code = qs['code'][0]
                else:
                    self.wfile.write(bytes(self.html_template % {'head': '', 'message': 'Authorization failed.'}, 'utf-8'))

        for port in self.ports:
            try:
                httpServer = HTTPServer(('localhost', port), HTTPServerHandler)
            except Exception as e:
                print(f"Port {port}: {e}")
                continue
            break
        else:
            print("All available ports are blocked")
            raise PortsBlockedException(f"All available ports are blocked: {self.ports}")
        print(f"Choosen port {port}")
        self.redirect_uri = f"http://localhost:{port}/consumer/exchange/"
        authorize_url = (
            "/o/authorize?client_id=%s&state=random_state_string&response_type=code&"
            "redirect_uri=%s" % (self.client_id, self.redirect_uri)
        )
        if register:
            authorize_url = "%s/accounts/register/?next=%s" % (self.server_url, urlquote(authorize_url))
        else:
            authorize_url = "%s%s" % (self.server_url, authorize_url)
        webbrowser.open_new(authorize_url)

        httpServer.handle_request()
        authorization_code = httpServer.authorization_code
        return self._get_tokens(authorization_code=authorization_code)

    def get_refreshed_token(self, refresh_token):
        return self._get_tokens(refresh_token=refresh_token, grant_type="refresh_token")
