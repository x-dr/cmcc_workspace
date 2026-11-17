"""Sign business-test clients in through the real workspace login endpoint.

Access-boundary tests import app.create_app directly to exercise anonymous users.
"""
from flask.testing import FlaskClient

from app import create_app as create_workspace_app

PASSWORD = 'business-test-password'


class SignedInWorkspaceClient(FlaskClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        response = self.post('/api/access/login', json={'password': PASSWORD})
        if response.status_code != 200:
            raise AssertionError('Business test could not sign in to the workspace')


def create_app(**kwargs):
    app = create_workspace_app(**kwargs)
    app.extensions['access_security'].set_password(PASSWORD, PASSWORD)
    app.test_client_class = SignedInWorkspaceClient
    return app
