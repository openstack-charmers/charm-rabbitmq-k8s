#!/usr/bin/env python3
#
# Copyright 2021 David Ames
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

"""RabbitMQ Operator Peer relation interface.

This is an internal interface used by the RabbitMQ operator charm.
"""

import logging

from ops.framework import (
    EventBase,
    EventSource,
    Object,
    ObjectEvents,
)


class PeersConnectedEvent(EventBase):
    """Event triggered when the peer relation is created.

    This event is triggered at the start of the relations lifecycle
    as the relation is created.
    """


class PeersNodeEvent(EventBase):
    """Peer Event which stores the nodename triggering the event."""

    def __init__(self, handle, nodename):
        super().__init__(handle)
        self.nodename = nodename

    def snapshot(self):
        """Store event data into snapshot."""
        return {"nodename": self.nodename}

    def restore(self, snapshot):
        """Restore data from snapshot into event."""
        super().restore(snapshot)
        self.nodename = snapshot["nodename"]


class ReadyPeersEvent(PeersNodeEvent):
    """Event triggered when peer relation is ready for use.

    This event is triggered when the peer relation has been configured
    for use - this is done by the lead unit generating the username
    and password for the operator admin user and passing this on the
    relation.
    """


class PeersLeavingEvent(PeersNodeEvent):
    """Event triggered when the peer unit leaves the relation.

    This event is triggered when a peer unit leaves the relation.
    This is almost certainly a scale-back event.
    """


class PeersBrokenEvent(EventBase):
    """Event triggered when the peer relation is destroyed.

    This event is triggered when the peer relation is removed from
    the application which in reality only occurs as a unit is removed
    from the deployment or when the application is removed from the
    deployment.
    """


class RabbitMQOperatorPeersEvents(ObjectEvents):
    """RabbitMQ Operator Peer interface events."""

    connected = EventSource(PeersConnectedEvent)
    ready = EventSource(ReadyPeersEvent)
    goneaway = EventSource(PeersBrokenEvent)
    leaving = EventSource(PeersLeavingEvent)


class RabbitMQOperatorPeers(Object):
    """RabbitMQ Operator Peer interface."""

    on = RabbitMQOperatorPeersEvents()

    OPERATOR_PASSWORD = "operator_password"
    OPERATOR_USER_CREATED = "operator_user_created"
    ERLANG_COOKIE = "erlang_cookie"
    NODENAME = "nodename"

    def __init__(self, charm, relation_name):
        super().__init__(charm, relation_name)
        self.relation_name = relation_name
        self.framework.observe(
            charm.on[relation_name].relation_created, self.on_created
        )
        self.framework.observe(
            charm.on[relation_name].relation_changed, self.on_changed
        )
        self.framework.observe(
            charm.on[relation_name].relation_departed, self.on_departed
        )
        self.framework.observe(
            charm.on[relation_name].relation_broken, self.on_broken
        )

    @property
    def peers_rel(self):
        """Peer relation."""
        return self.framework.model.get_relation(self.relation_name)

    def on_created(self, event):
        """Relation created event handler."""
        logging.debug("RabbitMQOperatorPeers on_created")
        self.on.connected.emit()

    def on_broken(self, event):
        """Relation broken event handler."""
        logging.debug("RabbitMQOperatorPeers on_broken")
        self.on.gonewaway.emit()

    def on_departed(self, event):
        """Relation broken event handler."""
        logging.debug("RabbitMQOperatorPeers on_departed")
        self.on.leaving.emit(event.departing_unit.name)

    def on_changed(self, event):
        """Relation changed event handler."""
        logging.debug("RabbitMQOperatorPeers on_changed")
        if self.operator_password and self.erlang_cookie:
            if event.unit:
                self.on.ready.emit(event.unit.name)

    def set_operator_password(self, password: str):
        """Set admin operator password in relation data bag."""
        logging.debug("Setting operator password")
        self.peers_rel.data[self.peers_rel.app][
            self.OPERATOR_PASSWORD
        ] = password

    def set_operator_user_created(self, user: str):
        """Set admin operator user create information in relation data bag."""
        logging.debug("Setting operator user created")
        self.peers_rel.data[self.peers_rel.app][
            self.OPERATOR_USER_CREATED
        ] = user

    def set_erlang_cookie(self, cookie: str):
        """Set Erlang cookie for RabbitMQ clustering."""
        logging.debug("Setting erlang cookie")
        self.peers_rel.data[self.peers_rel.app][self.ERLANG_COOKIE] = cookie

    def store_password(self, username: str, password: str):
        """Store username and password."""
        logging.debug(f"Storing password for {username}")
        self.peers_rel.data[self.peers_rel.app][username] = password

    def delete_user(self, username: str):
        """Delete username from application data."""
        if username in self.peers_rel.data[self.peers_rel.app]:
            del self.peers_rel.data[self.peers_rel.app][username]

    def set_nodename(self, nodename: str):
        """Advertise nodename to peers."""
        logging.debug(f"Setting nodename {nodename}")
        self.peers_rel.data[self.model.unit][self.NODENAME] = nodename

    def retrieve_password(self, username: str) -> str:
        """Retrieve persisted password for provided username."""
        if not self.peers_rel:
            return None
        return str(self.peers_rel.data[self.peers_rel.app].get(username))

    @property
    def operator_password(self) -> str:
        """Password for admin operator user."""
        if not self.peers_rel:
            return None
        return self.peers_rel.data[self.peers_rel.app].get(
            self.OPERATOR_PASSWORD
        )

    @property
    def operator_user_created(self) -> str:
        """Username for amdin operator user and flag to indicate created."""
        if not self.peers_rel:
            return None
        return self.peers_rel.data[self.peers_rel.app].get(
            self.OPERATOR_USER_CREATED
        )

    @property
    def erlang_cookie(self) -> str:
        """Erlang cookie for RabbitMQ cluster."""
        if not self.peers_rel:
            return None
        return self.peers_rel.data[self.peers_rel.app].get(self.ERLANG_COOKIE)
