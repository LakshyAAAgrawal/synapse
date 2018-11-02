# -*- coding: utf-8 -*-
# Copyright 2018 New Vector
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

from synapse.api.urls import ConsentURIBuilder
from synapse.rest.client.v1 import admin, login, room
from synapse.rest.consent import consent_resource

from tests import unittest
from tests.server import render


class ConsentResourceTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]
    user_id = True
    hijack_auth = False

    def make_homeserver(self, reactor, clock):

        config = self.default_config()
        config.user_consent_version = "1"
        config.public_baseurl = ""
        config.form_secret = "123abc"

        # Make some temporary templates...
        temp_consent_path = self.mktemp()
        os.mkdir(temp_consent_path)
        os.mkdir(os.path.join(temp_consent_path, 'en'))
        config.user_consent_template_dir = os.path.abspath(temp_consent_path)

        with open(os.path.join(temp_consent_path, "en/1.html"), 'w') as f:
            f.write("{{version}}")

        with open(os.path.join(temp_consent_path, "en/success.html"), 'w') as f:
            f.write("yay!")

        hs = self.setup_test_homeserver(config=config)
        return hs

    def test_accept_consent(self):
        """
        A user can use the consent form to accept the terms.
        """
        uri_builder = ConsentURIBuilder(self.hs.config)
        store = self.hs.get_datastore()
        resource = consent_resource.ConsentResource(self.hs)

        # Register a user
        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        # They haven't consented, so they'll have a consent version of None
        user_data = self.get_success(store.get_user_by_id(user_id))
        self.assertIs(user_data["consent_version"], None)

        # Fetch the consent page, to get the consent version
        consent_uri = (
            uri_builder.build_user_consent_uri(user_id).replace("_matrix/", "")
            + "&u=user"
        )
        request, channel = self.make_request(
            "GET", consent_uri, access_token=access_token, shorthand=False
        )
        render(request, resource, self.reactor)
        self.assertEqual(channel.code, 200)

        # Get the version from the body
        version = channel.result["body"].decode('ascii')

        # POST to the consent page, saying we've agreed
        request, channel = self.make_request(
            "POST",
            consent_uri + "&v=" + version,
            access_token=access_token,
            shorthand=False,
        )
        render(request, resource, self.reactor)
        self.assertEqual(channel.code, 200)

        # Now we've consented!
        user_data = self.get_success(store.get_user_by_id(user_id))
        self.assertEqual(user_data["consent_version"], "1")
