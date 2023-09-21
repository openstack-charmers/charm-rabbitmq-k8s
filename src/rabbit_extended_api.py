#
# Copyright 2023 Canonical Ltd.
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
#
# Learn more at: https://juju.is/docs/sdk

"""Add missing functionality to rabbitmq_admin.AdminAPI."""

import json
import urllib
from typing import (
    Dict,
    List,
)

import rabbitmq_admin


class ExtendedAdminApi(rabbitmq_admin.AdminAPI):
    """Extend rabbitmq_admin.AdminAPI to cover missing endpoints the charm needs."""

    def list_queues(self):
        """A list of queues."""
        return self._api_get("/api/queues")

    def list_quorum_queues(self) -> List[Dict]:
        """A list of quorum queues."""
        return [
            q for q in self._api_get("/api/queues") if q["type"] == "quorum"
        ]

    def get_queue(self, vhost, queue):
        """A list of nodes in the RabbitMQ cluster."""
        return self._api_get(
            "/api/queues/{}/{}".format(
                urllib.parse.quote_plus(vhost), urllib.parse.quote_plus(queue)
            )
        )

    def rebalance_queues(self):
        """Rebalance the queues leaders."""
        return self._api_post("/api/rebalance/queues")

    def grow_queue(
        self, node, selector, vhost_pattern=None, queue_pattern=None
    ):
        """Add a member to queues.

        Which queues have the member added is decided by the selector,
        vhost_pattern and queue_pattern
        """
        if not vhost_pattern:
            vhost_pattern = ".*"
        if not queue_pattern:
            queue_pattern = ".*"
        data = {
            "strategy": selector,
            "queue_pattern": queue_pattern,
            "vhost_pattern": vhost_pattern,
        }
        self._api_post(
            "/api/queues/quorum/replicas/on/{}/grow".format(node), data=data
        )

    def add_member(self, node, vhost, queue):
        """Add a member to a queue."""
        data = {"node": node}
        self._api_post(
            "/api/queues/quorum/{}/{}/replicas/add".format(
                urllib.parse.quote_plus(vhost), urllib.parse.quote_plus(queue)
            ),
            data=data,
        )

    def delete_member(self, node, vhost, queue):
        """Remove a member to a queue."""
        # rabbitmq_admin does not seem to handle json encoding for DELETE requests
        data = json.dumps({"node": node})
        self._api_delete(
            "/api/queues/quorum/{}/{}/replicas/delete".format(
                urllib.parse.quote_plus(vhost), urllib.parse.quote_plus(queue)
            ),
            data=data,
        )
