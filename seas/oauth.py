# Copyright 2013-2014 Synappio LLC. All rights reserved.
'''Helper class for doing oauth2 linking'''
import logging
from urllib import urlencode

import requests

log = logging.getLogger(__name__)

class OAuth2(object):

    def __init__(
            self,
            authorize_uri=None,
            access_token_uri=None,
            client_id=None,
            client_secret=None,
            redirect_uri=None,
            session=None):
        if session is None:
            self._session = requests.Session()
        else:
            self._session = session
        self.authorize_uri = authorize_uri
        self.access_token_uri = access_token_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorize_url(self, response_type='code', scope=None):
        params = dict(
            response_type=response_type,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri)
        if scope is not None:
            params['scope'] = scope
        return self.authorize_uri + '?' + urlencode(params)

    def get_access_token(self, code=None, refresh_token=None):
        '''Exchange a code or refresh_token for an access_token'''
        data = dict(
            client_id=self.client_id,
            client_secret=self.client_secret)
        if code:
            data.update(
                grant_type='authorization_code',
                code=code,
                redirect_uri=self.redirect_uri)
        else:
            data.update(
                grant_type='refresh_token',
                refresh_token=refresh_token)
        resp = self._session.post(self.access_token_uri, data=data)
        resp.raise_for_status()
        return resp.json()

