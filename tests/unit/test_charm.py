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

import os
import unittest
from unittest.mock import (
    MagicMock,
    Mock,
    call,
)

import ops.model
from ops.testing import (
    Harness,
)

import charm


class TestCharm(unittest.TestCase):
    """Unit tests for RabbitMQ operator."""

    def setUp(self, *unused):
        """Setup test fixtures for unit tests."""
        os.environ["JUJU_VERSION"] = "3.4.4"
        self.harness = Harness(charm.RabbitMQOperatorCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

        # Setup RabbitMQ API mocking
        self.mock_admin_api = MagicMock()
        self.mock_admin_api.overview.return_value = {
            "product_version": "3.19.2"
        }
        self.harness.charm._get_admin_api = Mock()
        self.harness.charm._get_admin_api.return_value = self.mock_admin_api

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
        # self.harness.charm._render_and_push_config_files = Mock()
        # self.harness.charm._render_and_push_plugins = Mock()
        self.harness.charm._set_ownership_on_data_dir = Mock()
        # Check the initial Pebble plan is empty
        self.harness.set_can_connect("rabbitmq", True)
        initial_plan = self.harness.get_container_pebble_plan("rabbitmq")
        self.assertEqual(initial_plan.to_yaml(), "{}\n")
        # Expected plan after Pebble ready with default config
        expected_plan = {
            "services": {
                "rabbitmq": {
                    "override": "replace",
                    "summary": "RabbitMQ Server",
                    "command": "/usr/lib/rabbitmq/bin/rabbitmq-server",
                    "startup": "enabled",
                    "user": "rabbitmq",
                    "group": "rabbitmq",
                    "requires": ["epmd"],
                },
                "notifier": {
                    "command": "/usr/bin/notifier",
                    "override": "replace",
                    "startup": "enabled",
                    "summary": "Pebble notifier",
                    "requires": ["rabbitmq"],
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
        # Get the rabbitmq container from the model
        container = self.harness.model.unit.get_container("rabbitmq")
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
        ).get_service("rabbitmq")
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

    def test_get_queue_growth_selector(self):
        """Test the method chosen to grow a queue."""
        # 1->2
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(1, 1), "all"
        )

        # 1->2
        # 2->3
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(1, 2), "all"
        )

        # 1->2
        # 2->3
        # 3->3
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(1, 3), "individual"
        )

        # 2->3
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(2, 2), "all"
        )

        # 2->3
        # 3->3
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(2, 3), "even"
        )

        # 3->3
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(3, 3), "even"
        )

        # 3->3
        # 4->5
        # 5->5
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(3, 5), "even"
        )

        # 4->5
        self.assertEqual(
            self.harness.charm.get_queue_growth_selector(4, 4), "even"
        )

    def test_generate_nodename(self):
        """Test conversion of unit name to rabbit node name."""
        self.assertEqual(
            self.harness.charm.generate_nodename("unit/1"),
            "rabbit@unit-1.rabbitmq-k8s-endpoints",
        )

    def test_unit_in_cluster(self):
        """Test check whether unit is in rabbit cluster."""
        self.mock_admin_api.list_nodes.return_value = [
            {"name": "rabbit@unit-1.rabbitmq-k8s-endpoints"}
        ]
        self.assertTrue(self.harness.charm.unit_in_cluster("unit/1"))
        self.assertFalse(self.harness.charm.unit_in_cluster("unit/2"))

    def test_grow_queues_onto_unit(self):
        """Test growing a queue onto a unit."""
        queue_one_member = {
            "name": "queue1",
            "vhost": "openstack",
            "members": ["node1"],
        }
        queue_two_member = {
            "name": "queue2",
            "vhost": "openstack",
            "members": ["node1", "node2"],
        }
        queue_three_member = {
            "name": "queue3",
            "vhost": "openstack",
            "members": ["node1", "node2", "node3"],
        }
        self.mock_admin_api.list_quorum_queues.return_value = [
            queue_one_member
        ]
        self.harness.charm.grow_queues_onto_unit("unit/1")
        self.mock_admin_api.grow_queue.assert_called_once_with(
            "rabbit@unit-1.rabbitmq-k8s-endpoints", "all"
        )

        self.mock_admin_api.grow_queue.reset_mock()
        self.mock_admin_api.list_quorum_queues.return_value = [
            queue_two_member,
            queue_three_member,
        ]
        self.harness.charm.grow_queues_onto_unit("unit/1")
        self.mock_admin_api.grow_queue.assert_called_once_with(
            "rabbit@unit-1.rabbitmq-k8s-endpoints", "even"
        )

        self.mock_admin_api.grow_queue.reset_mock()
        self.mock_admin_api.list_quorum_queues.return_value = [
            queue_one_member,
            queue_two_member,
            queue_three_member,
        ]
        self.harness.charm.grow_queues_onto_unit("unit/1")
        self.mock_admin_api.add_member.assert_has_calls(
            [
                call(
                    "rabbit@unit-1.rabbitmq-k8s-endpoints",
                    "openstack",
                    "queue1",
                ),
                call(
                    "rabbit@unit-1.rabbitmq-k8s-endpoints",
                    "openstack",
                    "queue2",
                ),
            ]
        )

    def test_add_member_action(self):
        """Test actions for adding member to queue."""
        action_event = MagicMock()
        action_event.params = {
            "unit-name": "unit/1",
            "vhost": "/",
            "queue-name": "test_queue",
        }
        self.harness.charm._on_add_member_action(action_event)
        self.mock_admin_api.add_member.assert_called_once_with(
            "rabbit@unit-1.rabbitmq-k8s-endpoints", "/", "test_queue"
        )

    def test_delete_member_action(self):
        """Test actions for adding member to queue."""
        action_event = MagicMock()
        action_event.params = {
            "unit-name": "unit/1",
            "vhost": "/",
            "queue-name": "test_queue",
        }
        self.harness.charm._on_delete_member_action(action_event)
        self.mock_admin_api.delete_member.assert_called_once_with(
            "rabbit@unit-1.rabbitmq-k8s-endpoints", "/", "test_queue"
        )

    def test_ensure_ha_is_called_when_unit_is_leader_and_ready(self):
        """Test the notifier custom notice."""
        self.harness.set_leader(True)
        self.harness.set_can_connect(charm.RABBITMQ_CONTAINER, True)
        peers_relation_id = self.harness.add_relation("peers", "rabbitmq-k8s")
        self.harness.add_relation_unit(peers_relation_id, "rabbitmq-k8s/0")
        self.harness.update_relation_data(
            peers_relation_id,
            self.harness.charm.app.name,
            {
                "operator_password": "foobar",
                "operator_user_created": "rmqadmin",
                "erlang_cookie": "magicsecurity",
            },
        )
        self.harness.charm.ensure_queue_ha = Mock()
        self.harness.pebble_notify(
            charm.RABBITMQ_CONTAINER, charm.TIMER_NOTICE
        )
        self.harness.charm.ensure_queue_ha.assert_called_once()

    def test_ensure_ha_is_not_called_when_unit_is_not_leader(self):
        """Test the notifier custom notice when not leader."""
        self.harness.set_leader(False)
        self.harness.set_can_connect(charm.RABBITMQ_CONTAINER, True)
        peers_relation_id = self.harness.add_relation("peers", "rabbitmq-k8s")
        self.harness.add_relation_unit(peers_relation_id, "rabbitmq-k8s/0")
        self.harness.update_relation_data(
            peers_relation_id,
            self.harness.charm.app.name,
            {
                "operator_password": "foobar",
                "operator_user_created": "rmqadmin",
                "erlang_cookie": "magicsecurity",
            },
        )
        self.harness.charm.ensure_queue_ha = Mock()
        self.harness.pebble_notify(
            charm.RABBITMQ_CONTAINER, charm.TIMER_NOTICE
        )
        self.harness.charm.ensure_queue_ha.assert_not_called()

    def test_no_undersized_queues(self):
        """Test nothing is done when no undersized queues."""
        self.mock_admin_api.list_quorum_queues.return_value = []
        nodes = ["node1", "node2", "node3"]
        undersized_queues = []

        result = self.harness.charm._add_members_to_undersized_queues(
            self.mock_admin_api, nodes, undersized_queues, 3, False
        )
        self.assertEqual(result, [])

    def test_not_enough_nodes_to_replicate(self):
        """Test that the queues are still added to existing nodes.

        Even if there are not enough nodes to replicate, the charm will add to
        existing nodes if they are some available.
        """
        queues = [{"name": "queue1", "members": ["node1"], "vhost": "/"}]
        self.mock_admin_api.list_quorum_queues.return_value = queues
        nodes = ["node1", "node2"]
        undersized_queues = queues

        result = self.harness.charm._add_members_to_undersized_queues(
            self.mock_admin_api, nodes, undersized_queues, 3, False
        )
        self.assertEqual(result, ["queue1"])
        self.mock_admin_api.add_member.assert_called_once_with(
            "node2", "/", "queue1"
        )

    def test_exact_number_of_nodes_needed(self):
        """Test that the queues are added to the correct nodes.

        Order matters since the algorithm will to the node with the least
        members first.
        """
        self.mock_admin_api.list_quorum_queues.return_value = [
            {"name": "queue1", "members": ["node1"], "vhost": "/"},
            {"name": "queue2", "members": ["node2"], "vhost": "/"},
            {
                "name": "queue3",
                "members": ["node1", "node2", "node3"],
                "vhost": "/",
            },
        ]
        nodes = ["node1", "node2", "node3"]
        undersized_queues = [
            {"name": "queue1", "members": ["node1"], "vhost": "/"},
            {"name": "queue2", "members": ["node2"], "vhost": "/"},
        ]

        result = self.harness.charm._add_members_to_undersized_queues(
            self.mock_admin_api, nodes, undersized_queues, 3, False
        )
        self.assertEqual(result, ["queue1", "queue2"])
        self.mock_admin_api.add_member.assert_has_calls(
            [
                call("node3", "/", "queue1"),
                call("node2", "/", "queue1"),
                call("node3", "/", "queue2"),
                call("node1", "/", "queue2"),
            ]
        )
