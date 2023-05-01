# Copyright 2021 David
# Copyright 2021 Canonical Ltd.
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

"""Unit tests for RabbitMQ operator."""

import unittest
from unittest.mock import (
    MagicMock,
    Mock,
    patch,
)

import ops.model
from ops.testing import (
    Harness,
)

import charm


class TestCharm(unittest.TestCase):
    """Unit tests for RabbitMQ operator."""

    @patch("charm.KubernetesServicePatch", lambda _, service_type, ports: None)
    def setUp(self, *unused):
        """Setup test fixtures for unit tests."""
        self.harness = Harness(charm.RabbitMQOperatorCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

        # Setup RabbitMQ API mocking
        mock_admin_api = MagicMock()
        mock_admin_api.overview.return_value = {"product_version": "3.19.2"}
        self.harness.charm._get_admin_api = Mock()
        self.harness.charm._get_admin_api.return_value = mock_admin_api

        # network_get is not implemented in the testing harness
        # so mock out for now
        # TODO: remove when implemented
        self.harness.charm._amqp_bind_address = Mock(return_value="10.5.0.1")
        self.harness.charm._peers_bind_address = Mock(return_value="10.10.1.1")
        self.maxDiff = None

    def test_action(self):
        """Test actions for operator."""
        action_event = Mock()
        self.harness.charm._on_get_operator_info_action(action_event)
        self.assertTrue(action_event.set_results.called)

    def test_rabbitmq_pebble_ready(self):
        """Test pebble handler."""
        # Get the rabbitmq container from the model
        container = self.harness.model.unit.get_container("rabbitmq")
        self.harness.set_can_connect(container, True)
        # self.harness.charm._render_and_push_config_files = Mock()
        # self.harness.charm._render_and_push_plugins = Mock()
        # Check the initial Pebble plan is empty
        initial_plan = self.harness.get_container_pebble_plan("rabbitmq")
        self.assertEqual(initial_plan.to_yaml(), "{}\n")
        # Expected plan after Pebble ready with default config
        expected_plan = {
            "services": {
                "rabbitmq-server": {
                    "override": "replace",
                    "summary": "RabbitMQ Server",
                    "command": "rabbitmq-server",
                    "startup": "enabled",
                    "user": "rabbitmq",
                    "group": "rabbitmq",
                    "requires": ["epmd"],
                },
                "epmd": {
                    "override": "replace",
                    "summary": "Erlang EPM service",
                    "command": "epmd -d",
                    "user": "rabbitmq",
                    "group": "rabbitmq",
                    "startup": "enabled",
                },
            },
        }
        # RabbitMQ is up, operator user initialized
        peers_relation_id = self.harness.add_relation("peers", "rabbitmq-k8s")
        self.harness.add_relation_unit(peers_relation_id, "rabbitmq-k8s/0")
        # Peer relation complete
        self.harness.update_relation_data(
            peers_relation_id,
            self.harness.charm.app.name,
            {
                "operator_password": "foobar",
                "operator_user_created": "rmqadmin",
                "erlang_cookie": "magicsecurity",
            },
        )
        # Emit the PebbleReadyEvent carrying the rabbitmq container
        self.harness.charm.on.rabbitmq_pebble_ready.emit(container)
        # Get the plan now we've run PebbleReady
        updated_plan = self.harness.get_container_pebble_plan(
            "rabbitmq"
        ).to_dict()
        # Check we've got the plan we expected
        self.assertEqual(expected_plan, updated_plan)
        # Check the service was started
        service = self.harness.model.unit.get_container(
            "rabbitmq"
        ).get_service("rabbitmq-server")
        self.assertTrue(service.is_running())

    def test_update_status(self):
        """This test validates the charm, the peers and the amqp relation."""
        self.harness.set_leader(True)
        self.harness.model.get_binding = Mock()
        # Early not initialized
        self.harness.charm.on.update_status.emit()
        self.assertEqual(
            self.harness.model.unit.status,
            ops.model.WaitingStatus(
                "Waiting for leader to create operator user"
            ),
        )

        # RabbitMQ is up, operator user initialized
        peers_relation_id = self.harness.add_relation("peers", "rabbitmq-k8s")
        self.harness.add_relation_unit(peers_relation_id, "rabbitmq-k8s/0")
        # Peer relation complete
        self.harness.update_relation_data(
            peers_relation_id,
            self.harness.charm.app.name,
            {
                "operator_password": "foobar",
                "operator_user_created": "rmqadmin",
                "erlang_cookie": "magicsecurity",
            },
        )
        # AMQP relation incomplete
        amqp_relation_id = self.harness.add_relation("amqp", "amqp-client-app")
        self.harness.add_relation_unit(amqp_relation_id, "amqp-client-app/0")

        # AMQP relation complete
        self.harness.update_relation_data(
            amqp_relation_id,
            "amqp-client-app",
            {"username": "client", "vhost": "client-vhost"},
        )
        self.harness.charm.on.update_status.emit()
        self.assertEqual(
            self.harness.model.unit.status, ops.model.ActiveStatus()
        )
