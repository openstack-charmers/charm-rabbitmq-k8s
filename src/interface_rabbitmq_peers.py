#!/usr/bin/env python3

"""
RabbitMQ Operator Peer relation interface

This is an internal interface used by the RabbitMQ operator charm.
"""

import logging

from ops.framework import (
    EventBase,
    ObjectEvents,
    EventSource,
    Object,
)


class PeersConnectedEvent(EventBase):
    """Event triggered when the peer relation is created.

    This event is triggered at the start of the relations lifecycle
    as the relation is created.
    """


class ReadyPeersEvent(EventBase):
    """Event triggered when peer relation is ready for use.

    This event is triggered when the peer relation has been configured
    for use - this is done by the lead unit generating the username
    and password for the operator admin user and passing this on the
    relation.
    """


class PeersBrokenEvent(EventBase):
    """Event triggered when the peer relation is destroyed.

    This event is triggered when the peer relation is removed from
    the application which in reality only occurs as a unit is removed
    from the deployment or when the application is removed from the
    deployment.
    """


class RabbitMQOperatorPeersEvents(ObjectEvents):
    """RabbitMQ Operator Peer interface events"""

    connected = EventSource(PeersConnectedEvent)
    ready = EventSource(ReadyPeersEvent)
    goneaway = EventSource(PeersBrokenEvent)


class RabbitMQOperatorPeers(Object):
    """RabbitMQ Operator Peer interface"""

    on = RabbitMQOperatorPeersEvents()

    OPERATOR_PASSWORD = "operator_password"
    OPERATOR_USER_CREATED = "operator_user_created"
    ERLANG_COOKIE = "erlang_cookie"

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
            charm.on[relation_name].relation_broken, self.on_broken
        )

    @property
    def peers_rel(self):
        return self.framework.model.get_relation(self.relation_name)

    def on_created(self, event):
        logging.debug("RabbitMQOperatorPeers on_created")
        self.on.connected.emit()

    def on_broken(self, event):
        logging.debug("RabbitMQOperatorPeers on_broken")
        self.on.gonewaway.emit()

    def on_changed(self, event):
        logging.debug("RabbitMQOperatorPeers on_changed")
        if self.operator_password and self.erlang_cookie:
            self.on.ready.emit()

    def set_operator_password(self, password: str):
        logging.debug("Setting operator password")
        self.peers_rel.data[self.peers_rel.app][
            self.OPERATOR_PASSWORD
        ] = password

    def set_operator_user_created(self, user: str):
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

    def retrieve_password(self, username: str) -> str:
        """Retrieve persisted password for provided username"""
        if not self.peers_rel:
            return None
        return str(self.peers_rel.data[self.peers_rel.app].get(username))

    @property
    def operator_password(self) -> str:
        if not self.peers_rel:
            return None
        return self.peers_rel.data[self.peers_rel.app].get(
            self.OPERATOR_PASSWORD
        )

    @property
    def operator_user_created(self) -> str:
        if not self.peers_rel:
            return None
        return self.peers_rel.data[self.peers_rel.app].get(
            self.OPERATOR_USER_CREATED
        )

    @property
    def erlang_cookie(self) -> str:
        if not self.peers_rel:
            return None
        return self.peers_rel.data[self.peers_rel.app].get(self.ERLANG_COOKIE)
